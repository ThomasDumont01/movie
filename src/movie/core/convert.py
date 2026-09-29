"""Planification et exécution sûre des conversions de médias locaux."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from movie.core.models import (
    ConvertPlan,
    ConvertResult,
    DiscScan,
    DiscTitle,
    MovieError,
    OutputExistsError,
    OutputFormat,
    OutputQuality,
    ProbedMedia,
    ProgressUpdate,
)
from movie.core.storage import conversion_required_bytes, ensure_free_space
from movie.core.verification import (
    validate_conversion,
    validate_conversion_request,
    validate_extracted_title,
    validate_source,
)
from movie.core.workflow import (
    is_occupied,
    phase_callback,
    prepare_destination,
    publish_without_overwrite,
    report_stage,
    single_mkv,
)
from movie.formats import (
    allowed_qualities,
    default_quality,
    supported_output_values,
)

_MAX_FILENAME_BYTES = 240


class _MakeMkvBackend(Protocol):
    def scan_iso(
        self,
        path: Path,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> DiscScan: ...

    def rip_iso_title(
        self,
        path: Path,
        title_id: int,
        staging_directory: Path,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> object: ...


class _ProbeBackend(Protocol):
    def probe(self, path: Path) -> ProbedMedia: ...


class _ConverterBackend(Protocol):
    def convert(
        self,
        source: Path,
        destination: Path,
        *,
        output_format: OutputFormat,
        quality: OutputQuality,
        duration_seconds: float | None = None,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> Path: ...


def build_convert_plan(
    source: Path | str,
    output_format: OutputFormat | str,
    *,
    output_quality: OutputQuality | str | None = None,
    output: Path | str | None = None,
    output_directory: Path | str | None = None,
    iso_title: DiscTitle | None = None,
    source_media: ProbedMedia | None = None,
) -> ConvertPlan:
    """Valide les choix et calcule la destination sans rien créer."""

    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise MovieError(f"Le fichier source est introuvable : {source_path}")

    selected_format, selected_quality = conversion_settings(
        output_format,
        output_quality,
    )
    is_iso = source_path.suffix.casefold() == ".iso"
    if is_iso and iso_title is None:
        raise MovieError("Une image ISO nécessite le choix explicite d'un titre.")
    if not is_iso and iso_title is not None:
        raise MovieError("Un titre MakeMKV ne peut être associé qu'à une image ISO.")
    if is_iso and source_media is not None:
        raise MovieError("L'analyse d'un fichier ne peut pas être associée à une ISO.")
    if source_media is not None and source_media.path.resolve() != source_path:
        raise MovieError("L'analyse fournie ne correspond pas au fichier source.")
    destination = _destination_path(
        source_path,
        selected_format,
        output=output,
        output_directory=output_directory,
    )
    if destination == source_path:
        raise MovieError(
            "La source et la destination sont identiques. Choisis un autre nom de fichier."
        )
    if is_occupied(destination):
        raise OutputExistsError(
            f"Le fichier existe déjà et ne sera pas remplacé : {destination}"
        )
    return ConvertPlan(
        source=source_path,
        output=destination,
        output_format=selected_format,
        output_quality=selected_quality,
        iso_title=iso_title,
        source_media=source_media,
    )


def conversion_settings(
    output_format: OutputFormat | str,
    output_quality: OutputQuality | str | None,
) -> tuple[OutputFormat, OutputQuality]:
    try:
        selected_format = OutputFormat(output_format)
    except ValueError as error:
        raise MovieError(
            "Le format de sortie doit être l'un des suivants : "
            f"{supported_output_values()}."
        ) from error

    if output_quality is None:
        selected_quality = default_quality(selected_format)
    else:
        try:
            selected_quality = OutputQuality(output_quality)
        except ValueError as error:
            raise MovieError(
                "Le profil doit être source, high, balanced ou compact."
            ) from error

    allowed = allowed_qualities(selected_format)
    if selected_quality not in allowed:
        profiles = ", ".join(item.value for item in allowed)
        raise MovieError(
            f"Le format {selected_format.value.upper()} accepte les profils : {profiles}."
        )
    return selected_format, selected_quality


class ConversionService:
    """Orchestre extraction éventuelle, conversion, contrôle et publication."""

    def __init__(
        self,
        makemkv: _MakeMkvBackend,
        probe: _ProbeBackend,
        converter: _ConverterBackend,
    ) -> None:
        self.makemkv = makemkv
        self.probe = probe
        self.converter = converter

    def execute(
        self,
        plan: ConvertPlan,
        *,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> ConvertResult:
        prepare_destination(plan.output)
        _ensure_free_space(plan)
        with tempfile.TemporaryDirectory(
            prefix=".movie-convert-", dir=plan.output.parent
        ) as work_directory:
            staging = Path(work_directory)
            warnings: list[str] = []
            source_file = plan.source
            direct_iso_mkv = (
                plan.iso_title is not None and plan.output_format is OutputFormat.MKV
            )

            if plan.iso_title is not None:
                extraction_end = 0.94 if direct_iso_mkv else 0.45
                make_mkv_run = self.makemkv.rip_iso_title(
                    plan.source,
                    plan.iso_title.title_id,
                    staging,
                    on_progress=phase_callback(
                        on_progress,
                        start=0.0,
                        end=extraction_end,
                    ),
                )
                warnings.extend(_make_mkv_diagnostics(make_mkv_run))
                source_file = single_mkv(staging)
                report_stage(
                    on_progress,
                    "Vérification de l'extraction ISO",
                    0.0,
                    total_fraction=extraction_end,
                )
                source_media = self.probe.probe(source_file)
                warnings.extend(validate_extracted_title(plan.iso_title, source_media))
                source_ready = extraction_end + 0.03
            else:
                report_stage(
                    on_progress,
                    "Analyse du fichier source",
                    0.0,
                    total_fraction=0.0,
                )
                source_media = plan.source_media or self.probe.probe(source_file)
                validate_source(source_media)
                source_ready = 0.04

            validate_conversion_request(plan, source_media)
            report_stage(
                on_progress,
                "Analyse du fichier source",
                1.0,
                total_fraction=source_ready,
            )

            if direct_iso_mkv:
                staged_output = source_file
                output_media = source_media
            else:
                staged_output = staging / f".movie-output.{plan.output_format.value}"
                staged_output = self.converter.convert(
                    source_file,
                    staged_output,
                    output_format=plan.output_format,
                    quality=plan.output_quality,
                    duration_seconds=source_media.duration_seconds,
                    on_progress=phase_callback(
                        on_progress,
                        start=source_ready,
                        end=0.96,
                    ),
                )
                report_stage(
                    on_progress,
                    "Vérification du fichier converti",
                    0.0,
                    total_fraction=0.96,
                )
                output_media = self.probe.probe(staged_output)
                warnings.extend(
                    validate_conversion(
                        plan,
                        source_media,
                        output_media,
                    )
                )

            report_stage(
                on_progress,
                "Publication du fichier final",
                0.0,
                total_fraction=0.99,
            )
            publish_without_overwrite(staged_output, plan.output)
            report_stage(
                on_progress,
                "Publication du fichier final",
                1.0,
                total_fraction=1.0,
            )

        return ConvertResult(
            output=plan.output,
            media=output_media,
            warnings=tuple(dict.fromkeys(warnings)),
        )


def _destination_path(
    source: Path,
    output_format: OutputFormat,
    *,
    output: Path | str | None,
    output_directory: Path | str | None,
) -> Path:
    if output is not None and output_directory is not None:
        raise MovieError(
            "Choisis soit un fichier, soit un dossier de sortie, pas les deux."
        )
    if output is not None:
        destination = Path(output).expanduser()
        if destination.suffix.casefold() != f".{output_format.value}":
            raise MovieError(
                f"La destination doit se terminer par .{output_format.value}."
            )
        if destination.exists() and destination.is_dir():
            raise MovieError(f"La destination doit être un fichier : {destination}")
        if destination.is_symlink():
            raise OutputExistsError(
                f"Le fichier existe déjà et ne sera pas remplacé : {destination}"
            )
        return destination.resolve()

    directory = (
        Path(output_directory).expanduser()
        if output_directory is not None
        else source.parent
    )
    if directory.exists() and not directory.is_dir():
        raise MovieError(f"La destination doit être un dossier : {directory}")
    directory = directory.resolve()
    stem = source.stem
    suffix = f".{output_format.value}"
    filename = _bounded_filename(stem, suffix)
    if source.parent == directory and filename == source.name:
        stem = f"{stem}-converted"
        filename = _bounded_filename(stem, suffix)
    return directory / filename


def _bounded_filename(stem: str, suffix: str) -> str:
    available = _MAX_FILENAME_BYTES - len(suffix.encode("utf-8"))
    normalized = " ".join(stem.split()).strip(" .") or "media"
    encoded = normalized.encode("utf-8")
    if len(encoded) > available:
        normalized = encoded[:available].decode("utf-8", errors="ignore").rstrip(" .")
    return f"{normalized or 'media'}{suffix}"


def _make_mkv_diagnostics(run: object) -> tuple[str, ...]:
    diagnostics = getattr(run, "diagnostics", ())
    return diagnostics if isinstance(diagnostics, tuple) else ()


def _ensure_free_space(plan: ConvertPlan) -> None:
    required = conversion_required_bytes(
        plan.source,
        output_format=plan.output_format,
        iso_title=plan.iso_title,
    )
    ensure_free_space(
        plan.output.parent,
        required,
        operation="une conversion sûre",
    )

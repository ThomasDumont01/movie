"""Planification et exécution sûre des conversions de médias locaux."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import replace
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
_BATCH_SUFFIX = "-converted"


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
        source_media: ProbedMedia | None = None,
        audio_track_index: int | None = None,
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
    audio_track_index: int | None = None,
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
    source_streams = (
        iso_title.streams
        if iso_title is not None
        else source_media.streams
        if source_media is not None
        else ()
    )
    audio_streams = tuple(stream for stream in source_streams if stream.kind == "audio")
    if audio_track_index is not None:
        if selected_format is OutputFormat.MKV:
            raise MovieError("Le MKV conserve automatiquement toutes les pistes audio.")
        if audio_track_index < 0 or audio_track_index >= len(audio_streams):
            raise MovieError("La piste audio sélectionnée n'existe pas dans la source.")
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
        audio_track_index=audio_track_index,
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
        staging_root = Path(tempfile.gettempdir()).resolve()
        _ensure_free_space(plan, staging_root)
        staging = Path(tempfile.mkdtemp(prefix="movie-convert-", dir=staging_root))
        staged_output: Path | None = None
        verified = False
        published = False
        preserve_staging = False
        try:
            warnings: list[str] = []
            source_file = plan.source
            direct_iso_mkv = (
                plan.iso_title is not None and plan.output_format is OutputFormat.MKV
            )

            if plan.iso_title is not None:
                extraction_end = 0.90 if direct_iso_mkv else 0.42
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
                source_ready = extraction_end + 0.04
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
                    source_media=source_media,
                    audio_track_index=plan.audio_track_index,
                    on_progress=phase_callback(
                        on_progress,
                        start=source_ready,
                        end=0.90,
                    ),
                )
                report_stage(
                    on_progress,
                    "Vérification du fichier converti",
                    0.0,
                    total_fraction=0.90,
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
                    "Vérification du fichier converti",
                    1.0,
                    total_fraction=0.94,
                )

            verified = True
            report_stage(
                on_progress,
                "Publication du fichier final",
                0.0,
                total_fraction=0.94,
            )

            def report_copy_progress(fraction: float) -> None:
                report_stage(
                    on_progress,
                    "Copie vers la destination",
                    fraction,
                    total_fraction=0.94 + 0.05 * fraction,
                )

            publish_without_overwrite(
                staged_output,
                plan.output,
                on_copy_progress=(
                    report_copy_progress if on_progress is not None else None
                ),
            )
            published = True
            report_stage(
                on_progress,
                "Publication du fichier final",
                1.0,
                total_fraction=1.0,
            )
        except MovieError as error:
            if (
                verified
                and not published
                and staged_output is not None
                and staged_output.is_file()
            ):
                preserve_staging = True
                raise MovieError(
                    f"{error} Le fichier local vérifié a été conservé ici : "
                    f"{staged_output}"
                ) from error
            raise
        finally:
            if not preserve_staging:
                shutil.rmtree(staging, ignore_errors=True)

        return ConvertResult(
            output=plan.output,
            media=replace(output_media, path=plan.output),
            warnings=tuple(dict.fromkeys(warnings)),
        )


def discover_video_files(
    source_directory: Path | str,
    probe: _ProbeBackend,
) -> tuple[ProbedMedia, ...]:
    """Détecte par le contenu toutes les vidéos d'une arborescence."""

    root = Path(source_directory).expanduser().resolve()
    if not root.is_dir():
        raise MovieError(f"Le dossier source est introuvable : {root}")
    videos: list[ProbedMedia] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            media = probe.probe(path)
        except MovieError:
            continue
        if any(stream.kind == "video" and not stream.is_artwork for stream in media.streams):
            videos.append(media)
    return tuple(videos)


def folder_output_path(source: Path | str, output_directory: Path | str) -> Path:
    """Calcule le nom stable de la copie convertie d'un dossier."""

    root = Path(source).expanduser().resolve()
    parent = Path(output_directory).expanduser().resolve()
    return parent / f"{root.name}{_BATCH_SUFFIX}"


def execute_folder_conversion(
    source_directory: Path | str,
    destination: Path | str,
    videos: tuple[ProbedMedia, ...],
    *,
    output_format: OutputFormat,
    output_quality: OutputQuality,
    service: ConversionService,
    on_progress: Callable[[ProgressUpdate], None] | None = None,
) -> tuple[ConvertResult, ...]:
    """Copie une arborescence et y remplace chaque vidéo par sa conversion."""

    source = Path(source_directory).expanduser().resolve()
    output = Path(destination).expanduser().resolve()
    if not source.is_dir():
        raise MovieError(f"Le dossier source est introuvable : {source}")
    if output == source or source in output.parents:
        raise MovieError("La copie convertie doit être créée hors du dossier source.")
    if is_occupied(output):
        raise OutputExistsError(
            f"Le dossier existe déjà et ne sera pas remplacé : {output}"
        )

    media_by_path = {media.path.resolve(): media for media in videos}
    targets = _folder_targets(source, media_by_path, output_format)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.movie-", dir=output.parent)
    )
    results: list[ConvertResult] = []
    try:
        for directory in sorted(
            (
                path
                for path in source.rglob("*")
                if path.is_dir() and not path.is_symlink()
            ),
            key=lambda path: len(path.parts),
        ):
            (staging / directory.relative_to(source)).mkdir(parents=True, exist_ok=True)

        files = sorted(
            path
            for path in source.rglob("*")
            if path.is_file() or path.is_symlink()
        )
        converted_index = 0
        for path in files:
            relative = path.relative_to(source)
            media = media_by_path.get(path.resolve()) if not path.is_symlink() else None
            if media is None:
                _copy_folder_entry(path, staging / relative)
                continue

            target = staging / targets[path.resolve()]
            plan = build_convert_plan(
                path,
                output_format,
                output_quality=output_quality,
                output=target,
                source_media=media,
            )

            def report(
                update: ProgressUpdate,
                *,
                index: int = converted_index,
                source_name: str = path.name,
            ) -> None:
                if on_progress is None:
                    return
                fraction = update.total_fraction
                bounded = max(0.0, min(1.0, fraction or 0.0))
                count = max(1, len(videos))
                on_progress(
                    ProgressUpdate(
                        current_label=(
                            f"{source_name} · {update.current_label or 'Conversion'}"
                        ),
                        total_label=f"{index + 1}/{count} vidéos",
                        current_fraction=update.current_fraction,
                        total_fraction=(index + bounded) / count,
                    )
                )

            results.append(service.execute(plan, on_progress=report))
            converted_index += 1

        for directory in sorted(
            (
                path
                for path in source.rglob("*")
                if path.is_dir() and not path.is_symlink()
            ),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            shutil.copystat(directory, staging / directory.relative_to(source))
        shutil.copystat(source, staging)
        if is_occupied(output):
            raise OutputExistsError(
                f"Le dossier existe déjà et ne sera pas remplacé : {output}"
            )
        os.rename(staging, output)
    except (MovieError, OSError) as error:
        shutil.rmtree(staging, ignore_errors=True)
        if isinstance(error, MovieError):
            raise
        raise MovieError("Impossible de créer la copie convertie du dossier.") from error

    published_results: list[ConvertResult] = []
    for result in results:
        published_path = output / result.output.relative_to(staging)
        published_results.append(
            replace(
                result,
                output=published_path,
                media=replace(result.media, path=published_path),
            )
        )
    return tuple(published_results)


def _folder_targets(
    source: Path,
    videos: dict[Path, ProbedMedia],
    output_format: OutputFormat,
) -> dict[Path, Path]:
    targets: dict[Path, Path] = {}
    occupied: dict[Path, Path] = {}
    directories = {
        path.relative_to(source)
        for path in source.rglob("*")
        if path.is_dir() and not path.is_symlink()
    }
    for path in sorted(
        item
        for item in source.rglob("*")
        if item.is_file() or item.is_symlink()
    ):
        resolved = path.resolve()
        relative = path.relative_to(source)
        target = (
            relative.with_suffix(f".{output_format.value}")
            if resolved in videos and not path.is_symlink()
            else relative
        )
        previous = occupied.get(target)
        if previous is not None or target in directories:
            other = previous or source / target
            raise MovieError(
                "Deux éléments produiraient le même chemin dans la copie : "
                f"{other} et {path}."
            )
        occupied[target] = path
        if resolved in videos and not path.is_symlink():
            targets[resolved] = target
    return targets


def _copy_folder_entry(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        destination.symlink_to(os.readlink(source))
    else:
        shutil.copy2(source, destination)


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


def _ensure_free_space(plan: ConvertPlan, staging_root: Path) -> None:
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
    if staging_root.stat().st_dev != plan.output.parent.stat().st_dev:
        ensure_free_space(
            staging_root,
            required,
            operation="la copie temporaire locale de la conversion",
        )

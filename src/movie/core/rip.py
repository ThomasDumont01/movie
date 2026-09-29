"""Planification, vérification et publication sûre d'un disque vidéo."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from movie.core.media import MediaProbe
from movie.core.models import (
    DiscError,
    DiscScan,
    DiscTitle,
    Drive,
    MovieError,
    OutputExistsError,
    ProbedMedia,
    ProgressUpdate,
    RipPlan,
    RipResult,
)
from movie.core.storage import ensure_free_space, rip_required_bytes
from movie.core.verification import validate_extracted_title as _validate_media
from movie.core.workflow import (
    is_occupied as _is_occupied,
)
from movie.core.workflow import (
    phase_callback as _phase_callback,
)
from movie.core.workflow import (
    prepare_destination,
)
from movie.core.workflow import (
    publish_without_overwrite as _publish_without_overwrite,
)
from movie.core.workflow import (
    report_stage as _report_stage,
)
from movie.core.workflow import (
    single_mkv as _single_mkv,
)

__all__ = ["MediaProbe", "RipService", "build_rip_plan", "main_title_candidates"]

_MAX_FILENAME_BYTES = 240


def select_main_title(scan: DiscScan) -> DiscTitle:
    """Choisit le titre principal, ou lève une exception si le choix est ambigu."""

    candidates = main_title_candidates(scan)
    if len(candidates) > 1:
        choices = ", ".join(str(title.title_id) for title in candidates)
        raise DiscError(
            "Plusieurs titres principaux ont la même durée. "
            f"Choisis-en un dans la liste proposée ({choices})."
        )
    return next(iter(candidates))


def main_title_candidates(scan: DiscScan) -> tuple[DiscTitle, ...]:
    """Retourne les titres les plus longs, à une seconde près.

    Certains disques présentent plusieurs PGC, playlists, angles ou éditions
    indiscernables à l'analyse. Une confirmation vaut mieux qu'un choix
    arbitraire.
    """

    titles_with_duration = [
        title for title in scan.titles if title.duration_seconds is not None
    ]
    if not titles_with_duration:
        raise DiscError(
            "Le disque ne contient aucun titre dont la durée est exploitable."
        )
    maximum = max(title.duration_seconds or 0 for title in titles_with_duration)
    return tuple(
        title
        for title in titles_with_duration
        if abs((title.duration_seconds or 0) - maximum) <= 1
    )


def build_rip_plan(
    scan: DiscScan,
    output_directory: Path | str,
    *,
    title_id: int | None = None,
) -> RipPlan:
    """Construit le plan sans créer de dossier ni de fichier."""

    title = (
        next((item for item in scan.titles if item.title_id == title_id), None)
        if title_id is not None
        else select_main_title(scan)
    )
    if title is None:
        raise DiscError(f"Le titre {title_id} n'existe pas sur ce disque.")

    destination = Path(output_directory).expanduser()
    if destination.exists() and not destination.is_dir():
        raise MovieError(f"La destination doit être un dossier : {destination}")
    destination = destination.resolve()

    output = destination / _safe_filename(title)
    if _is_occupied(output):
        raise OutputExistsError(
            f"Le fichier existe déjà et ne sera pas remplacé : {output}"
        )
    return RipPlan(
        drive_index=scan.drive.index,
        title=title,
        output=output,
    )


class _MakeMkvBackend(Protocol):
    def drives(self) -> tuple[Drive, ...]: ...

    def scan(
        self,
        drive_index: int,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> DiscScan: ...

    def rip_title(
        self,
        drive_index: int,
        title_id: int,
        staging_directory: Path,
        *,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> object: ...


class _ProbeBackend(Protocol):
    def probe(self, path: Path) -> ProbedMedia: ...


class RipService:
    """Orchestre la numérisation sans exposer de logique à l'interface."""

    def __init__(
        self,
        makemkv: _MakeMkvBackend,
        probe: _ProbeBackend,
    ) -> None:
        self.makemkv = makemkv
        self.probe = probe

    def scan(
        self,
        drive_index: int,
        *,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> DiscScan:
        return self.makemkv.scan(drive_index, on_progress=on_progress)

    def execute(
        self,
        plan: RipPlan,
        *,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> RipResult:
        """Écrit en staging, vérifie, puis publie sans écrasement."""

        prepare_destination(plan.output)
        staging_root = Path(tempfile.gettempdir()).resolve()
        _ensure_free_space(plan, staging_root)
        staging = Path(tempfile.mkdtemp(prefix="movie-rip-", dir=staging_root))
        staged_file: Path | None = None
        verified = False
        published = False
        preserve_staging = False
        try:
            warnings: list[str] = []
            extraction_end = 0.90
            make_mkv_run = self.makemkv.rip_title(
                plan.drive_index,
                plan.title.title_id,
                staging,
                on_progress=_phase_callback(
                    on_progress,
                    start=0.0,
                    end=extraction_end,
                ),
            )
            diagnostics = getattr(make_mkv_run, "diagnostics", ())
            if isinstance(diagnostics, tuple):
                warnings.extend(diagnostics)
            staged_file = _single_mkv(staging)
            _report_stage(
                on_progress,
                "Vérification du fichier MKV",
                0.0,
                total_fraction=extraction_end,
            )
            source_media = self.probe.probe(staged_file)
            warnings.extend(_validate_media(plan.title, source_media))
            verified = True
            verification_end = extraction_end + 0.04
            _report_stage(
                on_progress,
                "Vérification du fichier MKV",
                1.0,
                total_fraction=verification_end,
            )
            _report_stage(
                on_progress,
                "Publication du fichier final",
                0.0,
                total_fraction=verification_end,
            )

            def report_copy_progress(fraction: float) -> None:
                _report_stage(
                    on_progress,
                    "Copie vers la destination",
                    fraction,
                    total_fraction=verification_end + 0.05 * fraction,
                )

            _publish_without_overwrite(
                staged_file,
                plan.output,
                on_copy_progress=(
                    report_copy_progress if on_progress is not None else None
                ),
            )
            published = True
            _report_stage(
                on_progress,
                "Publication du fichier final",
                1.0,
                total_fraction=1.0,
            )
        except MovieError as error:
            if (
                verified
                and not published
                and staged_file is not None
                and staged_file.is_file()
            ):
                preserve_staging = True
                raise MovieError(
                    f"{error} Le MKV local vérifié a été conservé ici : {staged_file}"
                ) from error
            raise
        finally:
            if not preserve_staging:
                shutil.rmtree(staging, ignore_errors=True)

        return RipResult(
            output=plan.output,
            media=replace(source_media, path=plan.output),
            warnings=tuple(dict.fromkeys(warnings)),
        )


def _safe_filename(title: DiscTitle) -> str:
    source_name = title.output_name or f"titre-{title.title_id:02}.mkv"
    path = Path(source_name)
    if path.name != source_name or not path.stem:
        return f"titre-{title.title_id:02}.mkv"
    candidate = f"{path.stem}.mkv"
    if len(candidate.encode("utf-8")) > _MAX_FILENAME_BYTES:
        return f"titre-{title.title_id:02}.mkv"
    return candidate


def _ensure_free_space(plan: RipPlan, staging_root: Path) -> None:
    """Refuse avant l'extraction une destination manifestement trop petite."""

    if not plan.title.size_bytes:
        return
    required = rip_required_bytes(plan.title)
    ensure_free_space(
        plan.output.parent,
        required,
        operation="une numérisation sûre",
    )
    if staging_root.stat().st_dev != plan.output.parent.stat().st_dev:
        ensure_free_space(
            staging_root,
            required,
            operation="la copie temporaire locale du disque",
        )

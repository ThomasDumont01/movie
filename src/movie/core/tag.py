"""Enrichissement vérifié et atomique d'un fichier multimédia existant."""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from movie.core.models import (
    MovieError,
    MovieMetadata,
    ProbedMedia,
    ProgressUpdate,
    TagPlan,
    TagResult,
)
from movie.core.storage import ensure_free_space, tag_required_bytes
from movie.core.verification import (
    validate_metadata,
    validate_preserved_media,
    validate_source,
)
from movie.core.workflow import phase_callback, report_stage

_SUPPORTED_SUFFIXES = {".mkv", ".mp4", ".m4a"}


class _ProbeBackend(Protocol):
    def probe(self, path: Path) -> ProbedMedia: ...


class _TaggerBackend(Protocol):
    def tag(
        self,
        source: Path,
        destination: Path,
        metadata: MovieMetadata,
        *,
        source_media: ProbedMedia,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> Path: ...


def build_tag_plan(source: Path | str, metadata: MovieMetadata) -> TagPlan:
    """Valide le média et les informations sans modifier le fichier."""

    unresolved = Path(source).expanduser()
    if unresolved.is_symlink():
        raise MovieError("Le fichier à enrichir ne doit pas être un lien symbolique.")
    source_path = unresolved.resolve()
    if not source_path.is_file():
        raise MovieError(f"Le fichier à enrichir est introuvable : {source_path}")
    if source_path.suffix.casefold() not in _SUPPORTED_SUFFIXES:
        raise MovieError("La commande tag accepte uniquement les fichiers MKV, MP4 et M4A.")

    title = " ".join(metadata.title.split())
    if not title:
        raise MovieError("Un titre est nécessaire pour écrire les métadonnées.")
    if metadata.year is not None and not 1 <= metadata.year <= 9999:
        raise MovieError("L'année doit être comprise entre 1 et 9999.")
    if metadata.poster_url and metadata.poster_path:
        raise MovieError("Choisis une jaquette distante ou locale, pas les deux.")

    normalized = replace(
        metadata,
        title=title,
        summary=metadata.summary.strip() if metadata.summary else None,
        genres=tuple(
            dict.fromkeys(genre.strip() for genre in metadata.genres if genre.strip())
        ),
        poster_path=(
            metadata.poster_path.expanduser()
            if metadata.poster_path is not None
            else None
        ),
    )
    return TagPlan(source=source_path, metadata=normalized)


class TagService:
    """Remuxe, vérifie puis remplace atomiquement le média d'origine."""

    def __init__(self, probe: _ProbeBackend, tagger: _TaggerBackend) -> None:
        self.probe = probe
        self.tagger = tagger

    def execute(
        self,
        plan: TagPlan,
        *,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> TagResult:
        source_media = self.probe.probe(plan.source)
        validate_source(source_media)
        _ensure_free_space(plan.source)

        source_mode = stat.S_IMODE(plan.source.stat().st_mode)
        with tempfile.TemporaryDirectory(
            prefix=".movie-tag-", dir=plan.source.parent
        ) as work_directory:
            staging = Path(work_directory)
            staged_file = staging / f"tagged{plan.source.suffix.casefold()}"
            report_stage(
                on_progress,
                "Préparation des métadonnées",
                0.0,
                total_fraction=0.0,
            )
            self.tagger.tag(
                plan.source,
                staged_file,
                plan.metadata,
                source_media=source_media,
                on_progress=phase_callback(on_progress, start=0.02, end=0.94),
            )
            report_stage(
                on_progress,
                "Vérification du fichier enrichi",
                0.0,
                total_fraction=0.94,
            )
            output_media = self.probe.probe(staged_file)
            validate_preserved_media(
                source_media,
                output_media,
                replace_artwork=plan.metadata.has_artwork,
            )
            validate_metadata(plan.metadata, output_media)
            report_stage(
                on_progress,
                "Vérification du fichier enrichi",
                1.0,
                total_fraction=0.98,
            )
            try:
                staged_file.chmod(source_mode)
                os.replace(staged_file, plan.source)
            except OSError as error:
                raise MovieError(
                    "Impossible de remplacer le fichier d'origine en toute sécurité."
                ) from error
            report_stage(
                on_progress,
                "Publication des métadonnées",
                1.0,
                total_fraction=1.0,
            )

        return TagResult(
            output=plan.source,
            media=replace(output_media, path=plan.source),
            warnings=(),
        )


def _ensure_free_space(source: Path) -> None:
    ensure_free_space(
        source.parent,
        tag_required_bytes(source),
        operation="modifier ce fichier sans risque",
    )

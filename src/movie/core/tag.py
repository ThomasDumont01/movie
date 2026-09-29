"""Enrichissement vérifié et atomique d'un fichier multimédia existant."""

from __future__ import annotations

import os
import re
import stat
import tempfile
import unicodedata
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from movie.core.models import (
    MovieError,
    MovieMetadata,
    OutputExistsError,
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
from movie.core.workflow import (
    is_occupied,
    phase_callback,
    prepare_destination,
    publish_without_overwrite,
    report_stage,
)
from movie.formats import taggable_format_names, taggable_suffixes

_MAX_FILENAME_BYTES = 240
_NON_FILENAME_CHARACTERS = re.compile(r"[^a-z0-9]+")
_LATIN_TRANSLITERATION = str.maketrans(
    {
        "æ": "ae",
        "ð": "d",
        "ø": "o",
        "þ": "th",
        "ł": "l",
        "œ": "oe",
    }
)


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


def build_tag_plan(
    source: Path | str,
    metadata: MovieMetadata,
) -> TagPlan:
    """Valide le média et les informations sans modifier le fichier."""

    unresolved = Path(source).expanduser()
    if unresolved.is_symlink():
        raise MovieError("Le fichier à enrichir ne doit pas être un lien symbolique.")
    source_path = unresolved.resolve()
    if not source_path.is_file():
        raise MovieError(f"Le fichier à enrichir est introuvable : {source_path}")
    if source_path.suffix.casefold() not in taggable_suffixes():
        raise MovieError(
            "La commande tag accepte uniquement les fichiers "
            f"{taggable_format_names()}."
        )

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
    output = _tagged_path(source_path, normalized)
    if output != source_path and is_occupied(output):
        raise OutputExistsError(f"Le fichier renommé existe déjà : {output}")
    return TagPlan(source=source_path, output=output, metadata=normalized)


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
        if plan.output != plan.source:
            prepare_destination(plan.output)

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
            except OSError as error:
                raise MovieError(
                    "Impossible de préserver les permissions du fichier d'origine."
                ) from error
            _publish_tagged_file(staged_file, plan.source, plan.output)
            report_stage(
                on_progress,
                "Publication des métadonnées",
                1.0,
                total_fraction=1.0,
            )

        return TagResult(
            output=plan.output,
            media=replace(output_media, path=plan.output),
            warnings=(),
        )


def _ensure_free_space(source: Path) -> None:
    ensure_free_space(
        source.parent,
        tag_required_bytes(source),
        operation="modifier ce fichier sans risque",
    )


def _tagged_path(source: Path, metadata: MovieMetadata) -> Path:
    """Construit le nom stable ``année_titre_normalisé.extension``."""

    title = _filename_slug(metadata.title)
    prefix = f"{metadata.year}_" if metadata.year else ""
    suffix = source.suffix.casefold()
    filename = _bounded_filename(prefix, title, suffix)
    return source.with_name(filename)


def _filename_slug(value: str) -> str:
    folded = value.casefold().translate(_LATIN_TRANSLITERATION)
    ascii_value = "".join(
        character
        for character in unicodedata.normalize("NFKD", folded)
        if not unicodedata.combining(character) and character.isascii()
    )
    return _NON_FILENAME_CHARACTERS.sub("_", ascii_value).strip("_") or "media"


def _bounded_filename(prefix: str, title: str, extension: str) -> str:
    reserved = f"{prefix}{extension}"
    available = _MAX_FILENAME_BYTES - len(reserved.encode("utf-8"))
    bounded_title = title[:available].rstrip("_") or "media"
    return f"{prefix}{bounded_title}{extension}"


def _publish_tagged_file(staged_file: Path, source: Path, output: Path) -> None:
    if output == source:
        try:
            os.replace(staged_file, source)
        except OSError as error:
            raise MovieError(
                "Impossible de remplacer le fichier d'origine en toute sécurité."
            ) from error
        return

    publish_without_overwrite(staged_file, output)
    try:
        source.unlink()
    except OSError as error:
        try:
            output.unlink()
        except OSError as rollback_error:
            raise MovieError(
                "Le nouveau fichier a été publié, mais l'ancien nom n'a pas pu être "
                f"retiré. Vérifie manuellement ces deux chemins : {source} et {output}."
            ) from rollback_error
        raise MovieError(
            "Le fichier n'a pas pu être renommé ; l'original a été conservé."
        ) from error

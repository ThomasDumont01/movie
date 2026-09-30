"""Enrichissement vérifié et atomique d'un fichier multimédia existant."""

from __future__ import annotations

import re
import stat
import tempfile
import unicodedata
from collections.abc import Callable
from dataclasses import replace
from datetime import date
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
from movie.core.storage import (
    ensure_free_space,
    tag_in_place_required_bytes,
    tag_required_bytes,
)
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
    rename_without_overwrite,
    replace_with_verified_file,
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
    def tag_in_place(
        self,
        source: Path,
        metadata: MovieMetadata,
        *,
        work_directory: Path,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> Path: ...

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
    release_date = metadata.release_date.strip() if metadata.release_date else None
    normalized_year = metadata.year
    if release_date is not None:
        try:
            parsed_date = date.fromisoformat(release_date)
        except ValueError as error:
            raise MovieError(
                "La date doit être une date ISO valide (AAAA-MM-JJ)."
            ) from error
        release_date = parsed_date.isoformat()
        if metadata.year is not None and metadata.year != parsed_date.year:
            raise MovieError("L'année et la date complète ne correspondent pas.")
        if normalized_year is None:
            normalized_year = parsed_date.year
    if metadata.poster_url and metadata.poster_path:
        raise MovieError("Choisis une jaquette distante ou locale, pas les deux.")
    if metadata.fanart_url and metadata.fanart_path:
        raise MovieError("Choisis un arrière-plan distant ou local, pas les deux.")

    normalized = replace(
        metadata,
        title=title,
        year=normalized_year,
        release_date=release_date,
        summary=metadata.summary.strip() if metadata.summary else None,
        genres=tuple(
            dict.fromkeys(genre.strip() for genre in metadata.genres if genre.strip())
        ),
        poster_path=(
            metadata.poster_path.expanduser()
            if metadata.poster_path is not None
            else None
        ),
        fanart_path=(
            metadata.fanart_path.expanduser()
            if metadata.fanart_path is not None
            else None
        ),
    )
    output = _tagged_path(source_path, normalized)
    if output != source_path and is_occupied(output):
        raise OutputExistsError(f"Le fichier renommé existe déjà : {output}")
    return TagPlan(source=source_path, output=output, metadata=normalized)


class TagService:
    """Enrichit un média, directement pour MKV ou par remuxage pour MP4/M4V."""

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
        if plan.source.suffix.casefold() == ".mkv":
            return self._execute_mkv_in_place(
                plan,
                source_media,
                on_progress=on_progress,
            )
        return self._execute_remux(
            plan,
            source_media,
            on_progress=on_progress,
        )

    def _execute_mkv_in_place(
        self,
        plan: TagPlan,
        source_media: ProbedMedia,
        *,
        on_progress: Callable[[ProgressUpdate], None] | None,
    ) -> TagResult:
        staging_root = Path(tempfile.gettempdir()).resolve()
        _ensure_in_place_space(plan.source, staging_root)
        if plan.output != plan.source:
            prepare_destination(plan.output)

        with tempfile.TemporaryDirectory(
            prefix="movie-tag-mkv-", dir=staging_root
        ) as work_directory:
            report_stage(
                on_progress,
                "Préparation des métadonnées",
                0.0,
                total_fraction=0.0,
            )
            self.tagger.tag_in_place(
                plan.source,
                plan.metadata,
                work_directory=Path(work_directory),
                on_progress=phase_callback(on_progress, start=0.02, end=0.82),
            )
            report_stage(
                on_progress,
                "Vérification du MKV enrichi",
                0.0,
                total_fraction=0.82,
            )
            output_media = self.probe.probe(plan.source)
            validate_preserved_media(
                source_media,
                output_media,
                replace_artwork=plan.metadata.has_artwork,
            )
            validate_metadata(plan.metadata, output_media)
            report_stage(
                on_progress,
                "Vérification du MKV enrichi",
                1.0,
                total_fraction=0.94,
            )
            if plan.output != plan.source:
                report_stage(
                    on_progress,
                    "Renommage du fichier",
                    0.0,
                    total_fraction=0.94,
                )
                rename_without_overwrite(plan.source, plan.output)
            report_stage(
                on_progress,
                "Métadonnées enregistrées",
                1.0,
                total_fraction=1.0,
            )

        return TagResult(
            output=plan.output,
            media=replace(output_media, path=plan.output),
        )

    def _execute_remux(
        self,
        plan: TagPlan,
        source_media: ProbedMedia,
        *,
        on_progress: Callable[[ProgressUpdate], None] | None,
    ) -> TagResult:
        staging_root = Path(tempfile.gettempdir()).resolve()
        _ensure_free_space(plan.source, staging_root)
        if plan.output != plan.source:
            prepare_destination(plan.output)

        source_mode = stat.S_IMODE(plan.source.stat().st_mode)
        with tempfile.TemporaryDirectory(
            prefix="movie-tag-", dir=staging_root
        ) as work_directory:
            staging = Path(work_directory)
            staged_file = staging / f"tagged{plan.source.suffix.casefold()}"
            warnings = (
                [
                    (
                        "L'arrière-plan panoramique ne peut être intégré de manière "
                        "fiable que dans un MKV ; la jaquette reste intégrée."
                    )
                ]
                if plan.metadata.has_fanart
                else []
            )
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
                on_progress=phase_callback(on_progress, start=0.02, end=0.88),
            )
            report_stage(
                on_progress,
                "Vérification du fichier enrichi",
                0.0,
                total_fraction=0.88,
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
                total_fraction=0.90,
            )
            try:
                staged_file.chmod(source_mode)
            except OSError as error:
                raise MovieError(
                    "Impossible de préserver les permissions du fichier d'origine."
                ) from error
            report_stage(
                on_progress,
                "Publication du fichier enrichi",
                0.0,
                total_fraction=0.90,
            )

            def report_copy_progress(fraction: float) -> None:
                report_stage(
                    on_progress,
                    "Copie vers la destination",
                    fraction,
                    total_fraction=0.90 + 0.08 * fraction,
                )

            _publish_tagged_file(
                staged_file,
                plan.source,
                plan.output,
                on_copy_progress=(
                    report_copy_progress if on_progress is not None else None
                ),
            )
            report_stage(
                on_progress,
                "Publication des métadonnées",
                1.0,
                total_fraction=1.0,
            )

        return TagResult(
            output=plan.output,
            media=replace(output_media, path=plan.output),
            warnings=tuple(warnings),
        )


def _ensure_free_space(source: Path, staging_root: Path) -> None:
    required = tag_required_bytes(source)
    ensure_free_space(
        source.parent,
        required,
        operation="modifier ce fichier sans risque",
    )
    if staging_root.stat().st_dev != source.parent.stat().st_dev:
        ensure_free_space(
            staging_root,
            required,
            operation="la copie temporaire locale du média",
        )


def _ensure_in_place_space(source: Path, staging_root: Path) -> None:
    required = tag_in_place_required_bytes()
    ensure_free_space(
        source.parent,
        required,
        operation="modifier les métadonnées du MKV",
    )
    if staging_root.stat().st_dev != source.parent.stat().st_dev:
        ensure_free_space(
            staging_root,
            required,
            operation="préparer localement les métadonnées et illustrations",
        )


def _tagged_path(source: Path, metadata: MovieMetadata) -> Path:
    """Construit le nom stable ``titre_normalisé.extension``."""

    title = _filename_slug(metadata.title)
    suffix = source.suffix.casefold()
    filename = _bounded_filename(title, suffix)
    return source.with_name(filename)


def _filename_slug(value: str) -> str:
    folded = value.casefold().translate(_LATIN_TRANSLITERATION)
    ascii_value = "".join(
        character
        for character in unicodedata.normalize("NFKD", folded)
        if not unicodedata.combining(character) and character.isascii()
    )
    return _NON_FILENAME_CHARACTERS.sub("_", ascii_value).strip("_") or "media"


def _bounded_filename(title: str, extension: str) -> str:
    available = _MAX_FILENAME_BYTES - len(extension.encode("utf-8"))
    bounded_title = title[:available].rstrip("_") or "media"
    return f"{bounded_title}{extension}"


def _publish_tagged_file(
    staged_file: Path,
    source: Path,
    output: Path,
    *,
    on_copy_progress: Callable[[float], None] | None = None,
) -> None:
    if output == source:
        replace_with_verified_file(
            staged_file,
            source,
            on_copy_progress=on_copy_progress,
        )
        return

    publish_without_overwrite(
        staged_file,
        output,
        on_copy_progress=on_copy_progress,
    )
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

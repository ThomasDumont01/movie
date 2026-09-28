"""Garanties communes aux opérations longues qui produisent un fichier."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from movie.core.models import MovieError, OutputExistsError, ProgressUpdate


def is_occupied(path: Path) -> bool:
    """Détecte aussi les liens symboliques cassés, que ``Path.exists`` ignore."""

    return path.exists() or path.is_symlink()


def prepare_destination(output: Path) -> None:
    if is_occupied(output):
        raise OutputExistsError(
            f"Le fichier existe déjà et ne sera pas remplacé : {output}"
        )
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise MovieError(
            f"Impossible de préparer le dossier de destination : {output.parent}"
        ) from error


def publish_without_overwrite(staged_file: Path, destination: Path) -> None:
    try:
        # Le staging et la destination partagent le même volume : le lien dur
        # publie atomiquement et échoue si une autre opération a gagné la course.
        os.link(staged_file, destination)
    except FileExistsError as error:
        raise OutputExistsError(
            f"Un fichier est apparu pendant l'opération : {destination}. "
            "Rien n'a été écrasé."
        ) from error
    except OSError as error:
        raise MovieError(
            "Publication sûre impossible ; aucun fichier final n'a été créé."
        ) from error


def single_mkv(staging: Path) -> Path:
    outputs = sorted(
        path
        for path in staging.iterdir()
        if path.is_file() and path.suffix.casefold() == ".mkv"
    )
    if len(outputs) != 1:
        raise MovieError(
            "MakeMKV devait produire exactement un MKV pour le titre choisi ; "
            f"{len(outputs)} fichier(s) trouvé(s). Rien n'a été publié."
        )
    if outputs[0].stat().st_size == 0:
        raise MovieError("Le MKV temporaire est vide. Rien n'a été publié.")
    return outputs[0]


def report_stage(
    callback: Callable[[ProgressUpdate], None] | None,
    label: str,
    fraction: float,
    *,
    total_fraction: float,
) -> None:
    if callback is not None:
        callback(
            ProgressUpdate(
                current_label=label,
                total_label=None,
                current_fraction=fraction,
                total_fraction=total_fraction,
            )
        )


def phase_callback(
    callback: Callable[[ProgressUpdate], None] | None,
    *,
    start: float,
    end: float,
) -> Callable[[ProgressUpdate], None] | None:
    """Replace la progression d'un outil dans une phase de l'opération totale."""

    if callback is None:
        return None

    def report(update: ProgressUpdate) -> None:
        fraction = update.total_fraction
        if fraction is None:
            fraction = update.current_fraction
        bounded = max(0.0, min(1.0, fraction if fraction is not None else 0.0))
        callback(
            ProgressUpdate(
                current_label=update.current_label,
                total_label=update.total_label,
                current_fraction=update.current_fraction,
                total_fraction=start + (end - start) * bounded,
            )
        )

    return report

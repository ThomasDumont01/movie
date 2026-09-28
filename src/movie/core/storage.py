"""Contrôles communs des destinations et de l'espace de travail."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from movie.core.models import DiscTitle, MovieError, OutputFormat

PUBLICATION_MARGIN_BYTES = 512 * 1024 * 1024
TAG_MARGIN_BYTES = 256 * 1024 * 1024


def rip_required_bytes(title: DiscTitle) -> int:
    """Estime l'espace maximal nécessaire au MKV et à sa publication."""

    return (title.size_bytes or 0) + PUBLICATION_MARGIN_BYTES


def conversion_required_bytes(
    source: Path,
    *,
    output_format: OutputFormat,
    iso_title: DiscTitle | None,
) -> int:
    """Estime les fichiers temporaires coexistants pendant une conversion."""

    source_size = source.stat().st_size
    if iso_title is not None and iso_title.size_bytes:
        source_size = iso_title.size_bytes
    # Une conversion ISO vers MP4/M4V conserve simultanément le MKV extrait et
    # le fichier final. Toute autre conversion ne crée qu'une nouvelle sortie.
    copies = 2 if iso_title is not None and output_format is not OutputFormat.MKV else 1
    return source_size * copies + PUBLICATION_MARGIN_BYTES


def tag_required_bytes(source: Path) -> int:
    return source.stat().st_size + TAG_MARGIN_BYTES


def output_directory_issue(
    directory: Path,
    *,
    required_bytes: int = 0,
    must_exist: bool,
) -> str | None:
    """Explique pourquoi une destination ne peut pas accueillir l'opération."""

    destination = directory.expanduser()
    if destination.exists():
        if not destination.is_dir():
            return f"ce chemin n'est pas un dossier : {destination}"
        probe_directory = destination
    else:
        if must_exist:
            return f"le dossier est introuvable ou déconnecté : {destination}"
        probe_directory = _nearest_existing_parent(destination)
        if probe_directory is None:
            return f"aucun dossier parent accessible n'existe pour : {destination}"

    if not os.access(probe_directory, os.W_OK | os.X_OK):
        return f"le dossier n'est pas accessible en écriture : {destination}"
    try:
        available = shutil.disk_usage(probe_directory).free
    except OSError:
        return f"l'espace disponible ne peut pas être vérifié : {destination}"
    if required_bytes and available < required_bytes:
        return (
            f"espace insuffisant dans {destination} "
            f"({format_bytes(required_bytes)} nécessaires, "
            f"{format_bytes(available)} disponibles)"
        )
    return None


def ensure_free_space(directory: Path, required: int, *, operation: str) -> None:
    """Refuse une opération si son staging ne peut manifestement pas tenir."""

    try:
        available = shutil.disk_usage(directory).free
    except OSError as error:
        raise MovieError(
            f"Impossible de vérifier l'espace disponible dans : {directory}"
        ) from error
    if available < required:
        raise MovieError(
            f"Espace insuffisant pour {operation} : "
            f"{format_bytes(required)} nécessaires, "
            f"{format_bytes(available)} disponibles."
        )


def format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("octets", "Ko", "Mo", "Go", "To"):
        if amount < 1024 or unit == "To":
            return f"{int(amount)} {unit}" if unit == "octets" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} octets"


def _nearest_existing_parent(path: Path) -> Path | None:
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            return None
        candidate = parent
    return candidate if candidate.is_dir() else None

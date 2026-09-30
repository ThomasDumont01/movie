"""Modification directe et ciblée des métadonnées Matroska."""

from __future__ import annotations

import json
import shutil
import subprocess
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from movie.core.models import (
    MovieError,
    MovieMetadata,
    ProgressUpdate,
    ToolUnavailableError,
)
from movie.ffmpeg import prepare_artwork, prepare_fanart

_MANAGED_TAG_NAMES = frozenset(
    {
        "COMMENT",
        "DATE",
        "DATE_RELEASED",
        "DESCRIPTION",
        "GENRE",
        "MOVIE_SOURCE",
        "SYNOPSIS",
        "TITLE",
        "YEAR",
    }
)


@dataclass(frozen=True, slots=True)
class _Attachment:
    attachment_id: int
    filename: str
    content_type: str
    description: str

    @property
    def is_image(self) -> bool:
        return self.content_type.casefold().startswith("image/")

    @property
    def is_fanart(self) -> bool:
        name = _fold(self.filename)
        description = _fold(self.description)
        return "fanart" in name or "arriere plan" in description


class MatroskaEditor:
    """Écrit les tags et illustrations d'un MKV sans remuxer ses pistes."""

    def __init__(
        self,
        *,
        mkvpropedit: str | None = None,
        mkvextract: str | None = None,
        mkvmerge: str | None = None,
    ) -> None:
        self.mkvpropedit = mkvpropedit
        self.mkvextract = mkvextract
        self.mkvmerge = mkvmerge

    def edit(
        self,
        source: Path,
        metadata: MovieMetadata,
        *,
        work_directory: Path,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> Path:
        """Modifie uniquement les en-têtes et pièces jointes du MKV existant."""

        if source.suffix.casefold() != ".mkv":
            raise MovieError("La modification directe est réservée aux fichiers MKV.")

        propedit = self.mkvpropedit or find_mkvpropedit()
        extract = self.mkvextract or find_mkvextract()
        _report(on_progress, "Lecture des métadonnées MKV", 0.05)

        tags_file = work_directory / "global-tags.xml"
        self._extract_global_tags(extract, source, tags_file)
        _update_global_tags(tags_file, metadata)

        poster = prepare_artwork(metadata, work_directory)
        fanart = prepare_fanart(metadata, work_directory)
        _report(on_progress, "Préparation des métadonnées MKV", 0.35)

        attachments: tuple[_Attachment, ...] = ()
        if poster is not None or fanart is not None:
            merge = self.mkvmerge or find_mkvmerge()
            attachments = self._identify_attachments(merge, source)

        command = [
            propedit,
            str(source),
            "--flush-on-close",
            "--edit",
            "info",
            "--set",
            f"title={metadata.title}",
            "--tags",
            f"global:{tags_file}",
        ]
        if poster is not None:
            for attachment in attachments:
                if attachment.is_image:
                    command.extend(
                        ("--delete-attachment", str(attachment.attachment_id))
                    )
        elif fanart is not None:
            for attachment in attachments:
                if attachment.is_image and attachment.is_fanart:
                    command.extend(
                        ("--delete-attachment", str(attachment.attachment_id))
                    )

        if poster is not None:
            poster_path, mime_type = poster
            _add_attachment(
                command,
                poster_path,
                mime_type=mime_type,
                description="Jaquette",
            )
        if fanart is not None:
            _add_attachment(
                command,
                fanart,
                mime_type=_image_mime_type(fanart),
                description="Arrière-plan",
            )

        _report(on_progress, "Écriture directe dans le MKV", 0.55)
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            raise MovieError("Impossible de lancer mkvpropedit.") from error
        if result.returncode != 0:
            details = (result.stderr or result.stdout).strip()
            suffix = f" : {details[-1_000:]}" if details else "."
            raise MovieError(
                "mkvpropedit n'a pas pu modifier les métadonnées du MKV" + suffix
            )
        if not source.is_file() or source.stat().st_size == 0:
            raise MovieError("Le MKV est introuvable ou vide après sa modification.")
        _report(on_progress, "Écriture directe dans le MKV", 1.0)
        return source

    @staticmethod
    def _extract_global_tags(executable: str, source: Path, output: Path) -> None:
        try:
            result = subprocess.run(
                [
                    executable,
                    str(source),
                    "tags",
                    "--no-track-tags",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            raise MovieError("Impossible de lancer mkvextract.") from error
        if result.returncode != 0:
            details = (result.stderr or result.stdout).strip()
            suffix = f" : {details[-1_000:]}" if details else "."
            raise MovieError("Impossible de lire les métadonnées du MKV" + suffix)
        try:
            output_is_empty = not output.is_file() or output.stat().st_size == 0
        except OSError as error:
            raise MovieError(
                "Impossible de préparer les métadonnées du MKV."
            ) from error
        if output_is_empty:
            # mkvextract réussit sans créer de fichier lorsqu'un MKV ne contient
            # encore aucun tag global. C'est le cas normal d'un premier tag.
            try:
                ElementTree.ElementTree(ElementTree.Element("Tags")).write(
                    output,
                    encoding="utf-8",
                    xml_declaration=True,
                )
            except OSError as error:
                raise MovieError(
                    "Impossible de préparer les métadonnées du MKV."
                ) from error

    @staticmethod
    def _identify_attachments(executable: str, source: Path) -> tuple[_Attachment, ...]:
        try:
            result = subprocess.run(
                [
                    executable,
                    "--identification-format",
                    "json",
                    "--identify",
                    str(source),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            raise MovieError("Impossible de lancer mkvmerge.") from error
        if result.returncode != 0:
            details = (result.stderr or result.stdout).strip()
            suffix = f" : {details[-1_000:]}" if details else "."
            raise MovieError(
                "Impossible d'identifier les illustrations du MKV" + suffix
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise MovieError("mkvmerge a renvoyé une analyse illisible.") from error

        raw_attachments = payload.get("attachments", [])
        if not isinstance(raw_attachments, list):
            raise MovieError("mkvmerge a renvoyé une liste de pièces jointes invalide.")
        attachments: list[_Attachment] = []
        for item in raw_attachments:
            if not isinstance(item, dict) or not isinstance(item.get("id"), int):
                continue
            attachments.append(
                _Attachment(
                    attachment_id=item["id"],
                    filename=str(item.get("file_name") or ""),
                    content_type=str(item.get("content_type") or ""),
                    description=str(item.get("description") or ""),
                )
            )
        return tuple(attachments)


def find_mkvpropedit() -> str:
    return _find_mkvtool(
        "mkvpropedit",
        "mkvpropedit est introuvable. Installe MKVToolNix avec "
        "« brew install mkvtoolnix ».",
    )


def find_mkvextract() -> str:
    return _find_mkvtool(
        "mkvextract",
        "mkvextract est introuvable. Installe MKVToolNix avec "
        "« brew install mkvtoolnix ».",
    )


def find_mkvmerge() -> str:
    return _find_mkvtool(
        "mkvmerge",
        "mkvmerge est introuvable. Installe MKVToolNix avec "
        "« brew install mkvtoolnix ».",
    )


def _find_mkvtool(name: str, message: str) -> str:
    executable = shutil.which(name)
    if not executable:
        raise ToolUnavailableError(message)
    return executable


def _update_global_tags(path: Path, metadata: MovieMetadata) -> None:
    try:
        tree = ElementTree.parse(path)
    except (ElementTree.ParseError, OSError) as error:
        raise MovieError(
            "Les métadonnées Matroska existantes sont illisibles."
        ) from error
    root = tree.getroot()
    if root.tag != "Tags":
        raise MovieError("Le document de métadonnées Matroska est invalide.")

    global_tags = list(root.findall("Tag"))
    for tag in global_tags:
        for simple in list(tag.findall("Simple")):
            name = simple.findtext("Name", default="").upper()
            if name in _MANAGED_TAG_NAMES:
                tag.remove(simple)
    target = global_tags[0] if global_tags else ElementTree.SubElement(root, "Tag")
    if target.find("Targets") is None:
        target.insert(0, ElementTree.Element("Targets"))

    values = (
        ("TITLE", metadata.title),
        ("DATE_RELEASED", metadata.date_value),
        ("DESCRIPTION", (metadata.summary or "")[:4000] or None),
        ("SYNOPSIS", (metadata.summary or "")[:4000] or None),
        ("GENRE", ", ".join(metadata.genres) or None),
        ("MOVIE_SOURCE", metadata.source_url),
        (
            "COMMENT",
            f"TMDB: {metadata.source_url}" if metadata.source_url else None,
        ),
    )
    for name, value in values:
        if value:
            simple = ElementTree.SubElement(target, "Simple")
            ElementTree.SubElement(simple, "Name").text = name
            ElementTree.SubElement(simple, "String").text = value

    ElementTree.indent(tree, space="  ")
    try:
        tree.write(path, encoding="utf-8", xml_declaration=True)
    except OSError as error:
        raise MovieError("Impossible de préparer les métadonnées Matroska.") from error


def _add_attachment(
    command: list[str],
    path: Path,
    *,
    mime_type: str,
    description: str,
) -> None:
    command.extend(
        (
            "--attachment-name",
            path.name,
            "--attachment-mime-type",
            mime_type,
            "--attachment-description",
            description,
            "--add-attachment",
            str(path),
        )
    )


def _image_mime_type(path: Path) -> str:
    try:
        return {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }[path.suffix.casefold()]
    except KeyError as error:  # pragma: no cover - prepare_fanart filtre déjà le format
        raise MovieError(
            f"Format d'image non pris en charge : {path.suffix}"
        ) from error


def _fold(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(character)
    ).replace("-", " ")


def _report(
    callback: Callable[[ProgressUpdate], None] | None,
    label: str,
    fraction: float,
) -> None:
    if callback is not None:
        callback(ProgressUpdate(label, None, fraction, fraction))

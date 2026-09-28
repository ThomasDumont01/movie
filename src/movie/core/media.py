"""Inspection générique des fichiers multimédias avec ffprobe."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from movie.core.models import (
    MediaStream,
    MovieError,
    ProbedMedia,
    ToolUnavailableError,
)


class MediaProbe:
    """Lit un média avec ffprobe sans le modifier."""

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or _find_ffprobe()

    def probe(self, path: Path) -> ProbedMedia:
        try:
            result = subprocess.run(
                [
                    self.executable,
                    "-v",
                    "error",
                    "-show_format",
                    "-show_streams",
                    "-show_chapters",
                    "-of",
                    "json",
                    str(path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            raise MovieError("Impossible de lancer ffprobe.") from error
        if result.returncode != 0:
            details = (result.stderr or result.stdout).strip()
            raise MovieError(
                f"ffprobe ne peut pas lire le média temporaire : {details[-1_000:]}"
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise MovieError("ffprobe a renvoyé une analyse illisible.") from error

        raw_duration = payload.get("format", {}).get("duration")
        try:
            duration = float(raw_duration) if raw_duration is not None else None
        except (TypeError, ValueError):
            duration = None
        streams = payload.get("streams", [])
        chapters = payload.get("chapters", [])
        normalized_streams = tuple(
            MediaStream(
                kind=_stream_kind(stream),
                language=_stream_language(stream),
                codec=(
                    str(stream.get("codec_name"))
                    if stream.get("codec_name")
                    else None
                ),
                stream_id=(
                    int(stream["index"])
                    if isinstance(stream.get("index"), int)
                    else None
                ),
                is_artwork=_is_artwork(stream),
            )
            for stream in streams
            if isinstance(stream, dict) and stream.get("codec_type")
        )
        raw_tags = payload.get("format", {}).get("tags", {})
        format_tags = (
            tuple(
                sorted(
                    (str(key), str(value))
                    for key, value in raw_tags.items()
                )
            )
            if isinstance(raw_tags, dict)
            else ()
        )
        return ProbedMedia(
            path=path,
            duration_seconds=duration,
            stream_types=tuple(stream.kind for stream in normalized_streams),
            chapter_count=len(chapters) if isinstance(chapters, list) else 0,
            format_tags=format_tags,
            streams=normalized_streams,
        )


def _find_ffprobe() -> str:
    executable = shutil.which("ffprobe")
    if not executable:
        raise ToolUnavailableError(
            "ffprobe est introuvable. Installe FFmpeg puis relance « movie doctor »."
        )
    return executable


def _stream_language(stream: dict[object, object]) -> str | None:
    tags = stream.get("tags")
    if not isinstance(tags, dict):
        return None
    value = tags.get("language") or tags.get("LANGUAGE")
    if not isinstance(value, str):
        return None
    normalized = value.casefold().strip()
    return normalized if normalized not in {"", "und", "nolang"} else None


def _stream_kind(stream: dict[object, object]) -> str:
    if _is_artwork(stream):
        return "attachment"
    return str(stream.get("codec_type"))


def _is_artwork(stream: dict[object, object]) -> bool:
    disposition = stream.get("disposition")
    if isinstance(disposition, dict) and disposition.get("attached_pic") == 1:
        return True
    tags = stream.get("tags")
    if not isinstance(tags, dict):
        return False
    mime_type = tags.get("mimetype") or tags.get("MIMETYPE")
    return isinstance(mime_type, str) and mime_type.casefold().startswith("image/")

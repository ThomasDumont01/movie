"""Inspection générique des fichiers multimédias avec ffprobe."""

from __future__ import annotations

import errno
import json
import shutil
import subprocess
import time
from pathlib import Path

from movie.core.models import (
    MediaStream,
    MovieError,
    ProbedMedia,
    ToolUnavailableError,
)
from movie.formats import input_format_hint


class MediaProbe:
    """Lit un média avec ffprobe sans le modifier."""

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or _find_ffprobe()

    def probe(self, path: Path) -> ProbedMedia:
        output = self._probe_output(path)
        media = self._parse_probe(path, output)
        hint = input_format_hint(path.suffix)
        if hint is None or "video" in media.stream_types:
            return media

        try:
            forced_output = self._probe_output(path, input_format=hint)
            forced_media = self._parse_probe(path, forced_output)
        except MovieError:
            return media
        return forced_media if "video" in forced_media.stream_types else media

    def _probe_output(self, path: Path, *, input_format: str | None = None) -> str:
        result: subprocess.CompletedProcess[str] | None = None
        last_error: OSError | None = None
        for delay in (0.25, 0.75, 2.0, 4.0, None):
            result, last_error = self._run_probe(path, input_format=input_format)
            if result is not None and (
                result.returncode == 0 or not _is_transient_result(result)
            ):
                break
            if result is None and not _is_transient_os_error(last_error):
                break
            if delay is not None:
                time.sleep(delay)

        if result is None:
            raise MovieError("Impossible de lancer ffprobe.") from last_error
        if result.returncode != 0:
            details = (result.stderr or result.stdout).strip()
            raise MovieError(
                f"FFmpeg ne reconnaît pas ce fichier multimédia : {details[-1_000:]}"
            )

        return result.stdout

    def _run_probe(
        self,
        path: Path,
        *,
        input_format: str | None,
    ) -> tuple[subprocess.CompletedProcess[str] | None, OSError | None]:
        command = [self.executable, "-v", "error"]
        if input_format is not None:
            command.extend(("-f", input_format))
        command.extend(
            (
                "-show_format",
                "-show_streams",
                "-show_chapters",
                "-of",
                "json",
                str(path),
            )
        )
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            return None, error
        return result, None

    @staticmethod
    def _parse_probe(path: Path, output: str) -> ProbedMedia:
        try:
            payload = json.loads(output)
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
                    str(stream.get("codec_name")) if stream.get("codec_name") else None
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
            tuple(sorted((str(key), str(value)) for key, value in raw_tags.items()))
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


def _is_transient_result(result: subprocess.CompletedProcess[str]) -> bool:
    details = (result.stderr or result.stdout).casefold()
    return any(
        marker in details
        for marker in (
            "resource temporarily unavailable",
            "resource busy",
            "device or resource busy",
        )
    )


def _is_transient_os_error(error: OSError | None) -> bool:
    return error is not None and error.errno in {errno.EAGAIN, errno.EBUSY}


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

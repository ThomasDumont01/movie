"""Commandes FFmpeg des formats de conversion officiellement pris en charge."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from movie.core.models import (
    MovieError,
    OutputFormat,
    OutputQuality,
    ProgressUpdate,
)
from movie.ffmpeg import find_ffmpeg, run_ffmpeg


class MediaConverter:
    """Convertit un média selon un nombre réduit de profils prévisibles."""

    _VIDEO_PROFILES: ClassVar[dict[OutputQuality, tuple[int, str]]] = {
        OutputQuality.HIGH: (18, "320k"),
        OutputQuality.BALANCED: (21, "256k"),
        OutputQuality.COMPACT: (25, "160k"),
    }
    _AUDIO_PROFILES: ClassVar[dict[OutputQuality, str]] = {
        OutputQuality.HIGH: "320k",
        OutputQuality.BALANCED: "256k",
        OutputQuality.COMPACT: "160k",
    }

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable

    def convert(
        self,
        source: Path,
        destination: Path,
        *,
        output_format: OutputFormat,
        quality: OutputQuality,
        audio_track: int | None = None,
        duration_seconds: float | None = None,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> Path:
        if output_format is OutputFormat.MKV:
            command = self._mkv_command(source, destination, quality)
        elif output_format is OutputFormat.MP4:
            command = self._mp4_command(source, destination, quality)
        elif output_format is OutputFormat.M4A:
            command = self._m4a_command(
                source,
                destination,
                quality,
                audio_track,
            )
        else:  # pragma: no cover
            raise MovieError(f"Format de conversion non pris en charge : {output_format}")

        command.extend(("-progress", "pipe:1", "-nostats", "-y", str(destination)))
        label = f"Conversion en {output_format.value.upper()}"
        return run_ffmpeg(
            command,
            action=f"créer le {output_format.value.upper()}",
            expected_output=destination,
            duration_seconds=duration_seconds,
            on_progress=on_progress,
            progress_label=label,
        )

    def _base_command(self, source: Path) -> list[str]:
        return [
            self.executable or find_ffmpeg(),
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(source),
        ]

    def _mkv_command(
        self,
        source: Path,
        destination: Path,
        quality: OutputQuality,
    ) -> list[str]:
        if quality is not OutputQuality.SOURCE:
            raise MovieError("Le MKV utilise obligatoirement le profil source.")
        command = self._base_command(source)
        command.extend(
            ("-map", "0", "-map_metadata", "0", "-map_chapters", "0", "-c", "copy")
        )
        return command

    def _mp4_command(
        self,
        source: Path,
        destination: Path,
        quality: OutputQuality,
    ) -> list[str]:
        try:
            crf, audio_bitrate = self._VIDEO_PROFILES[quality]
        except KeyError as error:
            raise MovieError(
                "Le MP4 nécessite un profil high, balanced ou compact."
            ) from error
        command = self._base_command(source)
        command.extend(("-map", "0:v:0", "-map", "0:a?"))
        command.extend(
            (
                "-map_metadata", "0", "-map_chapters", "0",
                "-c:v:0", "libx264", "-preset:v:0", "medium",
                "-crf:v:0", str(crf), "-pix_fmt:v:0", "yuv420p",
                "-c:a", "aac", "-b:a", audio_bitrate,
                "-movflags", "+faststart",
            )
        )
        return command

    def _m4a_command(
        self,
        source: Path,
        destination: Path,
        quality: OutputQuality,
        audio_track: int | None,
    ) -> list[str]:
        if audio_track is None or audio_track < 0:
            raise MovieError("La conversion M4A nécessite une piste audio explicite.")
        command = self._base_command(source)
        command.extend(("-map", f"0:a:{audio_track}", "-map_metadata", "0"))
        if quality is OutputQuality.LOSSLESS:
            command.extend(("-c:a", "alac"))
        else:
            try:
                bitrate = self._AUDIO_PROFILES[quality]
            except KeyError as error:
                raise MovieError(
                    "Le M4A nécessite un profil high, balanced, compact ou lossless."
                ) from error
            command.extend(("-c:a", "aac", "-b:a", bitrate))
        command.extend(("-movflags", "+faststart"))
        return command

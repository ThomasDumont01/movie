"""Commandes FFmpeg des formats de conversion officiellement pris en charge."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from movie.core.models import (
    MovieError,
    OutputFormat,
    OutputQuality,
    ProbedMedia,
    ProgressUpdate,
)
from movie.ffmpeg import find_ffmpeg, run_ffmpeg
from movie.formats import (
    TRANSPORT_STREAM_SUFFIXES,
    EncodingFamily,
    SubtitleMode,
    input_format_hint,
    output_spec,
    subtitle_codec_supported,
)
from movie.matroska import find_mkvmerge

_H264_CODEC_NAMES = frozenset({"h264", "avc", "avc1"})


def copies_video_without_reencoding(
    source_media: ProbedMedia | None,
    output_format: OutputFormat,
) -> bool:
    """Indique si le flux vidéo H.264 peut être remuxé sans perte."""

    if source_media is None:
        return False
    if output_spec(output_format).encoding is not EncodingFamily.H264:
        return False
    video_stream = next(
        (
            stream
            for stream in source_media.streams
            if stream.kind == "video" and not stream.is_artwork
        ),
        None,
    )
    return (
        video_stream is not None
        and video_stream.codec is not None
        and video_stream.codec.casefold() in _H264_CODEC_NAMES
    )


class MediaConverter:
    """Convertit un média avec des associations conteneur/codecs prévisibles."""

    _VIDEO_PROFILES: ClassVar[dict[OutputQuality, tuple[int, str]]] = {
        OutputQuality.HIGH: (18, "320k"),
        OutputQuality.BALANCED: (21, "256k"),
        OutputQuality.COMPACT: (25, "160k"),
    }
    _VP9_PROFILES: ClassVar[dict[OutputQuality, tuple[int, str]]] = {
        OutputQuality.HIGH: (20, "192k"),
        OutputQuality.BALANCED: (30, "128k"),
        OutputQuality.COMPACT: (38, "96k"),
    }
    _LEGACY_PROFILES: ClassVar[dict[OutputQuality, tuple[int, str]]] = {
        OutputQuality.HIGH: (2, "320k"),
        OutputQuality.BALANCED: (4, "192k"),
        OutputQuality.COMPACT: (7, "128k"),
    }

    def __init__(
        self,
        executable: str | None = None,
        *,
        mkvmerge_executable: str | None = None,
    ) -> None:
        self.executable = executable
        self.mkvmerge_executable = mkvmerge_executable

    def convert(
        self,
        source: Path,
        destination: Path,
        *,
        output_format: OutputFormat,
        quality: OutputQuality,
        duration_seconds: float | None = None,
        source_media: ProbedMedia | None = None,
        audio_track_index: int | None = None,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> Path:
        spec = output_spec(output_format)
        if spec.copies_source and source.suffix.casefold() in TRANSPORT_STREAM_SUFFIXES:
            if quality is not OutputQuality.SOURCE:
                raise MovieError("Le MKV utilise obligatoirement le profil source.")
            return _run_mkvmerge(
                self.mkvmerge_executable or find_mkvmerge(),
                source,
                destination,
                on_progress=on_progress,
            )
        if spec.copies_source:
            command = self._mkv_command(source, destination, quality)
        else:
            command = self._video_command(
                source,
                destination,
                output_format,
                quality,
                source_media,
                audio_track_index,
            )

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
        command = [
            self.executable or find_ffmpeg(),
            "-v",
            "error",
            "-nostdin",
        ]
        hint = input_format_hint(source.suffix)
        if hint is not None:
            command.extend(("-f", hint))
        command.extend(("-i", str(source)))
        return command

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

    def _video_command(
        self,
        source: Path,
        destination: Path,
        output_format: OutputFormat,
        quality: OutputQuality,
        source_media: ProbedMedia | None,
        audio_track_index: int | None,
    ) -> list[str]:
        spec = output_spec(output_format)
        if quality is OutputQuality.SOURCE:
            raise MovieError(
                f"Le {spec.label} nécessite un profil high, balanced ou compact."
            )
        command = self._base_command(source)
        subtitle_ordinals = tuple(
            ordinal
            for ordinal, stream in enumerate(
                stream
                for stream in (source_media.streams if source_media else ())
                if stream.kind == "subtitle"
            )
            if subtitle_codec_supported(output_format, stream.codec)
        )
        audio_map = "0:a?" if audio_track_index is None else f"0:a:{audio_track_index}"
        command.extend(
            (
                "-map",
                "0:V:0",
                "-map",
                audio_map,
                "-map_metadata",
                "0",
                "-map_chapters",
                "0" if spec.preserves_chapters else "-1",
            )
        )
        for ordinal in subtitle_ordinals:
            command.extend(("-map", f"0:s:{ordinal}"))
        if spec.encoding is EncodingFamily.H264:
            self._add_h264_options(
                command,
                output_format,
                quality,
                copy_video=copies_video_without_reencoding(
                    source_media,
                    output_format,
                ),
            )
        elif spec.encoding is EncodingFamily.VP9:
            crf, audio_bitrate = self._profile(self._VP9_PROFILES, quality, spec.label)
            command.extend(
                (
                    "-c:v:0",
                    "libvpx-vp9",
                    "-crf:v:0",
                    str(crf),
                    "-b:v:0",
                    "0",
                    "-row-mt:v:0",
                    "1",
                    "-pix_fmt:v:0",
                    "yuv420p",
                    "-c:a",
                    "libopus",
                    "-b:a",
                    audio_bitrate,
                )
            )
        elif spec.encoding in {
            EncodingFamily.MPEG4,
            EncodingFamily.MPEG2,
            EncodingFamily.WMV2,
            EncodingFamily.FLV1,
        }:
            self._add_legacy_options(command, spec.encoding, quality, spec.label)
        else:  # pragma: no cover - le catalogue est exhaustif et testé
            raise MovieError(f"Encodage non pris en charge : {spec.encoding}")
        if subtitle_ordinals:
            subtitle_codec = {
                SubtitleMode.COPY: "copy",
                SubtitleMode.MOV_TEXT: "mov_text",
                SubtitleMode.WEBVTT: "webvtt",
            }[spec.subtitle_mode]
            command.extend(("-c:s", subtitle_codec))
        if audio_track_index is not None:
            command.extend(("-disposition:a:0", "default"))
        return command

    def _add_h264_options(
        self,
        command: list[str],
        output_format: OutputFormat,
        quality: OutputQuality,
        *,
        copy_video: bool,
    ) -> None:
        spec = output_spec(output_format)
        crf, audio_bitrate = self._profile(
            self._VIDEO_PROFILES,
            quality,
            spec.label,
        )
        if copy_video:
            command.extend(("-c:v:0", "copy"))
        else:
            command.extend(
                (
                    "-c:v:0",
                    "libx264",
                    "-preset:v:0",
                    "medium",
                    "-crf:v:0",
                    str(crf),
                    "-pix_fmt:v:0",
                    "yuv420p",
                )
            )
        command.extend(("-c:a", "aac", "-b:a", audio_bitrate))
        if output_format in {OutputFormat.TS, OutputFormat.MTS}:
            command.extend(("-f", "mpegts"))
        else:
            command.extend(("-movflags", "+faststart"))

    def _add_legacy_options(
        self,
        command: list[str],
        encoding: EncodingFamily,
        quality: OutputQuality,
        label: str,
    ) -> None:
        video_quality, audio_bitrate = self._profile(
            self._LEGACY_PROFILES,
            quality,
            label,
        )
        video_codec, audio_codec = {
            EncodingFamily.MPEG4: ("mpeg4", "libmp3lame"),
            EncodingFamily.MPEG2: ("mpeg2video", "mp2"),
            EncodingFamily.WMV2: ("wmv2", "wmav2"),
            EncodingFamily.FLV1: ("flv", "libmp3lame"),
        }[encoding]
        command.extend(
            (
                "-c:v:0",
                video_codec,
                "-q:v:0",
                str(video_quality),
                "-pix_fmt:v:0",
                "yuv420p",
                "-c:a",
                audio_codec,
                "-b:a",
                audio_bitrate,
            )
        )

    @staticmethod
    def _profile(
        profiles: dict[OutputQuality, tuple[int, str]],
        quality: OutputQuality,
        label: str,
    ) -> tuple[int, str]:
        try:
            return profiles[quality]
        except KeyError as error:
            raise MovieError(
                f"Le {label} nécessite un profil high, balanced ou compact."
            ) from error


def _run_mkvmerge(
    executable: str,
    source: Path,
    destination: Path,
    *,
    on_progress: Callable[[ProgressUpdate], None] | None,
) -> Path:
    """Remuxe un transport stream en MKV même si certains PTS/DTS sont absents."""

    command = [
        executable,
        "--gui-mode",
        "--output",
        str(destination),
        str(source),
    ]
    try:
        result = (
            _run_mkvmerge_with_progress(command, on_progress)
            if on_progress is not None
            else subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
        )
    except OSError as error:
        raise MovieError("Impossible de lancer mkvmerge.") from error
    if result.returncode not in {0, 1}:
        details = (result.stderr or result.stdout).strip()
        suffix = f" : {details[-1_000:]}" if details else "."
        raise MovieError("mkvmerge n'a pas pu créer le MKV" + suffix)
    if not destination.is_file() or destination.stat().st_size == 0:
        raise MovieError("mkvmerge n'a pas produit de MKV exploitable.")
    if on_progress is not None:
        on_progress(ProgressUpdate("Conversion en MKV", None, 1.0, None))
    return destination


def _run_mkvmerge_with_progress(
    command: list[str],
    callback: Callable[[ProgressUpdate], None],
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    if process.stdout is None:
        process.kill()
        raise MovieError("mkvmerge n'a pas ouvert son flux de progression.")

    lines: list[str] = []
    try:
        for line in process.stdout:
            lines.append(line)
            fraction = _mkvmerge_progress_fraction(line)
            if fraction is not None:
                callback(ProgressUpdate("Conversion en MKV", None, fraction, None))
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise
    finally:
        process.stdout.close()
    return_code = process.wait()
    output = "".join(lines)
    return subprocess.CompletedProcess(command, return_code, output, output)


def _mkvmerge_progress_fraction(line: str) -> float | None:
    prefix = "#GUI#progress "
    value = line.strip()
    if not value.startswith(prefix) or not value.endswith("%"):
        return None
    try:
        return max(0.0, min(1.0, int(value[len(prefix) : -1]) / 100))
    except ValueError:
        return None

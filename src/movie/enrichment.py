"""Écriture sans réencodage des métadonnées d'un média final."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from movie.core.models import (
    MediaStream,
    MovieError,
    MovieMetadata,
    ProbedMedia,
    ProgressUpdate,
)
from movie.ffmpeg import (
    find_ffmpeg,
    metadata_arguments,
    prepare_artwork,
    run_ffmpeg,
)


class MediaTagger:
    """Remuxe un MKV, MP4 ou M4V en conservant ses flux utiles."""

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable

    def tag(
        self,
        source: Path,
        destination: Path,
        metadata: MovieMetadata,
        *,
        source_media: ProbedMedia,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> Path:
        artwork = prepare_artwork(metadata, destination.parent)
        suffix = source.suffix.casefold()
        if suffix == ".mkv":
            command = self._mkv_command(
                source,
                metadata,
                artwork,
                source_media,
            )
        elif suffix in {".mp4", ".m4v"}:
            command = self._mp4_command(source, metadata, artwork, source_media)
        else:  # pragma: no cover - le plan refuse le format avant l'exécution
            raise MovieError(
                "Les métadonnées sont prises en charge pour MKV, MP4 et M4V."
            )

        command.extend(("-progress", "pipe:1", "-nostats", "-y", str(destination)))
        return run_ffmpeg(
            command,
            action="écrire les métadonnées",
            expected_output=destination,
            duration_seconds=source_media.duration_seconds,
            on_progress=on_progress,
            progress_label="Écriture des métadonnées",
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
        metadata: MovieMetadata,
        artwork: tuple[Path, str] | None,
        source_media: ProbedMedia,
    ) -> list[str]:
        command = self._base_command(source)
        mapped_streams = self._map_source_streams(command, source_media, artwork)
        command.extend(("-map_metadata", "0", "-map_chapters", "0", "-c", "copy"))
        command.extend(metadata_arguments(metadata))
        if artwork is not None:
            poster, mime_type = artwork
            attachment_index = len(mapped_streams)
            command.extend(
                (
                    "-attach",
                    str(poster),
                    f"-metadata:s:{attachment_index}",
                    f"mimetype={mime_type}",
                    f"-metadata:s:{attachment_index}",
                    f"filename={poster.name}",
                    f"-metadata:s:{attachment_index}",
                    "title=Jaquette",
                )
            )
        return command

    def _mp4_command(
        self,
        source: Path,
        metadata: MovieMetadata,
        artwork: tuple[Path, str] | None,
        source_media: ProbedMedia,
    ) -> list[str]:
        command = self._base_command(source)
        if artwork is not None:
            poster, _mime_type = artwork
            command.extend(("-i", str(poster)))
        mapped_streams = self._map_source_streams(command, source_media, artwork)
        if artwork is not None:
            command.extend(("-map", "1:v:0"))
        command.extend(("-map_metadata", "0", "-map_chapters", "0", "-c", "copy"))
        if artwork is not None:
            video_count = sum(stream.kind == "video" for stream in mapped_streams)
            command.extend(
                (
                    f"-c:v:{video_count}",
                    "mjpeg",
                    f"-disposition:v:{video_count}",
                    "attached_pic",
                    f"-metadata:s:v:{video_count}",
                    "title=Jaquette",
                )
            )
        command.extend(metadata_arguments(metadata))
        command.extend(("-movflags", "+faststart"))
        return command

    @staticmethod
    def _map_source_streams(
        command: list[str],
        source_media: ProbedMedia,
        artwork: tuple[Path, str] | None,
    ) -> tuple[MediaStream, ...]:
        if artwork is None:
            command.extend(("-map", "0"))
            return source_media.streams
        mapped = tuple(
            stream for stream in source_media.streams if not stream.is_artwork
        )
        if any(stream.stream_id is None for stream in mapped):
            raise MovieError(
                "Impossible d'identifier toutes les pistes du fichier source."
            )
        for stream in mapped:
            command.extend(("-map", f"0:{stream.stream_id}"))
        return mapped

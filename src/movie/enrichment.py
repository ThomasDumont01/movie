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
    prepare_fanart,
    run_ffmpeg,
)
from movie.formats import taggable_format_names, taggable_suffixes
from movie.matroska import MatroskaEditor


class MediaTagger:
    """Remuxe un média compatible en conservant ses flux utiles."""

    def __init__(
        self,
        executable: str | None = None,
        *,
        matroska_editor: MatroskaEditor | None = None,
    ) -> None:
        self.executable = executable
        self.matroska_editor = matroska_editor or MatroskaEditor()

    def tag_in_place(
        self,
        source: Path,
        metadata: MovieMetadata,
        *,
        work_directory: Path,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> Path:
        """Modifie les en-têtes d'un MKV sans recopier ses pistes."""

        return self.matroska_editor.edit(
            source,
            metadata,
            work_directory=work_directory,
            on_progress=on_progress,
        )

    def tag(
        self,
        source: Path,
        destination: Path,
        metadata: MovieMetadata,
        *,
        source_media: ProbedMedia,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> Path:
        poster = prepare_artwork(metadata, destination.parent)
        suffix = source.suffix.casefold()
        if suffix == ".mkv":
            fanart = prepare_fanart(metadata, destination.parent)
            command = self._mkv_command(
                source,
                metadata,
                poster,
                fanart,
                source_media,
            )
        elif suffix in taggable_suffixes() - {".mkv"}:
            command = self._mp4_command(source, metadata, poster, source_media)
        else:  # pragma: no cover - le plan refuse le format avant l'exécution
            raise MovieError(
                "Les métadonnées et les jaquettes sont prises en charge pour "
                f"{taggable_format_names()}."
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
        poster: tuple[Path, str] | None,
        fanart: Path | None,
        source_media: ProbedMedia,
    ) -> list[str]:
        command = self._base_command(source)
        mapped_streams = self._map_source_streams(command, source_media, poster)
        command.extend(("-map_metadata", "0", "-map_chapters", "0", "-c", "copy"))
        attachment_index = len(mapped_streams)
        if poster is not None:
            poster_path, mime_type = poster
            self._add_mkv_attachment(
                command,
                poster_path,
                mime_type=mime_type,
                stream_index=attachment_index,
                title="Jaquette",
            )
            attachment_index += 1
        if fanart is not None:
            self._add_mkv_attachment(
                command,
                fanart,
                mime_type=_mime_type(fanart),
                stream_index=attachment_index,
                title="Arrière-plan",
            )
        command.extend(metadata_arguments(metadata))
        return command

    @staticmethod
    def _add_mkv_attachment(
        command: list[str],
        path: Path,
        *,
        mime_type: str,
        stream_index: int,
        title: str,
    ) -> None:
        command.extend(
            (
                "-attach",
                str(path),
                f"-metadata:s:{stream_index}",
                f"mimetype={mime_type}",
                f"-metadata:s:{stream_index}",
                f"filename={path.name}",
                f"-metadata:s:{stream_index}",
                f"title={title}",
            )
        )

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


def _mime_type(path: Path) -> str:
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }[path.suffix.casefold()]

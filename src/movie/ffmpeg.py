"""Infrastructure FFmpeg commune à l'enrichissement et à la conversion."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from movie import __version__
from movie.core.models import (
    MovieError,
    MovieMetadata,
    ProgressUpdate,
    ToolUnavailableError,
)

_MAX_ARTWORK_BYTES = 20_000_000
_USER_AGENT = f"Movie/{__version__} (+local personal media organizer)"


def find_ffmpeg() -> str:
    executable = shutil.which("ffmpeg")
    if not executable:
        raise ToolUnavailableError(
            "FFmpeg est introuvable. Installe-le avec « brew install ffmpeg »."
        )
    return executable


def run_ffmpeg(
    command: list[str],
    *,
    action: str,
    expected_output: Path,
    duration_seconds: float | None = None,
    on_progress: Callable[[ProgressUpdate], None] | None = None,
    progress_label: str,
) -> Path:
    """Exécute FFmpeg, relaie sa progression et contrôle le fichier produit."""

    result = (
        _run_with_progress(
            command,
            duration_seconds=duration_seconds,
            callback=on_progress,
            label=progress_label,
        )
        if on_progress is not None
        else _run_captured(command)
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        suffix = f" : {details[-1_000:]}" if details else "."
        raise MovieError(f"FFmpeg n'a pas pu {action}{suffix}")
    if not expected_output.is_file() or expected_output.stat().st_size == 0:
        raise MovieError(f"FFmpeg n'a pas produit de {action} exploitable.")
    return expected_output


def metadata_arguments(metadata: MovieMetadata) -> tuple[str, ...]:
    arguments = [
        "-metadata",
        f"title={metadata.title}",
        "-metadata",
        f"date={metadata.date_value or ''}",
        "-metadata",
        "year=",
        "-metadata",
        f"description={(metadata.summary or '')[:4000]}",
        "-metadata",
        f"synopsis={(metadata.summary or '')[:4000]}",
        "-metadata",
        f"genre={', '.join(metadata.genres)}",
        "-metadata",
        f"MOVIE_SOURCE={metadata.source_url or ''}",
        "-metadata",
        f"comment={f'TMDB: {metadata.source_url}' if metadata.source_url else ''}",
    ]
    return tuple(arguments)


def prepare_artwork(
    metadata: MovieMetadata,
    directory: Path,
) -> tuple[Path, str] | None:
    """Prépare une jaquette distante ou locale dans le dossier temporaire."""

    return _prepare_image(
        remote_url=metadata.poster_url,
        local_path=metadata.poster_path,
        directory=directory,
        filename_stem="cover",
        label="jaquette",
    )


def prepare_fanart(
    metadata: MovieMetadata,
    directory: Path,
) -> Path | None:
    """Prépare l'arrière-plan panoramique destiné à la pièce jointe MKV."""

    prepared = _prepare_image(
        remote_url=metadata.fanart_url,
        local_path=metadata.fanart_path,
        directory=directory,
        filename_stem="fanart",
        label="arrière-plan",
    )
    return prepared[0] if prepared is not None else None


def _prepare_image(
    *,
    remote_url: str | None,
    local_path: Path | None,
    directory: Path,
    filename_stem: str,
    label: str,
) -> tuple[Path, str] | None:
    if remote_url and local_path:
        raise MovieError(f"Choisis un {label} distant ou local, pas les deux.")
    if remote_url:
        return _download_image(
            remote_url,
            directory,
            filename_stem=filename_stem,
            label=label,
        )
    if local_path is None:
        return None

    source = local_path.expanduser().resolve()
    if not source.is_file():
        raise MovieError(f"L'illustration « {label} » est introuvable : {source}")
    try:
        if source.stat().st_size > _MAX_ARTWORK_BYTES:
            raise MovieError(f"L'illustration « {label} » est trop volumineuse.")
        mime_type, extension = _image_format(source.read_bytes())
        destination = directory / f"{filename_stem}{extension}"
        shutil.copyfile(source, destination)
    except MovieError:
        raise
    except OSError as error:
        raise MovieError(
            f"Impossible de préparer l'illustration « {label} »."
        ) from error
    return destination, mime_type


def download_poster(url: str, directory: Path) -> tuple[Path, str]:
    return _download_image(
        url,
        directory,
        filename_stem="cover",
        label="jaquette",
    )


def _download_image(
    url: str,
    directory: Path,
    *,
    filename_stem: str,
    label: str,
) -> tuple[Path, str]:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise MovieError(
            f"L'adresse de l'illustration « {label} » n'est pas une URL HTTPS valide."
        )
    request = Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urlopen(request, timeout=20) as response:
            final_url = urlsplit(response.geturl())
            if final_url.scheme != "https":
                raise MovieError(
                    f"L'illustration « {label} » a été redirigée vers une adresse "
                    "non sécurisée."
                )
            mime_type = response.headers.get_content_type()
            if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
                raise MovieError(
                    f"L'illustration « {label} » n'est pas une image prise en charge."
                )
            content = response.read(_MAX_ARTWORK_BYTES + 1)
    except MovieError:
        raise
    except OSError as error:
        raise MovieError(
            f"Impossible de télécharger l'illustration « {label} » : {error}"
        ) from error
    if len(content) > _MAX_ARTWORK_BYTES:
        raise MovieError(f"L'illustration « {label} » est trop volumineuse.")
    detected_mime, extension = _image_format(content)
    if detected_mime != mime_type:
        raise MovieError(
            f"Le contenu de l'illustration « {label} » ne correspond pas à son "
            "type d'image."
        )
    path = directory / f"{filename_stem}{extension}"
    try:
        path.write_bytes(content)
    except OSError as error:
        raise MovieError(
            f"Impossible d'écrire l'illustration temporaire « {label} »."
        ) from error
    return path, mime_type


def _image_format(content: bytes) -> tuple[str, str]:
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise MovieError("Le fichier n'est pas une image JPEG, PNG ou WebP valide.")


def _run_captured(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise MovieError("Impossible de lancer FFmpeg.") from error


def _run_with_progress(
    command: list[str],
    *,
    duration_seconds: float | None,
    callback: Callable[[ProgressUpdate], None],
    label: str,
) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as error:
        raise MovieError("Impossible de lancer FFmpeg.") from error
    if process.stdout is None:
        process.kill()
        raise MovieError("FFmpeg n'a pas ouvert son flux de progression.")

    lines: list[str] = []
    try:
        for line in process.stdout:
            lines.append(line)
            key, separator, raw_value = line.strip().partition("=")
            if not separator:
                continue
            fraction = _progress_fraction(key, raw_value, duration_seconds)
            if fraction is not None:
                callback(
                    ProgressUpdate(
                        current_label=label,
                        total_label=None,
                        current_fraction=max(0.0, min(1.0, fraction)),
                        total_fraction=None,
                    )
                )
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


def _progress_fraction(
    key: str,
    raw_value: str,
    duration_seconds: float | None,
) -> float | None:
    if key in {"out_time_us", "out_time_ms"} and duration_seconds:
        try:
            return int(raw_value) / (duration_seconds * 1_000_000)
        except ValueError:
            return None
    if key == "progress" and raw_value == "end":
        return 1.0
    return None

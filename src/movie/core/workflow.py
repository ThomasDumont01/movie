"""Garanties communes aux opérations longues qui produisent un fichier."""

from __future__ import annotations

import ctypes
import errno
import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from movie.core.models import MovieError, OutputExistsError, ProgressUpdate

_RENAME_EXCL = 0x00000004
_UNSUPPORTED_RENAME_ERRORS = {errno.EINVAL, errno.ENOSYS, errno.ENOTSUP}


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


def publish_without_overwrite(
    staged_file: Path,
    destination: Path,
    *,
    on_copy_progress: Callable[[float], None] | None = None,
) -> None:
    """Publie un fichier vérifié, même entre deux volumes différents."""

    if not _same_filesystem(staged_file, destination.parent):
        _copy_then_publish(
            staged_file,
            destination,
            on_progress=on_copy_progress,
        )
        return
    _publish_same_filesystem(staged_file, destination)


def replace_with_verified_file(
    staged_file: Path,
    destination: Path,
    *,
    on_copy_progress: Callable[[float], None] | None = None,
) -> None:
    """Remplace atomiquement une destination par un fichier déjà vérifié."""

    if _same_filesystem(staged_file, destination.parent):
        try:
            os.replace(staged_file, destination)
        except OSError as error:
            raise MovieError(
                "Impossible de remplacer le fichier d'origine en toute sécurité."
            ) from error
        return

    temporary = _copy_to_destination_volume(
        staged_file,
        destination,
        on_progress=on_copy_progress,
    )
    try:
        os.replace(temporary, destination)
    except OSError as error:
        raise MovieError(
            "Impossible de remplacer le fichier d'origine en toute sécurité."
        ) from error
    finally:
        _unlink_temporary(temporary)


def rename_without_overwrite(source: Path, destination: Path) -> None:
    """Renomme sur le même volume sans écraser un chemin apparu entre-temps."""

    if source == destination:
        return
    if not _same_filesystem(source, destination.parent):
        raise MovieError("Le renommage direct doit rester sur le même volume.")
    if sys.platform == "darwin":
        try:
            if _darwin_rename_without_overwrite(source, destination):
                return
        except FileExistsError as error:
            raise _destination_appeared(destination) from error
        except OSError as error:
            raise MovieError(
                "Impossible de renommer le fichier enrichi en toute sécurité."
            ) from error
    try:
        _replace_reserved_destination(source, destination)
    except FileExistsError as error:
        raise _destination_appeared(destination) from error
    except OSError as error:
        raise MovieError(
            "Impossible de renommer le fichier enrichi en toute sécurité."
        ) from error


def _publish_same_filesystem(staged_file: Path, destination: Path) -> None:
    if sys.platform == "darwin":
        try:
            if _darwin_rename_without_overwrite(staged_file, destination):
                return
        except FileExistsError as error:
            raise _destination_appeared(destination) from error
        except OSError as error:
            raise MovieError(
                "Publication sûre impossible ; aucun fichier final n'a été créé."
            ) from error

    try:
        # Le staging et la destination partagent le même volume : le lien dur
        # publie atomiquement et échoue si une autre opération a gagné la course.
        os.link(staged_file, destination)
    except FileExistsError as error:
        raise _destination_appeared(destination) from error
    except OSError:
        # Dernier recours pour les volumes réseau ne proposant ni lien dur ni
        # renommage exclusif. La destination est d'abord réservée avec O_EXCL,
        # puis remplacée atomiquement par le fichier vérifié.
        try:
            _replace_reserved_destination(staged_file, destination)
        except FileExistsError as error:
            raise _destination_appeared(destination) from error
        except OSError as error:
            raise MovieError(
                "Publication sûre impossible ; aucun fichier final n'a été créé."
            ) from error


def _same_filesystem(source: Path, destination_directory: Path) -> bool:
    try:
        return source.stat().st_dev == destination_directory.stat().st_dev
    except OSError as error:
        raise MovieError(
            "Impossible de comparer les volumes de travail et de destination."
        ) from error


def _copy_then_publish(
    source: Path,
    destination: Path,
    *,
    on_progress: Callable[[float], None] | None,
) -> None:
    """Copie vers le volume cible avant une publication atomique locale."""

    temporary = _copy_to_destination_volume(
        source,
        destination,
        on_progress=on_progress,
    )
    try:
        _publish_same_filesystem(temporary, destination)
    except MovieError:
        raise
    except OSError as error:
        raise MovieError(
            "La copie du fichier vérifié vers la destination a échoué ; "
            "aucun fichier final n'a été créé."
        ) from error
    finally:
        _unlink_temporary(temporary)


def _copy_to_destination_volume(
    source: Path,
    destination: Path,
    *,
    on_progress: Callable[[float], None] | None,
) -> Path:
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".movie-copy",
            dir=destination.parent,
        )
        temporary = Path(raw_path)
        source_size = source.stat().st_size
        copied = 0
        with (
            source.open("rb") as input_file,
            os.fdopen(descriptor, "wb") as output_file,
        ):
            descriptor = -1
            while chunk := input_file.read(8 * 1024 * 1024):
                output_file.write(chunk)
                copied += len(chunk)
                if on_progress is not None and source_size:
                    on_progress(min(1.0, copied / source_size))
            output_file.flush()
            os.fsync(output_file.fileno())
        if temporary.stat().st_size != source_size:
            raise MovieError(
                "La copie vers la destination est incomplète ; "
                "aucun fichier final n'a été créé."
            )
        return temporary
    except MovieError:
        if temporary is not None:
            _unlink_temporary(temporary)
        raise
    except OSError as error:
        if temporary is not None:
            _unlink_temporary(temporary)
        raise MovieError(
            "La copie du fichier vérifié vers la destination a échoué ; "
            "aucun fichier final n'a été créé."
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _unlink_temporary(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        # Un NAS peut retenir brièvement le fichier après une erreur. Le
        # nettoyage ne doit jamais masquer le diagnostic principal.
        pass


def _darwin_rename_without_overwrite(source: Path, destination: Path) -> bool:
    """Publie atomiquement sur macOS, y compris sur les partages SMB.

    ``os.link`` n'est pas pris en charge par certains NAS. ``renamex_np`` avec
    ``RENAME_EXCL`` conserve la même garantie : un chemin apparu entre la
    planification et la publication n'est jamais remplacé.
    """

    try:
        renamex = ctypes.CDLL(None, use_errno=True).renamex_np
    except AttributeError:
        return False
    renamex.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    renamex.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = renamex(
        os.fsencode(source),
        os.fsencode(destination),
        _RENAME_EXCL,
    )
    if result == 0:
        return True

    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(error_number, os.strerror(error_number), destination)
    if error_number in _UNSUPPORTED_RENAME_ERRORS:
        return False
    raise OSError(error_number, os.strerror(error_number), destination)


def _destination_appeared(destination: Path) -> OutputExistsError:
    return OutputExistsError(
        f"Un fichier est apparu pendant l'opération : {destination}. "
        "Rien n'a été écrasé."
    )


def _replace_reserved_destination(source: Path, destination: Path) -> None:
    descriptor = os.open(
        destination,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        source.stat().st_mode & 0o777,
    )
    reservation = os.fstat(descriptor)
    published = False
    try:
        current = destination.stat(follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (reservation.st_dev, reservation.st_ino):
            raise OSError("La réservation de la destination a été remplacée.")
        os.replace(source, destination)
        published = True
    finally:
        os.close(descriptor)
        if not published:
            try:
                current = destination.stat(follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (
                    reservation.st_dev,
                    reservation.st_ino,
                ):
                    destination.unlink()
            except FileNotFoundError:
                pass


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

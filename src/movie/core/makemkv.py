"""Adaptateur minimal et testable pour le protocole robot de MakeMKV."""

from __future__ import annotations

import csv
import shutil
import subprocess
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

from movie.core.models import (
    DiscError,
    DiscScan,
    DiscTitle,
    Drive,
    MediaStream,
    ProgressUpdate,
    ToolUnavailableError,
)

# Identifiants stables issus de l'API MakeMKV. Les autres attributs sont ignorés
# volontairement : ils varient selon le support et la version de MakeMKV.
_TITLE_NAME = 2
_TITLE_CHAPTERS = 8
_TITLE_DURATION = 9
_TITLE_SIZE_BYTES = 11
_TITLE_SOURCE_NAME = 16
_TITLE_OUTPUT_NAME = 27
_DISC_TYPE = 1
_STREAM_TYPE = 1
_STREAM_LANGUAGE = 3
_STREAM_LANGUAGE_NAME = 4
_VIDEO_STREAM_CODE = 6201
_AUDIO_STREAM_CODE = 6202
_SUBTITLE_STREAM_CODE = 6203

_SCAN_PROGRESS_PHASES = {
    "scanning cd-rom devices": (0.00, 0.03),
    "processing title sets": (0.03, 0.08),
    "scanning contents": (0.08, 0.82),
    "processing titles": (0.82, 0.94),
    "decrypting data": (0.94, 1.00),
}

_RIP_PROGRESS_PHASES = {
    "scanning cd-rom devices": (0.00, 0.02),
    "processing title sets": (0.02, 0.05),
    "scanning contents": (0.05, 0.25),
    "processing titles": (0.25, 0.28),
    "decrypting data": (0.28, 0.30),
    "analyzing seamless segments": (0.30, 0.32),
    "saving to mkv file": (0.32, 1.00),
    "saving title": (0.32, 1.00),
    "saving titles": (0.32, 1.00),
    "copying data": (0.32, 1.00),
}


class MakeMkvError(DiscError):
    """MakeMKV a refusé l'opération ou n'a pas produit de résultat fiable."""


@dataclass(frozen=True, slots=True)
class MakeMkvRun:
    """Trace compacte d'un appel MakeMKV terminé."""

    return_code: int
    output: str

    @property
    def diagnostics(self) -> tuple[str, ...]:
        """Retourne les avertissements utiles émis même après un succès."""

        return parse_robot_report(self.output).warnings


def find_makemkvcon() -> str:
    """Retourne le binaire MakeMKV sur macOS, ou une erreur explicite."""

    candidates = [
        shutil.which("makemkvcon"),
        "/Applications/MakeMKV.app/Contents/MacOS/makemkvcon",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate))

    raise ToolUnavailableError(
        "MakeMKV est introuvable. Installe l'application officielle : "
        "https://www.makemkv.com/download/"
    )


class MakeMkvClient:
    """Lance MakeMKV sans shell et interprète son mode robot."""

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or find_makemkvcon()

    def drives(self) -> tuple[Drive, ...]:
        """Liste les lecteurs signalés par MakeMKV, sans écrire de fichier."""

        result = self._run(("-r", "--cache=1", "info", "disc:9999"))
        report = parse_robot_report(result.output)
        if result.return_code != 0 and not report.drives:
            raise MakeMkvError(
                _failure_message("Impossible de lister les lecteurs", result)
            )
        # MakeMKV réserve parfois des entrées vides jusqu'à son nombre maximal de
        # lecteurs. Elles ne doivent pas apparaître comme de vrais lecteurs.
        return tuple(
            drive for drive in report.drives if drive.name or drive.device_path
        )

    def scan(
        self,
        drive_index: int,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> DiscScan:
        """Analyse les titres du disque vidéo indiqué, sans le modifier."""

        return self._scan_source(
            f"disc:{drive_index}",
            drive_index=drive_index,
            source_name="le disque",
            on_progress=on_progress,
        )

    def scan_iso(
        self,
        path: Path,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> DiscScan:
        """Analyse une image ISO locale comme une source MakeMKV."""

        source = path.expanduser().resolve()
        if not source.is_file():
            raise DiscError(f"L'image ISO est introuvable : {source}")
        if source.suffix.casefold() != ".iso":
            raise DiscError(f"La source n'est pas une image ISO : {source}")
        return self._scan_source(
            f"iso:{source}",
            drive_index=None,
            source_name="l'image ISO",
            iso_path=source,
            on_progress=on_progress,
        )

    def _scan_source(
        self,
        source: str,
        *,
        drive_index: int | None,
        source_name: str,
        iso_path: Path | None = None,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> DiscScan:
        result = self._run_streaming(
            (
                "-r",
                "--cache=128",
                "--minlength=0",
                "--messages=-stdout",
                "--progress=-same",
                "info",
                source,
            ),
            on_progress=on_progress,
            progress_profile="scan",
        )
        report = parse_robot_report(result.output)
        if result.return_code != 0:
            raise MakeMkvError(
                _failure_message(f"Impossible d'analyser {source_name}", result)
            )

        if drive_index is None:
            drive = Drive(
                index=-1,
                state=2,
                enabled=1,
                flags=0,
                name="Image ISO",
                disc_label=(
                    next(
                        (item.disc_label for item in report.drives if item.disc_label),
                        None,
                    )
                    or (iso_path.stem if iso_path is not None else None)
                ),
                device_path=str(iso_path) if iso_path is not None else None,
            )
        else:
            drive = next(
                (item for item in report.drives if item.index == drive_index), None
            )
            if drive is None:
                raise DiscError(
                    f"Le lecteur {drive_index} n'a pas été trouvé par MakeMKV."
                )
        if not report.titles:
            if any(
                "failed to open disc" in warning.casefold()
                for warning in report.warnings
            ):
                raise DiscError(
                    f"MakeMKV n'a pas pu ouvrir {source_name}. Vérifie que la source "
                    "est lisible et qu'aucune autre application ne l'utilise."
                )
            details = (
                f" Détail MakeMKV : {report.warnings[-1]}" if report.warnings else ""
            )
            raise DiscError(
                f"Aucun titre exploitable n'a été trouvé dans {source_name}." + details
            )
        return DiscScan(
            drive=drive,
            disc_type=report.disc_type,
            titles=report.titles,
            warnings=report.warnings,
        )

    def rip_title(
        self,
        drive_index: int,
        title_id: int,
        staging_directory: Path,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> MakeMkvRun:
        """Extrait un titre directement dans un répertoire temporaire.

        Le format MKV est produit par MakeMKV ; aucun encodeur n'est invoqué.
        """

        return self._rip_source_title(
            f"disc:{drive_index}",
            title_id,
            staging_directory,
            source_name="du disque",
            on_progress=on_progress,
        )

    def rip_iso_title(
        self,
        path: Path,
        title_id: int,
        staging_directory: Path,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> MakeMkvRun:
        """Extrait un titre d'une image ISO vers un MKV temporaire."""

        source = path.expanduser().resolve()
        if not source.is_file():
            raise DiscError(f"L'image ISO est introuvable : {source}")
        return self._rip_source_title(
            f"iso:{source}",
            title_id,
            staging_directory,
            source_name="de l'image ISO",
            on_progress=on_progress,
        )

    def _rip_source_title(
        self,
        source: str,
        title_id: int,
        staging_directory: Path,
        *,
        source_name: str,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> MakeMkvRun:
        result = self._run_streaming(
            (
                "-r",
                "--cache=512",
                "--minlength=0",
                "--messages=-stdout",
                "--progress=-same",
                "mkv",
                source,
                str(title_id),
                str(staging_directory),
            ),
            on_progress=on_progress,
            progress_profile="rip",
        )
        if result.return_code != 0:
            raise MakeMkvError(
                _failure_message(f"L'extraction {source_name} a échoué", result)
            )
        return result

    def _run_streaming(
        self,
        arguments: Sequence[str],
        *,
        on_progress: Callable[[ProgressUpdate], None] | None,
        progress_profile: str | None = None,
    ) -> MakeMkvRun:
        """Lance MakeMKV et transmet ses mises à jour pendant son exécution."""

        try:
            process = subprocess.Popen(
                [self.executable, *arguments],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as error:
            raise MakeMkvError("Impossible de lancer MakeMKV.") from error

        if (
            process.stdout is None
        ):  # Protection théorique pour le typage et les erreurs système.
            process.kill()
            raise MakeMkvError("MakeMKV n'a pas ouvert son flux de sortie.")

        progress = _ProgressParser(profile=progress_profile)
        lines: list[str] = []
        try:
            for line in process.stdout:
                lines.append(line)
                update = progress.consume(line)
                if update and on_progress:
                    on_progress(update)
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
        return MakeMkvRun(return_code=return_code, output=output)

    def _run(self, arguments: Sequence[str]) -> MakeMkvRun:
        try:
            result = subprocess.run(
                [self.executable, *arguments],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            raise MakeMkvError("Impossible de lancer MakeMKV.") from error
        return MakeMkvRun(
            return_code=result.returncode,
            output="\n".join(part for part in (result.stdout, result.stderr) if part),
        )


@dataclass(frozen=True, slots=True)
class _RobotReport:
    drives: tuple[Drive, ...]
    disc_type: str | None
    titles: tuple[DiscTitle, ...]
    warnings: tuple[str, ...]


def parse_robot_report(text: str) -> _RobotReport:
    """Parse les lignes MakeMKV avec CSV afin de préserver virgules et guillemets."""

    drives: list[Drive] = []
    cinfo: dict[int, str] = {}
    title_attributes: dict[int, dict[int, str]] = defaultdict(dict)
    title_streams: dict[int, dict[int, dict[int, tuple[int, str]]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    warnings: list[str] = []

    for raw_line in text.splitlines():
        prefix, separator, payload = raw_line.partition(":")
        if not separator:
            continue
        fields = _read_csv(payload)
        try:
            if prefix == "DRV":
                drive = _parse_drive(fields)
                if drive:
                    drives.append(drive)
            elif prefix == "CINFO" and len(fields) >= 3:
                cinfo[_to_int(fields[0])] = fields[-1]
            elif prefix == "TINFO" and len(fields) >= 4:
                title_id = _to_int(fields[0])
                attribute_id = _to_int(fields[1])
                title_attributes[title_id][attribute_id] = fields[-1]
            elif prefix == "SINFO" and len(fields) >= 5:
                title_id = _to_int(fields[0])
                stream_id = _to_int(fields[1])
                attribute_id = _to_int(fields[2])
                attribute_code = _to_int(fields[3])
                title_streams[title_id][stream_id][attribute_id] = (
                    attribute_code,
                    fields[-1],
                )
            elif prefix in {"TCOUNT", "TCOUT"}:
                continue
            elif prefix == "MSG" and len(fields) >= 4:
                _collect_warning(fields, warnings)
        except ValueError:
            warnings.append(f"Ligne MakeMKV illisible ignorée : {raw_line}")

    titles = tuple(
        _build_title(title_id, attributes, title_streams[title_id])
        for title_id, attributes in sorted(title_attributes.items())
    )
    return _RobotReport(
        drives=tuple(drives),
        disc_type=cinfo.get(_DISC_TYPE),
        titles=titles,
        warnings=tuple(warnings),
    )


def parse_duration(value: str) -> float | None:
    """Convertit les durées MakeMKV `H:MM:SS[.mmm]` en secondes."""

    try:
        hours, minutes, seconds = value.strip().split(":")
        return int(hours) * 3_600 + int(minutes) * 60 + float(seconds)
    except (TypeError, ValueError):
        return None


def _read_csv(payload: str) -> list[str]:
    return next(csv.reader(StringIO(payload), escapechar="\\"), [])


def _parse_drive(fields: list[str]) -> Drive | None:
    if len(fields) < 6:
        return None
    return Drive(
        index=_to_int(fields[0]),
        state=_to_int(fields[1]),
        enabled=_to_int(fields[2]),
        flags=_to_int(fields[3]),
        name=fields[4],
        disc_label=fields[5] or None,
        device_path=fields[6] if len(fields) > 6 and fields[6] else None,
    )


def _build_title(
    title_id: int,
    attributes: dict[int, str],
    streams: dict[int, dict[int, tuple[int, str]]],
) -> DiscTitle:
    output_name = attributes.get(_TITLE_OUTPUT_NAME)
    return DiscTitle(
        title_id=title_id,
        name=attributes.get(_TITLE_NAME) or output_name or f"Titre {title_id}",
        duration_seconds=parse_duration(attributes.get(_TITLE_DURATION, "")),
        chapter_count=_optional_int(attributes.get(_TITLE_CHAPTERS)),
        size_bytes=_optional_int(attributes.get(_TITLE_SIZE_BYTES)),
        source_name=attributes.get(_TITLE_SOURCE_NAME),
        output_name=output_name,
        stream_count=len(streams),
        video_languages=_video_languages(streams.values()),
        streams=_build_streams(streams),
    )


def _build_streams(
    streams: dict[int, dict[int, tuple[int, str]]],
) -> tuple[MediaStream, ...]:
    result: list[MediaStream] = []
    for stream_id, attributes in sorted(streams.items()):
        stream_type = attributes.get(_STREAM_TYPE)
        if not stream_type:
            continue
        code, name = stream_type
        kind = {
            _VIDEO_STREAM_CODE: "video",
            _AUDIO_STREAM_CODE: "audio",
            _SUBTITLE_STREAM_CODE: "subtitle",
        }.get(code, name.casefold().strip())
        language_attribute = attributes.get(_STREAM_LANGUAGE) or attributes.get(
            _STREAM_LANGUAGE_NAME
        )
        language = (
            _normalize_language(language_attribute[1]) if language_attribute else None
        )
        result.append(
            MediaStream(
                kind=kind,
                language=language,
                stream_id=stream_id,
            )
        )
    return tuple(result)


def _video_languages(streams: Iterable[dict[int, tuple[int, str]]]) -> tuple[str, ...]:
    languages: set[str] = set()
    for attributes in streams:
        stream_type = attributes.get(_STREAM_TYPE)
        if not stream_type:
            continue
        type_code, type_name = stream_type
        if type_code != _VIDEO_STREAM_CODE and type_name.casefold() != "video":
            continue
        language = attributes.get(_STREAM_LANGUAGE) or attributes.get(
            _STREAM_LANGUAGE_NAME
        )
        if language:
            normalized = _normalize_language(language[1])
            if normalized:
                languages.add(normalized)
    return tuple(sorted(languages))


def _normalize_language(value: str) -> str | None:
    language = value.casefold().strip()
    if language in {"", "und", "nolang"}:
        return None
    return language


def _collect_warning(fields: list[str], warnings: list[str]) -> None:
    # Les codes MakeMKV 5xxx ne signifient pas tous une erreur : le message
    # « Operation successfully completed » en fait partie. Les erreurs fatales
    # sont déjà traitées via le code de retour du processus ; ici, on n'affiche
    # que les diagnostics explicitement négatifs.
    message = fields[3].strip()
    normalized = message.casefold()
    markers = (
        "warning",
        "failed",
        "error",
        "skipped",
        "unable",
        "cannot",
        "corrupt",
        "damaged",
        "invalid",
        "read error",
        "av sync",
        "synchronization",
        "removed",
        "empty",
        "not found",
        "not available",
        "recommended",
        "avertissement",
        "échoué",
        "erreur",
        "ignoré",
        "impossible",
        "corromp",
        "endommag",
        "invalide",
        "synchronisation",
        "recommand",
    )
    if (
        any(marker in normalized for marker in markers)
        and message
        and message not in warnings
    ):
        warnings.append(message)


def _to_int(value: str) -> int:
    return int(value)


def _optional_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


class _ProgressParser:
    """Maintient les deux barres MakeMKV en un état unique."""

    def __init__(self, *, profile: str | None = None) -> None:
        self.current_label: str | None = None
        self.total_label: str | None = None
        self._phases = (
            _SCAN_PROGRESS_PHASES
            if profile == "scan"
            else _RIP_PROGRESS_PHASES
            if profile == "rip"
            else None
        )
        self._last_overall_fraction = 0.0

    def consume(self, raw_line: str) -> ProgressUpdate | None:
        prefix, separator, payload = raw_line.rstrip("\n").partition(":")
        if not separator:
            return None
        fields = _read_csv(payload)
        if prefix == "PRGC" and len(fields) >= 3:
            self.current_label = fields[-1]
            return None
        if prefix == "PRGT" and len(fields) >= 3:
            self.total_label = fields[-1]
            return None
        if prefix != "PRGV" or len(fields) < 3:
            return None
        try:
            current, total, maximum = (int(value) for value in fields[:3])
        except ValueError:
            return None
        if maximum <= 0:
            return None
        current_fraction = _fraction(current, maximum)
        total_fraction = _fraction(total, maximum)
        if self._phases is not None:
            phase = self._phases.get((self.current_label or "").casefold().strip())
            if phase is not None:
                start, end = phase
                mapped = start + (end - start) * current_fraction
                self._last_overall_fraction = max(
                    self._last_overall_fraction,
                    mapped,
                )
            total_fraction = self._last_overall_fraction
        return ProgressUpdate(
            current_label=self.current_label,
            total_label=self.total_label,
            current_fraction=current_fraction,
            total_fraction=total_fraction,
        )


def _fraction(value: int, maximum: int) -> float:
    return max(0.0, min(1.0, value / maximum))


def _failure_message(prefix: str, result: MakeMkvRun) -> str:
    details = result.output.strip()
    if not details:
        return f"{prefix} (code MakeMKV {result.return_code})."
    return f"{prefix} (code MakeMKV {result.return_code}) : {details[-2_000:]}"

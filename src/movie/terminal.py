"""Composants d'affichage et de saisie de l'interface en terminal."""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from time import monotonic
from typing import TextIO

from movie.core.models import (
    DiscTitle,
    Drive,
    MovieError,
    ProgressUpdate,
)


def _print_header(title: str) -> None:
    heading = f"Movie · {title}"
    print(heading)
    print("─" * len(heading))


def _print_subheading(title: str) -> None:
    print(f"\n{title}")


def _print_step(number: int, total: int, title: str) -> None:
    print(f"\n[{number}/{total}] {title}")


def _print_drive(drive: Drive) -> None:
    print(f"  Lecteur : [{drive.index}] {drive.name or 'lecteur sans nom'}")
    print(f"  Disque  : {drive.disc_label or 'aucun disque détecté'}")
    if drive.device_path:
        print(f"  Accès   : {drive.device_path}")


def _print_selected_drive(drives: Sequence[Drive], selected_index: int) -> None:
    drive = next((item for item in drives if item.index == selected_index), None)
    if drive is None:
        return
    print(
        f"Lecteur : [{drive.index}] {drive.name or 'lecteur sans nom'}"
        f" · {drive.disc_label or 'aucun disque détecté'}"
    )


def _prompt_for_title(candidates: Sequence[DiscTitle]) -> int:
    """Demande un titre tant que l'utilisateur n'a pas saisi un choix valide."""

    print("Plusieurs titres principaux sont possibles :")
    for title in candidates:
        print(f"  {_format_title(title)}")

    valid_ids = {title.title_id for title in candidates}
    choices = ", ".join(str(title_id) for title_id in sorted(valid_ids))
    while True:
        try:
            answer = input(f"Choisis un titre ({choices}) : ").strip()
        except (EOFError, KeyboardInterrupt) as error:
            raise MovieError("Choix du titre annulé.") from error
        try:
            selected = int(answer)
        except ValueError:
            selected = -1
        if selected in valid_ids:
            return selected
        print(f"Choix invalide. Saisis l'un des numéros suivants : {choices}.")


def _prompt_for_drive(drives: Sequence[Drive]) -> int:
    """Affiche les lecteurs disponibles et demande lequel utiliser."""

    usable = tuple(drive for drive in drives if drive.name or drive.device_path)
    if not usable:
        raise MovieError("Aucun lecteur optique n'est disponible.")
    if len(usable) == 1:
        return next(iter(usable)).index

    print("\nLecteurs disponibles :")
    for drive in usable:
        disc = drive.disc_label or "aucun disque détecté"
        print(f"  [{drive.index}] {drive.name} · {disc}")
    valid_ids = {drive.index for drive in usable}
    choices = ", ".join(str(index) for index in sorted(valid_ids))
    while True:
        selected = _prompt_int(f"Choisis un lecteur ({choices})")
        if selected in valid_ids:
            return selected
        print(f"Choix invalide. Saisis l'un des numéros suivants : {choices}.")


def _prompt_path(label: str, *, default: str | None = None) -> Path:
    prompt = f"{label}{f' [{default}]' if default else ''} : "
    while True:
        answer = _read_answer(prompt)
        value = answer or default
        if value:
            return _path_from_input(value)
        print("Un chemin est nécessaire.")


def _prompt_existing_file(label: str) -> Path:
    """Demande un fichier existant et répète la question en cas d'erreur."""

    while True:
        path = _prompt_path(label)
        if path.is_file():
            return path
        print(f"Fichier introuvable : {path}")


def _prompt_optional_path(label: str, *, default: str | None = None) -> Path | None:
    shown_default = default or "non configuré"
    answer = _read_answer(f"{label} [{shown_default}] (- pour effacer) : ")
    if not answer:
        return _path_from_input(default) if default else None
    if answer == "-":
        return None
    return _path_from_input(answer)


def _prompt_optional_existing_file(label: str) -> Path | None:
    """Demande éventuellement un fichier local existant."""

    while True:
        answer = _read_answer(f"{label} [aucun] : ")
        if not answer:
            return None
        path = _path_from_input(answer).resolve()
        if path.is_file():
            return path
        print(f"Fichier introuvable : {path}")


def _prompt_required_text(label: str, *, default: str | None = None) -> str:
    """Demande un texte non vide, avec une valeur proposée facultative."""

    prompt = f"{label}{f' [{default}]' if default else ''} : "
    while True:
        answer = _read_answer(prompt)
        value = " ".join((answer or default or "").split())
        if value:
            return value
        print("Une valeur est nécessaire.")


def _prompt_optional_text(label: str) -> str | None:
    answer = _read_answer(f"{label} [facultatif] : ")
    return answer or None


def _prompt_optional_year(label: str = "Année") -> int | None:
    while True:
        answer = _read_answer(f"{label} [facultatif] : ")
        if not answer:
            return None
        try:
            year = int(answer)
        except ValueError:
            print("Saisis une année entière, ou laisse vide.")
            continue
        if 1 <= year <= 9999:
            return year
        print("L'année doit être comprise entre 1 et 9999.")


def _prompt_genres() -> tuple[str, ...]:
    answer = _read_answer("Genres séparés par des virgules [facultatif] : ")
    return tuple(
        dict.fromkeys(part.strip() for part in answer.split(",") if part.strip())
    )


def _prompt_yes_no(label: str, *, default: bool = False) -> bool:
    suffix = "[O/n]" if default else "[o/N]"
    while True:
        answer = _read_answer(f"{label} {suffix} : ").casefold()
        if not answer:
            return default
        if answer in {"o", "oui", "y", "yes"}:
            return True
        if answer in {"n", "non", "no"}:
            return False
        print("Réponds par oui ou non.")


def _prompt_choice(
    label: str,
    *,
    choices: dict[str, str],
    default: str,
) -> str:
    rendered_choices = " / ".join(
        f"{value} ({description})" for value, description in choices.items()
    )
    while True:
        answer = _read_answer(
            f"{label} [{default}] — {rendered_choices} : "
        ).casefold()
        selected = answer or default
        if selected in choices:
            return selected
        print(f"Choix invalide. Valeurs acceptées : {', '.join(choices)}.")


def _prompt_int(label: str) -> int:
    while True:
        answer = _read_answer(f"{label} : ")
        try:
            return int(answer)
        except ValueError:
            print("Saisis un nombre entier.")


def _prompt_optional_int(label: str, *, default: int | None = None) -> int | None:
    shown_default = str(default) if default is not None else "automatique"
    while True:
        answer = _read_answer(f"{label} [{shown_default}] (- pour effacer) : ")
        if not answer:
            return default
        if answer == "-":
            return None
        try:
            value = int(answer)
        except ValueError:
            print(
                "Saisis un nombre entier, ou - pour utiliser la détection automatique."
            )
            continue
        if value >= 0:
            return value
        print("Le numéro du lecteur ne peut pas être négatif.")


def _prompt_float(
    label: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    while True:
        answer = _read_answer(f"{label} [{default:g}] : ")
        if not answer:
            return default
        try:
            value = float(answer.replace(",", "."))
        except ValueError:
            print("Saisis un nombre.")
            continue
        if minimum <= value <= maximum:
            return value
        print(f"La valeur doit être comprise entre {minimum:g} et {maximum:g}.")


def _read_answer(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt) as error:
        raise MovieError("Saisie annulée.") from error


def _path_from_input(value: str) -> Path:
    """Accepte aussi les chemins glissés dans Terminal (guillemets ou antislash)."""

    normalized = value
    if "\\" in value or value[:1] in {'"', "'"}:
        try:
            parts = shlex.split(value)
        except ValueError:
            parts = []
        if len(parts) == 1:
            normalized = parts[0]
    return Path(normalized).expanduser()


def _alert_user(enabled: bool) -> None:
    """Joue discrètement le son système macOS avant une question bloquante."""

    if not enabled:
        return
    sound = Path("/System/Library/Sounds/Glass.aiff")
    if sys.platform == "darwin" and sound.is_file():
        try:
            subprocess.Popen(
                ["afplay", str(sound)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except OSError:
            pass
    sys.stdout.write("\a")
    sys.stdout.flush()


def _format_title(title: DiscTitle) -> str:
    duration_text = (
        _format_duration(title.duration_seconds)
        if title.duration_seconds is not None
        else "durée inconnue"
    )
    return (
        f"[{title.title_id}] {title.name} — {duration_text} — "
        f"{title.chapter_count if title.chapter_count is not None else '?'} chapitres — "
        f"{_stream_summary(title)}"
        f"{_video_languages_label(title)}"
    )


def _stream_summary(title: DiscTitle) -> str:
    if not title.streams:
        return f"{title.stream_count} pistes"
    counts: dict[str, int] = {}
    for stream in title.streams:
        counts[stream.kind] = counts.get(stream.kind, 0) + 1
    labels = {
        "video": "vidéo",
        "audio": "audio",
        "subtitle": "sous-titre",
    }
    return ", ".join(
        f"{count} {labels.get(kind, kind)}{'s' if count > 1 else ''}"
        for kind, count in counts.items()
    )


def _video_languages_label(title: DiscTitle) -> str:
    if not title.video_languages:
        return ""
    return f" — vidéo : {', '.join(title.video_languages)}"


def _format_duration(seconds: float) -> str:
    rounded = round(seconds)
    hours, remainder = divmod(rounded, 3_600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02}:{seconds:02}"


def _format_file_size(size_bytes: int) -> str:
    amount = float(size_bytes)
    for unit in ("octets", "Ko", "Mo", "Go", "To"):
        if amount < 1024 or unit == "To":
            return f"{int(amount)} {unit}" if unit == "octets" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{size_bytes} octets"


def _media_summary(stream_types: Sequence[str]) -> str:
    counts = {
        kind: stream_types.count(kind)
        for kind in ("video", "audio", "subtitle", "attachment")
        if stream_types.count(kind)
    }
    labels = {
        "video": "vidéo",
        "audio": "audio",
        "subtitle": "sous-titre",
        "attachment": "jaquette",
    }
    return " · ".join(
        f"{count} {labels[kind]}{'s' if count > 1 else ''}"
        for kind, count in counts.items()
    ) or "aucune piste utile"


class _ProgressRenderer:
    """Maintient une seule barre monotone pendant toute une opération longue."""

    def __init__(
        self,
        *,
        operation_label: str = "Opération",
        threshold_seconds: float = 12,
        refresh_interval_seconds: float = 0.1,
        clock: Callable[[], float] = monotonic,
        stream: TextIO | None = None,
    ) -> None:
        self._clock = clock
        self._stream = stream or sys.stdout
        self._operation_label = operation_label
        self._threshold_seconds = threshold_seconds
        self._refresh_interval_seconds = refresh_interval_seconds
        self._started_at: float | None = None
        self._paused_at: float | None = None
        self._paused_seconds = 0.0
        self._active_label: str | None = None
        self._last_fraction = 0.0
        self._bar_is_visible = False
        self._is_finished = False
        self._last_rendered_at: float | None = None
        self._last_rendered_fraction: float | None = None
        self._last_rendered_label: str | None = None
        self._estimated_remaining_seconds: float | None = None
        self._progress_samples: list[tuple[float, float]] = []
        self._bar_width = max(
            12,
            min(30, (shutil.get_terminal_size(fallback=(80, 24)).columns - 45)),
        )
        self._clear_line = (
            "\x1b[K" if getattr(self._stream, "isatty", lambda: False)() else ""
        )
        self._lock = threading.RLock()
        self._timer: threading.Timer | None = None
        self._uses_real_timer = getattr(self._stream, "isatty", lambda: False)()

    def start(self, initial_label: str) -> None:
        with self._lock:
            if self._started_at is not None:
                return
            self._started_at = self._clock()
            self._active_label = initial_label
            self._arm_timer(initial=True)

    def __call__(self, update: ProgressUpdate) -> None:
        with self._lock:
            if self._is_finished:
                return
            fraction = update.total_fraction
            if fraction is None:
                fraction = update.current_fraction
            if fraction is None:
                return

            label = _progress_label(
                update.current_label or update.total_label or "Traitement"
            )
            if self._started_at is None:
                self.start(label)
            else:
                self._active_label = label
            self._last_fraction = max(
                self._last_fraction,
                max(0.0, min(1.0, fraction)),
            )
            assert self._started_at is not None
            now = self._clock()
            can_refresh = (
                self._last_rendered_at is None
                or now - self._last_rendered_at >= self._refresh_interval_seconds
            )
            has_changed = (
                self._last_rendered_fraction is None
                or abs(self._last_fraction - self._last_rendered_fraction) >= 0.001
                or label != self._last_rendered_label
            )
            if (
                self._elapsed(now) >= self._threshold_seconds
                and can_refresh
                and has_changed
            ):
                self._write_bar(self._last_fraction, label, now=now)
                self._last_rendered_at = now

    def finish(self) -> None:
        with self._lock:
            if self._is_finished:
                return
            self._cancel_timer()
            now = self._clock()
            elapsed = self._elapsed(now)
            summary = (
                f"{self._operation_label} terminée en "
                f"{_format_timespan(elapsed)}"
            )
            if self._bar_is_visible:
                self._write_bar(1.0, summary, now=now, show_eta=False)
                self._stream.write("\n")
            else:
                self._stream.write(f"✓ {summary}\n")
            self._stream.flush()
            self._is_finished = True

    def pause(self) -> None:
        """Libère proprement la ligne avant une question interactive."""

        with self._lock:
            if self._is_finished or self._paused_at is not None:
                return
            self._cancel_timer()
            self._paused_at = self._clock()
            if self._bar_is_visible:
                self._stream.write("\n")
                self._stream.flush()

    def resume(self, label: str | None = None) -> None:
        """Reprend le chronométrage sans compter le temps de réflexion humaine."""

        with self._lock:
            if self._is_finished or self._paused_at is None:
                return
            self._paused_seconds += max(0.0, self._clock() - self._paused_at)
            self._paused_at = None
            if label:
                self._active_label = label
            self._arm_timer()

    def cancel(self) -> None:
        with self._lock:
            self._cancel_timer()
            if self._bar_is_visible:
                self._stream.write("\n")
                self._stream.flush()
            self._is_finished = True

    def _arm_timer(self, *, initial: bool = False) -> None:
        self._cancel_timer()
        if not self._uses_real_timer:
            return
        elapsed = self._elapsed(self._clock())
        remaining_delay = (
            self._threshold_seconds
            if initial
            else max(0.0, self._threshold_seconds - elapsed)
        )
        if remaining_delay <= 0:
            self._show_delayed_bar()
            return
        self._timer = threading.Timer(
            remaining_delay,
            self._show_delayed_bar,
        )
        self._timer.daemon = True
        self._timer.start()

    def _show_delayed_bar(self) -> None:
        with self._lock:
            if (
                self._active_label is None
                or self._is_finished
                or self._paused_at is not None
            ):
                return
            now = self._clock()
            self._write_bar(self._last_fraction, self._active_label, now=now)
            self._last_rendered_at = now

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _write_bar(
        self,
        fraction: float,
        label: str,
        *,
        now: float,
        show_eta: bool = True,
    ) -> None:
        percentage = fraction * 100
        terminal_width = shutil.get_terminal_size(fallback=(80, 24)).columns
        eta = self._remaining_time(fraction, now) if show_eta else None
        eta_suffix = f" · ~{_format_eta(eta)}" if eta is not None else ""
        desired_status_width = len(label) + len(eta_suffix)
        bar_width = min(
            self._bar_width,
            max(12, terminal_width - 14 - desired_status_width),
        )
        bar = _progress_bar(fraction, width=bar_width)
        reserved = bar_width + 14
        label_width = max(12, terminal_width - reserved)
        label_space = max(1, label_width - len(eta_suffix))
        status = f"{label[:label_space]}{eta_suffix}"[:label_width]
        self._stream.write(
            f"\r[{bar}] {percentage:5.1f} % — {status}{self._clear_line}"
        )
        self._stream.flush()
        self._bar_is_visible = True
        self._last_rendered_fraction = fraction
        self._last_rendered_label = label

    def _elapsed(self, now: float) -> float:
        if self._started_at is None:
            return 0.0
        paused = self._paused_seconds
        if self._paused_at is not None:
            paused += max(0.0, now - self._paused_at)
        return max(0.0, now - self._started_at - paused)

    def _remaining_time(self, fraction: float, now: float) -> float | None:
        elapsed = self._elapsed(now)
        if elapsed < 10 or not 0.05 <= fraction < 0.99:
            return None
        self._progress_samples.append((elapsed, fraction))
        self._progress_samples = [
            sample
            for sample in self._progress_samples[-30:]
            if elapsed - sample[0] <= 30
        ]
        rates = [
            (current_fraction - previous_fraction) / (current_time - previous_time)
            for (previous_time, previous_fraction), (current_time, current_fraction)
            in zip(self._progress_samples, self._progress_samples[1:])
            if current_time > previous_time
            and current_fraction > previous_fraction
        ]
        if len(rates) < 3:
            return None
        ordered = sorted(rates[-15:])
        rate = ordered[len(ordered) // 2]
        estimate = (1.0 - fraction) / rate
        if self._estimated_remaining_seconds is None:
            self._estimated_remaining_seconds = estimate
        else:
            self._estimated_remaining_seconds = (
                self._estimated_remaining_seconds * 0.7 + estimate * 0.3
            )
        return (
            self._estimated_remaining_seconds
            if self._estimated_remaining_seconds >= 1
            else None
        )


def _progress_bar(fraction: float, *, width: int = 20) -> str:
    filled = round(max(0.0, min(1.0, fraction)) * width)
    return "█" * filled + "░" * (width - filled)


def _format_timespan(seconds: float) -> str:
    rounded = max(0, round(seconds))
    if rounded < 1:
        return "moins de 1 s"
    hours, remainder = divmod(rounded, 3_600)
    minutes, remaining_seconds = divmod(remainder, 60)
    if hours:
        return f"{hours} h {minutes:02} min"
    if minutes:
        return f"{minutes} min {remaining_seconds:02} s"
    return f"{remaining_seconds} s"


def _format_eta(seconds: float) -> str:
    """Formate une estimation compacte afin de préserver le libellé d'étape."""

    rounded = max(1, round(seconds))
    if rounded < 60:
        return f"{rounded} s"
    total_minutes = max(1, round(rounded / 60))
    hours, minutes = divmod(total_minutes, 60)
    if hours:
        return f"{hours} h" if minutes == 0 else f"{hours} h {minutes:02} min"
    return f"{minutes} min"


def _progress_label(label: str) -> str:
    translations = {
        "scanning cd-rom devices": "Ouverture de la source",
        "processing title sets": "Ouverture de la source",
        "scanning contents": "Lecture du contenu",
        "processing titles": "Identification des titres",
        "decrypting data": "Finalisation de l'analyse",
        "saving title": "Copie du média",
        "saving titles": "Copie du média",
        "copying data": "Copie du média",
        "analyzing seamless segments": "Préparation des pistes",
        "saving to mkv file": "Copie du média",
    }
    normalized = label.casefold().strip()
    return translations.get(normalized, label.strip())

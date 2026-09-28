"""Configuration utilisateur persistante de Movie."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from movie.core.models import MovieError, OutputFormat, OutputQuality


@dataclass(frozen=True, slots=True)
class MovieConfig:
    """Valeurs par défaut utilisées par l'interface en ligne de commande."""

    output_directory: Path | None = None
    drive_index: int | None = None
    auto_run: bool = False
    alert_sound: bool = True
    progress_delay_seconds: float = 12.0
    open_browser: bool = True
    convert_format: OutputFormat = OutputFormat.MP4
    convert_quality: OutputQuality = OutputQuality.BALANCED


def config_path() -> Path:
    """Retourne le chemin de configuration, surchargeable pour les tests."""

    override = os.environ.get("MOVIE_CONFIG_FILE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "movie" / "config.json"


def load_config(path: Path | None = None) -> MovieConfig:
    """Charge la configuration ou fournit des valeurs vides si elle n'existe pas."""

    source = path or config_path()
    if not source.exists():
        return MovieConfig()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MovieError(f"Configuration illisible : {source}") from error
    if not isinstance(payload, dict):
        raise MovieError(f"La configuration doit contenir un objet JSON : {source}")

    raw_output = payload.get("output_directory")
    if raw_output is not None and not isinstance(raw_output, str):
        raise MovieError("Le paramètre output_directory doit être un chemin textuel.")
    output = Path(raw_output).expanduser() if raw_output else None
    drive_index = payload.get("drive_index")
    if drive_index is not None and (
        not isinstance(drive_index, int) or isinstance(drive_index, bool) or drive_index < 0
    ):
        raise MovieError("Le paramètre drive_index doit être un entier positif.")
    auto_run = _boolean_setting(payload, "auto_run", False)
    alert_sound = _boolean_setting(payload, "alert_sound", True)
    open_browser = _boolean_setting(payload, "open_browser", True)
    migrated_format = payload.get(
        "convert_format",
        payload.get("output_format", OutputFormat.MP4.value),
    )
    conversion_payload: dict[object, object] = {
        "convert_format": migrated_format
    }
    convert_format = _enum_setting(
        conversion_payload,
        "convert_format",
        OutputFormat,
        OutputFormat.MP4,
    )
    default_quality = (
        OutputQuality.SOURCE
        if convert_format is OutputFormat.MKV
        else OutputQuality.BALANCED
    )
    migrated_quality = payload.get(
        "convert_quality",
        payload.get("output_quality", default_quality.value),
    )
    conversion_payload["convert_quality"] = migrated_quality
    convert_quality = _enum_setting(
        conversion_payload,
        "convert_quality",
        OutputQuality,
        default_quality,
    )
    _validate_conversion_settings(convert_format, convert_quality)
    progress_delay = payload.get("progress_delay_seconds", 12.0)
    if (
        not isinstance(progress_delay, (int, float))
        or isinstance(progress_delay, bool)
        or not 0 <= float(progress_delay) <= 60
    ):
        raise MovieError(
            "Le paramètre progress_delay_seconds doit être compris entre 0 et 60."
        )
    return MovieConfig(
        output_directory=output,
        drive_index=drive_index,
        auto_run=auto_run,
        alert_sound=alert_sound,
        progress_delay_seconds=float(progress_delay),
        open_browser=open_browser,
        convert_format=convert_format,
        convert_quality=convert_quality,
    )


def save_config(config: MovieConfig, path: Path | None = None) -> Path:
    """Enregistre la configuration par remplacement atomique."""

    _validate_conversion_settings(config.convert_format, config.convert_quality)
    destination = path or config_path()
    payload = {
        "output_directory": (
            str(config.output_directory.expanduser())
            if config.output_directory is not None
            else None
        ),
        "drive_index": config.drive_index,
        "auto_run": config.auto_run,
        "alert_sound": config.alert_sound,
        "progress_delay_seconds": config.progress_delay_seconds,
        "open_browser": config.open_browser,
        "convert_format": config.convert_format.value,
        "convert_quality": config.convert_quality.value,
    }
    temporary_path: Path | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            delete=False,
        ) as temporary:
            json.dump(payload, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary_path = Path(temporary.name)
        temporary_path.replace(destination)
    except OSError as error:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise MovieError(f"Impossible d'enregistrer la configuration : {destination}") from error
    return destination


def _boolean_setting(payload: dict[object, object], name: str, default: bool) -> bool:
    value = payload.get(name, default)
    if not isinstance(value, bool):
        raise MovieError(f"Le paramètre {name} doit être vrai ou faux.")
    return value


def _enum_setting[EnumType: StrEnum](
    payload: dict[object, object],
    name: str,
    enum_type: type[EnumType],
    default: EnumType,
) -> EnumType:
    value = payload.get(name, default)
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        raise MovieError(f"Le paramètre {name} doit être une valeur textuelle.")
    try:
        return enum_type(value)
    except ValueError as error:
        choices = ", ".join(item.value for item in enum_type)
        raise MovieError(
            f"Le paramètre {name} doit valoir l'une de ces valeurs : {choices}."
        ) from error


def _validate_conversion_settings(
    output_format: OutputFormat,
    output_quality: OutputQuality,
) -> None:
    allowed = {
        OutputFormat.MKV: {OutputQuality.SOURCE},
        OutputFormat.MP4: {
            OutputQuality.HIGH,
            OutputQuality.BALANCED,
            OutputQuality.COMPACT,
        },
        OutputFormat.M4V: {
            OutputQuality.HIGH,
            OutputQuality.BALANCED,
            OutputQuality.COMPACT,
        },
    }
    if output_quality not in allowed[output_format]:
        profiles = ", ".join(item.value for item in allowed[output_format])
        raise MovieError(
            f"Le format {output_format.value.upper()} accepte les profils : {profiles}."
        )

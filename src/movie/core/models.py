"""Modèles immuables des flux de numérisation et de conversion."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class MovieError(RuntimeError):
    """Erreur compréhensible pouvant être présentée à l'utilisateur."""


class ToolUnavailableError(MovieError):
    """Un outil externe obligatoire est absent."""


class DiscError(MovieError):
    """Le lecteur ou le disque ne permet pas de poursuivre le flux."""


class OutputExistsError(MovieError):
    """La destination existe déjà et ne doit pas être remplacée."""


class OutputFormat(StrEnum):
    """Conteneurs que Movie sait produire et vérifier."""

    MKV = "mkv"
    MP4 = "mp4"
    M4V = "m4v"
    MOV = "mov"
    WEBM = "webm"
    AVI = "avi"
    MPG = "mpg"
    ASF = "asf"
    WMV = "wmv"
    FLV = "flv"
    TS = "ts"
    MTS = "mts"


class OutputQuality(StrEnum):
    """Profils de traitement proposés à l'utilisateur."""

    SOURCE = "source"
    HIGH = "high"
    BALANCED = "balanced"
    COMPACT = "compact"


@dataclass(frozen=True, slots=True)
class Drive:
    """Lecteur retourné par MakeMKV."""

    index: int  # numéro d'index MakeMKV
    state: int  # état du lecteur
    enabled: int  # indique si le lecteur est activé
    flags: int  # indique si le lecteur est prêt
    name: str
    disc_label: str | None
    device_path: str | None

    @property
    def has_disc(self) -> bool:
        """MakeMKV est la source de vérité ; un libellé signifie un média chargé."""

        return bool(self.disc_label)


@dataclass(frozen=True, slots=True)
class MediaStream:
    """Piste audio, vidéo, sous-titre ou pièce jointe d'un média."""

    kind: str
    language: str | None = None
    codec: str | None = None
    stream_id: int | None = None
    is_artwork: bool = False


@dataclass(frozen=True, slots=True)
class DiscTitle:
    """Titre d'un disque vidéo tel qu'analysé par MakeMKV."""

    title_id: int
    name: str
    duration_seconds: float | None
    chapter_count: int | None
    size_bytes: int | None
    source_name: str | None
    output_name: str | None
    stream_count: int
    video_languages: tuple[str, ...] = ()
    streams: tuple[MediaStream, ...] = ()


@dataclass(frozen=True, slots=True)
class DiscScan:
    """Résultat normalisé de l'analyse d'un disque."""

    drive: Drive
    disc_type: str | None
    titles: tuple[DiscTitle, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProgressUpdate:
    """État de progression exploitable par le terminal puis l'interface."""

    current_label: str | None
    total_label: str | None
    current_fraction: float | None
    total_fraction: float | None


@dataclass(frozen=True, slots=True)
class MovieMetadata:
    """Informations confirmées en ligne ou saisies par l'utilisateur."""

    title: str
    year: int | None = None
    summary: str | None = None
    genres: tuple[str, ...] = ()
    source_url: str | None = None
    poster_url: str | None = None
    poster_path: Path | None = None

    @property
    def has_artwork(self) -> bool:
        """Indique si une jaquette distante ou locale doit être intégrée."""

        return self.poster_url is not None or self.poster_path is not None


@dataclass(frozen=True, slots=True)
class RipPlan:
    """Plan affiché avant toute écriture sur le disque."""

    drive_index: int
    title: DiscTitle
    output: Path


@dataclass(frozen=True, slots=True)
class ConvertPlan:
    """Conversion explicite d'une source locale vers un nouveau conteneur."""

    source: Path
    output: Path
    output_format: OutputFormat
    output_quality: OutputQuality
    iso_title: DiscTitle | None = None
    source_media: ProbedMedia | None = None


@dataclass(frozen=True, slots=True)
class TagPlan:
    """Enrichissement sûr d'un média existant, sans réencodage."""

    source: Path
    output: Path
    metadata: MovieMetadata


@dataclass(frozen=True, slots=True)
class ProbedMedia:
    """Informations lues dans le MKV temporaire avec ffprobe."""

    path: Path
    duration_seconds: float | None
    stream_types: tuple[str, ...]
    chapter_count: int
    format_tags: tuple[tuple[str, str], ...] = ()
    streams: tuple[MediaStream, ...] = ()


@dataclass(frozen=True, slots=True)
class RipResult:
    """Résultat publié après une vérification réussie."""

    output: Path
    media: ProbedMedia
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConvertResult:
    """Résultat publié après une conversion vérifiée."""

    output: Path
    media: ProbedMedia
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TagResult:
    """Résultat d'un enrichissement publié à la place du média source."""

    output: Path
    media: ProbedMedia
    warnings: tuple[str, ...] = ()

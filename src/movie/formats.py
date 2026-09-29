"""Catalogue central des sorties vidéo fiables proposées par Movie."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from movie.core.models import OutputFormat, OutputQuality


class EncodingFamily(StrEnum):
    """Familles de commandes FFmpeg maintenues par le convertisseur."""

    COPY = "copy"
    H264 = "h264"
    VP9 = "vp9"
    MPEG4 = "mpeg4"
    MPEG2 = "mpeg2"
    WMV2 = "wmv2"
    FLV1 = "flv1"


@dataclass(frozen=True, slots=True)
class OutputFormatSpec:
    """Règles de création et de contrôle d'un conteneur de sortie."""

    label: str
    choice_description: str
    encoding: EncodingFamily
    video_codec: str | None
    audio_codec: str | None
    preserves_chapters: bool
    preserves_audio_languages: bool
    supports_tagging: bool = False

    @property
    def copies_source(self) -> bool:
        return self.encoding is EncodingFamily.COPY

    @property
    def codec_description(self) -> str:
        """Nom lisible de l'association de codecs produite."""

        return {
            EncodingFamily.COPY: "qualité source",
            EncodingFamily.H264: "H.264/AAC",
            EncodingFamily.VP9: "VP9/Opus",
            EncodingFamily.MPEG4: "MPEG-4/MP3",
            EncodingFamily.MPEG2: "MPEG-2/MP2",
            EncodingFamily.WMV2: "WMV2/WMA2",
            EncodingFamily.FLV1: "FLV/MP3",
        }[self.encoding]


TRANSCODE_QUALITIES = (
    OutputQuality.HIGH,
    OutputQuality.BALANCED,
    OutputQuality.COMPACT,
)


OUTPUT_FORMAT_SPECS: dict[OutputFormat, OutputFormatSpec] = {
    OutputFormat.MKV: OutputFormatSpec(
        "MKV",
        "archive fidèle : toutes les pistes, sans réencodage",
        EncodingFamily.COPY,
        None,
        None,
        True,
        True,
        supports_tagging=True,
    ),
    OutputFormat.MP4: OutputFormatSpec(
        "MP4",
        "universel : H.264/AAC",
        EncodingFamily.H264,
        "h264",
        "aac",
        True,
        True,
        supports_tagging=True,
    ),
    OutputFormat.M4V: OutputFormatSpec(
        "M4V",
        "écosystème Apple : H.264/AAC",
        EncodingFamily.H264,
        "h264",
        "aac",
        True,
        True,
        supports_tagging=True,
    ),
    OutputFormat.MOV: OutputFormatSpec(
        "MOV",
        "QuickTime et montage : H.264/AAC",
        EncodingFamily.H264,
        "h264",
        "aac",
        True,
        True,
    ),
    OutputFormat.WEBM: OutputFormatSpec(
        "WebM",
        "web moderne : VP9/Opus, encodage plus lent",
        EncodingFamily.VP9,
        "vp9",
        "opus",
        True,
        True,
    ),
    OutputFormat.AVI: OutputFormatSpec(
        "AVI",
        "anciens appareils : MPEG-4/MP3",
        EncodingFamily.MPEG4,
        "mpeg4",
        "mp3",
        False,
        False,
    ),
    OutputFormat.MPG: OutputFormatSpec(
        "MPEG",
        "anciens lecteurs : MPEG-2/MP2",
        EncodingFamily.MPEG2,
        "mpeg2video",
        "mp2",
        False,
        False,
    ),
    OutputFormat.ASF: OutputFormatSpec(
        "ASF",
        "ancien conteneur Windows : WMV2/WMA2",
        EncodingFamily.WMV2,
        "wmv2",
        "wmav2",
        False,
        False,
    ),
    OutputFormat.WMV: OutputFormatSpec(
        "WMV",
        "anciens logiciels Windows : WMV2/WMA2",
        EncodingFamily.WMV2,
        "wmv2",
        "wmav2",
        False,
        False,
    ),
    OutputFormat.FLV: OutputFormatSpec(
        "FLV",
        "anciens lecteurs Flash : FLV/MP3",
        EncodingFamily.FLV1,
        "flv1",
        "mp3",
        False,
        False,
    ),
    OutputFormat.TS: OutputFormatSpec(
        "MPEG-TS",
        "transport et diffusion : H.264/AAC",
        EncodingFamily.H264,
        "h264",
        "aac",
        False,
        True,
    ),
    OutputFormat.MTS: OutputFormatSpec(
        "MTS",
        "caméscopes et transport MPEG-TS : H.264/AAC",
        EncodingFamily.H264,
        "h264",
        "aac",
        False,
        True,
    ),
}


def output_spec(output_format: OutputFormat) -> OutputFormatSpec:
    """Retourne la règle unique associée à une sortie prise en charge."""

    return OUTPUT_FORMAT_SPECS[output_format]


def output_format_choices() -> dict[str, str]:
    """Libellés ordonnés utilisés par les interfaces interactives."""

    return {
        output_format.value: spec.choice_description
        for output_format, spec in OUTPUT_FORMAT_SPECS.items()
    }


def supported_output_values() -> str:
    """Liste textuelle stable des extensions produites."""

    return ", ".join(output_format.value for output_format in OUTPUT_FORMAT_SPECS)


def taggable_suffixes() -> frozenset[str]:
    """Extensions dont les informations et la jaquette sont vérifiables."""

    return frozenset(
        f".{output_format.value}"
        for output_format, spec in OUTPUT_FORMAT_SPECS.items()
        if spec.supports_tagging
    )


def taggable_format_names() -> str:
    """Liste lisible des conteneurs acceptés par la commande ``tag``."""

    return ", ".join(
        spec.label for spec in OUTPUT_FORMAT_SPECS.values() if spec.supports_tagging
    )


def allowed_qualities(output_format: OutputFormat) -> tuple[OutputQuality, ...]:
    """Profils valides pour le conteneur demandé."""

    return (
        (OutputQuality.SOURCE,)
        if output_spec(output_format).copies_source
        else TRANSCODE_QUALITIES
    )


def default_quality(output_format: OutputFormat) -> OutputQuality:
    """Profil choisi lorsqu'aucune préférence n'est fournie."""

    return (
        OutputQuality.SOURCE
        if output_spec(output_format).copies_source
        else OutputQuality.BALANCED
    )


def requires_video(output_format: OutputFormat) -> bool:
    """Indique si la sortie est nécessairement un fichier vidéo encodé."""

    return not output_spec(output_format).copies_source

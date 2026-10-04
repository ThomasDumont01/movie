"""Règles communes de vérification des médias avant publication."""

from __future__ import annotations

from collections import Counter

from movie.core.models import (
    ConvertPlan,
    DiscTitle,
    MediaStream,
    MovieError,
    MovieMetadata,
    OutputFormat,
    ProbedMedia,
)
from movie.formats import (
    OutputFormatSpec,
    output_spec,
    output_subtitle_codec,
    requires_video,
    subtitle_codec_supported,
)


def validate_source(media: ProbedMedia) -> None:
    """Vérifie qu'un fichier contient au moins un flux média exploitable."""

    if not any(kind in {"video", "audio"} for kind in media.stream_types):
        raise MovieError(
            "La source ne contient aucune piste audio ou vidéo exploitable."
        )


def validate_extracted_title(
    title: DiscTitle,
    media: ProbedMedia,
) -> tuple[str, ...]:
    """Contrôle une extraction sans confondre une piste vide avec un échec.

    MakeMKV peut annoncer une piste pendant l'analyse puis la retirer pendant
    l'extraction lorsqu'elle est vide, en particulier pour les sous-titres
    forcés. Les anomalies rendant le film inutilisable sont bloquantes ; les
    différences optionnelles restent visibles comme avertissements.
    """

    warnings: list[str] = []
    if "video" not in media.stream_types:
        raise MovieError(
            "Le MKV temporaire ne contient aucune piste vidéo. Rien n'a été publié."
        )
    actual_streams = media.streams or tuple(
        MediaStream(kind=kind) for kind in media.stream_types
    )
    if title.streams:
        expected_kinds = Counter(stream.kind for stream in title.streams)
        actual_kinds = Counter(
            stream.kind for stream in actual_streams if stream.kind != "attachment"
        )
        if expected_kinds["audio"] and not actual_kinds["audio"]:
            raise MovieError(
                "Le support contient de l'audio, mais le MKV n'a aucune piste audio. "
                "Rien n'a été publié."
            )
        missing_kinds = expected_kinds - actual_kinds
        if missing_kinds:
            details = ", ".join(
                _stream_count_label(kind, count)
                for kind, count in sorted(missing_kinds.items())
                if count > 0
            )
            warnings.append(
                f"MakeMKV annonçait {details} de plus pendant l'analyse. "
                "Une piste vide ou optionnelle peut être retirée automatiquement ; "
                "la vidéo et l'audio principal restent valides."
            )
        expected_languages = Counter(
            (stream.kind, stream.language)
            for stream in title.streams
            if stream.language
            and actual_kinds[stream.kind] >= expected_kinds[stream.kind]
        )
        actual_languages = Counter(
            (stream.kind, stream.language)
            for stream in actual_streams
            if stream.language and stream.kind != "attachment"
        )
        missing_languages = expected_languages - actual_languages
        if missing_languages:
            details = ", ".join(
                f"{count}× {_stream_kind_label(kind)} {language}"
                for (kind, language), count in sorted(missing_languages.items())
            )
            warnings.append(
                "Certaines étiquettes de langue annoncées par le support ne sont pas "
                f"restituées à l'identique ({details}). Les pistes restent lisibles."
            )
    if title.chapter_count and media.chapter_count < title.chapter_count:
        warnings.append(
            "L'extraction contient "
            f"{media.chapter_count} chapitre(s), contre {title.chapter_count} annoncé(s) "
            "par le support. Les pistes vidéo et audio ont bien été vérifiées."
        )
    if media.duration_seconds is not None and media.duration_seconds <= 0:
        raise MovieError(
            "La durée du MKV est nulle. Le fichier semble incomplet et n'a pas été publié."
        )
    if title.duration_seconds and media.duration_seconds is None:
        warnings.append(
            "ffprobe n'a pas pu confirmer la durée du MKV. La vidéo et l'audio "
            "ont néanmoins été détectés."
        )
    elif title.duration_seconds and media.duration_seconds:
        difference = abs(title.duration_seconds - media.duration_seconds)
        blocking_difference = max(3.0, title.duration_seconds * 0.05)
        warning_difference = max(1.0, title.duration_seconds * 0.005)
        if difference > blocking_difference:
            raise MovieError(
                "La durée du MKV diffère de plus de 5 % de celle du titre source ; "
                "le fichier semble tronqué et n'a pas été publié."
            )
        if difference > warning_difference:
            warnings.append(
                "La navigation du support annonce "
                f"{title.duration_seconds:.1f} s, tandis que le flux extrait dure "
                f"{media.duration_seconds:.1f} s. Cet écart non bloquant est courant "
                "sur certains titres très courts ou images fixes."
            )
    return tuple(warnings)


def validate_conversion_request(
    plan: ConvertPlan,
    source: ProbedMedia,
) -> None:
    """Valide les flux nécessaires au format demandé."""

    videos = tuple(stream for stream in source.streams if stream.kind == "video")
    if requires_video(plan.output_format) and not videos:
        raise MovieError(
            f"La conversion {plan.output_format.value.upper()} nécessite "
            "une piste vidéo."
        )


def validate_conversion(
    plan: ConvertPlan,
    source: ProbedMedia,
    output: ProbedMedia,
) -> tuple[str, ...]:
    """Vérifie le conteneur, les codecs et les flux du fichier converti."""

    if plan.output_format is OutputFormat.MKV:
        return _validate_mkv(source, output)
    spec = output_spec(plan.output_format)
    return _validate_video_output(
        source,
        output,
        output_format=plan.output_format,
        spec=spec,
        audio_track_index=plan.audio_track_index,
    )


def validate_preserved_media(
    source: ProbedMedia,
    output: ProbedMedia,
    *,
    replace_artwork: bool,
) -> None:
    """Vérifie qu'un remuxage de métadonnées n'a altéré aucun contenu utile."""

    expected = Counter(
        (stream.kind, stream.language, stream.codec)
        for stream in source.streams
        if not (replace_artwork and stream.is_artwork)
    )
    actual = Counter(
        (stream.kind, stream.language, stream.codec)
        for stream in output.streams
        if not (replace_artwork and stream.is_artwork)
    )
    if expected - actual:
        raise MovieError(
            "La vérification indique qu'une piste a été supprimée ou modifiée "
            "pendant l'écriture des métadonnées."
        )
    if source.chapter_count and output.chapter_count < source.chapter_count:
        raise MovieError(
            "La vérification indique que des chapitres ont été supprimés "
            "pendant l'écriture des métadonnées."
        )
    if (
        source.duration_seconds
        and output.duration_seconds
        and abs(source.duration_seconds - output.duration_seconds) > 3
    ):
        raise MovieError("La durée a changé pendant l'écriture des métadonnées.")


def validate_metadata(metadata: MovieMetadata, media: ProbedMedia) -> None:
    """Relit chaque métadonnée demandée après son écriture."""

    tags = {key.casefold(): value for key, value in media.format_tags}
    if tags.get("title") != metadata.title:
        raise MovieError("Le titre n'a pas été retrouvé après écriture.")
    stored_date = tags.get("date_released") or tags.get("date") or tags.get("year", "")
    expected_date = metadata.date_value
    if expected_date is not None and expected_date not in stored_date:
        raise MovieError("La date n'a pas été retrouvée après écriture.")
    if expected_date is None and stored_date:
        raise MovieError("L'ancienne date n'a pas été retirée.")
    if metadata.summary and metadata.summary not in tags.get("description", ""):
        raise MovieError("La description n'a pas été retrouvée après écriture.")
    if metadata.summary is None and tags.get("description"):
        raise MovieError("L'ancienne description n'a pas été retirée.")
    if metadata.genres:
        actual_genres = tags.get("genre", "").casefold()
        if any(genre.casefold() not in actual_genres for genre in metadata.genres):
            raise MovieError("Les genres n'ont pas été retrouvés après écriture.")
    elif tags.get("genre"):
        raise MovieError("Les anciens genres n'ont pas été retirés.")
    if metadata.source_url and not any(
        metadata.source_url in value for value in tags.values()
    ):
        raise MovieError("Le lien TMDB n'a pas été retrouvé après écriture.")
    if metadata.source_url is None and any(
        "themoviedb.org/movie/" in tags.get(key, "")
        for key in ("movie_source", "comment")
    ):
        raise MovieError("L'ancien lien TMDB n'a pas été retiré.")
    embedded_images = sum(stream.is_artwork for stream in media.streams)
    expected_images = int(metadata.has_artwork)
    if media.path.suffix.casefold() == ".mkv":
        expected_images += int(metadata.has_fanart)
    if embedded_images < expected_images:
        raise MovieError(
            "Toutes les illustrations n'ont pas été retrouvées après écriture."
        )


def _validate_mkv(source: ProbedMedia, output: ProbedMedia) -> tuple[str, ...]:
    source_streams = Counter(stream.kind for stream in source.streams)
    output_streams = Counter(stream.kind for stream in output.streams)
    missing = source_streams - output_streams
    if missing:
        details = ", ".join(
            f"{count}× {kind}" for kind, count in sorted(missing.items())
        )
        raise MovieError(
            f"Le MKV a perdu des pistes ({details}) ; rien n'a été publié."
        )
    expected_codecs = Counter(
        (stream.kind, _expected_mkv_codec(source, stream))
        for stream in source.streams
        if stream.kind != "attachment" and stream.codec
    )
    actual_codecs = Counter(
        (stream.kind, stream.codec)
        for stream in output.streams
        if stream.kind != "attachment" and stream.codec
    )
    if expected_codecs - actual_codecs:
        raise MovieError("Le MKV n'a pas conservé les codecs des pistes source.")
    expected_languages = Counter(
        (stream.kind, stream.language)
        for stream in source.streams
        if stream.kind != "attachment" and stream.language
    )
    actual_languages = Counter(
        (stream.kind, stream.language)
        for stream in output.streams
        if stream.kind != "attachment" and stream.language
    )
    warnings: list[str] = []
    if any(
        stream.kind == "subtitle" and stream.codec == "dvb_teletext"
        for stream in source.streams
    ):
        warnings.append(
            "Le sous-titre télétexte a été converti en SubRip par mkvmerge afin "
            "d'être stocké dans le MKV ; son contenu reste présent."
        )
    if expected_languages - actual_languages:
        warnings.append(
            "Le remuxeur MKV a normalisé une ou plusieurs étiquettes de langue ; "
            "toutes les pistes et tous les codecs sont présents."
        )
    if source.chapter_count and output.chapter_count < source.chapter_count:
        raise MovieError("Le MKV a perdu un ou plusieurs chapitres.")
    _validate_duration(source, output, "MKV")
    return tuple(warnings)


def _expected_mkv_codec(source: ProbedMedia, stream: MediaStream) -> str:
    """Tient compte des normalisations documentées du remuxeur Matroska."""

    assert stream.codec is not None
    if (
        source.path.suffix.casefold() in {".m2ts", ".mts", ".ts"}
        and stream.kind == "subtitle"
        and stream.codec == "dvb_teletext"
    ):
        # Matroska n'a pas de représentation native du télétexte DVB. mkvmerge
        # extrait donc les pages de sous-titres sous forme de texte SubRip sans
        # supprimer la piste (mkvmerge -J l'annonce lui-même comme SubRip/SRT).
        return "subrip"
    return stream.codec


def _validate_video_output(
    source: ProbedMedia,
    output: ProbedMedia,
    *,
    output_format: OutputFormat,
    spec: OutputFormatSpec,
    audio_track_index: int | None,
) -> tuple[str, ...]:
    label = spec.label
    videos = tuple(stream for stream in output.streams if stream.kind == "video")
    audios = tuple(stream for stream in output.streams if stream.kind == "audio")
    source_audios = tuple(stream for stream in source.streams if stream.kind == "audio")
    subtitles = tuple(stream for stream in output.streams if stream.kind == "subtitle")
    source_subtitles = tuple(
        stream for stream in source.streams if stream.kind == "subtitle"
    )
    expected_subtitles = tuple(
        stream
        for stream in source_subtitles
        if subtitle_codec_supported(output_format, stream.codec)
    )
    expected_audios = source_audios
    if audio_track_index is not None:
        if audio_track_index >= len(source_audios):
            raise MovieError(
                "La piste audio sélectionnée n'existe plus dans la source."
            )
        expected_audios = (source_audios[audio_track_index],)
    if not videos or videos[0].codec != spec.video_codec:
        raise MovieError(
            f"Le {label} vérifié ne contient pas de vidéo {spec.video_codec}."
        )
    if len(audios) < len(expected_audios):
        raise MovieError(f"Le {label} a perdu une ou plusieurs pistes audio.")
    if audio_track_index is not None and len(audios) != 1:
        raise MovieError(
            f"Le {label} devait contenir une seule piste audio pour le montage."
        )
    if any(stream.codec != spec.audio_codec for stream in audios):
        raise MovieError(
            f"Toutes les pistes audio du {label} doivent être en {spec.audio_codec}."
        )
    missing_languages = _missing_languages(expected_audios, audios)
    if missing_languages and spec.preserves_audio_languages:
        raise MovieError(f"Le {label} a perdu la langue d'une piste audio.")
    if len(subtitles) < len(expected_subtitles):
        raise MovieError(f"Le {label} a perdu un ou plusieurs sous-titres compatibles.")
    expected_subtitle_codecs = Counter(
        output_subtitle_codec(output_format, stream.codec)
        for stream in expected_subtitles
        if stream.codec is not None
    )
    actual_subtitle_codecs = Counter(
        stream.codec for stream in subtitles if stream.codec is not None
    )
    if expected_subtitle_codecs - actual_subtitle_codecs:
        raise MovieError(
            f"Le {label} n'a pas conservé le codec attendu des sous-titres."
        )
    missing_subtitle_languages = _missing_languages(expected_subtitles, subtitles)
    if missing_subtitle_languages and spec.preserves_audio_languages:
        raise MovieError(f"Le {label} a perdu la langue d'un sous-titre.")
    _validate_duration(source, output, label)

    warnings: list[str] = []
    omitted_audio_count = len(source_audios) - len(expected_audios)
    if omitted_audio_count:
        warnings.append(
            f"{omitted_audio_count} autre(s) piste(s) audio volontairement écartée(s) "
            "pour produire un fichier de montage simple."
        )
    if missing_languages:
        warnings.append(
            f"Le conteneur {label} ne conserve pas toujours les étiquettes de langue "
            "des pistes audio ; les pistes elles-mêmes ont bien été vérifiées."
        )
    omitted_subtitle_count = len(source_subtitles) - len(expected_subtitles)
    if omitted_subtitle_count:
        warnings.append(
            f"{omitted_subtitle_count} piste(s) de sous-titres image ne sont pas "
            f"compatibles avec le {label} et n'ont pas été intégrées ; "
            "utilise le MKV pour les conserver sans compromis."
        )
    if missing_subtitle_languages:
        warnings.append(
            f"Le {label} ne conserve pas les langues de tous les sous-titres."
        )
    if source.chapter_count and output.chapter_count < source.chapter_count:
        if spec.preserves_chapters:
            warnings.append(
                f"Le {label} contient {output.chapter_count} chapitre(s), contre "
                f"{source.chapter_count} dans la source."
            )
        else:
            warnings.append(
                f"Le conteneur {label} ne conserve pas les chapitres de la source."
            )
    return tuple(warnings)


def _missing_languages(
    source: tuple[MediaStream, ...],
    output: tuple[MediaStream, ...],
) -> bool:
    expected = Counter(stream.language for stream in source if stream.language)
    actual = Counter(stream.language for stream in output if stream.language)
    return bool(expected - actual)


def _validate_duration(source: ProbedMedia, output: ProbedMedia, label: str) -> None:
    if (
        source.duration_seconds
        and output.duration_seconds
        and abs(source.duration_seconds - output.duration_seconds) > 3
    ):
        raise MovieError(f"La durée du {label} diffère trop de celle de la source.")


def _stream_kind_label(kind: str) -> str:
    return {
        "video": "vidéo",
        "audio": "audio",
        "subtitle": "sous-titre",
        "attachment": "pièce jointe",
    }.get(kind, kind)


def _stream_count_label(kind: str, count: int) -> str:
    label = {
        "video": "vidéo",
        "audio": "audio",
        "subtitle": "de sous-titres",
        "attachment": "joint",
    }.get(kind, kind)
    return f"{count} flux {label}"

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
    OutputQuality,
    ProbedMedia,
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
                "Le DVD contient de l'audio, mais le MKV n'a aucune piste audio. "
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
                "Certaines étiquettes de langue annoncées par le DVD ne sont pas "
                f"restituées à l'identique ({details}). Les pistes restent lisibles."
            )
    if title.chapter_count and media.chapter_count < title.chapter_count:
        warnings.append(
            "L'extraction contient "
            f"{media.chapter_count} chapitre(s), contre {title.chapter_count} annoncé(s) "
            "par le DVD. Les pistes vidéo et audio ont bien été vérifiées."
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
                "La durée du MKV diffère de plus de 5 % de celle du titre DVD ; "
                "le fichier semble tronqué et n'a pas été publié."
            )
        if difference > warning_difference:
            warnings.append(
                "La navigation du DVD annonce "
                f"{title.duration_seconds:.1f} s, tandis que le flux extrait dure "
                f"{media.duration_seconds:.1f} s. Cet écart non bloquant est courant "
                "sur certains titres très courts ou images fixes."
            )
    return tuple(warnings)


def validate_conversion_request(
    plan: ConvertPlan,
    source: ProbedMedia,
    *,
    audio_track: int | None,
) -> None:
    """Valide les flux nécessaires au format demandé."""

    videos = tuple(stream for stream in source.streams if stream.kind == "video")
    audios = tuple(stream for stream in source.streams if stream.kind == "audio")
    if plan.output_format is OutputFormat.MP4 and not videos:
        raise MovieError("La conversion MP4 nécessite une piste vidéo.")
    if plan.output_format is OutputFormat.M4A:
        if not audios:
            raise MovieError("La source ne contient aucune piste audio.")
        if audio_track is None or not 0 <= audio_track < len(audios):
            selected = "aucune" if audio_track is None else str(audio_track)
            raise MovieError(
                f"La piste audio {selected} n'existe pas ; "
                f"choix possibles : 0 à {len(audios) - 1}."
            )


def validate_conversion(
    plan: ConvertPlan,
    source: ProbedMedia,
    output: ProbedMedia,
    *,
    audio_track: int | None,
) -> tuple[str, ...]:
    """Vérifie le conteneur, les codecs et les flux du fichier converti."""

    if plan.output_format is OutputFormat.MKV:
        return _validate_mkv(source, output)
    if plan.output_format is OutputFormat.MP4:
        return _validate_mp4(source, output)
    return _validate_m4a(plan, source, output, audio_track=audio_track)


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
            "L'écriture des métadonnées aurait supprimé ou modifié une piste ; "
            "le fichier d'origine a été conservé."
        )
    if source.chapter_count and output.chapter_count < source.chapter_count:
        raise MovieError(
            "L'écriture des métadonnées aurait supprimé des chapitres ; "
            "le fichier d'origine a été conservé."
        )
    if (
        source.duration_seconds
        and output.duration_seconds
        and abs(source.duration_seconds - output.duration_seconds) > 3
    ):
        raise MovieError(
            "La durée a changé pendant l'écriture des métadonnées ; "
            "le fichier d'origine a été conservé."
        )


def validate_metadata(metadata: MovieMetadata, media: ProbedMedia) -> None:
    """Relit chaque métadonnée demandée avant le remplacement de l'original."""

    tags = {key.casefold(): value for key, value in media.format_tags}
    if tags.get("title") != metadata.title:
        raise MovieError(
            "Le titre n'a pas été retrouvé après écriture ; "
            "le fichier d'origine a été conservé."
        )
    if metadata.year is not None and str(metadata.year) not in tags.get("date", ""):
        raise MovieError(
            "L'année n'a pas été retrouvée après écriture ; "
            "le fichier d'origine a été conservé."
        )
    if metadata.year is None and tags.get("date"):
        raise MovieError(
            "L'ancienne année n'a pas été retirée ; "
            "le fichier d'origine a été conservé."
        )
    if metadata.summary and metadata.summary not in tags.get("description", ""):
        raise MovieError(
            "La description n'a pas été retrouvée après écriture ; "
            "le fichier d'origine a été conservé."
        )
    if metadata.summary is None and tags.get("description"):
        raise MovieError(
            "L'ancienne description n'a pas été retirée ; "
            "le fichier d'origine a été conservé."
        )
    if metadata.genres:
        actual_genres = tags.get("genre", "").casefold()
        if any(genre.casefold() not in actual_genres for genre in metadata.genres):
            raise MovieError(
                "Les genres n'ont pas été retrouvés après écriture ; "
                "le fichier d'origine a été conservé."
            )
    elif tags.get("genre"):
        raise MovieError(
            "Les anciens genres n'ont pas été retirés ; "
            "le fichier d'origine a été conservé."
        )
    if metadata.source_url and not any(
        metadata.source_url in value for value in tags.values()
    ):
        raise MovieError(
            "Le lien TMDB n'a pas été retrouvé après écriture ; "
            "le fichier d'origine a été conservé."
        )
    if metadata.source_url is None and any(
        "themoviedb.org/movie/" in tags.get(key, "")
        for key in ("movie_source", "comment")
    ):
        raise MovieError(
            "L'ancien lien TMDB n'a pas été retiré ; "
            "le fichier d'origine a été conservé."
        )
    if metadata.has_artwork and "attachment" not in media.stream_types:
        raise MovieError(
            "La jaquette n'a pas été retrouvée après écriture ; "
            "le fichier d'origine a été conservé."
        )


def _validate_mkv(source: ProbedMedia, output: ProbedMedia) -> tuple[str, ...]:
    source_streams = Counter(stream.kind for stream in source.streams)
    output_streams = Counter(stream.kind for stream in output.streams)
    missing = source_streams - output_streams
    if missing:
        details = ", ".join(f"{count}× {kind}" for kind, count in sorted(missing.items()))
        raise MovieError(f"Le MKV a perdu des pistes ({details}) ; rien n'a été publié.")
    expected_details = Counter(
        (stream.kind, stream.language, stream.codec)
        for stream in source.streams
        if stream.kind != "attachment" and (stream.language or stream.codec)
    )
    actual_details = Counter(
        (stream.kind, stream.language, stream.codec)
        for stream in output.streams
        if stream.kind != "attachment" and (stream.language or stream.codec)
    )
    if expected_details - actual_details:
        raise MovieError(
            "Le MKV n'a pas conservé les codecs ou langues des pistes source."
        )
    if source.chapter_count and output.chapter_count < source.chapter_count:
        raise MovieError("Le MKV a perdu un ou plusieurs chapitres.")
    _validate_duration(source, output, "MKV")
    return ()


def _validate_mp4(source: ProbedMedia, output: ProbedMedia) -> tuple[str, ...]:
    videos = tuple(stream for stream in output.streams if stream.kind == "video")
    audios = tuple(stream for stream in output.streams if stream.kind == "audio")
    source_audios = tuple(stream for stream in source.streams if stream.kind == "audio")
    if not videos or videos[0].codec != "h264":
        raise MovieError("Le MP4 vérifié ne contient pas de vidéo H.264.")
    if len(audios) < len(source_audios):
        raise MovieError("Le MP4 a perdu une ou plusieurs pistes audio.")
    if any(stream.codec != "aac" for stream in audios):
        raise MovieError("Toutes les pistes audio du MP4 doivent être en AAC.")
    _validate_languages(source_audios, audios, "MP4")
    _validate_duration(source, output, "MP4")

    warnings: list[str] = []
    subtitle_count = sum(stream.kind == "subtitle" for stream in source.streams)
    if subtitle_count:
        warnings.append(
            f"{subtitle_count} piste(s) de sous-titres ne sont pas intégrées au MP4 ; "
            "utilise le MKV pour les conserver sans compromis."
        )
    if source.chapter_count and output.chapter_count < source.chapter_count:
        warnings.append(
            f"Le MP4 contient {output.chapter_count} chapitre(s), contre "
            f"{source.chapter_count} dans la source."
        )
    return tuple(warnings)


def _validate_m4a(
    plan: ConvertPlan,
    source: ProbedMedia,
    output: ProbedMedia,
    *,
    audio_track: int | None,
) -> tuple[str, ...]:
    audios = tuple(stream for stream in output.streams if stream.kind == "audio")
    expected_codec = "alac" if plan.output_quality is OutputQuality.LOSSLESS else "aac"
    if len(audios) != 1 or audios[0].codec != expected_codec:
        raise MovieError(
            f"Le M4A doit contenir exactement une piste audio {expected_codec.upper()}."
        )
    _validate_duration(source, output, "M4A")
    source_audios = tuple(stream for stream in source.streams if stream.kind == "audio")
    assert audio_track is not None
    selected = source_audios[audio_track]
    if selected.language and audios[0].language != selected.language:
        raise MovieError("Le M4A a perdu la langue de la piste audio sélectionnée.")

    ignored = sum(
        stream.kind in {"video", "subtitle"} for stream in source.streams
    ) + max(0, len(source_audios) - 1)
    if ignored:
        return (
            (
                f"{ignored} piste(s) non audio ou non sélectionnée(s) ont été "
                "volontairement exclues du M4A."
            ),
        )
    return ()


def _validate_languages(
    source: tuple[MediaStream, ...],
    output: tuple[MediaStream, ...],
    label: str,
) -> None:
    expected = Counter(stream.language for stream in source if stream.language)
    actual = Counter(stream.language for stream in output if stream.language)
    if expected - actual:
        raise MovieError(f"Le {label} a perdu la langue d'une piste audio.")


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

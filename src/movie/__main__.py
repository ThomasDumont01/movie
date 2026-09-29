"""Interface interactive de numérisation, conversion et métadonnées."""

from __future__ import annotations

import argparse
import shutil
import sys
import webbrowser
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from movie import __version__
from movie.config import MovieConfig, config_path, load_config, save_config
from movie.conversion import MediaConverter, copies_video_without_reencoding
from movie.core.convert import (
    ConversionService,
    build_convert_plan,
    conversion_settings,
)
from movie.core.makemkv import MakeMkvClient, find_makemkvcon
from movie.core.media import MediaProbe
from movie.core.models import (
    DiscTitle,
    Drive,
    MovieError,
    MovieMetadata,
    OutputFormat,
    OutputQuality,
    ProbedMedia,
)
from movie.core.rip import (
    RipService,
    build_rip_plan,
    main_title_candidates,
)
from movie.core.storage import (
    conversion_required_bytes,
    output_directory_issue,
    rip_required_bytes,
)
from movie.core.tag import TagService, build_tag_plan
from movie.enrichment import MediaTagger
from movie.formats import (
    output_format_choices,
    output_spec,
    requires_video,
    taggable_format_names,
    taggable_suffixes,
)
from movie.matroska import find_mkvextract, find_mkvmerge, find_mkvpropedit
from movie.metadata import MetadataClient, movie_search_url
from movie.terminal import (
    _alert_user,
    _format_duration,
    _format_file_size,
    _format_title,
    _media_summary,
    _print_drive,
    _print_header,
    _print_selected_drive,
    _print_step,
    _print_subheading,
    _ProgressRenderer,
    _prompt_choice,
    _prompt_existing_file,
    _prompt_float,
    _prompt_for_drive,
    _prompt_for_title,
    _prompt_genres,
    _prompt_optional_existing_file,
    _prompt_optional_int,
    _prompt_optional_path,
    _prompt_optional_text,
    _prompt_optional_year,
    _prompt_path,
    _prompt_required_text,
    _prompt_yes_no,
    _read_answer,
)


class _MovieArgumentParser(argparse.ArgumentParser):
    """ArgumentParser dont les libellés visibles restent cohérents en français."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["add_help"] = False
        super().__init__(*args, **kwargs)
        self.add_argument(
            "-h",
            "--help",
            action="help",
            help="affiche cette aide",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = _MovieArgumentParser(
        prog="movie",
        description=(
            "Numérise, convertit ou renseigne un média avec des commandes "
            "interactives simples."
        ),
        epilog="Utilisation la plus simple : uv run movie rip",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"movie {__version__}",
        help="affiche la version installée",
    )
    commands = parser.add_subparsers(dest="command", title="commandes")

    commands.add_parser(
        "doctor",
        help="vérifie que les outils multimédias sont prêts",
        description="Vérifie les logiciels nécessaires et explique comment les installer.",
    )
    commands.add_parser(
        "drives",
        help="affiche les lecteurs et les disques détectés",
        description="Affiche les lecteurs optiques actuellement disponibles.",
    )
    commands.add_parser(
        "config",
        help="modifie les préférences enregistrées",
        description="Configure les valeurs utilisées automatiquement par Movie.",
    )

    commands.add_parser(
        "scan",
        help="inspecte le disque vidéo sans créer de fichier",
        description="Analyse le DVD, Blu-ray ou UHD inséré et présente ses titres.",
    )

    commands.add_parser(
        "rip",
        help="archive un film du disque en MKV",
        description=(
            "Guide la sélection du film, crée un MKV fidèle, puis vérifie le résultat."
        ),
    )
    commands.add_parser(
        "convert",
        help="convertit un ISO ou un fichier multimédia",
        description=(
            "Convertit une image ISO ou tout média lisible par FFmpeg vers un "
            "format vidéo compatible, puis contrôle le fichier produit."
        ),
    )
    commands.add_parser(
        "tag",
        help="ajoute ou corrige les informations d'un média",
        description=(
            "Ajoute des métadonnées TMDB ou manuelles et une jaquette à un "
            f"fichier {taggable_format_names()}, "
            "sans réencoder son contenu."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            return _doctor()
        if args.command == "drives":
            return _drives()
        if args.command == "config":
            return _config()
        if args.command == "scan":
            return _scan()
        if args.command == "rip":
            return _rip()
        if args.command == "convert":
            return _convert()
        if args.command == "tag":
            return _tag()
    except MovieError as error:
        print(f"✗ {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(
            "\nOpération interrompue. Aucun résultat incomplet n'a été publié.",
            file=sys.stderr,
        )
        return 130

    parser.print_help()
    return 0


def _doctor() -> int:
    _print_header("Diagnostic")
    makemkv = _tool_status(find_makemkvcon)
    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    mkvpropedit = _tool_status(find_mkvpropedit)
    mkvextract = _tool_status(find_mkvextract)
    mkvmerge = _tool_status(find_mkvmerge)
    makemkv_missing = makemkv.startswith("introuvable")
    mkvtoolnix_missing = any(
        path.startswith("introuvable") for path in (mkvpropedit, mkvextract, mkvmerge)
    )
    print(f"  {'✓' if not makemkv_missing else '✗'} MakeMKV : {makemkv}")
    print(f"  {'✓' if ffprobe else '✗'} ffprobe : {ffprobe or 'introuvable'}")
    print(f"  {'✓' if ffmpeg else '✗'} FFmpeg : {ffmpeg or 'introuvable'}")
    print(
        f"  {'✓' if not mkvtoolnix_missing else '✗'} MKVToolNix : "
        f"{mkvpropedit if not mkvtoolnix_missing else 'incomplet ou introuvable'}"
    )
    if makemkv_missing or not ffprobe or not ffmpeg or mkvtoolnix_missing:
        _print_subheading("Installation des outils manquants")
        if makemkv_missing:
            print("  MakeMKV :")
            print("    Téléchargement officiel : https://www.makemkv.com/download/")
            print(
                "    Ouvrir la page sur macOS : open https://www.makemkv.com/download/"
            )
        if not ffprobe or not ffmpeg:
            print("  FFmpeg et ffprobe :")
            print("    Avec Homebrew : brew install ffmpeg")
            print("    Informations : https://formulae.brew.sh/formula/ffmpeg")
        if mkvtoolnix_missing:
            print("  MKVToolNix :")
            print("    Avec Homebrew : brew install mkvtoolnix")
            print("    Informations : https://formulae.brew.sh/formula/mkvtoolnix")
        print("\nUne fois l'installation terminée, relance : uv run movie doctor")
        return 2
    print("\n✓ Movie est prêt à numériser, convertir et renseigner tes médias.")
    return 0


def _drives() -> int:
    _print_header("Lecteurs optiques")
    drives = MakeMkvClient().drives()
    if not drives:
        print("Aucun lecteur optique n'a été détecté.")
        return 0
    if len(drives) == 1:
        drive = next(iter(drives))
        _print_drive(drive)
        return 0
    print(f"{len(drives)} lecteurs détectés :\n")
    for drive in drives:
        _print_drive(drive)
    return 0


def _config() -> int:
    current = load_config()
    _print_header("Configuration")
    print(f"Fichier : {config_path()}")
    print("Entrée conserve la valeur affichée ; « - » efface une valeur facultative.\n")
    output = _prompt_optional_path(
        "Dossier de sortie par défaut",
        default=str(current.output_directory) if current.output_directory else None,
    )
    drive_index = _prompt_optional_int(
        "Lecteur par défaut",
        default=current.drive_index,
    )
    auto_run = _prompt_yes_no(
        "Lancer automatiquement lorsque toutes les informations sont connues ?",
        default=current.auto_run,
    )
    alert_sound = _prompt_yes_no(
        "Jouer un son lorsqu'une réponse est nécessaire ?",
        default=current.alert_sound,
    )
    progress_delay = _prompt_float(
        "Délai avant d'afficher une barre de progression (secondes)",
        default=current.progress_delay_seconds,
        minimum=0,
        maximum=60,
    )
    open_browser = _prompt_yes_no(
        "Ouvrir la recherche TMDB pendant la commande tag ?",
        default=current.open_browser,
    )
    convert_format = OutputFormat(
        _prompt_choice(
            "Format de conversion par défaut",
            choices=output_format_choices(),
            default=current.convert_format.value,
        )
    )
    if output_spec(convert_format).copies_source:
        convert_quality = OutputQuality.SOURCE
    else:
        configured_quality = (
            current.convert_quality
            if current.convert_quality is not OutputQuality.SOURCE
            else OutputQuality.BALANCED
        )
        convert_quality = _prompt_conversion_quality(
            convert_format,
            default=configured_quality,
        )
    saved_at = save_config(
        MovieConfig(
            output_directory=output,
            drive_index=drive_index,
            auto_run=auto_run,
            alert_sound=alert_sound,
            progress_delay_seconds=progress_delay,
            open_browser=open_browser,
            convert_format=convert_format,
            convert_quality=convert_quality,
        )
    )
    print(f"\n✓ Configuration enregistrée : {saved_at}")
    print(
        "Conversion par défaut : "
        f"{_output_description(convert_format, convert_quality)}."
    )
    return 0


def _scan() -> int:
    config = load_config()
    client = MakeMkvClient()
    _print_header("Analyse du disque")
    selected_drive, drives = _resolve_drive_for_operation(
        client,
        requested=None,
        configured=config.drive_index,
        alert=config.alert_sound,
    )
    if drives:
        _print_selected_drive(drives, selected_drive)
    print("\nAnalyse en cours…")
    progress = _ProgressRenderer(
        operation_label="Analyse du disque",
        threshold_seconds=config.progress_delay_seconds,
    )
    progress.start("Préparation du lecteur")
    try:
        scan = client.scan(selected_drive, on_progress=progress)
    except BaseException:
        progress.cancel()
        raise
    progress.finish()
    if not drives:
        _print_selected_drive((scan.drive,), selected_drive)
    candidates = main_title_candidates(scan)
    candidate_ids = {title.title_id for title in candidates}
    _print_subheading("Contenu détecté")
    print(f"  Disque  : {scan.drive.disc_label or 'sans nom'}")
    print(f"  Type    : {scan.disc_type or 'non identifié'}")
    print(f"  Titres  : {len(scan.titles)}")
    print("  ★       : titre principal possible\n")
    for title in scan.titles:
        marker = "★" if title.title_id in candidate_ids else " "
        print(f"{marker} {_format_title(title)}")
    if len(candidates) > 1:
        choices = ", ".join(str(title.title_id) for title in candidates)
        print(
            "\n⚠ Plusieurs titres principaux sont impossibles à départager "
            f"automatiquement ({choices}). Movie demandera lequel copier."
        )
    else:
        candidate = next(iter(candidates), None)
        if candidate is not None:
            print(f"\nTitre principal proposé : {candidate.title_id}.")
    for warning in scan.warnings:
        print(f"⚠ {warning}")
    print(
        "\nℹ Cette commande est uniquement informative. "
        "La commande rip refera automatiquement sa propre analyse du disque."
    )
    return 0


def _rip() -> int:
    config = load_config()
    service = _service()
    _print_header("Numérisation d'un disque")
    selected_drive, drives = _resolve_drive_for_operation(
        service.makemkv,
        requested=None,
        configured=config.drive_index,
        alert=config.alert_sound,
    )
    if drives:
        _print_selected_drive(drives, selected_drive)
    _print_step(1, 3, "Analyse du disque")
    print("Analyse en cours…")
    scan_progress = _ProgressRenderer(
        operation_label="Analyse du disque",
        threshold_seconds=config.progress_delay_seconds,
    )
    scan_progress.start("Préparation du lecteur")
    try:
        scan = service.scan(selected_drive, on_progress=scan_progress)
    except BaseException:
        scan_progress.cancel()
        raise
    scan_progress.finish()
    if not drives:
        _print_selected_drive((scan.drive,), selected_drive)
    print(
        f"Support reconnu : {scan.disc_type or 'type non identifié'} "
        f"· {scan.drive.disc_label or 'sans nom'} · {len(scan.titles)} titre(s)"
    )
    _print_step(2, 3, "Choix du film")
    candidates = main_title_candidates(scan)
    if len(candidates) > 1:
        _alert_user(config.alert_sound)
        title_id = _prompt_for_title(candidates)
    else:
        title_id = next(iter(candidates)).title_id
        print(f"Titre principal détecté automatiquement : {title_id}.")
    output_directory = _resolve_output_directory(
        configured=config.output_directory,
        fallback=None,
        required_bytes=rip_required_bytes(
            next(title for title in scan.titles if title.title_id == title_id)
        ),
        alert=config.alert_sound,
    )
    plan = build_rip_plan(
        scan,
        output_directory,
        title_id=title_id,
    )
    _print_subheading("Récapitulatif")
    print(f"  Titre       : {_format_title(plan.title)}")
    if plan.title.size_bytes:
        print(f"  Taille      : {_format_file_size(plan.title.size_bytes)}")
    print("  Sortie      : MKV · qualité source · toutes les pistes")
    print(f"  Destination : {plan.output}")
    if not config.auto_run and not _prompt_yes_no("Lancer cette numérisation ?"):
        print("Numérisation annulée. Aucun fichier n'a été écrit.")
        return 0

    _print_step(3, 3, "Création et vérification")
    print("Traitement en cours…")
    progress = _ProgressRenderer(
        operation_label="Numérisation du disque",
        threshold_seconds=config.progress_delay_seconds,
    )
    progress.start("Préparation de l'extraction")
    try:
        result = service.execute(plan, on_progress=progress)
    except BaseException:
        progress.cancel()
        raise
    progress.finish()
    print("\n✓ Film créé et vérifié")
    print(f"  Fichier   : {result.output}")
    if result.media.duration_seconds is not None:
        print(f"  Durée     : {_format_duration(result.media.duration_seconds)}")
    print(f"  Pistes    : {_media_summary(result.media.stream_types)}")
    print(f"  Chapitres : {result.media.chapter_count}")
    for warning in result.warnings:
        print(f"  ⚠ {warning}")
    return 0


def _convert() -> int:
    config = load_config()
    service = _conversion_service()
    _print_header("Conversion d'un média")

    _alert_user(config.alert_sound)
    source = _prompt_path("Fichier source (ISO ou média lisible par FFmpeg)")
    source = source.expanduser().resolve()
    if not source.is_file():
        raise MovieError(f"Le fichier source est introuvable : {source}")

    _print_step(1, 3, "Analyse de la source")
    iso_title: DiscTitle | None = None
    source_media = None
    if source.suffix.casefold() == ".iso":
        print("Analyse de l'image ISO en cours…")
        scan_progress = _ProgressRenderer(
            operation_label="Analyse de l'image ISO",
            threshold_seconds=config.progress_delay_seconds,
        )
        scan_progress.start("Ouverture de l'image ISO")
        try:
            scan = service.makemkv.scan_iso(source, on_progress=scan_progress)
        except BaseException:
            scan_progress.cancel()
            raise
        scan_progress.finish()
        candidates = main_title_candidates(scan)
        if len(candidates) > 1:
            _alert_user(config.alert_sound)
            title_id = _prompt_for_title(candidates)
        else:
            title_id = next(iter(candidates)).title_id
            print(f"Titre principal détecté automatiquement : {title_id}.")
        iso_title = next(
            (title for title in scan.titles if title.title_id == title_id),
            None,
        )
        if iso_title is None:
            raise MovieError(f"Le titre {title_id} n'existe pas dans l'image ISO.")
        source_streams = iso_title.streams
        print(f"Source reconnue : image ISO · {_format_title(iso_title)}")
    else:
        source_media = service.probe.probe(source)
        if not any(kind in {"video", "audio"} for kind in source_media.stream_types):
            raise MovieError(
                "La source ne contient aucune piste audio ou vidéo exploitable."
            )
        source_streams = source_media.streams
        print(
            f"Source reconnue : {source.name} · "
            f"{_media_summary(source_media.stream_types)}"
        )

    _print_step(2, 3, "Choix de la sortie")
    selected_format, selected_quality = _resolve_conversion_settings(config)
    if requires_video(selected_format) and not any(
        stream.kind == "video" for stream in source_streams
    ):
        raise MovieError(
            f"La conversion {selected_format.value.upper()} nécessite une piste vidéo."
        )

    required_bytes = conversion_required_bytes(
        source,
        output_format=selected_format,
        iso_title=iso_title,
    )
    output_directory = _resolve_output_directory(
        configured=config.output_directory,
        fallback=source.parent,
        required_bytes=required_bytes,
        alert=config.alert_sound,
    )
    plan = build_convert_plan(
        source,
        selected_format,
        output_quality=selected_quality,
        output_directory=output_directory,
        iso_title=iso_title,
        source_media=source_media,
    )

    _print_subheading("Récapitulatif")
    print(f"  Source      : {plan.source}")
    if plan.iso_title is not None:
        print(f"  Titre ISO   : {_format_title(plan.iso_title)}")
    conversion_description = _output_description(
        plan.output_format,
        plan.output_quality,
        plan.source_media,
    )
    print(f"  Conversion  : {conversion_description}")
    print(f"  Destination : {plan.output}")

    if not config.auto_run and not _prompt_yes_no("Lancer cette conversion ?"):
        print("Conversion annulée. Aucun fichier n'a été écrit.")
        return 0

    _print_step(3, 3, "Conversion et vérification")
    print("Traitement en cours…")
    progress = _ProgressRenderer(
        operation_label="Conversion du média",
        threshold_seconds=config.progress_delay_seconds,
    )
    progress.start("Préparation de la source")
    try:
        result = service.execute(plan, on_progress=progress)
    except BaseException:
        progress.cancel()
        raise
    progress.finish()

    print("\n✓ Média converti et vérifié")
    print(f"  Fichier   : {result.output}")
    if result.media.duration_seconds is not None:
        print(f"  Durée     : {_format_duration(result.media.duration_seconds)}")
    print(f"  Pistes    : {_media_summary(result.media.stream_types)}")
    print(f"  Chapitres : {result.media.chapter_count}")
    for warning in result.warnings:
        print(f"  ⚠ {warning}")
    return 0


def _tag() -> int:
    config = load_config()
    service = _tag_service()
    _print_header("Informations du média")

    _alert_user(config.alert_sound)
    source = _prompt_existing_file(f"Fichier à renseigner ({taggable_format_names()})")
    if source.suffix.casefold() not in taggable_suffixes():
        raise MovieError(
            "La commande tag accepte uniquement les fichiers "
            f"{taggable_format_names()}."
        )
    mode = _prompt_choice(
        "Origine des informations",
        choices={
            "tmdb": "film officiel, à partir d'un lien TMDB",
            "manuel": "film personnel ou informations saisies à la main",
        },
        default="tmdb",
    )
    if mode == "tmdb":
        metadata = _identify_movie(
            query=source.stem,
            open_browser=config.open_browser,
            alert=config.alert_sound,
        )
        if metadata is None:
            print("Ajout des métadonnées annulé. Le fichier n'a pas été modifié.")
            return 0
    else:
        metadata = _manual_metadata(source)

    plan = build_tag_plan(source, metadata)
    _print_subheading("Récapitulatif")
    year = f" ({plan.metadata.year})" if plan.metadata.year else ""
    print(f"  Fichier      : {plan.source}")
    if plan.output != plan.source:
        print(f"  Nouveau nom  : {plan.output.name}")
    print(f"  Titre        : {plan.metadata.title}{year}")
    print(
        f"  Informations : {'TMDB' if plan.metadata.source_url else 'saisie manuelle'}"
    )
    print(f"  Jaquette     : {'oui' if plan.metadata.has_artwork else 'non'}")
    if plan.metadata.has_fanart:
        fanart_status = (
            "intégré au MKV"
            if plan.source.suffix.casefold() == ".mkv"
            else "non pris en charge par ce conteneur"
        )
    else:
        fanart_status = "non"
    print(f"  Arrière-plan : {fanart_status}")
    if plan.source.suffix.casefold() == ".mkv":
        print(
            "  Traitement   : métadonnées modifiées directement, sans copie de la vidéo"
        )
        print("  Sécurité     : pistes relues et contrôlées après l'écriture")
    else:
        print("  Traitement   : copie des pistes, sans réencodage")
        print(
            "  Sécurité     : l'original reste intact tant que la vérification "
            "n'est pas finie"
        )

    if not config.auto_run and not _prompt_yes_no(
        "Écrire ces informations dans le fichier ?"
    ):
        print("Ajout des métadonnées annulé. Le fichier n'a pas été modifié.")
        return 0

    print("\nTraitement en cours…")
    progress = _ProgressRenderer(
        operation_label="Écriture des métadonnées",
        threshold_seconds=config.progress_delay_seconds,
    )
    progress.start("Préparation des métadonnées")
    try:
        result = service.execute(plan, on_progress=progress)
    except BaseException:
        progress.cancel()
        raise
    progress.finish()

    print("\n✓ Métadonnées écrites et vérifiées")
    print(f"  Fichier : {result.output}")
    print(f"  Titre   : {plan.metadata.title}{year}")
    for warning in result.warnings:
        print(f"  ⚠ {warning}")
    return 0


def _manual_metadata(source: Path) -> MovieMetadata:
    _print_subheading("Saisie manuelle")
    proposed_title = " ".join(source.stem.replace("_", " ").split())
    title = _prompt_required_text("Titre", default=proposed_title)
    year = _prompt_optional_year()
    summary = _prompt_optional_text("Description")
    genres = _prompt_genres()
    poster_path = _prompt_optional_existing_file("Jaquette locale JPEG, PNG ou WebP")
    fanart_path = _prompt_optional_existing_file(
        "Arrière-plan panoramique local JPEG, PNG ou WebP"
    )
    return MovieMetadata(
        title=title,
        year=year,
        summary=summary,
        genres=genres,
        poster_path=poster_path,
        fanart_path=fanart_path,
    )


def _identify_movie(
    *,
    query: str,
    open_browser: bool,
    alert: bool,
) -> MovieMetadata | None:
    """Ouvre une recherche puis fait confirmer la fiche choisie par l'utilisateur."""

    search_url = movie_search_url(query)
    _print_subheading("Informations du film")
    print(f"  Recherche TMDB : {search_url}")
    if open_browser:
        if webbrowser.open(search_url):
            print("  La recherche est ouverte dans le navigateur.")
        else:
            print("  Ouvre ce lien dans ton navigateur.")
    _alert_user(alert)

    client = MetadataClient()
    url = None
    while True:
        if url is None:
            url = _read_answer(
                "Colle le lien de la bonne fiche TMDB (Entrée pour ignorer) : "
            )
            if not url:
                print("Métadonnées ignorées.")
                return None
        try:
            metadata = client.fetch(url)
        except MovieError as error:
            print(f"Lien inutilisable : {error}")
            url = None
            continue

        year = f" ({metadata.year})" if metadata.year else ""
        print(f"  Film    : {metadata.title}{year}")
        if metadata.genres:
            print(f"  Genres  : {', '.join(metadata.genres)}")
        if metadata.summary:
            print(f"  Résumé  : {metadata.summary}")
        if _prompt_yes_no("Utiliser cette fiche ?", default=True):
            return metadata
        url = None


def _resolve_drive(
    drives: Sequence[Drive],
    *,
    requested: int | None,
    configured: int | None,
    alert: bool,
) -> int:
    """Applique la priorité option, configuration, puis choix interactif."""

    available = {drive.index for drive in drives if drive.name or drive.device_path}
    if requested is not None:
        if requested not in available:
            raise MovieError(f"Le lecteur {requested} n'est pas disponible.")
        return requested
    if configured is not None and configured in available:
        return configured
    if configured is not None:
        print(f"Le lecteur configuré [{configured}] n'est pas disponible.")
    if len(available) > 1:
        _alert_user(alert)
    return _prompt_for_drive(drives)


def _resolve_drive_for_operation(
    client: Any,
    *,
    requested: int | None,
    configured: int | None,
    alert: bool,
) -> tuple[int, tuple[Drive, ...]]:
    """Évite un inventaire MakeMKV lorsque le lecteur est déjà connu."""

    selected = requested if requested is not None else configured
    if selected is not None:
        if selected < 0:
            raise MovieError("Le numéro du lecteur ne peut pas être négatif.")
        return selected, ()
    drives = tuple(client.drives())
    return (
        _resolve_drive(
            drives,
            requested=None,
            configured=None,
            alert=alert,
        ),
        drives,
    )


def _resolve_output_directory(
    *,
    configured: Path | None,
    fallback: Path | None,
    required_bytes: int,
    alert: bool,
) -> Path:
    """Utilise la préférence si possible, sinon demande un repli temporaire."""

    candidate = configured or fallback
    if candidate is not None:
        issue = output_directory_issue(
            candidate,
            required_bytes=required_bytes,
            must_exist=configured is not None,
        )
        if issue is None:
            return candidate.expanduser().resolve()
        print(f"⚠ Destination indisponible : {issue}.")
        if configured is not None:
            print(
                "  Le dossier enregistré reste inchangé ; "
                "le prochain choix servira uniquement à cette opération."
            )

    _alert_user(alert)
    while True:
        replacement = _prompt_path("Nouveau dossier de destination")
        issue = output_directory_issue(
            replacement,
            required_bytes=required_bytes,
            must_exist=False,
        )
        if issue is None:
            return replacement.expanduser().resolve()
        print(f"⚠ Destination inutilisable : {issue}.")
        _alert_user(alert)


def _resolve_conversion_settings(
    config: MovieConfig,
) -> tuple[OutputFormat, OutputQuality]:
    selected_format = OutputFormat(
        _prompt_choice(
            "Format de conversion",
            choices=output_format_choices(),
            default=config.convert_format.value,
        )
    )

    if output_spec(selected_format).copies_source:
        selected_quality = OutputQuality.SOURCE
    elif config.auto_run:
        selected_quality = (
            config.convert_quality
            if config.convert_format is selected_format
            and config.convert_quality is not OutputQuality.SOURCE
            else OutputQuality.BALANCED
        )
    else:
        default = (
            config.convert_quality
            if config.convert_format is selected_format
            and config.convert_quality is not OutputQuality.SOURCE
            else OutputQuality.BALANCED
        )
        selected_quality = _prompt_conversion_quality(selected_format, default=default)
    return conversion_settings(selected_format, selected_quality)


def _prompt_conversion_quality(
    output_format: OutputFormat,
    *,
    default: OutputQuality,
) -> OutputQuality:
    choices = {
        "max": "meilleure qualité, fichier plus grand",
        "equilibre": "recommandé pour la plupart des usages",
        "compact": "fichier plus petit",
    }
    mapping = {
        "max": OutputQuality.HIGH,
        "equilibre": OutputQuality.BALANCED,
        "compact": OutputQuality.COMPACT,
    }
    reverse = {quality: label for label, quality in mapping.items()}
    default_label = reverse.get(default, "equilibre")
    if default_label not in choices:
        default_label = "equilibre"
    selected = _prompt_choice(
        "Priorité",
        choices=choices,
        default=default_label,
    )
    return mapping[selected]


def _output_description(
    output_format: OutputFormat,
    output_quality: OutputQuality,
    source_media: ProbedMedia | None = None,
) -> str:
    spec = output_spec(output_format)
    if spec.copies_source:
        return "MKV · qualité source · toutes les pistes"
    quality_labels = {
        OutputQuality.HIGH: "haute qualité",
        OutputQuality.BALANCED: "équilibré",
        OutputQuality.COMPACT: "compact",
    }
    profile = quality_labels.get(output_quality, output_quality.value)
    if copies_video_without_reencoding(source_media, output_format):
        return (
            f"{spec.label} · vidéo H.264 copiée sans réencodage · "
            f"audio AAC, profil {profile}"
        )
    return f"{spec.label} · {spec.codec_description} · profil {profile}"


def _service() -> RipService:
    return RipService(
        MakeMkvClient(),
        MediaProbe(),
    )


def _conversion_service() -> ConversionService:
    return ConversionService(
        MakeMkvClient(),
        MediaProbe(),
        MediaConverter(),
    )


def _tag_service() -> TagService:
    return TagService(
        MediaProbe(),
        MediaTagger(),
    )


def _tool_status(check: Callable[[], str]) -> str:
    try:
        return str(check())
    except MovieError:
        return "introuvable"


if __name__ == "__main__":
    raise SystemExit(main())

"""Tests des interactions de l'interface en ligne de commande."""

from __future__ import annotations

import os
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

from movie.__main__ import (
    _doctor,
    _drives,
    _identify_movie,
    _prompt_choice,
    _prompt_for_drive,
    _prompt_for_title,
    _prompt_optional_path,
    _prompt_path,
    _prompt_yes_no,
    _resolve_output_directory,
    main,
)
from movie.config import MovieConfig, load_config
from movie.core.models import (
    ConvertResult,
    DiscScan,
    DiscTitle,
    Drive,
    MediaStream,
    MovieError,
    MovieMetadata,
    OutputFormat,
    OutputQuality,
    ProbedMedia,
    RipResult,
    TagResult,
)


def _candidates() -> tuple[DiscTitle, ...]:
    return (
        DiscTitle(2, "Version A", 6_000, 20, None, None, "A.mkv", 3),
        DiscTitle(7, "Version B", 6_000, 20, None, None, "B.mkv", 3),
    )


class TitlePromptTests(TestCase):
    @patch("builtins.input", return_value="7")
    def test_returns_selected_title_id(self, _input: object) -> None:
        self.assertEqual(_prompt_for_title(_candidates()), 7)

    @patch("builtins.input", side_effect=["inconnu", "3", "2"])
    def test_repeats_until_choice_is_valid(self, _input: object) -> None:
        self.assertEqual(_prompt_for_title(_candidates()), 2)

    @patch("builtins.input", side_effect=EOFError)
    def test_reports_cancelled_choice(self, _input: object) -> None:
        with self.assertRaisesRegex(MovieError, "annulé"):
            _prompt_for_title(_candidates())


class InteractivePromptTests(TestCase):
    @patch("builtins.input", return_value="")
    def test_long_choice_list_is_rendered_on_separate_lines(
        self,
        _input: object,
    ) -> None:
        output = StringIO()
        choices = {str(index): f"Format {index}" for index in range(5)}

        with redirect_stdout(output):
            selected = _prompt_choice("Format", choices=choices, default="2")

        self.assertEqual(selected, "2")
        self.assertIn("  2", output.getvalue())
        self.assertIn("(par défaut)", output.getvalue())

    def test_single_drive_is_selected_without_question(self) -> None:
        drives = (Drive(4, 2, 1, 0, "Lecteur USB", "FILM", "/dev/disk4"),)
        with patch("builtins.input") as prompt:
            self.assertEqual(_prompt_for_drive(drives), 4)
        prompt.assert_not_called()

    @patch("builtins.input", side_effect=["99", "3"])
    def test_drive_question_repeats_until_valid(self, _input: object) -> None:
        drives = (
            Drive(1, 2, 1, 0, "Lecteur A", None, "/dev/disk1"),
            Drive(3, 2, 1, 0, "Lecteur B", "FILM", "/dev/disk3"),
        )
        self.assertEqual(_prompt_for_drive(drives), 3)

    @patch("builtins.input", return_value="")
    def test_path_uses_configured_default(self, _input: object) -> None:
        self.assertEqual(
            _prompt_path("Destination", default="~/Films"),
            Path("~/Films").expanduser(),
        )

    @patch("builtins.input", return_value=r"/tmp/Mon\ Film/video.mkv")
    def test_path_accepts_terminal_drag_and_drop_escaping(self, _input: object) -> None:
        self.assertEqual(
            _prompt_path("Source"),
            Path("/tmp/Mon Film/video.mkv"),
        )

    @patch("builtins.input", return_value="'/tmp/Mon Film/video.mkv'")
    def test_path_accepts_surrounding_quotes(self, _input: object) -> None:
        self.assertEqual(
            _prompt_path("Source"),
            Path("/tmp/Mon Film/video.mkv"),
        )

    @patch("builtins.input", return_value="oui")
    def test_confirmation_accepts_french_yes(self, _input: object) -> None:
        self.assertTrue(_prompt_yes_no("Continuer ?"))

    @patch("builtins.input", return_value="-")
    def test_optional_path_can_be_cleared(self, _input: object) -> None:
        self.assertIsNone(_prompt_optional_path("Destination", default="~/Films"))

    @patch("movie.__main__.output_directory_issue", side_effect=["déconnecté", None])
    @patch("builtins.input", return_value="/tmp/export-temporaire")
    def test_unavailable_configured_destination_uses_a_temporary_choice(
        self,
        _input: object,
        check_directory: MagicMock,
    ) -> None:
        output = StringIO()

        with redirect_stdout(output):
            selected = _resolve_output_directory(
                configured=Path("/Volumes/absent/Films"),
                fallback=None,
                required_bytes=1_000,
                alert=False,
            )

        self.assertEqual(selected, Path("/tmp/export-temporaire").resolve())
        self.assertTrue(check_directory.call_args_list[0].kwargs["must_exist"])
        self.assertFalse(check_directory.call_args_list[1].kwargs["must_exist"])
        self.assertIn("reste inchangé", output.getvalue())


class DoctorTests(TestCase):
    @patch("movie.__main__.shutil.which", return_value=None)
    @patch("movie.__main__.find_makemkvcon", side_effect=MovieError("absent"))
    def test_missing_tools_show_installation_help(
        self, _makemkv: object, _which: object
    ) -> None:
        output = StringIO()

        with redirect_stdout(output):
            result = _doctor()

        self.assertEqual(result, 2)
        self.assertIn("https://www.makemkv.com/download/", output.getvalue())
        self.assertIn("brew install ffmpeg", output.getvalue())
        self.assertIn("uv run movie doctor", output.getvalue())


class CommandFlowTests(TestCase):
    def test_no_command_displays_the_help(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            result = main([])

        self.assertEqual(result, 0)
        self.assertIn("uv run movie rip", output.getvalue())
        self.assertIn("inspecte le disque vidéo", output.getvalue())

    @patch("movie.__main__._doctor", side_effect=MovieError("outil cassé"))
    def test_expected_error_is_short_and_has_exit_code_two(
        self, _doctor: object
    ) -> None:
        errors = StringIO()
        with redirect_stderr(errors):
            result = main(["doctor"])

        self.assertEqual(result, 2)
        self.assertEqual(errors.getvalue(), "✗ outil cassé\n")

    @patch("movie.__main__._doctor", side_effect=KeyboardInterrupt)
    def test_keyboard_interrupt_has_no_traceback(self, _doctor: object) -> None:
        errors = StringIO()
        with redirect_stderr(errors):
            result = main(["doctor"])

        self.assertEqual(result, 130)
        self.assertIn("Aucun résultat incomplet", errors.getvalue())

    @patch("movie.__main__.MakeMkvClient")
    def test_drives_command_has_a_readable_empty_state(
        self, client_class: MagicMock
    ) -> None:
        client_class.return_value.drives.return_value = ()
        output = StringIO()

        with redirect_stdout(output):
            result = _drives()

        self.assertEqual(result, 0)
        self.assertIn("Aucun lecteur optique", output.getvalue())

    def test_config_command_saves_every_interactive_value(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_file = root / "config.json"
            output_directory = root / "Films"
            answers = [
                str(output_directory),
                "2",
                "oui",
                "non",
                "4,5",
                "non",
                "mp4",
                "max",
            ]
            output = StringIO()
            with (
                patch.dict(os.environ, {"MOVIE_CONFIG_FILE": str(config_file)}),
                patch("builtins.input", side_effect=answers),
                redirect_stdout(output),
            ):
                result = main(["config"])

            self.assertEqual(result, 0)
            saved = load_config(config_file)
            self.assertEqual(saved.output_directory, output_directory)
            self.assertEqual(saved.drive_index, 2)
            self.assertTrue(saved.auto_run)
            self.assertFalse(saved.alert_sound)
            self.assertEqual(saved.progress_delay_seconds, 4.5)
            self.assertFalse(saved.open_browser)
            self.assertIs(saved.convert_format, OutputFormat.MP4)
            self.assertIs(saved.convert_quality, OutputQuality.HIGH)
            self.assertIn("Configuration enregistrée", output.getvalue())

    @patch("movie.__main__.load_config")
    @patch("movie.__main__.MakeMkvClient")
    def test_scan_command_prints_one_clear_summary(
        self, client_class: MagicMock, load: MagicMock
    ) -> None:
        scan = _disc_scan()
        load.return_value = MovieConfig(progress_delay_seconds=60)
        client_class.return_value.drives.return_value = (scan.drive,)
        client_class.return_value.scan.return_value = scan
        output = StringIO()

        with redirect_stdout(output):
            result = main(["scan"])

        rendered = output.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("Movie · Analyse du disque", rendered)
        self.assertIn("✓ Analyse du disque terminée", rendered)
        self.assertIn("Contenu détecté", rendered)
        self.assertIn("uniquement informative", rendered)

    @patch("movie.__main__.load_config")
    @patch("movie.__main__._service")
    def test_rip_command_runs_the_three_user_facing_phases(
        self, service_factory: MagicMock, load: MagicMock
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            scan = _disc_scan(disc_type="Blu-ray disc")
            service = service_factory.return_value
            service.makemkv.drives.return_value = (scan.drive,)
            service.scan.return_value = scan
            output_path = Path(temporary_directory) / "film.mkv"
            service.execute.return_value = RipResult(
                output=output_path,
                media=ProbedMedia(
                    output_path,
                    120,
                    ("video", "audio"),
                    2,
                ),
            )
            load.return_value = MovieConfig(
                output_directory=Path(temporary_directory),
                auto_run=True,
                progress_delay_seconds=60,
            )
            output = StringIO()

            with redirect_stdout(output):
                result = main(["rip"])

            rendered = output.getvalue()
            self.assertEqual(result, 0)
            self.assertIn("[1/3] Analyse du disque", rendered)
            self.assertIn("Support reconnu : Blu-ray disc", rendered)
            self.assertIn("[2/3] Choix du film", rendered)
            self.assertIn("[3/3] Création et vérification", rendered)
            self.assertIn("✓ Film créé et vérifié", rendered)
            service.execute.assert_called_once()

    @patch("movie.__main__.load_config")
    @patch("movie.__main__._conversion_service")
    def test_convert_command_runs_a_separate_verified_pipeline(
        self, service_factory: MagicMock, load: MagicMock
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source = directory / "source.mkv"
            source.write_bytes(b"source")
            destination = directory / "source.mp4"
            source_media = ProbedMedia(
                source,
                120,
                ("video", "audio"),
                2,
                streams=(
                    MediaStream("video", codec="mpeg2video"),
                    MediaStream("audio", "fra", "ac3"),
                ),
            )
            service = service_factory.return_value
            service.probe.probe.return_value = source_media
            service.execute.return_value = ConvertResult(
                destination,
                ProbedMedia(
                    destination,
                    120,
                    ("video", "audio"),
                    2,
                    streams=(
                        MediaStream("video", codec="h264"),
                        MediaStream("audio", "fra", "aac"),
                    ),
                ),
            )
            load.return_value = MovieConfig(
                output_directory=directory,
                auto_run=True,
                alert_sound=False,
                convert_format=OutputFormat.MP4,
                convert_quality=OutputQuality.BALANCED,
                progress_delay_seconds=60,
            )
            output = StringIO()

            with (
                patch("builtins.input", return_value=str(source)),
                redirect_stdout(output),
            ):
                result = main(["convert"])

            rendered = output.getvalue()
            self.assertEqual(result, 0)
            self.assertIn("Movie · Conversion d'un média", rendered)
            self.assertIn("[1/3] Analyse de la source", rendered)
            self.assertIn("[3/3] Conversion et vérification", rendered)
            self.assertIn("✓ Média converti et vérifié", rendered)
            service.probe.probe.assert_called_once_with(source.resolve())
            service.execute.assert_called_once()
            plan = service.execute.call_args.args[0]
            self.assertIs(plan.source_media, source_media)

    @patch("movie.__main__.load_config")
    @patch("movie.__main__._conversion_service")
    def test_audio_only_source_is_refused_before_video_output_recap(
        self, service_factory: MagicMock, load: MagicMock
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "audio-only.mkv"
            source.write_bytes(b"source")
            service = service_factory.return_value
            service.probe.probe.return_value = ProbedMedia(
                source,
                120,
                ("audio",),
                0,
                streams=(MediaStream("audio", "fra", "aac"),),
            )
            load.return_value = MovieConfig(
                output_directory=Path(temporary_directory),
                auto_run=True,
                alert_sound=False,
                convert_format=OutputFormat.M4V,
                convert_quality=OutputQuality.BALANCED,
            )
            output = StringIO()
            errors = StringIO()

            with (
                patch("builtins.input", return_value=str(source)),
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                result = main(["convert"])

            self.assertEqual(result, 2)
            self.assertIn("nécessite une piste vidéo", errors.getvalue())
            self.assertNotIn("Récapitulatif", output.getvalue())
            service.execute.assert_not_called()

    def test_operational_commands_do_not_expose_technical_arguments(self) -> None:
        with self.assertRaises(SystemExit):
            main(["convert", "film.mkv", "--profile", "high"])

    @patch("movie.__main__.load_config")
    @patch("movie.__main__._tag_service")
    def test_tag_command_supports_manual_information(
        self, service_factory: MagicMock, load: MagicMock
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "vacances.mkv"
            source.write_bytes(b"source")
            media = ProbedMedia(
                source,
                120,
                ("video", "audio"),
                0,
                streams=(MediaStream("video"), MediaStream("audio")),
            )
            service_factory.return_value.execute.return_value = TagResult(source, media)
            load.return_value = MovieConfig(auto_run=True, alert_sound=False)
            output = StringIO()
            answers = [
                str(source),
                "manuel",
                "Vacances en Bretagne",
                "2024",
                "Film familial",
                "Famille, Voyage",
                "",
                "",
            ]

            with (
                patch("builtins.input", side_effect=answers),
                redirect_stdout(output),
            ):
                result = main(["tag"])

            self.assertEqual(result, 0)
            plan = service_factory.return_value.execute.call_args.args[0]
            self.assertEqual(plan.metadata.title, "Vacances en Bretagne")
            self.assertEqual(plan.metadata.genres, ("Famille", "Voyage"))
            self.assertEqual(plan.output.name, "vacances_en_bretagne.mkv")
            self.assertIn("Métadonnées écrites et vérifiées", output.getvalue())


class MovieIdentificationTests(TestCase):
    @patch("movie.__main__.webbrowser.open", return_value=True)
    @patch("movie.__main__.MetadataClient")
    @patch(
        "builtins.input",
        side_effect=["https://www.themoviedb.org/movie/1-film", ""],
    )
    def test_browser_link_flow_returns_confirmed_metadata(
        self,
        _input: object,
        client_class: MagicMock,
        browser_open: MagicMock,
    ) -> None:
        expected = MovieMetadata(
            title="Mon Film",
            year=2024,
            summary=None,
            genres=(),
            source_url="https://www.themoviedb.org/movie/1-film",
            poster_url=None,
        )
        client_class.return_value.fetch.return_value = expected

        result = _identify_movie(
            query="MON_FILM",
            open_browser=True,
            alert=False,
        )

        self.assertEqual(result, expected)
        browser_open.assert_called_once()


def _disc_scan(*, disc_type: str = "DVD disc") -> DiscScan:
    drive = Drive(0, 2, 999, 12, "Lecteur USB", "MON_FILM", "/dev/rdisk4")
    return DiscScan(
        drive=drive,
        disc_type=disc_type,
        titles=(
            DiscTitle(
                1,
                "Film",
                120,
                2,
                None,
                None,
                "film.mkv",
                2,
            ),
        ),
    )

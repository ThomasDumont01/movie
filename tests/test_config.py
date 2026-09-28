"""Tests de lecture et d'écriture de la configuration utilisateur."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from movie.config import MovieConfig, load_config, save_config
from movie.core.models import MovieError, OutputFormat, OutputQuality


class ConfigTests(TestCase):
    def test_missing_file_returns_empty_config(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            self.assertEqual(load_config(path), MovieConfig())

    def test_config_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            expected = MovieConfig(
                output_directory=Path("/Films"),
                drive_index=2,
                auto_run=True,
                alert_sound=False,
                progress_delay_seconds=8.5,
                open_browser=False,
                convert_format=OutputFormat.M4A,
                convert_quality=OutputQuality.LOSSLESS,
            )
            self.assertEqual(save_config(expected, path), path)
            self.assertEqual(load_config(path), expected)

    def test_invalid_json_is_reported(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text("pas du json", encoding="utf-8")
            with self.assertRaisesRegex(MovieError, "Configuration illisible"):
                load_config(path)

    def test_invalid_progress_delay_is_reported(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text('{"progress_delay_seconds": 90}', encoding="utf-8")
            with self.assertRaisesRegex(MovieError, "compris entre 0 et 60"):
                load_config(path)

    def test_invalid_conversion_profile_is_reported(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(
                '{"convert_format": "mkv", "convert_quality": "compact"}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(MovieError, "MKV"):
                load_config(path)

        with (
            TemporaryDirectory() as temporary_directory,
            self.assertRaisesRegex(MovieError, "MP4"),
        ):
            save_config(
                MovieConfig(
                    convert_format=OutputFormat.MP4,
                    convert_quality=OutputQuality.SOURCE,
                ),
                Path(temporary_directory) / "config.json",
            )

    def test_version_03_output_keys_are_migrated(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(
                '{"output_format": "mp4", "output_quality": "high"}',
                encoding="utf-8",
            )

            loaded = load_config(path)

            self.assertIs(loaded.convert_format, OutputFormat.MP4)
            self.assertIs(loaded.convert_quality, OutputQuality.HIGH)

    @patch("movie.config.Path.mkdir", side_effect=PermissionError)
    def test_unwritable_config_directory_is_reported(self, _mkdir: object) -> None:
        with self.assertRaisesRegex(MovieError, "Impossible d'enregistrer"):
            save_config(MovieConfig(), Path("/dossier-interdit/config.json"))

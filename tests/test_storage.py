"""Tests des estimations et contrôles de destination partagés."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

from movie.core.models import DiscTitle, OutputFormat
from movie.core.storage import (
    PUBLICATION_MARGIN_BYTES,
    conversion_required_bytes,
    output_directory_issue,
)


class StorageTests(TestCase):
    def test_local_conversion_counts_only_the_new_output(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "film.mkv"
            source.write_bytes(b"x" * 100)

            required = conversion_required_bytes(
                source,
                output_format=OutputFormat.MP4,
                iso_title=None,
            )

        self.assertEqual(required, 100 + PUBLICATION_MARGIN_BYTES)

    def test_iso_transcode_counts_intermediate_mkv_and_final_output(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "film.iso"
            source.write_bytes(b"iso")
            title = DiscTitle(0, "Film", 120, 2, 1_000, None, "film.mkv", 2)

            required = conversion_required_bytes(
                source,
                output_format=OutputFormat.MP4,
                iso_title=title,
            )

        self.assertEqual(required, 2_000 + PUBLICATION_MARGIN_BYTES)

    @patch("movie.core.storage.shutil.disk_usage")
    def test_insufficient_space_is_reported_as_an_actionable_issue(
        self,
        disk_usage: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            disk_usage.return_value.free = 20

            issue = output_directory_issue(
                Path(temporary_directory),
                required_bytes=100,
                must_exist=True,
            )

        self.assertIsNotNone(issue)
        self.assertIn("espace insuffisant", issue or "")

"""Tests des garanties communes de publication."""

from __future__ import annotations

import errno
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from movie.core.models import MovieError, OutputExistsError
from movie.core.workflow import publish_without_overwrite


class PublicationTests(TestCase):
    def test_verified_file_is_published_without_overwrite(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            staged = directory / "staged.mkv"
            destination = directory / "film.mkv"
            staged.write_bytes(b"verified")

            publish_without_overwrite(staged, destination)

            self.assertEqual(destination.read_bytes(), b"verified")

    def test_existing_destination_is_never_overwritten(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            staged = directory / "staged.mkv"
            destination = directory / "film.mkv"
            staged.write_bytes(b"verified")
            destination.write_bytes(b"existing")

            with self.assertRaisesRegex(OutputExistsError, "Rien n'a été écrasé"):
                publish_without_overwrite(staged, destination)

            self.assertEqual(staged.read_bytes(), b"verified")
            self.assertEqual(destination.read_bytes(), b"existing")

    def test_unsupported_hard_link_uses_reserved_atomic_replace(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            staged = directory / "staged.mkv"
            destination = directory / "film.mkv"
            staged.write_bytes(b"verified")

            with (
                patch("movie.core.workflow.sys.platform", "other"),
                patch(
                    "movie.core.workflow.os.link",
                    side_effect=OSError(errno.ENOTSUP, "unsupported"),
                ),
            ):
                publish_without_overwrite(staged, destination)

            self.assertFalse(staged.exists())
            self.assertEqual(destination.read_bytes(), b"verified")

    def test_failed_reserved_replace_removes_only_its_placeholder(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            staged = directory / "staged.mkv"
            destination = directory / "film.mkv"
            staged.write_bytes(b"verified")

            with (
                patch("movie.core.workflow.sys.platform", "other"),
                patch(
                    "movie.core.workflow.os.link",
                    side_effect=OSError(errno.ENOTSUP, "unsupported"),
                ),
                patch(
                    "movie.core.workflow.os.replace",
                    side_effect=PermissionError("denied"),
                ),
                self.assertRaisesRegex(MovieError, "Publication sûre impossible"),
            ):
                publish_without_overwrite(staged, destination)

            self.assertTrue(staged.is_file())
            self.assertFalse(destination.exists())

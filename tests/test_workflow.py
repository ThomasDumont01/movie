"""Tests des garanties communes de publication."""

from __future__ import annotations

import errno
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from movie.core.models import MovieError, OutputExistsError
from movie.core.workflow import (
    publish_without_overwrite,
    rename_without_overwrite,
    replace_with_verified_file,
)


class PublicationTests(TestCase):
    def test_direct_rename_moves_the_file_without_copying_it(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source = directory / "source.mkv"
            destination = directory / "film.mkv"
            source.write_bytes(b"same file")
            original_inode = source.stat().st_ino

            rename_without_overwrite(source, destination)

            self.assertFalse(source.exists())
            self.assertEqual(destination.read_bytes(), b"same file")
            self.assertEqual(destination.stat().st_ino, original_inode)

    def test_direct_rename_never_overwrites_an_existing_file(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source = directory / "source.mkv"
            destination = directory / "film.mkv"
            source.write_bytes(b"source")
            destination.write_bytes(b"existing")

            with self.assertRaisesRegex(OutputExistsError, "Rien n'a été écrasé"):
                rename_without_overwrite(source, destination)

            self.assertEqual(source.read_bytes(), b"source")
            self.assertEqual(destination.read_bytes(), b"existing")

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

    @patch("movie.core.workflow._same_filesystem", return_value=False)
    def test_cross_volume_file_is_copied_then_atomically_published(
        self,
        _same_filesystem: object,
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source = directory / "source" / "staged.mkv"
            destination = directory / "destination" / "film.mkv"
            source.parent.mkdir()
            destination.parent.mkdir()
            source.write_bytes(b"verified" * 1_024)
            progress: list[float] = []

            publish_without_overwrite(
                source,
                destination,
                on_copy_progress=progress.append,
            )

            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertEqual(progress[-1], 1.0)
            self.assertEqual(
                list(destination.parent.glob(".*.movie-copy")),
                [],
            )

    @patch("movie.core.workflow._same_filesystem", return_value=False)
    def test_cross_volume_replacement_is_copied_then_atomic(
        self,
        _same_filesystem: object,
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source = directory / "staged.mkv"
            destination = directory / "destination" / "film.mkv"
            destination.parent.mkdir()
            source.write_bytes(b"verified")
            destination.write_bytes(b"original")
            progress: list[float] = []

            replace_with_verified_file(
                source,
                destination,
                on_copy_progress=progress.append,
            )

            self.assertEqual(destination.read_bytes(), b"verified")
            self.assertEqual(source.read_bytes(), b"verified")
            self.assertEqual(progress[-1], 1.0)
            self.assertEqual(list(destination.parent.glob(".*.movie-copy")), [])

    @patch("movie.core.workflow._same_filesystem", return_value=False)
    @patch("movie.core.workflow.os.replace", side_effect=PermissionError("denied"))
    def test_cross_volume_replacement_failure_preserves_original(
        self,
        _replace: object,
        _same_filesystem: object,
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source = directory / "staged.mkv"
            destination = directory / "destination" / "film.mkv"
            destination.parent.mkdir()
            source.write_bytes(b"verified")
            destination.write_bytes(b"original")

            with self.assertRaisesRegex(MovieError, "remplacer le fichier"):
                replace_with_verified_file(source, destination)

            self.assertEqual(source.read_bytes(), b"verified")
            self.assertEqual(destination.read_bytes(), b"original")
            self.assertEqual(list(destination.parent.glob(".*.movie-copy")), [])

    @patch("movie.core.workflow._same_filesystem", return_value=False)
    @patch(
        "movie.core.workflow._publish_same_filesystem",
        side_effect=PermissionError("denied"),
    )
    def test_cross_volume_publication_failure_keeps_source_and_cleans_copy(
        self,
        _publish: object,
        _same_filesystem: object,
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source = directory / "staged.mkv"
            destination = directory / "destination" / "film.mkv"
            destination.parent.mkdir()
            source.write_bytes(b"verified")

            with self.assertRaisesRegex(MovieError, "copie du fichier vérifié"):
                publish_without_overwrite(source, destination)

            self.assertEqual(source.read_bytes(), b"verified")
            self.assertFalse(destination.exists())
            self.assertEqual(
                list(destination.parent.glob(".*.movie-copy")),
                [],
            )

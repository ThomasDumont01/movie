"""Tests du pipeline autonome d'écriture des métadonnées."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

from movie.core.models import (
    MediaStream,
    MovieError,
    MovieMetadata,
    ProbedMedia,
    ProgressUpdate,
)
from movie.core.tag import TagService, build_tag_plan


class TagPlanTests(TestCase):
    def test_mkv_mp4_and_m4v_are_supported(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            for suffix in (".mkv", ".mp4", ".m4v"):
                with self.subTest(suffix=suffix):
                    source = directory / f"film{suffix}"
                    source.write_bytes(b"source")
                    plan = build_tag_plan(
                        source,
                        MovieMetadata(title="  Mon   Film  ", genres=(" Drame ",)),
                    )
                    self.assertEqual(plan.metadata.title, "Mon Film")
                    self.assertEqual(plan.metadata.genres, ("Drame",))

    def test_unsupported_file_symlink_and_empty_title_are_refused(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            unsupported = directory / "film.avi"
            unsupported.write_bytes(b"source")
            with self.assertRaisesRegex(MovieError, "MKV, MP4 et M4V"):
                build_tag_plan(unsupported, MovieMetadata(title="Film"))

            target = directory / "film.mkv"
            target.write_bytes(b"source")
            link = directory / "lien.mkv"
            link.symlink_to(target)
            with self.assertRaisesRegex(MovieError, "lien symbolique"):
                build_tag_plan(link, MovieMetadata(title="Film"))

            with self.assertRaisesRegex(MovieError, "titre"):
                build_tag_plan(target, MovieMetadata(title="   "))


class TagExecutionTests(TestCase):
    def test_verified_result_replaces_source_and_reports_progress(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "film.mkv"
            source.write_bytes(b"original")
            metadata = _metadata()
            plan = build_tag_plan(source, metadata)
            updates: list[ProgressUpdate] = []

            result = TagService(
                _TagProbe(metadata),
                _FakeTagger(),
            ).execute(plan, on_progress=updates.append)

            self.assertEqual(source.read_bytes(), b"tagged")
            self.assertEqual(result.output, source.resolve())
            self.assertEqual(result.media.path, source.resolve())
            totals = [
                update.total_fraction
                for update in updates
                if update.total_fraction is not None
            ]
            self.assertEqual(totals, sorted(totals))
            self.assertEqual(totals[-1], 1.0)
            self.assertEqual(list(source.parent.glob(".movie-tag-*")), [])

    def test_tagger_or_verification_failure_preserves_original(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "film.mkv"
            source.write_bytes(b"original")
            plan = build_tag_plan(source, _metadata())

            with self.assertRaisesRegex(MovieError, "échec simulé"):
                TagService(_TagProbe(_metadata()), _FailingTagger()).execute(plan)
            self.assertEqual(source.read_bytes(), b"original")

            with self.assertRaisesRegex(MovieError, "titre"):
                TagService(_InvalidTagProbe(), _FakeTagger()).execute(plan)
            self.assertEqual(source.read_bytes(), b"original")

    @patch("movie.core.storage.shutil.disk_usage")
    def test_insufficient_space_stops_before_remux(self, disk_usage: MagicMock) -> None:
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "film.mkv"
            source.write_bytes(b"original")
            disk_usage.return_value.free = 1
            tagger = MagicMock()

            with self.assertRaisesRegex(MovieError, "Espace insuffisant"):
                TagService(_TagProbe(_metadata()), tagger).execute(
                    build_tag_plan(source, _metadata())
                )

            tagger.tag.assert_not_called()
            self.assertEqual(source.read_bytes(), b"original")


def _metadata() -> MovieMetadata:
    return MovieMetadata(
        title="Mon Film",
        year=2024,
        summary="Résumé",
        genres=("Drame", "Famille"),
        source_url="https://www.themoviedb.org/movie/1-film",
        poster_url="https://image.tmdb.org/cover.jpg",
    )


def _source_media(path: Path) -> ProbedMedia:
    return ProbedMedia(
        path,
        120,
        ("video", "audio"),
        2,
        streams=(
            MediaStream("video", codec="h264"),
            MediaStream("audio", "fra", "aac"),
        ),
    )


class _TagProbe:
    def __init__(self, metadata: MovieMetadata) -> None:
        self.metadata = metadata

    def probe(self, path: Path) -> ProbedMedia:
        if path.name.startswith("tagged"):
            tags = (
                ("title", self.metadata.title),
                ("date", str(self.metadata.year)),
                ("description", self.metadata.summary or ""),
                ("genre", ", ".join(self.metadata.genres)),
                ("comment", f"TMDB: {self.metadata.source_url}"),
            )
            media = _source_media(path)
            return ProbedMedia(
                path,
                media.duration_seconds,
                (*media.stream_types, "attachment"),
                media.chapter_count,
                tags,
                (*media.streams, MediaStream("attachment", codec="mjpeg")),
            )
        return _source_media(path)


class _InvalidTagProbe:
    def probe(self, path: Path) -> ProbedMedia:
        return _source_media(path)


class _FakeTagger:
    def tag(
        self,
        source: Path,
        destination: Path,
        metadata: MovieMetadata,
        *,
        source_media: ProbedMedia,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> Path:
        del source, metadata, source_media
        destination.write_bytes(b"tagged")
        if on_progress is not None:
            on_progress(ProgressUpdate("Écriture", None, 1.0, 1.0))
        return destination


class _FailingTagger(_FakeTagger):
    def tag(self, *args: object, **kwargs: object) -> Path:
        del args, kwargs
        raise MovieError("échec simulé")

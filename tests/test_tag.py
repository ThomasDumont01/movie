"""Tests du pipeline autonome d'écriture des métadonnées."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

from movie.core.models import (
    MediaStream,
    MovieError,
    MovieMetadata,
    OutputExistsError,
    ProbedMedia,
    ProgressUpdate,
)
from movie.core.tag import (
    TagService,
    _ensure_free_space,
    _publish_tagged_file,
    build_tag_plan,
)
from movie.formats import taggable_format_names, taggable_suffixes


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
                    self.assertEqual(plan.output.name, f"mon_film{suffix}")

    def test_name_contains_only_normalized_title_without_tmdb_id(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "A1_t00.mkv"
            source.write_bytes(b"source")

            plan = build_tag_plan(
                source,
                MovieMetadata(
                    title="Avengers: Endgame",
                    year=2019,
                    source_url=(
                        "https://www.themoviedb.org/movie/299534-avengers-endgame"
                    ),
                ),
            )

            self.assertEqual(plan.output.name, "avengers_endgame.mkv")
            self.assertEqual(plan.metadata.year, 2019)
            self.assertEqual(
                plan.metadata.source_url,
                "https://www.themoviedb.org/movie/299534-avengers-endgame",
            )

    def test_supported_formats_have_metadata_and_artwork_guarantees(self) -> None:
        self.assertEqual(
            taggable_suffixes(),
            frozenset({".mkv", ".mp4", ".m4v"}),
        )
        self.assertEqual(taggable_format_names(), "MKV, MP4, M4V")

    def test_manual_metadata_uses_safe_readable_filename(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "film.m4v"
            source.write_bytes(b"source")

            plan = build_tag_plan(
                source,
                MovieMetadata(title='Voyage : "Bretagne" / été', year=2024),
            )

            self.assertEqual(plan.output.name, "voyage_bretagne_ete.m4v")

    def test_existing_renamed_destination_is_refused(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source = directory / "film.mkv"
            source.write_bytes(b"source")
            (directory / "mon_film.mkv").write_bytes(b"existing")

            with self.assertRaisesRegex(OutputExistsError, "existe déjà"):
                build_tag_plan(
                    source,
                    MovieMetadata(title="Mon Film", year=2024),
                )

    def test_long_title_stays_within_filename_limit(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "film.mkv"
            source.write_bytes(b"source")

            plan = build_tag_plan(
                source,
                MovieMetadata(
                    title="É" * 300,
                    year=2024,
                    source_url="https://www.themoviedb.org/movie/123-film",
                ),
            )

            self.assertLessEqual(len(plan.output.name.encode("utf-8")), 240)
            self.assertTrue(plan.output.name.endswith(".mkv"))

    def test_source_url_is_never_used_in_filename(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "film.mkv"
            source.write_bytes(b"source")

            plan = build_tag_plan(
                source,
                MovieMetadata(
                    title="Mon Film",
                    source_url="https://example.com/movie/123-film",
                ),
            )

            self.assertEqual(plan.output.name, "mon_film.mkv")

    def test_unsupported_file_symlink_and_empty_title_are_refused(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            unsupported = directory / "film.avi"
            unsupported.write_bytes(b"source")
            with self.assertRaisesRegex(MovieError, "MKV, MP4, M4V"):
                build_tag_plan(unsupported, MovieMetadata(title="Film"))

            target = directory / "film.mkv"
            target.write_bytes(b"source")
            link = directory / "lien.mkv"
            link.symlink_to(target)
            with self.assertRaisesRegex(MovieError, "lien symbolique"):
                build_tag_plan(link, MovieMetadata(title="Film"))

            with self.assertRaisesRegex(MovieError, "titre"):
                build_tag_plan(target, MovieMetadata(title="   "))

    def test_remote_and_local_fanart_cannot_be_combined(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "film.mkv"
            source.write_bytes(b"source")

            with self.assertRaisesRegex(MovieError, "arrière-plan"):
                build_tag_plan(
                    source,
                    MovieMetadata(
                        title="Film",
                        fanart_url="https://image.example/fanart.jpg",
                        fanart_path=source.with_suffix(".jpg"),
                    ),
                )


class TagExecutionTests(TestCase):
    def test_verified_result_renames_source_and_reports_progress(self) -> None:
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

            self.assertFalse(source.exists())
            self.assertEqual(result.output, plan.output)
            self.assertEqual(result.output.read_bytes(), b"tagged")
            self.assertEqual(result.media.path, plan.output)
            totals = [
                update.total_fraction
                for update in updates
                if update.total_fraction is not None
            ]
            self.assertEqual(totals, sorted(totals))
            self.assertEqual(totals[-1], 1.0)
            self.assertEqual(list(source.parent.glob(".movie-tag-*")), [])

    def test_verified_result_is_safely_published_under_normalized_name(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "A1_t00.mkv"
            source.write_bytes(b"original")
            metadata = _metadata()
            plan = build_tag_plan(source, metadata)

            result = TagService(_TagProbe(metadata), _FakeTagger()).execute(plan)

            self.assertFalse(source.exists())
            self.assertEqual(result.output, plan.output)
            self.assertEqual(plan.output.read_bytes(), b"tagged")
            self.assertEqual(result.media.path, plan.output)

    def test_official_fanart_is_integrated_without_sidecar(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source = directory / "A1_t00.mkv"
            source.write_bytes(b"original")
            metadata = MovieMetadata(
                title="Avengers: Endgame",
                year=2019,
                summary="Résumé",
                genres=("Action",),
                source_url="https://www.themoviedb.org/movie/299534-film",
                poster_url="https://image.tmdb.org/poster.jpg",
                fanart_url="https://media.themoviedb.org/t/p/original/fanart.jpg",
            )
            plan = build_tag_plan(source, metadata)

            result = TagService(_TagProbe(metadata), _FakeTagger()).execute(plan)

            self.assertEqual(result.warnings, ())
            self.assertEqual(list(directory.glob("*-fanart.*")), [])

    def test_non_mkv_fanart_limit_is_reported_without_sidecar(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source = directory / "film.mp4"
            source.write_bytes(b"original")
            metadata = replace(
                _metadata(),
                fanart_url="https://media.themoviedb.org/t/p/original/fanart.jpg",
            )

            result = TagService(_TagProbe(metadata), _FakeTagger()).execute(
                build_tag_plan(source, metadata)
            )

            self.assertIn("MKV", result.warnings[0])
            self.assertEqual(list(directory.glob("*-fanart.*")), [])

    def test_missing_embedded_fanart_preserves_the_original(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source = directory / "film.mkv"
            source.write_bytes(b"original")
            metadata = replace(
                _metadata(),
                fanart_url="https://media.themoviedb.org/t/p/original/fanart.jpg",
            )

            with self.assertRaisesRegex(MovieError, "illustrations"):
                TagService(
                    _MissingFanartProbe(metadata),
                    _FakeTagger(),
                ).execute(build_tag_plan(source, metadata))

            self.assertEqual(source.read_bytes(), b"original")

    def test_tagging_uses_local_staging(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source_directory = directory / "nas"
            source_directory.mkdir()
            source = source_directory / "film.mkv"
            source.write_bytes(b"original")
            tagger = MagicMock(wraps=_FakeTagger())

            TagService(_TagProbe(_metadata()), tagger).execute(
                build_tag_plan(source, _metadata())
            )

            staged_output = tagger.tag.call_args.args[1]
            self.assertEqual(
                staged_output.parent.parent, Path(tempfile.gettempdir()).resolve()
            )
            self.assertNotEqual(staged_output.parent.parent, source.parent)

    @patch("movie.core.tag.ensure_free_space")
    def test_tagging_checks_source_and_local_staging_space(
        self,
        check_space: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "film.mkv"
            source.write_bytes(b"original")
            staging_root = MagicMock(spec=Path)
            staging_root.stat.return_value.st_dev = source.parent.stat().st_dev + 1

            _ensure_free_space(source, staging_root)

            self.assertEqual(check_space.call_count, 2)
            self.assertEqual(check_space.call_args_list[0].args[0], source.parent)
            self.assertIs(check_space.call_args_list[1].args[0], staging_root)

    @patch("movie.core.workflow.os.replace", side_effect=PermissionError)
    def test_in_place_publication_failure_preserves_original(
        self, _replace: MagicMock
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "mon_film.mkv"
            source.write_bytes(b"original")
            plan = build_tag_plan(source, _metadata())

            with self.assertRaisesRegex(MovieError, "remplacer le fichier"):
                TagService(_TagProbe(_metadata()), _FakeTagger()).execute(plan)

            self.assertEqual(source.read_bytes(), b"original")

    def test_failed_source_removal_rolls_back_renamed_output(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            staged = directory / "staged.mkv"
            source = directory / "source.mkv"
            output = directory / "output.mkv"
            staged.write_bytes(b"tagged")
            source.write_bytes(b"original")
            original_unlink = Path.unlink
            calls = 0

            def fail_once(path: Path, *, missing_ok: bool = False) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise PermissionError
                original_unlink(path, missing_ok=missing_ok)

            with (
                patch.object(Path, "unlink", autospec=True, side_effect=fail_once),
                self.assertRaisesRegex(MovieError, "original a été conservé"),
            ):
                _publish_tagged_file(staged, source, output)

            self.assertTrue(source.is_file())
            self.assertFalse(output.exists())

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
            artwork_count = int(self.metadata.has_artwork)
            if path.suffix.casefold() == ".mkv":
                artwork_count += int(self.metadata.has_fanart)
            artwork = tuple(
                MediaStream("attachment", codec="mjpeg", is_artwork=True)
                for _ in range(artwork_count)
            )
            return ProbedMedia(
                path,
                media.duration_seconds,
                (*media.stream_types, *("attachment",) * artwork_count),
                media.chapter_count,
                tags,
                (*media.streams, *artwork),
            )
        return _source_media(path)


class _InvalidTagProbe:
    def probe(self, path: Path) -> ProbedMedia:
        return _source_media(path)


class _MissingFanartProbe(_TagProbe):
    def probe(self, path: Path) -> ProbedMedia:
        media = super().probe(path)
        if not path.name.startswith("tagged"):
            return media
        artwork_seen = False
        streams: list[MediaStream] = []
        for stream in media.streams:
            if stream.is_artwork:
                if artwork_seen:
                    continue
                artwork_seen = True
            streams.append(stream)
        stream_types = tuple(stream.kind for stream in streams)
        return replace(media, stream_types=stream_types, streams=tuple(streams))


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

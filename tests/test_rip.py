"""Tests des garde-fous de planification, sans écriture réelle de MKV."""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from movie.core.models import (
    DiscError,
    DiscScan,
    DiscTitle,
    Drive,
    MediaStream,
    MovieError,
    OutputExistsError,
    ProbedMedia,
    ProgressUpdate,
)
from movie.core.rip import (
    RipService,
    _ensure_free_space,
    _single_mkv,
    _validate_media,
    build_rip_plan,
    main_title_candidates,
    select_main_title,
)


def _scan() -> DiscScan:
    return DiscScan(
        drive=Drive(0, 2, 999, 12, "DVD-RW USB", "MON_FILM", "/dev/rdisk4"),
        disc_type="DVD disc",
        titles=(
            DiscTitle(0, "Bonus", 728, 1, None, None, "BONUS_t00.mkv", 2),
            DiscTitle(1, "Film", 6_452, 20, None, None, "MON_FILM_t01.mkv", 3),
        ),
    )


class RipPlanTests(unittest.TestCase):
    def test_longest_title_is_proposed(self) -> None:
        self.assertEqual(select_main_title(_scan()).title_id, 1)

    def test_equal_longest_titles_require_explicit_choice(self) -> None:
        scan = DiscScan(
            drive=_scan().drive,
            disc_type="DVD disc",
            titles=(
                DiscTitle(0, "Film A", 6_000, 20, None, None, "A.mkv", 3),
                DiscTitle(1, "Film B", 6_000, 20, None, None, "B.mkv", 3),
            ),
        )

        self.assertEqual(
            [title.title_id for title in main_title_candidates(scan)], [0, 1]
        )
        with self.assertRaises(DiscError):
            select_main_title(scan)

    def test_equal_titles_remain_ambiguous_when_languages_differ(self) -> None:
        scan = DiscScan(
            drive=_scan().drive,
            disc_type="DVD disc",
            titles=(
                DiscTitle(0, "Version A", 6_000, 20, None, None, "A.mkv", 3, ("aaa",)),
                DiscTitle(1, "Version B", 6_000, 20, None, None, "B.mkv", 3, ("bbb",)),
            ),
        )

        with self.assertRaises(DiscError):
            select_main_title(scan)

    def test_plan_does_not_create_output_directory(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "Films"

            plan = build_rip_plan(_scan(), destination)

            self.assertEqual(plan.title.title_id, 1)
            self.assertEqual(plan.output, (destination / "MON_FILM_t01.mkv").resolve())
            self.assertFalse(destination.exists())

    def test_existing_destination_is_refused(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory)
            (destination / "MON_FILM_t01.mkv").touch()

            with self.assertRaises(OutputExistsError):
                build_rip_plan(_scan(), destination)

    def test_broken_symlink_destination_is_also_refused(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory)
            output = destination / "MON_FILM_t01.mkv"
            output.symlink_to(destination / "cible-absente.mkv")

            with self.assertRaises(OutputExistsError):
                build_rip_plan(_scan(), destination)

    def test_every_video_disc_opened_by_makemkv_is_accepted(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            for disc_type in (
                "DVD disc",
                "Blu-ray disc",
                "UHD Blu-ray disc",
                None,
            ):
                with self.subTest(disc_type=disc_type):
                    scan = replace(_scan(), disc_type=disc_type)
                    plan = build_rip_plan(
                        scan,
                        Path(temporary_directory) / "Films",
                    )
                    self.assertEqual(plan.title.title_id, 1)


class RipExecutionTests(unittest.TestCase):
    def test_verified_mkv_is_published_and_staging_is_cleaned(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "Films"
            plan = build_rip_plan(_scan(), destination)
            service = RipService(_FakeMakeMkv(), _ValidProbe())

            with patch(
                "movie.core.rip.tempfile.gettempdir",
                return_value=temporary_directory,
            ):
                result = service.execute(plan)

            self.assertEqual(result.output, plan.output)
            self.assertEqual(result.media.path, plan.output)
            self.assertEqual(plan.output.read_bytes(), b"mkv")
            self.assertEqual(list(Path(temporary_directory).glob("movie-rip-*")), [])

    def test_makemkv_writes_to_local_staging_before_publication(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "Films"
            plan = build_rip_plan(_scan(), destination)
            backend = _FakeMakeMkv()
            original_rip = backend.rip_title
            backend.rip_title = MagicMock(side_effect=original_rip)  # type: ignore[method-assign]

            RipService(backend, _ValidProbe()).execute(plan)

            staging = backend.rip_title.call_args.args[2]
            self.assertEqual(staging.parent, Path(tempfile.gettempdir()).resolve())
            self.assertNotEqual(staging.parent, destination)

    @patch("movie.core.rip.ensure_free_space")
    def test_space_is_checked_on_destination_and_local_staging(
        self,
        check_space: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            plan = build_rip_plan(_scan(), Path(temporary_directory) / "Films")
            plan = replace(plan, title=replace(plan.title, size_bytes=1_000_000))
            plan.output.parent.mkdir()
            staging_root = MagicMock(spec=Path)
            staging_root.stat.return_value.st_dev = plan.output.parent.stat().st_dev + 1

            _ensure_free_space(plan, staging_root)

            self.assertEqual(check_space.call_count, 2)
            self.assertEqual(check_space.call_args_list[0].args[0], plan.output.parent)
            self.assertIs(check_space.call_args_list[1].args[0], staging_root)

    def test_failed_verification_publishes_nothing(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "Films"
            plan = build_rip_plan(_scan(), destination)
            service = RipService(_FakeMakeMkv(), _InvalidProbe())

            with (
                patch(
                    "movie.core.rip.tempfile.gettempdir",
                    return_value=temporary_directory,
                ),
                self.assertRaisesRegex(Exception, "aucune piste vidéo"),
            ):
                service.execute(plan)

            self.assertFalse(plan.output.exists())
            self.assertEqual(list(Path(temporary_directory).glob("movie-rip-*")), [])

    def test_verified_local_mkv_is_preserved_when_nas_publication_fails(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan = build_rip_plan(_scan(), root / "Films")
            service = RipService(_FakeMakeMkv(), _ValidProbe())

            with (
                patch(
                    "movie.core.rip.tempfile.gettempdir",
                    return_value=temporary_directory,
                ),
                patch(
                    "movie.core.rip._publish_without_overwrite",
                    side_effect=MovieError("NAS indisponible"),
                ),
                self.assertRaisesRegex(
                    MovieError,
                    "MKV local vérifié a été conservé",
                ),
            ):
                service.execute(plan)

            recovery_directories = list(root.glob("movie-rip-*"))
            self.assertEqual(len(recovery_directories), 1)
            self.assertEqual(
                (recovery_directories[0] / "film.mkv").read_bytes(),
                b"mkv",
            )
            self.assertFalse(plan.output.exists())

    def test_progress_is_global_monotone_and_reaches_publication(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            plan = build_rip_plan(_scan(), Path(temporary_directory) / "Films")
            service = RipService(_FakeMakeMkv(), _ValidProbe())
            updates: list[ProgressUpdate] = []

            service.execute(plan, on_progress=updates.append)

            totals = [
                update.total_fraction
                for update in updates
                if update.total_fraction is not None
            ]
            self.assertEqual(totals, sorted(totals))
            self.assertEqual(totals[-1], 1.0)
            self.assertEqual(updates[-1].current_label, "Publication du fichier final")

    def test_non_blocking_validation_warning_is_returned(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            plan = build_rip_plan(_scan(), temporary_directory)

            result = RipService(_FakeMakeMkv(), _ChapterWarningProbe()).execute(plan)

            self.assertEqual(len(result.warnings), 1)
            self.assertIn("19 chapitre", result.warnings[0])

    def test_successful_makemkv_diagnostics_are_returned_to_the_user(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            plan = build_rip_plan(_scan(), temporary_directory)
            backend = _FakeMakeMkv()
            backend.rip_title = MagicMock(
                side_effect=lambda *args, **kwargs: _write_fake_rip(
                    args[2],
                    diagnostics=("AV synchronization issue corrected",),
                )
            )  # type: ignore[method-assign]

            result = RipService(backend, _ValidProbe()).execute(plan)

            self.assertIn("AV synchronization", result.warnings[0])

    @patch("movie.core.storage.shutil.disk_usage")
    def test_insufficient_space_stops_before_extraction(
        self, disk_usage: MagicMock
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            scan = replace(
                _scan(),
                titles=(
                    _scan().titles[0],
                    replace(_scan().titles[1], size_bytes=10_000_000_000),
                ),
            )
            plan = build_rip_plan(scan, Path(temporary_directory) / "Films")
            disk_usage.return_value.free = 1
            backend = _FakeMakeMkv()
            backend.rip_title = MagicMock()  # type: ignore[method-assign]

            with self.assertRaisesRegex(Exception, "Espace insuffisant"):
                RipService(backend, _ValidProbe()).execute(plan)

            backend.rip_title.assert_not_called()

    @patch("movie.core.workflow.Path.mkdir", side_effect=PermissionError)
    def test_unwritable_destination_is_reported(self, _mkdir: object) -> None:
        plan = build_rip_plan(_scan(), "/destination-interdite/Films")

        with self.assertRaisesRegex(Exception, "Impossible de préparer le dossier"):
            RipService(_FakeMakeMkv(), _ValidProbe()).execute(plan)


class MediaValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.title = DiscTitle(
            1,
            "Film",
            120,
            2,
            None,
            None,
            "film.mkv",
            3,
            streams=(
                MediaStream("video"),
                MediaStream("audio", "fra"),
                MediaStream("subtitle", "fra"),
            ),
        )

    def test_video_is_required(self) -> None:
        media = ProbedMedia(Path("x"), 120, ("audio",), 2)
        with self.assertRaisesRegex(MovieError, "aucune piste vidéo"):
            _validate_media(self.title, media)

    def test_missing_subtitle_is_a_warning_not_a_false_failure(self) -> None:
        media = ProbedMedia(
            Path("x"),
            120,
            ("video", "audio"),
            2,
            streams=(
                MediaStream("video"),
                MediaStream("audio", "fra"),
            ),
        )
        warnings = _validate_media(self.title, media)

        self.assertEqual(len(warnings), 1)
        self.assertIn("sous-titre", warnings[0])
        self.assertIn("piste vide", warnings[0])

    def test_audio_announced_by_the_source_is_required(self) -> None:
        media = ProbedMedia(
            Path("x"),
            120,
            ("video",),
            2,
            streams=(MediaStream("video"),),
        )

        with self.assertRaisesRegex(MovieError, "aucune piste audio"):
            _validate_media(self.title, media)

    def test_raw_scan_count_is_not_used_as_an_integrity_rule(self) -> None:
        title = replace(self.title, stream_count=12)
        media = ProbedMedia(
            Path("x"),
            120,
            ("video", "audio"),
            2,
            streams=(MediaStream("video"), MediaStream("audio", "fra")),
        )

        warnings = _validate_media(title, media)

        self.assertIn("sous-titre", warnings[0])

    def test_language_tag_difference_is_reported_without_deleting_the_movie(
        self,
    ) -> None:
        media = ProbedMedia(
            Path("x"),
            120,
            ("video", "audio", "subtitle"),
            2,
            streams=(
                MediaStream("video"),
                MediaStream("audio", "eng"),
                MediaStream("subtitle", "fra"),
            ),
        )
        warnings = _validate_media(self.title, media)

        self.assertIn("audio fra", warnings[0])
        self.assertIn("restent lisibles", warnings[0])

    def test_chapter_difference_warns_but_duration_difference_blocks(self) -> None:
        too_few_chapters = ProbedMedia(
            Path("x"), 120, ("video", "audio", "subtitle"), 1
        )
        warnings = _validate_media(self.title, too_few_chapters)
        self.assertIn("1 chapitre", "\n".join(warnings))

        wrong_duration = ProbedMedia(Path("x"), 150, ("video", "audio", "subtitle"), 2)
        with self.assertRaisesRegex(MovieError, "durée"):
            _validate_media(self.title, wrong_duration)

        slightly_different = replace(
            too_few_chapters,
            duration_seconds=118,
            chapter_count=2,
        )
        warnings = _validate_media(self.title, slightly_different)
        self.assertIn("navigation du support", "\n".join(warnings))

        short_title = replace(self.title, duration_seconds=15)
        truncated_short_media = replace(
            too_few_chapters,
            duration_seconds=5.1,
            chapter_count=2,
        )
        with self.assertRaisesRegex(MovieError, "tronqué"):
            _validate_media(short_title, truncated_short_media)

    def test_unavailable_duration_is_visible_but_not_destructive(self) -> None:
        media = ProbedMedia(
            Path("x"),
            None,
            ("video", "audio", "subtitle"),
            2,
            streams=self.title.streams,
        )

        warnings = _validate_media(self.title, media)

        self.assertIn("pas pu confirmer la durée", "\n".join(warnings))

    def test_staging_requires_exactly_one_non_empty_mkv(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            staging = Path(temporary_directory)
            with self.assertRaisesRegex(MovieError, "0 fichier"):
                _single_mkv(staging)

            empty = staging / "film.mkv"
            empty.touch()
            with self.assertRaisesRegex(MovieError, "vide"):
                _single_mkv(staging)

            empty.write_bytes(b"mkv")
            (staging / "autre.mkv").write_bytes(b"mkv")
            with self.assertRaisesRegex(MovieError, "2 fichier"):
                _single_mkv(staging)


class _FakeMakeMkv:
    def drives(self) -> tuple[Drive, ...]:
        return (_scan().drive,)

    def scan(
        self,
        drive_index: int,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> DiscScan:
        del drive_index, on_progress
        return _scan()

    def rip_title(
        self,
        drive_index: int,
        title_id: int,
        staging_directory: Path,
        *,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> None:
        del drive_index, title_id
        if on_progress is not None:
            on_progress(ProgressUpdate("Extraction", None, 0.5, 0.5))
            on_progress(ProgressUpdate("Extraction", None, 1.0, 1.0))
        (staging_directory / "film.mkv").write_bytes(b"mkv")


class _ValidProbe:
    def probe(self, path: Path) -> ProbedMedia:
        return ProbedMedia(
            path=path,
            duration_seconds=6_452,
            stream_types=("video", "audio", "subtitle"),
            chapter_count=20,
            streams=(
                MediaStream("video"),
                MediaStream("audio"),
                MediaStream("subtitle"),
            ),
        )


class _InvalidProbe:
    def probe(self, path: Path) -> ProbedMedia:
        return ProbedMedia(
            path=path,
            duration_seconds=6_452,
            stream_types=("audio",),
            chapter_count=20,
        )


class _ChapterWarningProbe(_ValidProbe):
    def probe(self, path: Path) -> ProbedMedia:
        return replace(super().probe(path), chapter_count=19)


def _write_fake_rip(
    staging_directory: Path,
    *,
    diagnostics: tuple[str, ...],
) -> SimpleNamespace:
    (staging_directory / "film.mkv").write_bytes(b"mkv")
    return SimpleNamespace(diagnostics=diagnostics)

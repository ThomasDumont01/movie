"""Tests de la pipeline de conversion indépendante de la numérisation DVD."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock

from movie.core.convert import ConversionService, build_convert_plan
from movie.core.models import (
    DiscScan,
    DiscTitle,
    MediaStream,
    MovieError,
    OutputExistsError,
    OutputFormat,
    OutputQuality,
    ProbedMedia,
    ProgressUpdate,
)


class ConvertPlanTests(TestCase):
    def test_default_profiles_and_extensions_are_predictable(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "film.avi"
            source.write_bytes(b"source")

            mp4 = build_convert_plan(source, "mp4")
            m4a = build_convert_plan(source, "m4a", audio_track=0)
            mkv = build_convert_plan(source, "mkv")

            self.assertEqual(mp4.output.name, "film.mp4")
            self.assertIs(mp4.output_quality, OutputQuality.BALANCED)
            self.assertEqual(m4a.output.name, "film.m4a")
            self.assertEqual(mkv.output.name, "film.mkv")
            self.assertIs(mkv.output_quality, OutputQuality.SOURCE)

    def test_same_extension_gets_a_distinct_default_name(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "film.mkv"
            source.write_bytes(b"source")

            plan = build_convert_plan(source, "mkv")

            self.assertEqual(plan.output.name, "film-converted.mkv")

    def test_explicit_destination_extension_and_collision_are_checked(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source = directory / "film.mkv"
            source.write_bytes(b"source")
            with self.assertRaisesRegex(MovieError, "terminer par .mp4"):
                build_convert_plan(source, "mp4", output=directory / "film.mkv")

            destination = directory / "film.mp4"
            destination.write_bytes(b"existing")
            with self.assertRaises(OutputExistsError):
                build_convert_plan(source, "mp4", output=destination)

            broken_link = directory / "broken.mp4"
            broken_link.symlink_to(directory / "missing.mp4")
            with self.assertRaises(OutputExistsError):
                build_convert_plan(source, "mp4", output=broken_link)

    def test_iso_requires_a_title_and_m4a_requires_an_audio_choice(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            iso = directory / "film.iso"
            iso.write_bytes(b"iso")
            media = directory / "film.mkv"
            media.write_bytes(b"mkv")

            with self.assertRaisesRegex(MovieError, "titre"):
                build_convert_plan(iso, "mp4")
            with self.assertRaisesRegex(MovieError, "piste audio"):
                build_convert_plan(media, "m4a")

            plan = build_convert_plan(iso, "m4a", iso_title=_iso_title())
            self.assertIsNone(plan.audio_track)

    def test_invalid_format_profile_pairs_are_refused(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "film.mkv"
            source.write_bytes(b"source")
            with self.assertRaisesRegex(MovieError, "MP4"):
                build_convert_plan(source, "mp4", output_quality="source")
            with self.assertRaisesRegex(MovieError, "MKV"):
                build_convert_plan(source, "mkv", output_quality="compact")


class ConversionExecutionTests(TestCase):
    def test_an_existing_source_analysis_is_reused(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source = directory / "film.mkv"
            source.write_bytes(b"source")
            fake_probe = _FakeProbe()
            inspected = fake_probe.probe(source)
            plan = build_convert_plan(
                source,
                "mp4",
                source_media=inspected,
            )
            probe = MagicMock(wraps=fake_probe)

            ConversionService(
                _FakeMakeMkv(), probe, _FakeConverter()
            ).execute(plan)

            self.assertEqual(probe.probe.call_count, 1)
            self.assertNotEqual(probe.probe.call_args.args[0], source)

    def test_mp4_is_converted_verified_and_published(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source = directory / "film.mkv"
            source.write_bytes(b"source")
            plan = build_convert_plan(source, "mp4")
            updates: list[ProgressUpdate] = []

            result = ConversionService(
                _FakeMakeMkv(), _FakeProbe(), _FakeConverter()
            ).execute(plan, on_progress=updates.append)

            self.assertEqual(result.output.read_bytes(), b"converted")
            self.assertEqual(result.media.streams[0].codec, "h264")
            self.assertIn("sous-titres", result.warnings[0])
            totals = [
                update.total_fraction
                for update in updates
                if update.total_fraction is not None
            ]
            self.assertEqual(totals, sorted(totals))
            self.assertEqual(totals[-1], 1.0)
            self.assertEqual(list(directory.glob(".movie-convert-*")), [])

    def test_m4a_validates_selected_audio_and_reports_omitted_streams(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source = directory / "film.mkv"
            source.write_bytes(b"source")
            plan = build_convert_plan(source, "m4a", audio_track=1)

            result = ConversionService(
                _FakeMakeMkv(), _FakeProbe(), _FakeConverter()
            ).execute(plan)

            self.assertEqual(result.output.suffix, ".m4a")
            self.assertEqual(result.media.streams[0].language, "eng")
            self.assertIn("volontairement exclues", result.warnings[0])

    def test_invalid_audio_track_fails_before_conversion(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source = directory / "film.mkv"
            source.write_bytes(b"source")
            plan = build_convert_plan(source, "m4a", audio_track=8)
            converter = MagicMock()

            with self.assertRaisesRegex(MovieError, "n'existe pas"):
                ConversionService(
                    _FakeMakeMkv(), _FakeProbe(), converter
                ).execute(plan)

            converter.convert.assert_not_called()
            self.assertFalse(plan.output.exists())

    def test_iso_to_mkv_publishes_extraction_without_redundant_remux(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            iso = directory / "film.iso"
            iso.write_bytes(b"iso")
            title = _iso_title()
            plan = build_convert_plan(iso, "mkv", iso_title=title)
            converter = MagicMock()

            result = ConversionService(
                _FakeMakeMkv(), _FakeProbe(), converter
            ).execute(plan)

            converter.convert.assert_not_called()
            self.assertEqual(result.output.read_bytes(), b"extracted")

    def test_iso_to_m4a_selects_from_the_extracted_mkv_tracks(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            iso = directory / "film.iso"
            iso.write_bytes(b"iso")
            plan = build_convert_plan(iso, "m4a", iso_title=_iso_title())
            converter = MagicMock(wraps=_FakeConverter())
            selector = MagicMock(return_value=1)

            result = ConversionService(
                _FakeMakeMkv(), _FakeProbe(), converter
            ).execute(plan, select_audio_track=selector)

            extracted_audios = selector.call_args.args[0]
            self.assertEqual(
                tuple(stream.language for stream in extracted_audios),
                ("fra", "eng"),
            )
            self.assertEqual(converter.convert.call_args.kwargs["audio_track"], 1)
            self.assertEqual(result.media.streams[0].language, "eng")

    def test_conversion_failure_never_publishes_partial_output(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source = directory / "film.mkv"
            source.write_bytes(b"source")
            plan = build_convert_plan(source, "mp4")

            with self.assertRaisesRegex(MovieError, "conversion simulée"):
                ConversionService(
                    _FakeMakeMkv(), _FakeProbe(), _FailingConverter()
                ).execute(plan)

            self.assertFalse(plan.output.exists())


def _iso_title() -> DiscTitle:
    return DiscTitle(
        2,
        "Film",
        120,
        2,
        1_000,
        "VTS_01_1.VOB",
        "film.mkv",
        3,
        streams=(
            MediaStream("video", codec="mpeg2video"),
            MediaStream("audio", "fra", "ac3"),
            MediaStream("subtitle", "fra", "dvd_subtitle"),
        ),
    )


class _FakeMakeMkv:
    def scan_iso(
        self,
        path: Path,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> DiscScan:
        del path, on_progress
        raise AssertionError("scan_iso n'est pas utilisé par ConversionService.execute")

    def rip_iso_title(
        self,
        path: Path,
        title_id: int,
        staging_directory: Path,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> None:
        del path, title_id
        if on_progress is not None:
            on_progress(ProgressUpdate("Extraction ISO", None, 1.0, 1.0))
        (staging_directory / "film.mkv").write_bytes(b"extracted")


class _FakeProbe:
    def probe(self, path: Path) -> ProbedMedia:
        if path.suffix == ".mp4":
            return ProbedMedia(
                path,
                120,
                ("video", "audio", "audio"),
                2,
                streams=(
                    MediaStream("video", codec="h264"),
                    MediaStream("audio", "fra", "aac"),
                    MediaStream("audio", "eng", "aac"),
                ),
            )
        if path.suffix == ".m4a":
            return ProbedMedia(
                path,
                120,
                ("audio",),
                0,
                streams=(MediaStream("audio", "eng", "aac"),),
            )
        return ProbedMedia(
            path,
            120,
            ("video", "audio", "audio", "subtitle"),
            2,
            streams=(
                MediaStream("video", codec="mpeg2video"),
                MediaStream("audio", "fra", "ac3"),
                MediaStream("audio", "eng", "ac3"),
                MediaStream("subtitle", "fra", "dvd_subtitle"),
            ),
        )


class _FakeConverter:
    def convert(
        self,
        source: Path,
        destination: Path,
        *,
        output_format: OutputFormat,
        quality: OutputQuality,
        audio_track: int | None = None,
        duration_seconds: float | None = None,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> Path:
        del source, output_format, quality, audio_track, duration_seconds
        destination.write_bytes(b"converted")
        if on_progress is not None:
            on_progress(ProgressUpdate("Conversion", None, 1.0, 1.0))
        return destination


class _FailingConverter(_FakeConverter):
    def convert(self, *args: object, **kwargs: object) -> Path:
        del args, kwargs
        raise MovieError("Échec de conversion simulée")

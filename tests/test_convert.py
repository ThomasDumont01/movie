"""Tests de la pipeline de conversion indépendante de la numérisation DVD."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

from movie.core.convert import (
    ConversionService,
    _ensure_free_space,
    build_convert_plan,
    discover_video_files,
    execute_folder_conversion,
    folder_output_path,
)
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
from movie.core.verification import validate_conversion
from movie.formats import OUTPUT_FORMAT_SPECS, output_spec


class ConvertPlanTests(TestCase):
    def test_default_profiles_and_extensions_are_predictable(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "film.avi"
            source.write_bytes(b"source")

            mp4 = build_convert_plan(source, "mp4")
            m4v = build_convert_plan(source, "m4v")
            mkv = build_convert_plan(source, "mkv")

            self.assertEqual(mp4.output.name, "film.mp4")
            self.assertIs(mp4.output_quality, OutputQuality.BALANCED)
            self.assertEqual(m4v.output.name, "film.m4v")
            self.assertIs(m4v.output_quality, OutputQuality.BALANCED)
            self.assertEqual(mkv.output.name, "film.mkv")
            self.assertIs(mkv.output_quality, OutputQuality.SOURCE)

            for output_format in OutputFormat:
                with self.subTest(output_format=output_format):
                    plan = build_convert_plan(source, output_format)
                    self.assertEqual(plan.output.suffix, f".{output_format.value}")
                    expected_quality = (
                        OutputQuality.SOURCE
                        if output_spec(output_format).copies_source
                        else OutputQuality.BALANCED
                    )
                    self.assertIs(plan.output_quality, expected_quality)

    def test_every_declared_output_has_a_conversion_rule(self) -> None:
        self.assertEqual(set(OUTPUT_FORMAT_SPECS), set(OutputFormat))
        for output_format, spec in OUTPUT_FORMAT_SPECS.items():
            with self.subTest(output_format=output_format):
                if spec.copies_source:
                    self.assertIsNone(spec.video_codec)
                    self.assertIsNone(spec.audio_codec)
                else:
                    self.assertIsNotNone(spec.video_codec)
                    self.assertIsNotNone(spec.audio_codec)

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

    def test_iso_requires_an_explicit_title_for_every_output(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            iso = directory / "film.iso"
            iso.write_bytes(b"iso")
            with self.assertRaisesRegex(MovieError, "titre"):
                build_convert_plan(iso, "mp4")
            plan = build_convert_plan(iso, "m4v", iso_title=_iso_title())
            self.assertIs(plan.output_format, OutputFormat.M4V)

    def test_invalid_format_profile_pairs_are_refused(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "film.mkv"
            source.write_bytes(b"source")
            with self.assertRaisesRegex(MovieError, "MP4"):
                build_convert_plan(source, "mp4", output_quality="source")
            with self.assertRaisesRegex(MovieError, "MKV"):
                build_convert_plan(source, "mkv", output_quality="compact")
            with self.assertRaisesRegex(MovieError, "M4V"):
                build_convert_plan(source, "m4v", output_quality="source")

    def test_audio_track_selection_is_validated_against_the_source(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "film.m2ts"
            source.write_bytes(b"source")
            media = ProbedMedia(
                source,
                120,
                ("video", "audio", "audio"),
                0,
                streams=(
                    MediaStream("video", codec="h264"),
                    MediaStream("audio", "fra", "eac3"),
                    MediaStream("audio", "qaa", "eac3"),
                ),
            )

            plan = build_convert_plan(
                source,
                "mp4",
                source_media=media,
                audio_track_index=1,
            )

            self.assertEqual(plan.audio_track_index, 1)
            with self.assertRaisesRegex(MovieError, "n'existe pas"):
                build_convert_plan(
                    source,
                    "mp4",
                    source_media=media,
                    audio_track_index=2,
                )

    def test_mkv_language_normalization_is_non_blocking(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source_path = directory / "source.m2ts"
            output_path = directory / "output.mkv"
            source_path.write_bytes(b"source")
            source = ProbedMedia(
                source_path,
                120,
                ("video", "audio", "audio", "audio", "subtitle"),
                0,
                streams=(
                    MediaStream("video", codec="h264"),
                    MediaStream("audio", "fra", "eac3"),
                    MediaStream("audio", "qaa", "eac3"),
                    MediaStream("audio", "fra", "eac3"),
                    MediaStream("subtitle", "fra", "dvb_subtitle"),
                ),
            )
            output = ProbedMedia(
                output_path,
                120,
                ("video", "audio", "audio", "audio", "subtitle"),
                0,
                streams=(
                    MediaStream("video", codec="h264"),
                    MediaStream("audio", "fre", "eac3"),
                    MediaStream("audio", "qaa", "eac3"),
                    MediaStream("audio", "qad", "eac3"),
                    MediaStream("subtitle", "fre", "dvb_subtitle"),
                ),
            )
            plan = build_convert_plan(
                source_path,
                "mkv",
                source_media=source,
            )

            warnings = validate_conversion(plan, source, output)

            self.assertIn("normalisé", warnings[0])

    def test_mkv_accepts_teletext_converted_to_subrip_by_mkvmerge(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source_path = directory / "source.m2ts"
            output_path = directory / "output.mkv"
            source_path.write_bytes(b"source")
            source = ProbedMedia(
                source_path,
                120,
                ("video", "audio", "subtitle"),
                0,
                streams=(
                    MediaStream("video", codec="h264"),
                    MediaStream("audio", "fra", "aac"),
                    MediaStream("subtitle", "fra", "dvb_teletext"),
                ),
            )
            output = ProbedMedia(
                output_path,
                120,
                ("video", "audio", "subtitle"),
                0,
                streams=(
                    MediaStream("video", codec="h264"),
                    MediaStream("audio", "fre", "aac"),
                    MediaStream("subtitle", "fre", "subrip"),
                ),
            )
            plan = build_convert_plan(source_path, "mkv", source_media=source)

            warnings = validate_conversion(plan, source, output)

            self.assertTrue(any("télétexte" in warning for warning in warnings))
            self.assertTrue(any("langue" in warning for warning in warnings))

    def test_mp4_validation_accepts_all_audio_and_text_subtitles(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source_path = directory / "source.mkv"
            source_path.write_bytes(b"source")
            source = ProbedMedia(
                source_path,
                120,
                ("video", "audio", "audio", "subtitle"),
                0,
                streams=(
                    MediaStream("video", codec="h264"),
                    MediaStream("audio", "fra", "eac3"),
                    MediaStream("audio", "eng", "eac3"),
                    MediaStream("subtitle", "fra", "subrip"),
                ),
            )
            output_path = directory / "output.mp4"
            output = ProbedMedia(
                output_path,
                120,
                ("video", "audio", "audio", "subtitle"),
                0,
                streams=(
                    MediaStream("video", codec="h264"),
                    MediaStream("audio", "fra", "aac"),
                    MediaStream("audio", "eng", "aac"),
                    MediaStream("subtitle", "fra", "mov_text"),
                ),
            )
            plan = build_convert_plan(
                source_path,
                "mp4",
                source_media=source,
            )

            warnings = validate_conversion(plan, source, output)

            self.assertEqual(warnings, ())


class ConversionExecutionTests(TestCase):
    def test_folder_conversion_copies_tree_and_converts_every_video(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "vacances"
            nested = source / "jour-1"
            nested.mkdir(parents=True)
            first = source / "camera-file.bin"
            second = nested / "clip.avi"
            note = nested / "notes.txt"
            first.write_bytes(b"video-one")
            second.write_bytes(b"video-two")
            note.write_text("souvenir", encoding="utf-8")
            media = tuple(_FakeProbe().probe(path) for path in (first, second))
            destination = folder_output_path(source, root)

            results = execute_folder_conversion(
                source,
                destination,
                media,
                output_format=OutputFormat.MP4,
                output_quality=OutputQuality.BALANCED,
                service=ConversionService(
                    _FakeMakeMkv(),
                    _FakeProbe(),
                    _FakeConverter(),
                ),
            )

            self.assertFalse((destination / "camera-file.bin").exists())
            self.assertEqual((destination / "camera-file.mp4").read_bytes(), b"converted")
            self.assertEqual(
                (destination / "jour-1" / "clip.mp4").read_bytes(),
                b"converted",
            )
            self.assertEqual(
                (destination / "jour-1" / "notes.txt").read_text(encoding="utf-8"),
                "souvenir",
            )
            self.assertEqual(len(results), 2)
            self.assertTrue(all(result.output.is_relative_to(destination) for result in results))

    def test_folder_conversion_refuses_output_name_collisions(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "sources"
            source.mkdir()
            first = source / "film.mov"
            second = source / "film.avi"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            media = tuple(_FakeProbe().probe(path) for path in (first, second))

            with self.assertRaisesRegex(MovieError, "même chemin"):
                execute_folder_conversion(
                    source,
                    root / "sources-converted",
                    media,
                    output_format=OutputFormat.MP4,
                    output_quality=OutputQuality.BALANCED,
                    service=ConversionService(
                        _FakeMakeMkv(),
                        _FakeProbe(),
                        _FakeConverter(),
                    ),
                )

            self.assertFalse((root / "sources-converted").exists())

    def test_folder_discovery_uses_content_instead_of_extensions(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            video = root / "recording.unknown"
            document = root / "document.mp4"
            video.write_bytes(b"video")
            document.write_bytes(b"text")
            probe = MagicMock()
            probe.probe.side_effect = (
                ProbedMedia(
                    document,
                    None,
                    (),
                    0,
                    streams=(),
                ),
                ProbedMedia(
                    video,
                    1,
                    ("video",),
                    0,
                    streams=(MediaStream("video", codec="h264"),),
                ),
            )

            discovered = discover_video_files(root, probe)

            self.assertEqual(tuple(media.path for media in discovered), (video,))

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

            ConversionService(_FakeMakeMkv(), probe, _FakeConverter()).execute(plan)

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
            self.assertEqual(result.media.path, result.output)
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

    def test_m4v_is_converted_verified_and_reports_omitted_subtitles(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source = directory / "film.mkv"
            source.write_bytes(b"source")
            plan = build_convert_plan(source, "m4v")

            result = ConversionService(
                _FakeMakeMkv(), _FakeProbe(), _FakeConverter()
            ).execute(plan)

            self.assertEqual(result.output.suffix, ".m4v")
            self.assertEqual(result.media.streams[0].codec, "h264")
            self.assertIn("M4V", result.warnings[0])

    def test_every_encoded_format_is_verified_and_published(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source = directory / "film.source"
            source.write_bytes(b"source")

            for output_format in OutputFormat:
                if output_spec(output_format).copies_source:
                    continue
                with self.subTest(output_format=output_format):
                    plan = build_convert_plan(source, output_format)
                    result = ConversionService(
                        _FakeMakeMkv(),
                        _FakeProbe(),
                        _FakeConverter(),
                    ).execute(plan)

                    self.assertEqual(result.output.suffix, f".{output_format.value}")
                    self.assertEqual(
                        result.media.streams[0].codec,
                        output_spec(output_format).video_codec,
                    )

    def test_iso_to_mkv_publishes_extraction_without_redundant_remux(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            iso = directory / "film.iso"
            iso.write_bytes(b"iso")
            title = _iso_title()
            plan = build_convert_plan(iso, "mkv", iso_title=title)
            converter = MagicMock()

            result = ConversionService(_FakeMakeMkv(), _FakeProbe(), converter).execute(
                plan
            )

            converter.convert.assert_not_called()
            self.assertEqual(result.output.read_bytes(), b"extracted")

    def test_iso_to_m4v_extracts_then_converts_all_audio_tracks(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            iso = directory / "film.iso"
            iso.write_bytes(b"iso")
            plan = build_convert_plan(iso, "m4v", iso_title=_iso_title())
            converter = MagicMock(wraps=_FakeConverter())

            result = ConversionService(_FakeMakeMkv(), _FakeProbe(), converter).execute(
                plan
            )

            self.assertEqual(
                tuple(
                    stream.language
                    for stream in result.media.streams
                    if stream.kind == "audio"
                ),
                ("fra", "eng"),
            )
            self.assertNotIn("audio_track", converter.convert.call_args.kwargs)

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

    def test_conversion_uses_local_staging(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source_directory = directory / "nas"
            source_directory.mkdir()
            source = source_directory / "film.mkv"
            source.write_bytes(b"source")
            converter = MagicMock(wraps=_FakeConverter())

            ConversionService(_FakeMakeMkv(), _FakeProbe(), converter).execute(
                build_convert_plan(source, "mp4")
            )

            staged_output = converter.convert.call_args.args[1]
            self.assertEqual(
                staged_output.parent.parent,
                Path(tempfile.gettempdir()).resolve(),
            )
            self.assertNotEqual(staged_output.parent, source.parent)

    @patch("movie.core.convert.ensure_free_space")
    def test_conversion_checks_destination_and_local_staging_space(
        self,
        check_space: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "film.mkv"
            source.write_bytes(b"source")
            plan = build_convert_plan(source, "mp4")
            staging_root = MagicMock(spec=Path)
            staging_root.stat.return_value.st_dev = plan.output.parent.stat().st_dev + 1

            _ensure_free_space(plan, staging_root)

            self.assertEqual(check_space.call_count, 2)
            self.assertEqual(check_space.call_args_list[0].args[0], plan.output.parent)
            self.assertIs(check_space.call_args_list[1].args[0], staging_root)

    def test_verified_conversion_is_preserved_when_publication_fails(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "film.mkv"
            source.write_bytes(b"source")
            plan = build_convert_plan(source, "mp4")

            with (
                patch(
                    "movie.core.convert.tempfile.gettempdir",
                    return_value=temporary_directory,
                ),
                patch(
                    "movie.core.convert.publish_without_overwrite",
                    side_effect=MovieError("NAS indisponible"),
                ),
                self.assertRaisesRegex(
                    MovieError,
                    "fichier local vérifié a été conservé",
                ),
            ):
                ConversionService(
                    _FakeMakeMkv(),
                    _FakeProbe(),
                    _FakeConverter(),
                ).execute(plan)

            recovery_directories = list(root.glob("movie-convert-*"))
            self.assertEqual(len(recovery_directories), 1)
            self.assertEqual(
                (recovery_directories[0] / ".movie-output.mp4").read_bytes(),
                b"converted",
            )
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
        try:
            output_format = OutputFormat(path.suffix.removeprefix("."))
        except ValueError:
            output_format = None
        if output_format is not None and not output_spec(output_format).copies_source:
            spec = output_spec(output_format)
            return ProbedMedia(
                path,
                120,
                ("video", "audio", "audio"),
                2,
                streams=(
                    MediaStream("video", codec=spec.video_codec),
                    MediaStream("audio", "fra", spec.audio_codec),
                    MediaStream("audio", "eng", spec.audio_codec),
                ),
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
        duration_seconds: float | None = None,
        source_media: ProbedMedia | None = None,
        audio_track_index: int | None = None,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> Path:
        del (
            source,
            output_format,
            quality,
            duration_seconds,
            source_media,
            audio_track_index,
        )
        destination.write_bytes(b"converted")
        if on_progress is not None:
            on_progress(ProgressUpdate("Conversion", None, 1.0, 1.0))
        return destination


class _FailingConverter(_FakeConverter):
    def convert(self, *args: object, **kwargs: object) -> Path:
        del args, kwargs
        raise MovieError("Échec de conversion simulée")

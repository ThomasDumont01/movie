"""Petit test d'intégration local entre FFmpeg, ffprobe et Movie."""

from __future__ import annotations

import base64
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from unittest.mock import MagicMock, patch

from movie.conversion import MediaConverter
from movie.core.media import MediaProbe
from movie.core.models import (
    DiscTitle,
    MediaStream,
    MovieMetadata,
    OutputFormat,
    OutputQuality,
    RipPlan,
)
from movie.core.rip import RipService
from movie.core.tag import TagService, build_tag_plan
from movie.enrichment import MediaTagger

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


@skipUnless(FFMPEG and FFPROBE, "FFmpeg et ffprobe ne sont pas installés")
class FfmpegIntegrationTests(TestCase):
    def test_real_rip_verification_accepts_makemkv_removing_empty_subtitle(
        self,
    ) -> None:
        assert FFMPEG is not None
        assert FFPROBE is not None
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "film.mkv"
            generated = subprocess.run(
                [
                    FFMPEG,
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=size=32x32:rate=10:duration=1",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=1",
                    "-shortest",
                    "-c:v",
                    "ffv1",
                    "-c:a",
                    "pcm_s16le",
                    str(source),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            analyzed_title = DiscTitle(
                0,
                "Film avec sous-titre vide",
                1,
                0,
                source.stat().st_size,
                None,
                source.name,
                3,
                streams=(
                    MediaStream("video"),
                    MediaStream("audio"),
                    MediaStream("subtitle"),
                ),
            )
            output = Path(temporary_directory) / "film-verifie.mkv"
            makemkv = MagicMock()

            def extract_to_staging(
                _drive_index: int,
                _title_id: int,
                staging: Path,
                *,
                on_progress: object = None,
            ) -> None:
                del on_progress
                shutil.copyfile(source, staging / "extraction.mkv")

            makemkv.rip_title.side_effect = extract_to_staging

            result = RipService(makemkv, MediaProbe(FFPROBE)).execute(
                RipPlan(0, analyzed_title, output)
            )

            self.assertTrue(output.is_file())
            self.assertIn("sous-titre", "\n".join(result.warnings))

    @patch("movie.enrichment.prepare_artwork")
    def test_real_remux_preserves_video_and_embeds_metadata_and_cover(
        self,
        prepare_artwork: MagicMock,
    ) -> None:
        assert FFMPEG is not None
        assert FFPROBE is not None
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source = directory / "source.mkv"
            enriched = directory / "enriched.mkv"
            poster = directory / "cover.png"
            poster.write_bytes(
                base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
                    "AAAAC0lEQVR4nGNgYAAAAAMAASsJTYQAAAAASUVORK5CYII="
                )
            )
            prepare_artwork.return_value = (poster, "image/png")
            generated = subprocess.run(
                [
                    FFMPEG,
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=size=32x32:rate=1:duration=1",
                    "-c:v",
                    "ffv1",
                    str(source),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            metadata = MovieMetadata(
                title="Film d'intégration",
                year=2024,
                summary="Test local",
                genres=("Test",),
                source_url="https://www.themoviedb.org/movie/1-test",
                poster_url="https://image.tmdb.org/poster.png",
            )

            source_media = MediaProbe(FFPROBE).probe(source)
            MediaTagger(FFMPEG).tag(
                source,
                enriched,
                metadata,
                source_media=source_media,
            )
            media = MediaProbe(FFPROBE).probe(enriched)

            tags = {key.casefold(): value for key, value in media.format_tags}
            self.assertIn("video", media.stream_types)
            self.assertIn("attachment", media.stream_types)
            self.assertEqual(tags.get("title"), metadata.title)

            retagged = directory / "retagged.mkv"
            corrected = MovieMetadata(
                title="Film corrigé",
                year=2025,
                summary="Métadonnées remplacées",
                genres=("Archive",),
                poster_path=poster,
            )
            MediaTagger(FFMPEG).tag(
                enriched,
                retagged,
                corrected,
                source_media=media,
            )
            corrected_media = MediaProbe(FFPROBE).probe(retagged)
            corrected_tags = {
                key.casefold(): value for key, value in corrected_media.format_tags
            }
            self.assertEqual(corrected_tags.get("title"), corrected.title)
            assert metadata.source_url is not None
            self.assertFalse(
                any(metadata.source_url in value for value in corrected_tags.values())
            )
            self.assertEqual(corrected_media.stream_types.count("attachment"), 1)

    @patch("movie.enrichment.prepare_artwork")
    def test_real_mp4_conversion_produces_h264_aac_metadata_and_cover(
        self,
        prepare_artwork: MagicMock,
    ) -> None:
        assert FFMPEG is not None
        assert FFPROBE is not None
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source = directory / "source.mkv"
            converted = directory / "converted.mp4"
            output = directory / "output.mp4"
            poster = directory / "cover.ppm"
            generated = subprocess.run(
                [
                    FFMPEG,
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=size=64x64:rate=24:duration=1",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=1",
                    "-shortest",
                    "-c:v",
                    "ffv1",
                    "-c:a",
                    "pcm_s16le",
                    str(source),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            poster.write_bytes(b"P6\n2 2\n255\n" + bytes((20, 80, 180)) * 4)
            prepare_artwork.return_value = (poster, "image/jpeg")
            metadata = MovieMetadata(
                title="Film MP4 d'intégration",
                year=2025,
                summary="Conversion locale réelle",
                genres=("Test",),
                source_url="https://www.themoviedb.org/movie/1-test",
                poster_url="https://image.tmdb.org/poster.jpg",
            )

            MediaConverter(FFMPEG).convert(
                source,
                converted,
                output_format=OutputFormat.MP4,
                quality=OutputQuality.BALANCED,
            )
            converted_media = MediaProbe(FFPROBE).probe(converted)
            MediaTagger(FFMPEG).tag(
                converted,
                output,
                metadata,
                source_media=converted_media,
            )
            media = MediaProbe(FFPROBE).probe(output)

            codecs = {(stream.kind, stream.codec) for stream in media.streams}
            tags = {key.casefold(): value for key, value in media.format_tags}
            self.assertIn(("video", "h264"), codecs)
            self.assertIn(("audio", "aac"), codecs)
            self.assertIn("attachment", media.stream_types)
            self.assertEqual(tags.get("title"), metadata.title)

    def test_real_mkv_mp4_and_m4v_outputs_are_readable(self) -> None:
        assert FFMPEG is not None
        assert FFPROBE is not None
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source = directory / "source.mkv"
            generated = subprocess.run(
                [
                    FFMPEG,
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=size=32x32:rate=10:duration=1",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=1",
                    "-shortest",
                    "-c:v",
                    "ffv1",
                    "-c:a",
                    "pcm_s16le",
                    str(source),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            converter = MediaConverter(FFMPEG)
            outputs = (
                (
                    directory / "copy.mkv",
                    OutputFormat.MKV,
                    OutputQuality.SOURCE,
                    "pcm_s16le",
                ),
                (
                    directory / "video.mp4",
                    OutputFormat.MP4,
                    OutputQuality.BALANCED,
                    "aac",
                ),
                (
                    directory / "video.m4v",
                    OutputFormat.M4V,
                    OutputQuality.BALANCED,
                    "aac",
                ),
            )
            for output, output_format, quality, expected_audio in outputs:
                with self.subTest(output=output.name):
                    converter.convert(
                        source,
                        output,
                        output_format=output_format,
                        quality=quality,
                    )
                    media = MediaProbe(FFPROBE).probe(output)
                    codecs = {
                        stream.codec
                        for stream in media.streams
                        if stream.kind == "audio"
                    }
                    self.assertIn(expected_audio, codecs)
                    if output_format is not OutputFormat.MKV:
                        self.assertIn(
                            ("video", "h264"),
                            {(stream.kind, stream.codec) for stream in media.streams},
                        )
                    self.assertGreater(output.stat().st_size, 0)

    @patch("movie.enrichment.prepare_artwork")
    def test_real_m4v_tagging_preserves_media_and_adds_cover(
        self,
        prepare_artwork: MagicMock,
    ) -> None:
        assert FFMPEG is not None
        assert FFPROBE is not None
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            intermediate = directory / "source.mkv"
            source = directory / "source.m4v"
            output = directory / "tagged.m4v"
            poster = directory / "cover.ppm"
            poster.write_bytes(b"P6\n2 2\n255\n" + bytes((30, 120, 200)) * 4)
            prepare_artwork.return_value = (poster, "image/jpeg")
            generated = subprocess.run(
                [
                    FFMPEG,
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=size=32x32:rate=10:duration=1",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=1",
                    "-shortest",
                    "-c:v",
                    "ffv1",
                    "-c:a",
                    "pcm_s16le",
                    str(intermediate),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            MediaConverter(FFMPEG).convert(
                intermediate,
                source,
                output_format=OutputFormat.M4V,
                quality=OutputQuality.BALANCED,
            )
            metadata = MovieMetadata(
                title="Enregistrement personnel",
                year=2026,
                summary="Archive familiale",
                genres=("Personnel",),
                poster_path=poster,
            )
            source_media = MediaProbe(FFPROBE).probe(source)

            MediaTagger(FFMPEG).tag(
                source,
                output,
                metadata,
                source_media=source_media,
            )
            media = MediaProbe(FFPROBE).probe(output)

            tags = {key.casefold(): value for key, value in media.format_tags}
            codecs = {(stream.kind, stream.codec) for stream in media.streams}
            self.assertIn(("video", "h264"), codecs)
            self.assertIn(("audio", "aac"), codecs)
            self.assertIn("attachment", media.stream_types)
            self.assertEqual(tags.get("title"), metadata.title)
            self.assertIn("2026", tags.get("date", ""))

    def test_real_tag_service_atomically_validates_every_supported_format(self) -> None:
        assert FFMPEG is not None
        assert FFPROBE is not None
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            mkv = directory / "source.mkv"
            mp4 = directory / "source.mp4"
            m4v = directory / "source.m4v"
            generated = subprocess.run(
                [
                    FFMPEG,
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=size=32x32:rate=10:duration=1",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=1",
                    "-shortest",
                    "-c:v",
                    "ffv1",
                    "-c:a",
                    "pcm_s16le",
                    str(mkv),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            converter = MediaConverter(FFMPEG)
            converter.convert(
                mkv,
                mp4,
                output_format=OutputFormat.MP4,
                quality=OutputQuality.BALANCED,
            )
            converter.convert(
                mkv,
                m4v,
                output_format=OutputFormat.M4V,
                quality=OutputQuality.BALANCED,
            )
            poster = directory / "cover.png"
            poster.write_bytes(
                base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
                    "AAAAC0lEQVR4nGNgYAAAAAMAASsJTYQAAAAASUVORK5CYII="
                )
            )
            metadata = MovieMetadata(
                title="Archive personnelle",
                year=2026,
                summary="Souvenir familial",
                genres=("Famille", "Voyage"),
                poster_path=poster,
            )
            service = TagService(MediaProbe(FFPROBE), MediaTagger(FFMPEG))

            for source in (mkv, mp4, m4v):
                with self.subTest(source=source.suffix):
                    result = service.execute(build_tag_plan(source, metadata))
                    self.assertEqual(result.output, source.resolve())
                    self.assertTrue(source.is_file())
                    self.assertIn("attachment", result.media.stream_types)
                    tags = {
                        key.casefold(): value for key, value in result.media.format_tags
                    }
                    self.assertEqual(tags.get("title"), metadata.title)

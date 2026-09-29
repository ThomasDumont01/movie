"""Tests du remuxage sans réencodage des métadonnées."""

from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

from movie.conversion import MediaConverter
from movie.core.models import (
    MediaStream,
    MovieError,
    MovieMetadata,
    OutputFormat,
    OutputQuality,
    ProbedMedia,
    ToolUnavailableError,
)
from movie.enrichment import MediaTagger
from movie.ffmpeg import (
    _progress_fraction,
    _run_captured,
    _run_with_progress,
    download_poster,
    find_ffmpeg,
    prepare_artwork,
    prepare_fanart,
)


class MediaTaggerTests(TestCase):
    def test_ffmpeg_progress_is_converted_to_fraction(self) -> None:
        self.assertEqual(_progress_fraction("out_time_us", "5000000", 10), 0.5)
        self.assertEqual(_progress_fraction("progress", "end", None), 1.0)

    @patch("movie.ffmpeg.subprocess.run")
    def test_enrichment_copies_streams_and_sets_metadata(self, run: MagicMock) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source = directory / "source.mkv"
            destination = directory / "enriched.mkv"
            source.write_bytes(b"source")
            metadata = MovieMetadata(
                title="Mon Film",
                year=2024,
                summary="Résumé",
                genres=("Aventure",),
                source_url="https://www.imdb.com/title/tt1/",
                poster_url=None,
            )

            def complete(command: list[str], **_: object) -> CompletedProcess[str]:
                Path(command[-1]).write_bytes(b"enriched")
                return CompletedProcess(command, 0, "", "")

            run.side_effect = complete
            result = MediaTagger("ffmpeg-test").tag(
                source,
                destination,
                metadata,
                source_media=_media(source),
            )

            self.assertEqual(result, destination)
            command = run.call_args.args[0]
            self.assertIn("copy", command)
            self.assertIn("title=Mon Film", command)
            self.assertIn("synopsis=Résumé", command)
            self.assertNotIn("-attach", command)

    @patch("movie.enrichment.prepare_fanart")
    @patch("movie.enrichment.prepare_artwork")
    @patch("movie.ffmpeg.subprocess.run")
    def test_mkv_embeds_poster_and_fanart_as_distinct_attachments(
        self,
        run: MagicMock,
        prepare_artwork_mock: MagicMock,
        prepare_fanart_mock: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source = directory / "source.mkv"
            destination = directory / "enriched.mkv"
            poster = directory / "cover.jpg"
            fanart = directory / "fanart.jpg"
            poster.write_bytes(b"poster")
            fanart.write_bytes(b"fanart")
            prepare_artwork_mock.return_value = (poster, "image/jpeg")
            prepare_fanart_mock.return_value = fanart

            def complete(command: list[str], **_: object) -> CompletedProcess[str]:
                Path(command[-1]).write_bytes(b"enriched")
                return CompletedProcess(command, 0, "", "")

            run.side_effect = complete
            source_media = ProbedMedia(
                source,
                120,
                ("video", "audio"),
                0,
                streams=(
                    MediaStream("video", codec="h264", stream_id=0),
                    MediaStream("audio", "fra", "aac", stream_id=1),
                ),
            )

            MediaTagger("ffmpeg-test").tag(
                source,
                destination,
                MovieMetadata(
                    title="Film",
                    poster_url="https://image.tmdb.org/poster.jpg",
                    fanart_url="https://image.tmdb.org/fanart.jpg",
                ),
                source_media=source_media,
            )

            command = run.call_args.args[0]
            self.assertEqual(command.count("-attach"), 2)
            self.assertIn("filename=cover.jpg", command)
            self.assertIn("title=Jaquette", command)
            self.assertIn("filename=fanart.jpg", command)
            self.assertIn("title=Arrière-plan", command)

    @patch("movie.enrichment.prepare_fanart")
    @patch("movie.ffmpeg.subprocess.run")
    def test_mp4_does_not_download_an_unsupported_fanart(
        self,
        run: MagicMock,
        prepare_fanart_mock: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source = directory / "source.mp4"
            destination = directory / "enriched.mp4"

            def complete(command: list[str], **_: object) -> CompletedProcess[str]:
                Path(command[-1]).write_bytes(b"enriched")
                return CompletedProcess(command, 0, "", "")

            run.side_effect = complete
            MediaTagger("ffmpeg-test").tag(
                source,
                destination,
                MovieMetadata(
                    title="Film",
                    fanart_url="https://image.tmdb.org/fanart.jpg",
                ),
                source_media=_media(source),
            )

            prepare_fanart_mock.assert_not_called()

    @patch("movie.ffmpeg.subprocess.run")
    def test_ffmpeg_failure_and_missing_output_are_reported(
        self, run: MagicMock
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            metadata = _metadata()
            run.return_value = CompletedProcess([], 1, "", "conteneur refusé")
            with self.assertRaisesRegex(MovieError, "conteneur refusé"):
                MediaTagger("ffmpeg-test").tag(
                    directory / "source.mkv",
                    directory / "sortie.mkv",
                    metadata,
                    source_media=_media(directory / "source.mkv"),
                )

            run.return_value = CompletedProcess([], 0, "", "")
            with self.assertRaisesRegex(MovieError, "n'a pas produit"):
                MediaTagger("ffmpeg-test").tag(
                    directory / "source.mkv",
                    directory / "sortie.mkv",
                    metadata,
                    source_media=_media(directory / "source.mkv"),
                )

    @patch("movie.ffmpeg.subprocess.run", side_effect=OSError)
    def test_ffmpeg_launch_failure_is_reported(self, _run: object) -> None:
        with self.assertRaisesRegex(MovieError, "Impossible de lancer FFmpeg"):
            _run_captured(["ffmpeg-test"])

    @patch("movie.ffmpeg.subprocess.Popen")
    def test_streaming_runner_reports_ffmpeg_progress(self, popen: MagicMock) -> None:
        process = popen.return_value
        process.stdout = _ClosableLines(
            ["out_time_us=5000000\n", "ligne ignorée\n", "progress=end\n"]
        )
        process.wait.return_value = 0
        updates = []

        result = _run_with_progress(
            ["ffmpeg-test"],
            duration_seconds=10,
            callback=updates.append,
            label="Conversion",
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            [update.current_fraction for update in updates],
            [0.5, 1.0],
        )
        self.assertTrue(process.stdout.closed)

    def test_invalid_progress_value_is_ignored(self) -> None:
        self.assertIsNone(_progress_fraction("out_time_us", "invalide", 10))
        self.assertIsNone(_progress_fraction("inconnu", "1", 10))

    @patch("movie.ffmpeg.shutil.which", return_value=None)
    def test_missing_ffmpeg_has_installation_help(self, _which: object) -> None:
        with self.assertRaisesRegex(ToolUnavailableError, "brew install ffmpeg"):
            find_ffmpeg()


class MediaConverterTests(TestCase):
    @patch("movie.ffmpeg.subprocess.run")
    def test_m2ts_input_forces_the_mpegts_demuxer(self, run: MagicMock) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            destination = directory / "film.mp4"

            def complete(command: list[str], **_: object) -> CompletedProcess[str]:
                destination.write_bytes(b"mp4")
                return CompletedProcess(command, 0, "", "")

            run.side_effect = complete
            MediaConverter("ffmpeg-test").convert(
                directory / "source.m2ts",
                destination,
                output_format=OutputFormat.MP4,
                quality=OutputQuality.BALANCED,
            )

            command = run.call_args.args[0]
            input_index = command.index("-i")
            self.assertEqual(command[input_index - 2 : input_index], ["-f", "mpegts"])

    @patch("movie.ffmpeg.subprocess.run")
    def test_mp4_uses_h264_aac_and_selected_profile(self, run: MagicMock) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            destination = directory / "film.mp4"

            def complete(command: list[str], **_: object) -> CompletedProcess[str]:
                Path(command[-1]).write_bytes(b"mp4")
                return CompletedProcess(command, 0, "", "")

            run.side_effect = complete
            result = MediaConverter("ffmpeg-test").convert(
                directory / "source.mkv",
                destination,
                output_format=OutputFormat.MP4,
                quality=OutputQuality.HIGH,
            )

            self.assertEqual(result, destination)
            command = run.call_args.args[0]
            self.assertIn("libx264", command)
            self.assertIn("aac", command)
            self.assertIn("18", command)
            self.assertIn("+faststart", command)
            self.assertNotIn("0:s?", command)

    def test_source_profile_is_refused_for_mp4(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            with self.assertRaisesRegex(MovieError, "high, balanced ou compact"):
                MediaConverter("ffmpeg-test").convert(
                    directory / "source.mkv",
                    directory / "film.mp4",
                    output_format=OutputFormat.MP4,
                    quality=OutputQuality.SOURCE,
                )

    @patch("movie.ffmpeg.subprocess.run")
    def test_m4v_uses_h264_aac_and_selected_profile(self, run: MagicMock) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)

            def complete(command: list[str], **_: object) -> CompletedProcess[str]:
                Path(command[-1]).write_bytes(b"m4v")
                return CompletedProcess(command, 0, "", "")

            run.side_effect = complete
            result = MediaConverter("ffmpeg-test").convert(
                directory / "source.mkv",
                directory / "film.m4v",
                output_format=OutputFormat.M4V,
                quality=OutputQuality.BALANCED,
            )

            self.assertEqual(result.suffix, ".m4v")
            command = run.call_args.args[0]
            self.assertIn("libx264", command)
            self.assertIn("aac", command)
            self.assertIn("21", command)


class PosterDownloadTests(TestCase):
    @patch("movie.ffmpeg.urlopen")
    def test_supported_https_poster_is_saved(self, urlopen: MagicMock) -> None:
        with TemporaryDirectory() as temporary_directory:
            response = _response(
                final_url="https://image.tmdb.org/cover.jpg",
                mime_type="image/jpeg",
                content=b"\xff\xd8\xffjpeg",
            )
            urlopen.return_value.__enter__.return_value = response

            path, mime_type = download_poster(
                "https://image.tmdb.org/cover.jpg",
                Path(temporary_directory),
            )

            self.assertEqual(mime_type, "image/jpeg")
            self.assertEqual(path.name, "cover.jpg")
            self.assertEqual(path.read_bytes(), b"\xff\xd8\xffjpeg")

    def test_local_poster_is_validated_and_copied(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source = directory / "poster.png"
            source.write_bytes(b"\x89PNG\r\n\x1a\ncontent")
            staging = directory / "staging"
            staging.mkdir()

            prepared = prepare_artwork(
                MovieMetadata(title="Film", poster_path=source),
                staging,
            )

            self.assertIsNotNone(prepared)
            assert prepared is not None
            self.assertEqual(prepared[1], "image/png")
            self.assertEqual(prepared[0].read_bytes(), source.read_bytes())

    def test_local_fanart_is_validated_and_gets_a_distinct_name(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source = directory / "panorama.webp"
            source.write_bytes(b"RIFF\x00\x00\x00\x00WEBPcontent")
            staging = directory / "staging"
            staging.mkdir()

            prepared = prepare_fanart(
                MovieMetadata(title="Film", fanart_path=source),
                staging,
            )

            self.assertIsNotNone(prepared)
            assert prepared is not None
            self.assertEqual(prepared.name, "fanart.webp")
            self.assertEqual(prepared.read_bytes(), source.read_bytes())

    @patch("movie.ffmpeg.urlopen")
    def test_remote_fanart_is_downloaded_separately_from_poster(
        self,
        urlopen: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            urlopen.return_value.__enter__.return_value = _response(
                final_url="https://media.themoviedb.org/t/p/original/fanart.jpg",
                mime_type="image/jpeg",
                content=b"\xff\xd8\xfffanart",
            )

            prepared = prepare_fanart(
                MovieMetadata(
                    title="Film",
                    fanart_url=("https://media.themoviedb.org/t/p/original/fanart.jpg"),
                ),
                Path(temporary_directory),
            )

            self.assertIsNotNone(prepared)
            assert prepared is not None
            self.assertEqual(prepared.name, "fanart.jpg")

    def test_invalid_local_poster_is_rejected(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "poster.jpg"
            source.write_bytes(b"pas une image")
            with self.assertRaisesRegex(MovieError, "JPEG, PNG ou WebP"):
                prepare_artwork(
                    MovieMetadata(title="Film", poster_path=source),
                    Path(temporary_directory),
                )

    def test_initial_poster_url_must_be_https(self) -> None:
        with self.assertRaisesRegex(MovieError, "URL HTTPS"):
            download_poster("http://image.tmdb.org/cover.jpg", Path("."))

    @patch("movie.ffmpeg.urlopen")
    def test_insecure_redirect_and_wrong_mime_are_rejected(
        self, urlopen: MagicMock
    ) -> None:
        urlopen.return_value.__enter__.return_value = _response(
            final_url="http://image.tmdb.org/cover.jpg",
            mime_type="image/jpeg",
            content=b"jpeg",
        )
        with self.assertRaisesRegex(MovieError, "non sécurisée"):
            download_poster("https://image.tmdb.org/cover.jpg", Path("."))

        urlopen.return_value.__enter__.return_value = _response(
            final_url="https://image.tmdb.org/cover.svg",
            mime_type="image/svg+xml",
            content=b"svg",
        )
        with self.assertRaisesRegex(MovieError, "image prise en charge"):
            download_poster("https://image.tmdb.org/cover.svg", Path("."))

    @patch("movie.ffmpeg._MAX_ARTWORK_BYTES", 3)
    @patch("movie.ffmpeg.urlopen")
    def test_oversized_poster_is_rejected(self, urlopen: MagicMock) -> None:
        urlopen.return_value.__enter__.return_value = _response(
            final_url="https://image.tmdb.org/cover.png",
            mime_type="image/png",
            content=b"1234",
        )
        with self.assertRaisesRegex(MovieError, "trop volumineuse"):
            download_poster("https://image.tmdb.org/cover.png", Path("."))

    @patch("movie.ffmpeg.urlopen", side_effect=OSError("hors ligne"))
    def test_network_failure_is_reported(self, _urlopen: object) -> None:
        with self.assertRaisesRegex(MovieError, "hors ligne"):
            download_poster("https://image.tmdb.org/cover.webp", Path("."))


class _ClosableLines(list[str]):
    closed = False

    def close(self) -> None:
        self.closed = True


def _metadata() -> MovieMetadata:
    return MovieMetadata(
        title="Film",
        year=None,
        summary=None,
        genres=(),
        source_url="https://www.themoviedb.org/movie/1-film",
        poster_url=None,
    )


def _media(path: Path) -> ProbedMedia:
    return ProbedMedia(
        path=path,
        duration_seconds=120,
        stream_types=("video", "audio"),
        chapter_count=2,
        streams=(
            MediaStream("video", codec="h264"),
            MediaStream("audio", "fra", "aac"),
        ),
    )


def _response(*, final_url: str, mime_type: str, content: bytes) -> MagicMock:
    response = MagicMock()
    response.geturl.return_value = final_url
    response.headers.get_content_type.return_value = mime_type
    response.read.return_value = content
    return response

"""Tests de l'analyse ffprobe sans fichier multimédia réel."""

from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess
from unittest import TestCase
from unittest.mock import MagicMock, patch

from movie.core.media import MediaProbe
from movie.core.models import MovieError


class MediaProbeTests(TestCase):
    @patch("movie.core.media.subprocess.run")
    def test_probe_normalizes_ffprobe_json(self, run: MagicMock) -> None:
        run.return_value = CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                '{"format":{"duration":"120.5","tags":{"title":"Film"}},'
                '"streams":[{"index":0,"codec_type":"video","codec_name":"mpeg2video"},'
                '{"index":1,"codec_type":"audio","codec_name":"ac3",'
                '"tags":{"language":"fra"}},'
                '{"index":2,"codec_type":"video","codec_name":"mjpeg",'
                '"disposition":{"attached_pic":1}}],'
                '"chapters":[{},{}]}'
            ),
            stderr="",
        )

        media = MediaProbe("ffprobe-test").probe(Path("film.mkv"))

        self.assertEqual(media.duration_seconds, 120.5)
        self.assertEqual(media.stream_types, ("video", "audio", "attachment"))
        self.assertEqual(media.chapter_count, 2)
        self.assertEqual(media.format_tags, (("title", "Film"),))
        audio = next(stream for stream in media.streams if stream.kind == "audio")
        self.assertEqual(audio.language, "fra")
        self.assertEqual(audio.codec, "ac3")
        artwork = next(stream for stream in media.streams if stream.is_artwork)
        self.assertEqual(artwork.kind, "attachment")

    @patch("movie.core.media.subprocess.run")
    def test_probe_reports_ffprobe_failure(self, run: MagicMock) -> None:
        run.return_value = CompletedProcess(
            args=[], returncode=1, stdout="", stderr="fichier invalide"
        )

        with self.assertRaisesRegex(MovieError, "fichier invalide"):
            MediaProbe("ffprobe-test").probe(Path("film.mkv"))

    @patch("movie.core.media.time.sleep")
    @patch("movie.core.media.subprocess.run")
    def test_probe_retries_a_temporarily_busy_network_file(
        self,
        run: MagicMock,
        sleep: MagicMock,
    ) -> None:
        run.side_effect = (
            CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr="Resource temporarily unavailable",
            ),
            CompletedProcess(
                args=[],
                returncode=0,
                stdout=(
                    '{"format":{"duration":"1"},'
                    '"streams":[{"codec_type":"video"}],"chapters":[]}'
                ),
                stderr="",
            ),
        )

        media = MediaProbe("ffprobe-test").probe(Path("film.mkv"))

        self.assertEqual(media.duration_seconds, 1.0)
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(0.25)

    @patch("movie.core.media.subprocess.run")
    def test_probe_reports_invalid_json(self, run: MagicMock) -> None:
        run.return_value = CompletedProcess(
            args=[], returncode=0, stdout="illisible", stderr=""
        )

        with self.assertRaisesRegex(MovieError, "analyse illisible"):
            MediaProbe("ffprobe-test").probe(Path("film.mkv"))

    @patch("movie.core.media.subprocess.run", side_effect=OSError)
    def test_probe_reports_executable_launch_failure(self, _run: object) -> None:
        with self.assertRaisesRegex(MovieError, "Impossible de lancer ffprobe"):
            MediaProbe("ffprobe-test").probe(Path("film.mkv"))

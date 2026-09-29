"""Tests du protocole robot MakeMKV, sans lecteur ni MakeMKV installé."""

from __future__ import annotations

import unittest
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from movie.core.makemkv import (
    MakeMkvClient,
    MakeMkvError,
    MakeMkvRun,
    _ProgressParser,
    find_makemkvcon,
    parse_duration,
    parse_robot_report,
)
from movie.core.models import DiscError, ProgressUpdate, ToolUnavailableError

DVD_SCAN = """\
MSG:1005,0,1,"MakeMKV started","%1 started","MakeMKV"
MSG:5011,0,0,"Operation successfully completed","Operation successfully completed"
DRV:0,2,999,12,"DVD-RW USB","MON, FILM","/dev/rdisk4"
CINFO:1,6209,"DVD disc"
CINFO:2,0,"MON, FILM"
TCOUNT:2
TINFO:0,2,0,"MON, FILM"
TINFO:0,8,0,"20"
TINFO:0,9,0,"1:47:32"
TINFO:0,10,0,"5.8 GB"
TINFO:0,11,0,"6227702579"
TINFO:0,16,0,"VTS_01_1.VOB"
TINFO:0,27,0,"MON_FILM_t00.mkv"
SINFO:0,0,1,6201,"Video"
SINFO:0,0,3,0,"fra"
SINFO:0,1,1,6202,"Audio"
SINFO:0,2,1,6203,"Subtitle"
TINFO:1,2,0,"Bonus"
TINFO:1,9,0,"0:12:08"
"""

BLURAY_SCAN = """\
MSG:5011,0,0,"Operation successfully completed","Operation successfully completed"
DRV:0,2,999,12,"BD-RE USB","FILM_UHD","/dev/rdisk5"
CINFO:1,6209,"Blu-ray disc"
CINFO:2,0,"FILM_UHD"
TCOUNT:1
TINFO:0,2,0,"Film UHD"
TINFO:0,8,0,"24"
TINFO:0,9,0,"2:12:03"
TINFO:0,11,0,"68719476736"
TINFO:0,16,0,"00000.mpls"
TINFO:0,27,0,"FILM_UHD_t00.mkv"
SINFO:0,0,1,6201,"Video"
SINFO:0,1,1,6202,"Audio"
SINFO:0,1,3,0,"fra"
SINFO:0,2,1,6203,"Subtitle"
"""


class MakeMkvParserTests(unittest.TestCase):
    def test_bluray_uses_the_same_normalized_disc_model(self) -> None:
        report = parse_robot_report(BLURAY_SCAN)

        self.assertEqual(report.disc_type, "Blu-ray disc")
        self.assertEqual(report.drives[0].name, "BD-RE USB")
        self.assertEqual(report.titles[0].source_name, "00000.mpls")
        self.assertEqual(report.titles[0].size_bytes, 68_719_476_736)
        self.assertEqual(
            tuple(stream.kind for stream in report.titles[0].streams),
            ("video", "audio", "subtitle"),
        )

    def test_scan_preserves_quoted_values_and_main_title_data(self) -> None:
        report = parse_robot_report(DVD_SCAN)

        self.assertEqual(report.drives[0].disc_label, "MON, FILM")
        self.assertEqual(report.drives[0].device_path, "/dev/rdisk4")
        self.assertEqual(report.disc_type, "DVD disc")
        self.assertEqual(len(report.titles), 2)
        title = report.titles[0]
        self.assertEqual(title.name, "MON, FILM")
        self.assertEqual(title.duration_seconds, 6_452)
        self.assertEqual(title.chapter_count, 20)
        self.assertEqual(title.stream_count, 3)
        self.assertEqual(title.video_languages, ("fra",))
        self.assertEqual(
            tuple(stream.kind for stream in title.streams),
            ("video", "audio", "subtitle"),
        )
        self.assertEqual(next(iter(title.streams)).language, "fra")
        self.assertEqual(report.warnings, ())

    def test_duration_parser_handles_invalid_value(self) -> None:
        self.assertEqual(parse_duration("0:00:01.5"), 1.5)
        self.assertIsNone(parse_duration("inconnue"))

    def test_relevant_success_diagnostics_are_preserved_without_routine_noise(
        self,
    ) -> None:
        run = MakeMkvRun(
            0,
            """\
MSG:1005,0,1,"MakeMKV started","%1 started","MakeMKV"
MSG:5011,0,0,"Operation successfully completed","ok"
MSG:0,0,0,"AV synchronization issue in stream 1","warning"
MSG:0,0,0,"Empty subtitle track was removed","warning"
""",
        )

        self.assertEqual(len(run.diagnostics), 2)
        self.assertIn("AV synchronization", run.diagnostics[0])
        self.assertIn("Empty subtitle", run.diagnostics[1])

    def test_progress_parser_exposes_total_progress(self) -> None:
        parser = _ProgressParser()
        parser.consume('PRGC:5017,0,"Saving title"\n')
        parser.consume('PRGT:5018,0,"Saving 1 title"\n')

        update = parser.consume("PRGV:16384,32768,65536\n")

        self.assertIsNotNone(update)
        assert update is not None
        self.assertEqual(update.current_label, "Saving title")
        self.assertEqual(update.total_label, "Saving 1 title")
        self.assertEqual(update.current_fraction, 0.25)
        self.assertEqual(update.total_fraction, 0.5)

    def test_scan_profile_maps_substeps_to_one_overall_percentage(self) -> None:
        parser = _ProgressParser(profile="scan")
        parser.consume('PRGC:1,0,"Scanning CD-ROM devices"\n')
        opening = parser.consume("PRGV:65536,65536,65536\n")
        parser.consume('PRGC:2,0,"Scanning contents"\n')
        content = parser.consume("PRGV:32768,0,65536\n")
        parser.consume('PRGC:3,0,"Decrypting data"\n')
        completed = parser.consume("PRGV:65536,0,65536\n")

        assert opening is not None
        assert content is not None
        assert completed is not None
        self.assertAlmostEqual(opening.total_fraction or 0, 0.03)
        self.assertAlmostEqual(content.total_fraction or 0, 0.45)
        self.assertEqual(completed.total_fraction, 1.0)

    def test_rip_profile_reserves_progress_for_the_actual_copy(self) -> None:
        parser = _ProgressParser(profile="rip")
        parser.consume('PRGC:1,0,"Scanning contents"\n')
        analyzed = parser.consume("PRGV:65536,65536,65536\n")
        parser.consume('PRGC:2,0,"Saving to MKV file"\n')
        copied = parser.consume("PRGV:32768,65536,65536\n")

        assert analyzed is not None
        assert copied is not None
        self.assertAlmostEqual(analyzed.total_fraction or 0, 0.25)
        self.assertAlmostEqual(copied.total_fraction or 0, 0.66)

    def test_open_disc_failure_gets_an_actionable_message(self) -> None:
        report = """\
DRV:0,0,999,0,"Lecteur DVD","",""
MSG:5010,0,0,"Failed to open disc","Failed to open disc"
TCOUNT:0
"""
        client = MakeMkvClient("/outil/makemkvcon")
        with (
            patch.object(
                client,
                "_run_streaming",
                return_value=MakeMkvRun(return_code=0, output=report),
            ),
            self.assertRaisesRegex(DiscError, "n'a pas pu ouvrir le disque"),
        ):
            client.scan(0)


class MakeMkvClientTests(unittest.TestCase):
    @patch("movie.core.makemkv.Path.is_file", return_value=True)
    @patch("movie.core.makemkv.shutil.which", return_value="/usr/local/bin/makemkvcon")
    def test_executable_is_found_on_path(
        self, _which: object, _is_file: object
    ) -> None:
        self.assertEqual(find_makemkvcon(), "/usr/local/bin/makemkvcon")

    @patch("movie.core.makemkv.Path.is_file", return_value=False)
    @patch("movie.core.makemkv.shutil.which", return_value=None)
    def test_missing_executable_has_installation_link(
        self, _which: object, _is_file: object
    ) -> None:
        with self.assertRaisesRegex(ToolUnavailableError, "makemkv.com/download"):
            find_makemkvcon()

    def test_drives_filters_reserved_empty_slots(self) -> None:
        client = MakeMkvClient("makemkv-test")
        output = (
            'DRV:0,2,999,0,"Lecteur","FILM","/dev/rdisk4"\nDRV:1,256,999,0,"","",""\n'
        )
        with patch.object(client, "_run", return_value=MakeMkvRun(0, output)):
            drives = client.drives()

        self.assertEqual(len(drives), 1)
        self.assertEqual(drives[0].disc_label, "FILM")

    def test_drive_listing_failure_is_reported(self) -> None:
        client = MakeMkvClient("makemkv-test")
        with (
            patch.object(client, "_run", return_value=MakeMkvRun(2, "erreur")),
            self.assertRaisesRegex(MakeMkvError, "Impossible de lister"),
        ):
            client.drives()

    def test_scan_builds_a_disc_and_reports_tool_failure(self) -> None:
        client = MakeMkvClient("makemkv-test")
        with patch.object(
            client,
            "_run_streaming",
            return_value=MakeMkvRun(0, DVD_SCAN),
        ):
            scan = client.scan(0)
        self.assertEqual(scan.drive.disc_label, "MON, FILM")
        self.assertEqual(len(scan.titles), 2)

        with (
            patch.object(
                client,
                "_run_streaming",
                return_value=MakeMkvRun(2, "lecture impossible"),
            ),
            self.assertRaisesRegex(MakeMkvError, "lecture impossible"),
        ):
            client.scan(0)

    def test_iso_scan_uses_makemkv_source_and_a_synthetic_drive(self) -> None:
        client = MakeMkvClient("makemkv-test")
        with TemporaryDirectory() as temporary_directory:
            iso = Path(temporary_directory) / "mon film.iso"
            iso.write_bytes(b"iso")
            with patch.object(
                client,
                "_run_streaming",
                return_value=MakeMkvRun(0, DVD_SCAN),
            ) as runner:
                scan = client.scan_iso(iso)

        arguments = runner.call_args.args[0]
        self.assertIn(f"iso:{iso.resolve()}", arguments)
        self.assertEqual(scan.drive.index, -1)
        self.assertEqual(scan.drive.name, "Image ISO")
        self.assertEqual(scan.drive.device_path, str(iso.resolve()))

    def test_missing_iso_is_refused_before_launching_makemkv(self) -> None:
        with self.assertRaisesRegex(DiscError, "introuvable"):
            MakeMkvClient("makemkv-test").scan_iso(Path("/missing/movie.iso"))

    def test_rip_title_passes_identifiers_and_reports_failure(self) -> None:
        client = MakeMkvClient("makemkv-test")
        with TemporaryDirectory() as temporary_directory:
            with patch.object(
                client,
                "_run_streaming",
                return_value=MakeMkvRun(0, "ok"),
            ) as runner:
                result = client.rip_title(3, 7, Path(temporary_directory))
            self.assertEqual(result.output, "ok")
            arguments = runner.call_args.args[0]
            self.assertIn("disc:3", arguments)
            self.assertIn("7", arguments)

            with (
                patch.object(
                    client,
                    "_run_streaming",
                    return_value=MakeMkvRun(1, "disque illisible"),
                ),
                self.assertRaisesRegex(MakeMkvError, "disque illisible"),
            ):
                client.rip_title(3, 7, Path(temporary_directory))

    def test_rip_iso_title_uses_the_iso_source(self) -> None:
        client = MakeMkvClient("makemkv-test")
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            iso = directory / "film.iso"
            iso.write_bytes(b"iso")
            with patch.object(
                client,
                "_run_streaming",
                return_value=MakeMkvRun(0, "ok"),
            ) as runner:
                client.rip_iso_title(iso, 4, directory)

        arguments = runner.call_args.args[0]
        self.assertIn(f"iso:{iso.resolve()}", arguments)
        self.assertIn("4", arguments)

    @patch("movie.core.makemkv.subprocess.Popen")
    def test_streaming_process_forwards_progress(self, popen: MagicMock) -> None:
        process = popen.return_value
        process.stdout = _ClosableLines(
            [
                'PRGC:5017,0,"Saving title"\n',
                'PRGT:5018,0,"Saving 1 title"\n',
                "PRGV:1,2,4\n",
            ]
        )
        process.wait.return_value = 0
        updates: list[ProgressUpdate] = []

        result = MakeMkvClient("makemkv-test")._run_streaming(
            ("info", "disc:0"),
            on_progress=updates.append,
        )

        self.assertEqual(result.return_code, 0)
        self.assertEqual(updates[0].total_fraction, 0.5)
        self.assertTrue(process.stdout.closed)

    @patch("movie.core.makemkv.subprocess.run")
    def test_captured_process_combines_outputs(self, run: MagicMock) -> None:
        run.return_value = CompletedProcess([], 0, "sortie", "diagnostic")
        result = MakeMkvClient("makemkv-test")._run(("info",))
        self.assertEqual(result.output, "sortie\ndiagnostic")

    @patch("movie.core.makemkv.subprocess.run", side_effect=OSError)
    def test_captured_process_launch_failure_is_reported(self, _run: object) -> None:
        with self.assertRaisesRegex(MakeMkvError, "Impossible de lancer"):
            MakeMkvClient("makemkv-test")._run(("info",))


class _ClosableLines(list[str]):
    closed = False

    def close(self) -> None:
        self.closed = True

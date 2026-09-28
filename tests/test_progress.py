"""Tests de la barre de progression terminale."""

from __future__ import annotations

import unittest
from io import StringIO
from unittest.mock import MagicMock, patch

from movie.core.models import ProgressUpdate
from movie.terminal import _format_eta, _progress_bar, _ProgressRenderer


class ProgressRendererTests(unittest.TestCase):
    def test_eta_format_handles_hour_boundaries(self) -> None:
        self.assertEqual(_format_eta(59), "59 s")
        self.assertEqual(_format_eta(60), "1 min")
        self.assertEqual(_format_eta(3_600), "1 h")
        self.assertEqual(_format_eta(7_199), "2 h")
        self.assertEqual(_format_eta(7_260), "2 h 01 min")

    def test_short_step_gets_a_compact_summary(self) -> None:
        clock = _Clock()
        output = StringIO()
        renderer = _ProgressRenderer(
            operation_label="Analyse du disque", clock=clock, stream=output
        )

        renderer(_update(0.5))
        clock.now = 5
        renderer.finish()

        self.assertEqual(output.getvalue(), "✓ Analyse du disque terminée en 5 s\n")

    def test_bar_appears_after_twelve_seconds_and_is_completed(self) -> None:
        clock = _Clock()
        output = StringIO()
        renderer = _ProgressRenderer(
            operation_label="Analyse du disque", clock=clock, stream=output
        )

        renderer(_update(0.1))
        for elapsed, fraction in (
            (10, 0.2),
            (11, 0.3),
            (12, 0.4),
            (13, 0.5),
            (14, 0.6),
            (15, 0.7),
        ):
            clock.now = elapsed
            renderer(_update(fraction))
        renderer.finish()

        self.assertIn(" 70.0 % — Titre en cours", output.getvalue())
        self.assertIn("100.0 % — Analyse du disque terminée en 15 s", output.getvalue())
        self.assertIn("· ~", output.getvalue())

    def test_new_step_reuses_the_same_line(self) -> None:
        clock = _Clock()
        output = StringIO()
        renderer = _ProgressRenderer(clock=clock, stream=output)

        renderer(_update(0.2, label="Lecture"))
        clock.now = 13
        renderer(_update(0.7, label="Lecture"))
        renderer(_update(0.1, label="Copie"))
        clock.now = 26
        renderer(_update(0.4, label="Copie"))
        renderer.finish()

        rendered = output.getvalue()
        self.assertIn(" — Lecture", rendered)
        self.assertIn(" — Copie", rendered)
        self.assertEqual(rendered.count("\n"), 1)

    def test_completed_fraction_waits_for_the_whole_operation(self) -> None:
        output = StringIO()
        renderer = _ProgressRenderer(stream=output)

        renderer(_update(1.0))
        self.assertEqual(output.getvalue(), "")
        renderer.finish()

        self.assertEqual(output.getvalue(), "✓ Opération terminée en moins de 1 s\n")

    def test_cancel_does_not_mark_step_as_completed(self) -> None:
        output = StringIO()
        renderer = _ProgressRenderer(stream=output)

        renderer(_update(0.2))
        renderer.cancel()

        self.assertNotIn("terminé", output.getvalue())

    @patch("movie.terminal.threading.Timer")
    def test_real_terminal_timer_uses_configured_delay(
        self, timer: MagicMock
    ) -> None:
        output = _TtyOutput()
        renderer = _ProgressRenderer(threshold_seconds=3.5, stream=output)

        renderer(_update(0.2))

        timer.assert_called_once()
        self.assertEqual(timer.call_args.args[0], 3.5)
        timer.call_args.args[1]()
        self.assertIn("20.0 % — Titre en cours", output.getvalue())

    @patch("movie.terminal.threading.Timer")
    def test_delay_starts_before_the_first_tool_update(self, timer: MagicMock) -> None:
        output = _TtyOutput()
        renderer = _ProgressRenderer(threshold_seconds=4, stream=output)

        renderer.start("Préparation")

        timer.assert_called_once()
        self.assertEqual(timer.call_args.args[0], 4)

    def test_global_fraction_is_preferred_and_never_moves_backwards(self) -> None:
        clock = _Clock()
        output = StringIO()
        renderer = _ProgressRenderer(
            threshold_seconds=0,
            refresh_interval_seconds=0,
            clock=clock,
            stream=output,
        )

        renderer(_update(0.9, total=0.3, label="Lecture"))
        renderer(_update(0.1, total=0.2, label="Copie"))
        renderer.finish()

        rendered = output.getvalue()
        self.assertIn(" 30.0 % — Lecture", rendered)
        self.assertIn(" 30.0 % — Copie", rendered)

    def test_identical_updates_are_not_rendered_repeatedly(self) -> None:
        clock = _Clock()
        output = StringIO()
        renderer = _ProgressRenderer(
            threshold_seconds=0,
            refresh_interval_seconds=0,
            clock=clock,
            stream=output,
        )

        renderer(_update(0.2))
        renderer(_update(0.2))
        renderer(_update(0.2))
        renderer.finish()

        self.assertEqual(output.getvalue().count(" 20.0 %"), 1)

    def test_pause_excludes_user_thinking_time_from_the_summary(self) -> None:
        clock = _Clock()
        output = StringIO()
        renderer = _ProgressRenderer(clock=clock, stream=output)

        renderer.start("Extraction")
        clock.now = 4
        renderer.pause()
        clock.now = 104
        renderer.resume("Conversion")
        clock.now = 110
        renderer.finish()

        self.assertIn("terminée en 10 s", output.getvalue())

    def test_bar_clamps_invalid_fraction(self) -> None:
        self.assertEqual(_progress_bar(-1), "░" * 20)
        self.assertEqual(_progress_bar(2), "█" * 20)


class _Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


class _TtyOutput(StringIO):
    def isatty(self) -> bool:
        return True


def _update(
    fraction: float,
    *,
    total: float | None = None,
    label: str = "Titre en cours",
) -> ProgressUpdate:
    return ProgressUpdate(
        current_label=label,
        total_label="Numérisation du DVD",
        current_fraction=fraction,
        total_fraction=fraction if total is None else total,
    )

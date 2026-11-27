"""Unit tests for the blind direction logic.

Reproduces the S_08 case at 2026-09-25: downward pressure is already close to
its causal threshold while upward pressure is exactly zero, so the direction
must be downside regardless of the state label.
"""

from __future__ import annotations

import unittest

from core.blind_break_detector import direction_from_snapshot


class DirectionFromSnapshotTest(unittest.TestCase):
    def _info(self, **overrides: float) -> dict[str, object]:
        base: dict[str, object] = {
            "state": "PRE_BREAK_TENSION",
            "ph_up": 0.0,
            "q_up": 0.02,
            "ph_down": 0.19,
            "q_down": 0.20,
            "trend_slope_z": 0.5,
            "q_trend": 2.0,
            "drawdown_norm": -5.78,
            "q_dd": -7.0,
            "l1_slope_break": 0.0,
            "q_slope": 0.01,
        }
        base.update(overrides)
        return base

    def test_downside_wins_with_zero_upward_pressure(self) -> None:
        direction, up, down = direction_from_snapshot(self._info())
        self.assertEqual(direction, "downside break risk")
        self.assertGreaterEqual(down, 0.90)
        self.assertLess(up, 0.5)

    def test_threshold_ratio_at_ninety_percent(self) -> None:
        direction, _, down = direction_from_snapshot(
            self._info(ph_up=0.0, ph_down=0.09, q_down=0.10)
        )
        self.assertAlmostEqual(down, 0.90)
        self.assertEqual(direction, "downside break risk")

    def test_upside_wins_when_upward_pressure_dominates(self) -> None:
        direction, up, down = direction_from_snapshot(
            self._info(ph_up=0.019, q_up=0.02, ph_down=0.0)
        )
        self.assertGreater(up, down)
        self.assertEqual(direction, "upside break risk")

    def test_negative_drawdown_ratio_is_positive(self) -> None:
        direction, _, down = direction_from_snapshot(
            self._info(ph_down=0.0, drawdown_norm=-6.0, q_dd=-6.0)
        )
        self.assertGreater(down, 0.0)
        self.assertEqual(direction, "downside break risk")

    def test_zero_threshold_is_safe(self) -> None:
        direction, up, down = direction_from_snapshot(
            self._info(q_up=0.0, q_down=0.0, drawdown_norm=-0.1, q_dd=-100.0)
        )
        self.assertEqual(up, 0.25)
        self.assertLess(down, 1.0)
        self.assertEqual(direction, "upside break risk")


if __name__ == "__main__":
    unittest.main()

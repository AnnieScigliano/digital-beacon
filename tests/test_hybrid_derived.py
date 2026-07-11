"""Tests for VoiceParameterStore's derived pair variables (Phase 1 of the
two-phone hug/dance roadmap): sway energy, pair synchrony, relative facing
angle, and pair energy — all computed from the rolling motion-history buffer
introduced alongside multi-device support (see test_hybrid_multidevice.py).

Run with:
    python -m pytest tests/test_hybrid_derived.py -q
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digital_beacon.state import VoiceParameterStore  # noqa: E402


class TestHybridDerived(unittest.TestCase):
    def setUp(self) -> None:
        self.store = VoiceParameterStore()

    def test_sway_energy_zero_when_still(self) -> None:
        for _ in range(5):
            self.store.update_hybrid_imu(0, 0, 0, 0.0, 0.0, 0.0, device="A")
        self.assertAlmostEqual(self.store.get_hybrid_sway_energy("A"), 0.0, delta=0.001)

    def test_sway_energy_matches_rms_of_varying_accel(self) -> None:
        for az in [3.0, 4.0, 3.0, 4.0]:
            self.store.update_hybrid_imu(0, 0, 0, 0.0, 0.0, az, device="A")
        # accel_mag sequence: 3,4,3,4 -> RMS = sqrt((9+16+9+16)/4) = sqrt(12.5)
        self.assertAlmostEqual(self.store.get_hybrid_sway_energy("A"), 12.5 ** 0.5, delta=0.001)

    def test_sway_energy_unknown_device_is_zero(self) -> None:
        self.assertEqual(self.store.get_hybrid_sway_energy("Z"), 0.0)

    def test_synchrony_high_for_matching_motion(self) -> None:
        for v in [1.0, 2.0, 1.0, 2.0, 1.0, 2.0]:
            self.store.update_hybrid_imu(0, 0, 0, 0.0, 0.0, v, device="A")
            self.store.update_hybrid_imu(0, 0, 0, 0.0, 0.0, v, device="B")
        self.assertGreater(self.store.get_hybrid_synchrony("A", "B"), 0.9)

    def test_synchrony_zero_for_flat_signal(self) -> None:
        # Device A never varies -> zero variance -> correlation undefined -> 0.0,
        # not "perfectly in sync" and not negative.
        for v in [1.0, 5.0, 1.0, 5.0, 1.0, 5.0]:
            self.store.update_hybrid_imu(0, 0, 0, 0.0, 0.0, 1.0, device="A")
            self.store.update_hybrid_imu(0, 0, 0, 0.0, 0.0, v, device="B")
        self.assertEqual(self.store.get_hybrid_synchrony("A", "B"), 0.0)

    def test_synchrony_zero_when_device_missing(self) -> None:
        self.store.update_hybrid_imu(0, 0, 0, 0.0, 0.0, 1.0, device="A")
        self.assertEqual(self.store.get_hybrid_synchrony("A", "B"), 0.0)

    def test_relative_facing_wraps_correctly(self) -> None:
        self.store.update_hybrid_imu(170.0, 0, 0, 0.0, 0.0, 0.0, device="A")
        self.store.update_hybrid_imu(-170.0, 0, 0, 0.0, 0.0, 0.0, device="B")
        # 170 - (-170) = 340 -> wraps to -20
        self.assertAlmostEqual(self.store.get_hybrid_relative_facing("A", "B"), -20.0, delta=0.001)

    def test_relative_facing_zero_when_device_missing(self) -> None:
        self.store.update_hybrid_imu(45.0, 0, 0, 0.0, 0.0, 0.0, device="A")
        self.assertEqual(self.store.get_hybrid_relative_facing("A", "B"), 0.0)

    def test_pair_energy_is_mean_of_both(self) -> None:
        self.store.update_hybrid_imu(0, 0, 0, 0.0, 0.0, 2.0, device="A")
        self.store.update_hybrid_imu(0, 0, 0, 0.0, 0.0, 4.0, device="B")
        self.assertAlmostEqual(self.store.get_hybrid_pair_energy("A", "B"), 3.0, delta=0.001)

    def test_panic_clears_motion_history(self) -> None:
        self.store.update_hybrid_imu(0, 0, 0, 0.0, 0.0, 5.0, device="A")
        self.store.panic()
        self.assertEqual(self.store.get_hybrid_sway_energy("A"), 0.0)

    def test_to_dict_includes_derived_block(self) -> None:
        self.store.update_hybrid_imu(0, 0, 0, 0.0, 0.0, 2.0, device="A")
        self.store.update_hybrid_imu(0, 0, 0, 0.0, 0.0, 4.0, device="B")
        derived = self.store.to_dict()["hybrid"]["derived"]
        self.assertAlmostEqual(derived["sway_energy_a"], 2.0, delta=0.01)
        self.assertAlmostEqual(derived["sway_energy_b"], 4.0, delta=0.01)
        self.assertAlmostEqual(derived["pair_energy"], 3.0, delta=0.01)


if __name__ == "__main__":
    unittest.main()

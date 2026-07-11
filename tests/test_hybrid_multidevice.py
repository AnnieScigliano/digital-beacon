"""Tests for VoiceParameterStore's multi-device hybrid sensor state (Phase 0
of the two-phone hug/dance plumbing) — per-device isolation and the
default-to-"A" backward-compat path used by every existing single-phone caller.

Run with:
    python -m pytest tests/test_hybrid_multidevice.py -q
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digital_beacon.state import VoiceParameterStore  # noqa: E402


class TestHybridMultiDevice(unittest.TestCase):
    def setUp(self) -> None:
        self.store = VoiceParameterStore()

    def test_default_device_is_a(self) -> None:
        self.store.update_hybrid_imu(45.0, 10.0, 0.0, 0.1, 0.2, 9.8)
        yaw, pitch, roll, ax, ay, az = self.store.get_hybrid_imu()
        self.assertEqual((yaw, pitch, roll, ax, ay, az), (45.0, 10.0, 0.0, 0.1, 0.2, 9.8))
        self.assertEqual(self.store.get_hybrid_devices(), ["A"])

    def test_devices_are_isolated(self) -> None:
        self.store.update_hybrid_imu(30.0, 0.0, 0.0, 0.0, 0.0, 9.8, device="A")
        self.store.update_hybrid_imu(-60.0, 5.0, 0.0, 0.0, 0.0, 9.8, device="B")

        yaw_a, *_ = self.store.get_hybrid_imu("A")
        yaw_b, *_ = self.store.get_hybrid_imu("B")
        self.assertEqual(yaw_a, 30.0)
        self.assertEqual(yaw_b, -60.0)
        self.assertEqual(set(self.store.get_hybrid_devices()), {"A", "B"})

    def test_unknown_device_reads_as_zero(self) -> None:
        self.store.update_hybrid_imu(30.0, 0.0, 0.0, 0.0, 0.0, 9.8, device="A")
        yaw, pitch, roll, ax, ay, az = self.store.get_hybrid_imu("C")
        self.assertEqual((yaw, pitch, roll, ax, ay, az), (0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
        self.assertNotIn("C", self.store.get_hybrid_devices())

    def test_hr_devices_are_isolated(self) -> None:
        self.store.update_hybrid_hr(72.0, 833.0, 1.0, device="A")
        self.store.update_hybrid_hr(95.0, 631.0, 0.8, device="B")
        self.assertEqual(self.store.get_hybrid_hr("A"), (72.0, 833.0, 1.0))
        self.assertEqual(self.store.get_hybrid_hr("B"), (95.0, 631.0, 0.8))

    def test_to_dict_mirrors_device_a_and_lists_all_devices(self) -> None:
        self.store.update_hybrid_imu(30.0, 0.0, 0.0, 0.0, 0.0, 9.8, device="A")
        self.store.update_hybrid_imu(-60.0, 5.0, 0.0, 0.0, 0.0, 9.8, device="B")
        hybrid = self.store.to_dict()["hybrid"]

        # Legacy flat fields mirror device "A" — old dashboard JS keeps working.
        self.assertEqual(hybrid["yaw"], 30.0)
        # New per-device breakdown carries both.
        self.assertEqual(hybrid["devices"]["A"]["yaw"], 30.0)
        self.assertEqual(hybrid["devices"]["B"]["yaw"], -60.0)

    def test_panic_clears_all_devices(self) -> None:
        self.store.update_hybrid_imu(30.0, 0.0, 0.0, 0.0, 0.0, 9.8, device="A")
        self.store.update_hybrid_imu(-60.0, 5.0, 0.0, 0.0, 0.0, 9.8, device="B")
        self.store.panic()
        self.assertEqual(self.store.get_hybrid_devices(), [])
        self.assertEqual(self.store.get_hybrid_imu("A"), (0.0, 0.0, 0.0, 0.0, 0.0, 0.0))


if __name__ == "__main__":
    unittest.main()

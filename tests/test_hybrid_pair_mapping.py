"""Tests for the Shaper's per-person hybrid pair mapping (Phase 2 of the
two-phone hug/dance roadmap, opt-in via config.HYBRID_PAIR_MODE_ENABLED):
harmonic-register assignment, the per-person pan/gain math, and — end to
end through the real audio callback — that device A's motion only audibly
affects register A, device B's only register B, and the shared band stays
silent until the pair is synchronized. Also covers the reverse: with pair
mode at its default (disabled), the original single-device Hybrid mode
(whole-field yaw rotation, HR-driven master pulse) must be completely
unaffected — that's what everything built before this MVP relies on.

Requires numpy + sounddevice (this project's own venv has both; the bare
system Python used for the other tests/ files does not):
    venv/bin/python -m pytest tests/test_hybrid_pair_mapping.py -q
or
    venv/bin/python -m unittest tests.test_hybrid_pair_mapping -v
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from digital_beacon import config  # noqa: E402
from digital_beacon.audio_engine import (  # noqa: E402
    AudioEngine,
    _hybrid_register,
    _person_pan_gain_mod,
)
from digital_beacon.state import VoiceParameterStore  # noqa: E402


def _make_engine(store: VoiceParameterStore) -> AudioEngine:
    """Build an AudioEngine without opening a real audio stream — the
    callback only touches _store/_voice_state/_sample_rate, so we can
    bypass __init__'s sounddevice-device check entirely."""
    engine = AudioEngine.__new__(AudioEngine)
    engine._store = store
    engine._sample_rate = 44100
    engine._block_size = 256
    engine._voice_state = {}
    engine._record_sink = None
    return engine


def _run_settled(engine: AudioEngine, frames: int = 256, n_calls: int = 3) -> np.ndarray:
    """Run the callback a few times so attack envelopes reach steady state,
    return the last block's stereo mix."""
    outdata = np.zeros((frames, 2), dtype=np.float32)
    for _ in range(n_calls):
        outdata = np.zeros((frames, 2), dtype=np.float32)
        engine._audio_callback(outdata, frames, None, None)
    return outdata


class TestHybridRegisterAssignment(unittest.TestCase):
    def test_each_range_maps_to_its_own_register(self) -> None:
        for n in config.HYBRID_HARMONICS_A:
            self.assertEqual(_hybrid_register(n), "A")
        for n in config.HYBRID_HARMONICS_B:
            self.assertEqual(_hybrid_register(n), "B")
        for n in config.HYBRID_HARMONICS_SHARED:
            self.assertEqual(_hybrid_register(n), "SHARED")

    def test_registers_do_not_overlap(self) -> None:
        a, b, shared = (set(config.HYBRID_HARMONICS_A), set(config.HYBRID_HARMONICS_B),
                        set(config.HYBRID_HARMONICS_SHARED))
        self.assertEqual(a & b, set())
        self.assertEqual(a & shared, set())
        self.assertEqual(b & shared, set())

    def test_harmonics_outside_any_range_are_untouched(self) -> None:
        untouched = set(range(1, config.N_BANDS + 1)) - set(config.HYBRID_HARMONICS_A) \
            - set(config.HYBRID_HARMONICS_B) - set(config.HYBRID_HARMONICS_SHARED)
        self.assertTrue(untouched)  # by design, some harmonics are left alone
        for n in untouched:
            self.assertEqual(_hybrid_register(n), "NONE")


class TestPersonPanGainMod(unittest.TestCase):
    def test_zero_motion_is_neutral(self) -> None:
        pan, gain = _person_pan_gain_mod(0.0, 0.0, 0.0)
        self.assertEqual(pan, 0.0)
        self.assertEqual(gain, 0.0)

    def test_yaw_pans_symmetrically(self) -> None:
        pan_pos, _ = _person_pan_gain_mod(90.0, 0.0, 0.0)
        pan_neg, _ = _person_pan_gain_mod(-90.0, 0.0, 0.0)
        self.assertGreater(pan_pos, 0.0)
        self.assertAlmostEqual(pan_pos, -pan_neg, delta=1e-9)

    def test_sway_and_lean_increase_gain(self) -> None:
        _, gain = _person_pan_gain_mod(0.0, 45.0, config.HYBRID_PAIR_ENERGY_REF)
        self.assertGreater(gain, 0.0)


class TestHybridPairCallback(unittest.TestCase):
    """End-to-end through the real _audio_callback, no mocking of the
    mapping math — only the sounddevice stream itself is bypassed.

    Pair mode is opt-in and OFF by default (config.HYBRID_PAIR_MODE_ENABLED)
    so the single-device Hybrid mode used by everything built before it is
    unaffected — these tests explicitly turn it on to exercise the new path.
    """

    def setUp(self) -> None:
        self._orig_pair_mode = config.HYBRID_PAIR_MODE_ENABLED
        config.HYBRID_PAIR_MODE_ENABLED = True

    def tearDown(self) -> None:
        config.HYBRID_PAIR_MODE_ENABLED = self._orig_pair_mode

    def test_person_a_yaw_pans_only_register_a(self) -> None:
        n = next(iter(config.HYBRID_HARMONICS_A))
        store = VoiceParameterStore()
        store.voice_on(n, voice_id=1, freq=110.0, gain=0.3)
        store.set_attack(n, 0.0)
        engine = _make_engine(store)

        store.update_hybrid_imu(180.0, 0.0, 0.0, 0.0, 0.0, 0.0, device="A")
        mix = _run_settled(engine)
        l_energy = float(np.sum(mix[:, 0] ** 2))
        r_energy = float(np.sum(mix[:, 1] ** 2))
        self.assertGreater(r_energy, l_energy)

    def test_person_b_yaw_does_not_move_register_a(self) -> None:
        n = next(iter(config.HYBRID_HARMONICS_A))
        store = VoiceParameterStore()
        store.voice_on(n, voice_id=1, freq=110.0, gain=0.3)
        store.set_attack(n, 0.0)
        engine = _make_engine(store)

        # Only B moves — A's own register should stay centered (untouched).
        store.update_hybrid_imu(180.0, 0.0, 0.0, 0.0, 0.0, 0.0, device="B")
        mix = _run_settled(engine)
        l_energy = float(np.sum(mix[:, 0] ** 2))
        r_energy = float(np.sum(mix[:, 1] ** 2))
        self.assertAlmostEqual(l_energy, r_energy, delta=max(l_energy, r_energy) * 0.01)

    def test_person_b_sway_boosts_only_register_b_gain(self) -> None:
        n_a = next(iter(config.HYBRID_HARMONICS_A))
        n_b = next(iter(config.HYBRID_HARMONICS_B))
        store = VoiceParameterStore()
        store.voice_on(n_a, voice_id=1, freq=110.0, gain=0.3)
        store.voice_on(n_b, voice_id=2, freq=220.0, gain=0.3)
        store.set_attack(n_a, 0.0)
        store.set_attack(n_b, 0.0)
        engine = _make_engine(store)

        baseline = _run_settled(engine)
        baseline_energy = float(np.sum(baseline ** 2))

        # B sways at the full reference energy; A stays still.
        store.update_hybrid_imu(0.0, 0.0, 0.0, 0.0, 0.0, config.HYBRID_PAIR_ENERGY_REF, device="B")
        after = _run_settled(engine)
        after_energy = float(np.sum(after ** 2))

        self.assertGreater(after_energy, baseline_energy)

    def test_shared_band_silent_until_synchronized(self) -> None:
        n_shared = next(iter(config.HYBRID_HARMONICS_SHARED))
        store = VoiceParameterStore()
        store.voice_on(n_shared, voice_id=1, freq=330.0, gain=0.3)
        store.set_attack(n_shared, 0.0)
        engine = _make_engine(store)

        # Neither device has reported yet -> synchrony defaults to 0 -> silent.
        silent = _run_settled(engine)
        self.assertAlmostEqual(float(np.sum(silent ** 2)), 0.0, delta=1e-6)

        # Feed identical, correlated motion on both devices -> synchrony -> ~1.
        for v in [1.0, 2.0, 1.0, 2.0, 1.0, 2.0]:
            store.update_hybrid_imu(0, 0, 0, 0.0, 0.0, v, device="A")
            store.update_hybrid_imu(0, 0, 0, 0.0, 0.0, v, device="B")
        bloom = _run_settled(engine)
        self.assertGreater(float(np.sum(bloom ** 2)), 0.0)

    def test_cross_bleed_grows_with_synchrony(self) -> None:
        # Same amplitude sine for both devices in both scenarios -- so
        # sway_energy and therefore pair_energy (and its master-gain
        # "breathing" pulse) stay about the same regardless of relative
        # timing. Only whether A and B share a frequency changes, which
        # swings synchrony from ~0 to ~1 without confounding the comparison
        # with a loudness change from pair_energy. (An earlier version of
        # this test compared "B silent" vs "B moving hard" on the SAME
        # store, which also changes pair_energy — a real confound, caught by
        # comparing against a real running dry-run session.) Two independent
        # stores, not one fed twice: the sway/synchrony rolling window is
        # keyed on wall-clock time, and a unit test runs both feeds within
        # the same ~2s window, so a second feed on the same store dilutes
        # into the first rather than replacing it (also caught live).
        def feed(store, device, freq, amp=2.0, n=20, dt=0.05):
            for i in range(n):
                t = i * dt
                az = amp * math.sin(2 * math.pi * freq * t)
                store.update_hybrid_imu(0, 0, 0, 0.0, 0.0, az, device=device)

        n_a = next(iter(config.HYBRID_HARMONICS_A))

        # Desynced: different frequencies, same amplitude.
        store_desync = VoiceParameterStore()
        store_desync.voice_on(n_a, voice_id=1, freq=110.0, gain=0.3)
        store_desync.set_attack(n_a, 0.0)
        engine_desync = _make_engine(store_desync)
        feed(store_desync, "A", freq=0.9)
        feed(store_desync, "B", freq=1.3)
        sync_before = store_desync.get_hybrid_synchrony("A", "B")
        pair_energy_before = store_desync.get_hybrid_pair_energy("A", "B")
        desynced = _run_settled(engine_desync)
        desynced_energy = float(np.sum(desynced ** 2))

        # Synced: same frequency for both, same amplitude as before.
        store_sync = VoiceParameterStore()
        store_sync.voice_on(n_a, voice_id=1, freq=110.0, gain=0.3)
        store_sync.set_attack(n_a, 0.0)
        engine_sync = _make_engine(store_sync)
        feed(store_sync, "A", freq=0.9)
        feed(store_sync, "B", freq=0.9)
        sync_after = store_sync.get_hybrid_synchrony("A", "B")
        pair_energy_after = store_sync.get_hybrid_pair_energy("A", "B")
        synced = _run_settled(engine_sync)
        synced_energy = float(np.sum(synced ** 2))

        self.assertLess(sync_before, 0.3)
        self.assertGreater(sync_after, 0.9)
        # Sanity: pair energy didn't swing much between the two scenarios --
        # the energy difference we're about to check is attributable to
        # cross-bleed, not incidental loudness drift from the master pulse.
        self.assertAlmostEqual(pair_energy_before, pair_energy_after, delta=0.3)

        self.assertGreater(synced_energy, desynced_energy)


class TestLegacySingleDeviceModeUnaffected(unittest.TestCase):
    """config.HYBRID_PAIR_MODE_ENABLED defaults to False — this is the
    single-device Hybrid mode that predates the two-phone pair mapping, and
    must behave exactly as it always has: one phone's yaw pans the WHOLE
    32-band field (not just its own 10-harmonic register), and HR still
    drives the master pulse. No setUp override here — this exercises the
    real default."""

    def test_pair_mode_defaults_to_disabled(self) -> None:
        self.assertFalse(config.HYBRID_PAIR_MODE_ENABLED)

    def test_single_device_yaw_still_pans_the_whole_field(self) -> None:
        # Pick a harmonic OUTSIDE register A (e.g. one in B's, or the shared
        # band, or the untouched tail) — under pair mode this would stay
        # centered; under the legacy default it must still respond, because
        # there's no such thing as "device A's own register" pre-pair-mode.
        n = next(iter(config.HYBRID_HARMONICS_B))
        store = VoiceParameterStore()
        store.voice_on(n, voice_id=1, freq=220.0, gain=0.3)
        store.set_attack(n, 0.0)
        engine = _make_engine(store)

        store.update_hybrid_imu(180.0, 0.0, 0.0, 0.0, 0.0, 0.0)  # device defaults to "A"
        mix = _run_settled(engine)
        l_energy = float(np.sum(mix[:, 0] ** 2))
        r_energy = float(np.sum(mix[:, 1] ** 2))
        self.assertGreater(r_energy, l_energy)

    def test_hr_still_drives_master_pulse_by_default(self) -> None:
        n = next(iter(config.HYBRID_HARMONICS_A))
        store = VoiceParameterStore()
        store.voice_on(n, voice_id=1, freq=110.0, gain=0.3)
        store.set_attack(n, 0.0)
        engine = _make_engine(store)

        baseline = _run_settled(engine)
        baseline_energy = float(np.sum(baseline ** 2))

        store.update_hybrid_hr(102.0, 588.0, 1.0)  # well above the 72 bpm reference -> positive pulse
        after = _run_settled(engine)
        after_energy = float(np.sum(after ** 2))

        self.assertGreater(after_energy, baseline_energy)


if __name__ == "__main__":
    unittest.main()

"""Real-time additive synthesis engine for the Shaper.

Adapted from NaturalHarmony/harmonic_shaper/audio_engine.py:
- Same architecture: numpy + sounddevice PortAudio callback.
- Bumped MAX_VOICES to config.MAX_VOICES (32).
- Equal-power stereo pan preserved.
- Phase accumulator continuity preserved across callbacks.
- The audio callback runs in a C thread, so the snapshot read must be
  brief and lock-free from its perspective — we rely on dict copy being
  fast for ≤32 entries.
"""

import logging
import threading
from typing import Optional, Union

import numpy as np

import math

try:
    import sounddevice as sd
    HAS_SOUNDDEVICE = True
except ImportError:
    HAS_SOUNDDEVICE = False
    sd = None  # type: ignore

from .state import VoiceParameterStore
from . import config

log = logging.getLogger(__name__)


class AudioEngine:
    """Stereo additive synthesis — one pure sine per active voice."""

    def __init__(
        self,
        store: VoiceParameterStore,
        sample_rate: int = config.AUDIO_SAMPLE_RATE,
        device: Optional[Union[int, str]] = config.AUDIO_DEVICE,
        channels: int = 2,
        blocksize: int = config.AUDIO_BLOCK_SIZE,
    ) -> None:
        if not HAS_SOUNDDEVICE:
            raise ImportError("sounddevice is required. pip install sounddevice")
        self._store = store
        self._sample_rate = sample_rate
        self._block_size = blocksize
        self._device = device
        self._stream: Optional["sd.OutputStream"] = None
        # Per-voice state: {harmonic_n: {"phase": float, "env": float, "params": VoiceParams}}
        # env ramps 0→1 on attack, 1→0 on release. Voices with env≈0 and inactive are pruned.
        self._voice_state: dict[int, dict] = {}
        self._running = False
        self._lock = threading.RLock()
        self._record_sink: Optional[list] = None

    def start(self) -> None:
        if self._running:
            return
        self._stream = sd.OutputStream(
            samplerate=self._sample_rate,
            blocksize=self._block_size,
            channels=2,
            dtype="float32",
            device=self._device,
            callback=self._audio_callback,
            finished_callback=self._on_stream_finished,
        )
        self._stream.start()
        self._running = True
        log.info("Shaper audio: sr=%d block=%d device=%s",
                 self._sample_rate, self._block_size, self._device)

    def stop(self) -> None:
        self._running = False
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as exc:
                log.warning("Error closing stream: %s", exc)
            self._stream = None
        log.info("Shaper audio stopped.")

    # ─── Recording tap ──────────────────────────────────────────────────
    # The Recorder calls attach_recorder(list) on start and detach_recorder
    # on stop. The audio callback appends the final mix (post-sidechain,
    # pre-limiter) to the list — copy of the buffer so it survives the
    # callback returning. Zero overhead when no recorder attached.

    def attach_recorder(self, sink: list) -> None:
        """Tap the final mix into `sink` (a Python list of ndarrays)."""
        with self._lock:
            self._record_sink = sink

    def detach_recorder(self) -> None:
        """Stop tapping. The sink list stays (Recorder owns it)."""
        with self._lock:
            self._record_sink = None

    @property
    def is_running(self) -> bool:
        return bool(self._running and self._stream and self._stream.active)

    @staticmethod
    def list_devices() -> str:
        if HAS_SOUNDDEVICE:
            return str(sd.query_devices())
        return "(sounddevice not installed)"

    def _audio_callback(self, outdata: np.ndarray, frames: int, time_info, status) -> None:
        if status:
            log.debug("Audio status: %s", status)
        dt = frames / self._sample_rate
        voices = self._store.get_snapshot()  # active voices only
        active_ns = set(voices.keys())
        tracked_ns = set(self._voice_state.keys())

        # ── Add new voices (just became active) ──────────────────────
        for n in active_ns - tracked_ns:
            self._voice_state[n] = {"phase": 0.0, "env": 0.0, "params": voices[n]}

        # ── Mark released voices (was tracked, no longer active) ────
        for n in tracked_ns - active_ns:
            self._voice_state[n]["params"].active = False

        # ── Update active voices' params ─────────────────────────────
        for n in active_ns & tracked_ns:
            self._voice_state[n]["params"] = voices[n]

        # Per-voice normalization. Count both currently-active voices AND
        # voices still in release tail (env > 0), because the latter still
        # contribute to the mix until env ramps down to zero. Without this,
        # the norm is too low during voice_on/off transitions and the mix
        # clips (was: n_active = len(active_ns), which undercounted).
        n_active = len(active_ns)
        for n, state in self._voice_state.items():
            if state["env"] > 0.001 and n not in active_ns:
                n_active += 1
        norm = 1.0 / (n_active ** 0.5) if n_active > 0 else 1.0

        # ── LFO: advance and get current value ────────────────────────
        lfo_val = self._store.advance_lfo(dt)  # -1..+1
        lfo_amount = self._store._lfo_amount     # global 0..1

        mix = np.zeros((frames, 2), dtype=np.float32)

        # ── Hybrid sensor modulation (computed BEFORE voice loop for thread safety) ──
        # HR → subtle master gain pulse + per-voice shape modulation
        bpm, rr_ms, hr_conf = self._store.get_hybrid_hr()
        hr_pulse = 0.0
        hr_shape_mod = 0.0
        if bpm > 0:
            # Normalize BPM around 72: 72 → 0, range ±30 → ±depth
            bpm_norm = (bpm - 72.0) / 30.0
            bpm_norm = max(-1.0, min(1.0, bpm_norm))
            hr_pulse = bpm_norm * config.HYBRID_HR_GAIN_DEPTH
            hr_shape_mod = bpm_norm * config.HYBRID_HR_SHAPE_DEPTH

        # IMU → per-voice pan modulation (yaw) + gain boost (accel)
        yaw, pitch, roll, ax, ay, az = self._store.get_hybrid_imu()
        imu_pan_mod = 0.0
        imu_gain_boost = 0.0
        if abs(yaw) > 0.01 or abs(pitch) > 0.01:
            # Yaw → pan offset (±90° yaw → ±0.3 pan)
            yaw_norm = yaw / 180.0  # -1..1
            imu_pan_mod = yaw_norm * 0.3
            # Accel magnitude → gain boost
            accel_mag = math.sqrt(ax*ax + ay*ay + az*az)
            imu_gain_boost = min(accel_mag / 20.0, 1.0) * config.HYBRID_ACCEL_GAIN_BOOST

        to_prune = []
        for n, state in self._voice_state.items():
            params = state["params"]
            if params.freq <= 0:
                to_prune.append(n)
                continue

            target_env = 1.0 if params.active else 0.0
            current_env = state["env"]
            attack_s = params.attack_s
            release_s = params.release_s

            # Compute envelope ramp
            if target_env > current_env:
                rate = 1.0 / max(attack_s, 0.0001)
                new_env = min(target_env, current_env + rate * dt)
            elif target_env < current_env:
                rate = 1.0 / max(release_s, 0.0001)
                new_env = max(target_env, current_env - rate * dt)
            else:
                new_env = current_env

            state["env"] = new_env

            if not params.active and new_env <= 0.0:
                to_prune.append(n)
                continue

            if new_env <= 0.0:
                continue

            # ── LFO modulation per voice ──────────────────────────────
            lfo_mod = lfo_val * lfo_amount
            mod_gain = params.gain * (1.0 + params.lfo_gain * lfo_mod)
            mod_pan = params.pan + params.lfo_pan * lfo_mod * 2.0  # ±2 range
            mod_pan = max(-1.0, min(1.0, mod_pan))
            mod_phase = params.phase + params.lfo_phase * lfo_mod * np.pi

            # ── Generate sine ─────────────────────────────────────────
            t = np.arange(frames, dtype=np.float64) / self._sample_rate
            start_phase = state["phase"]
            carrier_phases = 2.0 * np.pi * params.freq * t + start_phase
            sine = np.sin(carrier_phases + mod_phase).astype(np.float32)

            # ── Waveshaper (didgeridoo/vocal timbre) ──────────────────
            shape = params.shape
            # Apply HR shape modulation
            voice_shape = shape + hr_shape_mod
            voice_shape = max(0.0, min(1.0, voice_shape))
            if voice_shape > 0.0:
                drive = 1.0 + voice_shape * 4.0
                sine = np.tanh(sine * drive) / np.tanh(drive)

            # Apply IMU gain boost
            voice_gain = mod_gain * (1.0 + imu_gain_boost)
            voice_gain = min(voice_gain, 1.0)

            sine *= float(voice_gain) * norm * new_env
            state["phase"] = (
                carrier_phases[-1] + 2.0 * np.pi * params.freq / self._sample_rate
            ) % (2.0 * np.pi)

            # Apply IMU pan modulation to the voice
            voice_pan = mod_pan + imu_pan_mod
            voice_pan = max(-1.0, min(1.0, voice_pan))

            angle = (float(voice_pan) + 1.0) * (np.pi / 4.0)
            mix[:, 0] += sine * float(np.cos(angle))
            mix[:, 1] += sine * float(np.sin(angle))

        for n in to_prune:
            del self._voice_state[n]

        # ── Side-chain: beacon envelope → shaper master modulation ────
        beacon_level = self._store.get_beacon_level()
        sidechain_amount = self._store._sidechain_amount
        if sidechain_amount >= 0:
            sc_factor = (1.0 - sidechain_amount) + sidechain_amount * beacon_level
        else:
            sc_factor = 1.0 + abs(sidechain_amount) * (1.0 - beacon_level)

        # Master gain + side-chain + HR pulse + soft limiter
        mix *= self._store.get_master_gain() * sc_factor * (1.0 + hr_pulse)

        # ── Recording tap: hand a copy of the final mix to the recorder
        #    BEFORE the soft limiter, so the Recorder can do its own
        #    controlled limiting on the full mix (SC + Shaper).
        sink = self._record_sink
        if sink is not None:
            sink.append(mix.copy())

        mix = np.tanh(mix * 1.05) * 0.95
        outdata[:] = mix

    def _on_stream_finished(self) -> None:
        log.warning("Audio stream finished unexpectedly.")
        self._running = False

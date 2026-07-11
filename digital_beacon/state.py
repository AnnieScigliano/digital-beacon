"""Thread-safe voice parameter store for the Shaper.

Adapted from NaturalHarmony/harmonic_shaper/state.py:
- Raised polyphony from 5 to MAX_VOICES (32).
- Voice identity is `harmonic_n` (1..32). Multiple voice_ids can map to the
  same harmonic_n (layering) — we keep the most recent active one.
- f1 is exposed as a first-class store attribute so the OSC receiver can
  push updates without touching VoiceParams.
"""

import collections
import math
import threading
import time
from copy import copy
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import config


def _pearson_correlation(a: list[float], b: list[float]) -> float:
    """Pearson correlation of the most recent min(len(a), len(b)) samples,
    clipped to 0..1 (a negative/anti-phase correlation isn't "more out of
    sync" for our purposes — it's just not togetherness — so it reads as 0,
    same as no correlation at all). Returns 0.0 if either series is too
    short/flat to correlate (e.g. one device hasn't moved at all yet).

    Note: this compares each buffer's last N samples positionally, not by
    matching timestamps — the two devices sample at ~10Hz independently and
    aren't guaranteed to align exactly. Good enough for an MVP "are they
    moving together" signal; true time-alignment/resampling can follow if
    testing shows this drifts.
    """
    n = min(len(a), len(b))
    if n < 3:
        return 0.0
    a = a[-n:]
    b = b[-n:]
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((x - mean_b) ** 2 for x in b)
    denom = math.sqrt(var_a * var_b)
    if denom < 1e-9:
        return 0.0
    return max(0.0, cov / denom)


@dataclass
class VoiceParams:
    """Parameters for a single Shaper voice (one pure sine)."""
    harmonic_n: int = 0
    freq: float = 0.0          # Hz — set by beacon broadcast
    gain: float = config.DEFAULT_VOICE_GAIN
    pan: float = config.DEFAULT_VOICE_PAN       # -1..+1
    phase: float = config.DEFAULT_VOICE_PHASE_DEG  # radians
    attack_s: float = config.DEFAULT_VOICE_ATTACK_S
    release_s: float = config.DEFAULT_VOICE_RELEASE_S
    shape: float = config.DEFAULT_VOICE_SHAPE   # 0=pure sine, 1=rich
    lfo_gain: float = config.DEFAULT_LFO_GAIN   # LFO→gain mod amount
    lfo_pan: float = config.DEFAULT_LFO_PAN
    lfo_phase: float = config.DEFAULT_LFO_PHASE
    active: bool = False
    voice_id: Optional[int] = None

    def copy(self) -> "VoiceParams":
        return copy(self)

    def to_dict(self) -> dict:
        return {
            "harmonic_n": self.harmonic_n,
            "freq": round(self.freq, 3),
            "gain": round(self.gain, 4),
            "pan": round(self.pan, 4),
            "phase_deg": round(math.degrees(self.phase) % 360, 1),
            "attack_s": self.attack_s,
            "release_s": self.release_s,
            "shape": self.shape,
            "lfo_gain": self.lfo_gain,
            "lfo_pan": self.lfo_pan,
            "lfo_phase": self.lfo_phase,
            "active": self.active,
        }


class VoiceParameterStore:
    """Thread-safe store for per-harmonic Shaper parameters.

    Keyed by harmonic_n (1..32). The beacon populates voice_on/off/freq;
    control surfaces (MIDI/OSC/Web) set gain/pan/phase.
    """

    # Template for a never-yet-seen hybrid device — copied, never mutated in place.
    _HYBRID_DEVICE_DEFAULTS = {
        "bpm": 0.0, "rr_ms": 0.0, "confidence": 0.0,
        "yaw": 0.0, "pitch": 0.0, "roll": 0.0,
        "ax": 0.0, "ay": 0.0, "az": 0.0,
    }
    # Rolling window for sway-energy/synchrony — long enough to smooth a slow
    # embrace-sway, short enough to feel live rather than laggy.
    _HYBRID_MOTION_WINDOW_S = 2.0

    def __init__(self, on_change: Optional[Callable[[], None]] = None):
        self._lock = threading.RLock()
        self._voices: dict[int, VoiceParams] = {}
        self._active_history: list[int] = []  # chronological, for note stealing
        self.f1: float = config.DEFAULT_F1
        self._base_f1: float = config.DEFAULT_F1    # before vsrate
        self._vsrate: float = 1.0
        self._master_gain: float = config.DEFAULT_SHAPER_MASTER
        self._global_attack_s: float = config.DEFAULT_VOICE_ATTACK_S
        self._global_release_s: float = config.DEFAULT_VOICE_RELEASE_S
        # Side-chain
        self._beacon_level: float = 0.0          # updated via OSC /beacon/level
        self._sidechain_amount: float = config.DEFAULT_SIDECHAIN_AMOUNT
        # LFO
        self._lfo_rate_divisor: int = config.DEFAULT_LFO_RATE_DIVISOR
        self._lfo_waveform: str = config.DEFAULT_LFO_WAVEFORM
        self._lfo_amount: float = config.DEFAULT_LFO_AMOUNT
        self._lfo_phase: float = 0.0             # 0..1, advances in audio callback
        self._strum_period_s: float = config.DEFAULT_STRUM_PERIOD_S
        self._strum_times: list[float] = []       # recent strum timestamps
        self._on_change = on_change
        self._panic_callback: Optional[Callable[[], None]] = None
        # ─── Hybrid sensor state (one entry per connected device, e.g. "A"/"B") ──
        self._hybrid_mode: int = 0              # 0=HR, 1=IMU, 2=Hybrid — global, not per-device
        self._hybrid_devices: dict[str, dict] = {}
        self._hybrid_clock_ts: float = 0.0
        self._hybrid_clock_seq: int = 0
        self._hybrid_bpm_ref: float = config.HYBRID_BPM_REF
        # Rolling per-device accel-magnitude history, for sway-energy (RMS)
        # and pair-synchrony (cross-correlation) — see the hug/dance roadmap's
        # Phase 1. Keyed by device id; each value is a deque of (t, accel_mag).
        self._hybrid_motion_history: dict[str, collections.deque] = {}
        # Live control knob (not derived from motion): multiplier on
        # config.HYBRID_PAIR_YAW_PAN_RANGE, 1.0 = 100% = default depth.
        # Settable at runtime (dashboard slider) without restarting the
        # audio engine, for field-testing how strongly yaw should pan.
        self._hybrid_pan_sensitivity: float = 1.0

    # ─── Internal helpers ─────────────────────────────────────────────────

    def _notify(self) -> None:
        if self._on_change:
            try:
                self._on_change()
            except Exception:
                pass

    def _ensure(self, n: int) -> None:
        if n not in self._voices:
            v = VoiceParams(harmonic_n=n)
            v.attack_s = self._global_attack_s
            v.release_s = self._global_release_s
            self._voices[n] = v

    # ─── Beacon-driven lifecycle ──────────────────────────────────────────

    def voice_on(self, harmonic_n: int, voice_id: int, freq: float, gain: float = None) -> None:
        with self._lock:
            self._ensure(harmonic_n)
            v = self._voices[harmonic_n]
            v.voice_id = voice_id
            v.freq = freq
            v.active = True
            if gain is not None:
                v.gain = max(0.0, min(1.0, float(gain)))
            else:
                v.gain = config.DEFAULT_VOICE_GAIN

            if harmonic_n in self._active_history:
                self._active_history.remove(harmonic_n)
            self._active_history.append(harmonic_n)

            # Note stealing: drop oldest if over limit
            while len(self._active_history) > config.MAX_VOICES:
                oldest_n = self._active_history.pop(0)
                self._voices[oldest_n].active = False
        self._notify()

    def voice_off(self, voice_id: int) -> None:
        with self._lock:
            for n, v in self._voices.items():
                if v.voice_id == voice_id:
                    v.active = False
                    if n in self._active_history:
                        self._active_history.remove(n)
                    break
        self._notify()

    def voice_freq(self, voice_id: int, freq: float) -> None:
        with self._lock:
            for v in self._voices.values():
                if v.voice_id == voice_id:
                    v.freq = freq
                    break

    def update_f1(self, f1: float) -> None:
        with self._lock:
            self._base_f1 = max(config.F1_MIN, min(config.F1_MAX, float(f1)))
            self.f1 = self._base_f1 * self._vsrate
        self._notify()

    def set_vsrate(self, rate: float) -> None:
        with self._lock:
            self._vsrate = max(0.1, min(4.0, float(rate)))
            self.f1 = self._base_f1 * self._vsrate
        self._notify()

    # ─── Parameter control ────────────────────────────────────────────────

    def set_gain(self, harmonic_n: int, gain: float) -> None:
        with self._lock:
            self._ensure(harmonic_n)
            self._voices[harmonic_n].gain = max(0.0, min(1.0, float(gain)))
        self._notify()

    def set_pan(self, harmonic_n: int, pan: float) -> None:
        with self._lock:
            self._ensure(harmonic_n)
            self._voices[harmonic_n].pan = max(-1.0, min(1.0, float(pan)))
        self._notify()

    def set_phase(self, harmonic_n: int, phase_deg: float) -> None:
        with self._lock:
            self._ensure(harmonic_n)
            self._voices[harmonic_n].phase = math.radians(float(phase_deg) % 360)
        self._notify()

    def set_attack(self, harmonic_n: int, attack_s: float) -> None:
        with self._lock:
            self._ensure(harmonic_n)
            self._voices[harmonic_n].attack_s = max(0.0, min(5.0, float(attack_s)))
        self._notify()

    def set_release(self, harmonic_n: int, release_s: float) -> None:
        with self._lock:
            self._ensure(harmonic_n)
            self._voices[harmonic_n].release_s = max(0.0, min(5.0, float(release_s)))
        self._notify()

    def set_params(self, harmonic_n: int, **kwargs) -> None:
        with self._lock:
            self._ensure(harmonic_n)
            v = self._voices[harmonic_n]
            if "gain" in kwargs:
                v.gain = max(0.0, min(1.0, float(kwargs["gain"])))
            if "pan" in kwargs:
                v.pan = max(-1.0, min(1.0, float(kwargs["pan"])))
            if "phase_deg" in kwargs:
                v.phase = math.radians(float(kwargs["phase_deg"]) % 360)
            if "attack_s" in kwargs:
                v.attack_s = max(0.0, min(5.0, float(kwargs["attack_s"])))
            if "release_s" in kwargs:
                v.release_s = max(0.0, min(5.0, float(kwargs["release_s"])))
        self._notify()

    def set_master_gain(self, gain: float) -> None:
        with self._lock:
            self._master_gain = max(0.0, min(1.0, float(gain)))

    def get_master_gain(self) -> float:
        with self._lock:
            return self._master_gain

    def set_global_attack(self, attack_s: float) -> None:
        with self._lock:
            self._global_attack_s = max(0.0, min(5.0, float(attack_s)))
        self._notify()

    def set_global_release(self, release_s: float) -> None:
        with self._lock:
            self._global_release_s = max(0.0, min(5.0, float(release_s)))
        self._notify()

    # ─── Timbre ──────────────────────────────────────────────────────────

    def set_shape(self, harmonic_n: int, shape: float) -> None:
        with self._lock:
            self._ensure(harmonic_n)
            self._voices[harmonic_n].shape = max(0.0, min(1.0, float(shape)))
        self._notify()

    # ─── Side-chain ──────────────────────────────────────────────────────

    def set_beacon_level(self, level: float) -> None:
        with self._lock:
            self._beacon_level = max(0.0, min(1.0, float(level)))

    def get_beacon_level(self) -> float:
        with self._lock:
            return self._beacon_level

    def set_sidechain_amount(self, amount: float) -> None:
        with self._lock:
            self._sidechain_amount = max(-1.0, min(1.0, float(amount)))
        self._notify()

    # ─── LFO ─────────────────────────────────────────────────────────────

    def set_lfo_rate_divisor(self, divisor: int) -> None:
        with self._lock:
            self._lfo_rate_divisor = max(1, int(divisor))
        self._notify()

    def set_lfo_waveform(self, waveform: str) -> None:
        with self._lock:
            if waveform in ("sine", "triangle", "saw", "square", "samplehold"):
                self._lfo_waveform = waveform
        self._notify()

    def set_lfo_amount(self, amount: float) -> None:
        with self._lock:
            self._lfo_amount = max(0.0, min(1.0, float(amount)))
        self._notify()

    def set_lfo_gain(self, harmonic_n: int, amount: float) -> None:
        with self._lock:
            self._ensure(harmonic_n)
            self._voices[harmonic_n].lfo_gain = max(0.0, min(1.0, float(amount)))
        self._notify()

    def set_lfo_pan(self, harmonic_n: int, amount: float) -> None:
        with self._lock:
            self._ensure(harmonic_n)
            self._voices[harmonic_n].lfo_pan = max(0.0, min(1.0, float(amount)))
        self._notify()

    def set_lfo_phase(self, harmonic_n: int, amount: float) -> None:
        with self._lock:
            self._ensure(harmonic_n)
            self._voices[harmonic_n].lfo_phase = max(0.0, min(1.0, float(amount)))
        self._notify()

    def advance_lfo(self, dt: float) -> float:
        """Advance LFO phase by dt seconds. Returns current LFO value -1..+1."""
        with self._lock:
            period = self._strum_period_s * self._lfo_rate_divisor
            if period <= 0:
                period = 0.5
            self._lfo_phase = (self._lfo_phase + dt / period) % 1.0
            return self._lfo_value()

    def _lfo_value(self) -> float:
        """Compute LFO value -1..+1 from current phase and waveform."""
        p = self._lfo_phase
        wf = self._lfo_waveform
        if wf == "triangle":
            return 1.0 - 4.0 * abs(p - 0.5)
        elif wf == "saw":
            return 1.0 - 2.0 * p
        elif wf == "square":
            return 1.0 if p < 0.5 else -1.0
        elif wf == "samplehold":
            # Return last value; new random each cycle
            if not hasattr(self, '_lfo_sh_value'):
                self._lfo_sh_value = 0.0
            if p < 0.01:  # new cycle
                import random
                self._lfo_sh_value = random.uniform(-1.0, 1.0)
            return self._lfo_sh_value
        else:  # sine
            import math
            return math.sin(2.0 * math.pi * p)

    def record_strum(self, timestamp_s: float) -> None:
        """Record a strum event to estimate period."""
        with self._lock:
            self._strum_times.append(timestamp_s)
            if len(self._strum_times) > config.STRUM_WINDOW:
                self._strum_times.pop(0)
            if len(self._strum_times) >= 2:
                intervals = [self._strum_times[i] - self._strum_times[i-1]
                             for i in range(1, len(self._strum_times))]
                self._strum_period_s = sum(intervals) / len(intervals)

    # ─── Hybrid sensor setters ────────────────────────────────────────────────────

    def set_hybrid_clock(self, timestamp: float, seq: int, bpm_ref: float) -> None:
        with self._lock:
            self._hybrid_clock_ts = timestamp
            self._hybrid_clock_seq = seq
            self._hybrid_bpm_ref = bpm_ref

    def _ensure_hybrid_device(self, device: str) -> dict:
        """Must be called with self._lock already held."""
        if device not in self._hybrid_devices:
            self._hybrid_devices[device] = dict(self._HYBRID_DEVICE_DEFAULTS)
        return self._hybrid_devices[device]

    def update_hybrid_hr(self, bpm: float, rr_ms: float, confidence: float = 1.0,
                          device: str = "A") -> None:
        with self._lock:
            d = self._ensure_hybrid_device(device)
            d["bpm"] = bpm
            d["rr_ms"] = rr_ms
            d["confidence"] = confidence
        self._notify()

    def update_hybrid_imu(self, yaw: float, pitch: float, roll: float,
                          ax: float, ay: float, az: float, device: str = "A") -> None:
        with self._lock:
            d = self._ensure_hybrid_device(device)
            d["yaw"] = yaw
            d["pitch"] = pitch
            d["roll"] = roll
            d["ax"] = ax
            d["ay"] = ay
            d["az"] = az
            accel_mag = math.sqrt(ax * ax + ay * ay + az * az)
            self._record_motion_sample(device, accel_mag)
        self._notify()

    def _record_motion_sample(self, device: str, accel_mag: float) -> None:
        """Must be called with self._lock already held."""
        now = time.time()
        hist = self._hybrid_motion_history.setdefault(device, collections.deque())
        hist.append((now, accel_mag))
        cutoff = now - self._HYBRID_MOTION_WINDOW_S
        while hist and hist[0][0] < cutoff:
            hist.popleft()

    def set_hybrid_mode(self, mode: int) -> None:
        with self._lock:
            self._hybrid_mode = mode
        self._notify()

    # ─── Hybrid sensor getters (for audio engine + API) ─────────────────────────
    # `device` defaults to "A" — the single-phone/single-strap case behaves
    # exactly as before multi-device support existed.

    def get_hybrid_hr(self, device: str = "A") -> tuple[float, float, float]:
        with self._lock:
            d = self._hybrid_devices.get(device, self._HYBRID_DEVICE_DEFAULTS)
            return d["bpm"], d["rr_ms"], d["confidence"]

    def get_hybrid_imu(self, device: str = "A") -> tuple[float, float, float, float, float, float]:
        with self._lock:
            d = self._hybrid_devices.get(device, self._HYBRID_DEVICE_DEFAULTS)
            return (d["yaw"], d["pitch"], d["roll"], d["ax"], d["ay"], d["az"])

    def get_hybrid_devices(self) -> list[str]:
        """IDs of every device that has sent at least one reading so far."""
        with self._lock:
            return list(self._hybrid_devices.keys())

    def get_hybrid_mode(self) -> int:
        with self._lock:
            return self._hybrid_mode

    # ─── Derived pair variables (Phase 1 of the hug/dance roadmap) ──────────────
    # Computed from the rolling motion-history buffers above. None of these
    # are consumed by the audio engine yet — that's Phase 2's per-person
    # sound mapping. For now they're queryable (and dashboard-visible) so the
    # numbers can be sanity-checked against real slow-dance motion first.

    def get_hybrid_sway_energy(self, device: str = "A") -> float:
        """RMS of accel magnitude over the rolling window — "how much is
        this person moving right now." 0.0 if the device hasn't reported yet."""
        with self._lock:
            hist = self._hybrid_motion_history.get(device)
            if not hist:
                return 0.0
            values = [v for _, v in hist]
        return math.sqrt(sum(v * v for v in values) / len(values))

    def get_hybrid_pair_energy(self, device_a: str = "A", device_b: str = "B") -> float:
        """Mean of both devices' sway energy — overall "aliveness" of the pair."""
        return (self.get_hybrid_sway_energy(device_a) + self.get_hybrid_sway_energy(device_b)) / 2.0

    def get_hybrid_synchrony(self, device_a: str = "A", device_b: str = "B") -> float:
        """0..1: how correlated the two devices' recent motion is — NOT a
        distance measurement, a "moving as one vs. independently" signal.
        0.0 if either device lacks enough samples yet (session just started,
        or the other phone never connected)."""
        with self._lock:
            hist_a = self._hybrid_motion_history.get(device_a)
            hist_b = self._hybrid_motion_history.get(device_b)
            if not hist_a or not hist_b:
                return 0.0
            a = [v for _, v in hist_a]
            b = [v for _, v in hist_b]
        return _pearson_correlation(a, b)

    def get_hybrid_relative_facing(self, device_a: str = "A", device_b: str = "B") -> float:
        """Difference between the two devices' yaw, wrapped to -180..180.

        Each phone zeros its own yaw client-side when its wearer taps Start
        (see hybrid_bridge.py's sender page), so this is NOT an absolute
        compass-bearing difference — it's how much the pair's relative
        orientation has drifted since they began (0.0 at calibration time
        for both, growing as they turn relative to each other). 0.0 if
        either device hasn't reported yet.
        """
        with self._lock:
            if device_a not in self._hybrid_devices or device_b not in self._hybrid_devices:
                return 0.0
            yaw_a = self._hybrid_devices[device_a]["yaw"]
            yaw_b = self._hybrid_devices[device_b]["yaw"]
        return (yaw_a - yaw_b + 180.0) % 360.0 - 180.0

    def set_hybrid_pan_sensitivity(self, value: float) -> None:
        """0..3 multiplier on config.HYBRID_PAIR_YAW_PAN_RANGE — a live
        control knob, not derived from motion. 3.0 (300%) cap is generous
        headroom for testing; actual audible pan still clamps to ±1 at the
        point of use."""
        with self._lock:
            self._hybrid_pan_sensitivity = max(0.0, min(3.0, float(value)))
        self._notify()

    def get_hybrid_pan_sensitivity(self) -> float:
        with self._lock:
            return self._hybrid_pan_sensitivity

    # ─── Panic ───────────────────────────────────────────────────────────────

    def panic(self) -> None:
        with self._lock:
            for v in self._voices.values():
                v.active = False
                v.gain = config.DEFAULT_VOICE_GAIN
                v.pan = 0.0
                v.phase = 0.0
            self._active_history.clear()
            # Reset hybrid state — drop every device, not just one.
            self._hybrid_devices.clear()
            self._hybrid_motion_history.clear()
        self._notify()
        # Also notify the MIDI control (Launchpad) to clear lights + state
        if self._panic_callback:
            try:
                self._panic_callback()
            except Exception:
                pass

    # ─── Snapshot accessors ───────────────────────────────────────────────────────

    def get_snapshot(self) -> dict[int, VoiceParams]:
        """Active voices only — for the audio callback (hot path)."""
        with self._lock:
            return {k: v.copy() for k, v in self._voices.items()
                    if v.active and v.freq > 0}

    def get_all_snapshot(self) -> dict[int, VoiceParams]:
        with self._lock:
            return {k: v.copy() for k, v in self._voices.items()}

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "f1": self.f1,
                "master_gain": self._master_gain,
                "global_attack_s": self._global_attack_s,
                "global_release_s": self._global_release_s,
                "sidechain_amount": self._sidechain_amount,
                "beacon_level": self._beacon_level,
                "lfo_rate_divisor": self._lfo_rate_divisor,
                "lfo_waveform": self._lfo_waveform,
                "lfo_amount": self._lfo_amount,
                "strum_period_s": round(self._strum_period_s, 3),
                "hybrid": {
                    "mode": self._hybrid_mode,
                    # Legacy flat fields mirror device "A", so the existing
                    # single-device dashboard panel keeps working unchanged.
                    **self._hybrid_devices.get("A", self._HYBRID_DEVICE_DEFAULTS),
                    # Full per-device breakdown, for multi-device UI/mapping.
                    "devices": {
                        dev_id: dict(vals)
                        for dev_id, vals in self._hybrid_devices.items()
                    },
                    # Pair-derived variables (hardcoded to the "A"/"B" pair —
                    # this MVP is two dancers, not N; see Phase 1 of the
                    # hug/dance roadmap). Consumed by the audio engine only
                    # when Pair Mode is on (config.HYBRID_PAIR_MODE_ENABLED).
                    "derived": {
                        "sway_energy_a": round(self.get_hybrid_sway_energy("A"), 3),
                        "sway_energy_b": round(self.get_hybrid_sway_energy("B"), 3),
                        "pair_energy": round(self.get_hybrid_pair_energy(), 3),
                        "pair_synchrony": round(self.get_hybrid_synchrony(), 3),
                        "relative_facing_deg": round(self.get_hybrid_relative_facing(), 1),
                    },
                    # Live control knob, not derived from motion — see
                    # set_hybrid_pan_sensitivity.
                    "pan_sensitivity": round(self._hybrid_pan_sensitivity, 3),
                },
                "voices": {
                    str(k): {
                        "gain": v.gain,
                        "pan": v.pan,
                        "phase_deg": round(math.degrees(v.phase) % 360, 1),
                        "attack_s": v.attack_s,
                        "release_s": v.release_s,
                        "shape": v.shape,
                        "lfo_gain": v.lfo_gain,
                        "lfo_pan": v.lfo_pan,
                        "lfo_phase": v.lfo_phase,
                        "active": v.active,
                        "freq": v.freq,
                    }
                    for k, v in sorted(self._voices.items())
                },
            }

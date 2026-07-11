# Hybrid Sensor Mode v1.0 — Implementation Guide

This document describes the **Hybrid Sensor Mode** architecture in `digital-beacon`,
which combines **Fitbit Charge 6 heart rate** (BLE real-time) with **phone IMU**
(gyroscope + accelerometer via HTTP) to create a body-responsive harmonic field.

## Overview

The Hybrid Sensor Mode adds a new layer to the digital-beacon instrument:
- **Heart Rate** → subtle master gain pulse + spectral brightness shift
- **IMU Orientation** → spatial rotation (azimuth) + elevation tilt (front/back gain)
- **Temporal Sync** → beat-clock alignment between both sensor streams

## Architecture

There are two independent ways sensor data reaches sclang — both send the same
`/hybrid/*` OSC addresses directly to `127.0.0.1:57120`, no relay hop between
them:

```
┌────────────────────────────────────────────┐   ┌────────────────────────────────────────────┐
│  Fitbit Charge 6 / BLE HR strap             │   │  Phone (Web Sensors API)                    │
│      │ BLE                                  │   │      │ POST /imu {yaw,pitch,roll,ax,ay,az}   │
│      ▼                                      │   │      ▼                                      │
│  hybrid_bridge.py --mode ble  (this host)   │   │  hybrid_bridge.py Flask :5051 /imu           │
│      │  or  hr_relay.py --target            │   │      │                                      │
│      │      digital-beacon (cymatic-control)│   │      ▼  /hybrid/imu [yaw,pitch,roll,ax,ay,az]│
│      ▼  /hybrid/hr [bpm, rr_ms, confidence] │   └────────────────────────────────────────────┘
└────────────────────────────────────────────┘
                     │                                              │
                     ▼                                              ▼
              ┌──────────────────────────────────────────────────────────────┐
              │  SuperCollider (sclang :57120)                                │
              │      OSCdef(\hybrid_hr)   → master gain + band gain          │
              │      OSCdef(\hybrid_imu)  → azimuth rotation + front gain    │
              │      OSCdef(\hybrid_mode) → mode switch                      │
              │      OSCdef(\hybrid_sync) → temporal sync (from clock thread)│
              └──────────────────────────────────────────────────────────────┘

A third path bypasses hybrid_bridge.py entirely: api.py's HTTP endpoints
(/api/hybrid/hr, /api/hybrid/imu, /api/hybrid/mode) update the Shaper's own
state store AND forward the same /hybrid/* OSC messages to sclang — this is
the path the curl-based tests in HYBRID_TESTING_GUIDE.md exercise:

┌────────────────────────────────────────────┐
│  curl / any HTTP client                     │
│      ▼  POST /api/hybrid/hr, /imu, /mode    │
│  digital_beacon/api.py :8080                │
│      ├─▶ store.update_hybrid_*()  (Shaper)  │
│      └─▶ sc_osc.send_message("/hybrid/...") ─┼─▶ sclang :57120 (same OSCdefs as above)
└────────────────────────────────────────────┘
```

`digital_beacon/osc_receiver.py` also has handlers listening on port 9001 for
an older `/bridge/heartbeat` / `/spatial/imu` addressing scheme intended for a
future "Shaper" ingestion path — nothing currently sends to those addresses,
so they're dormant. Not part of the flow above.

## Files Modified/Created

### New Files

| File | Description |
|------|-------------|
| `hybrid_bridge.py` | Main bridge: receives BLE HR + HTTP IMU, forwards temporal-sync OSC to SC |

### Modified Files (digital-beacon)

| File | Changes |
|------|---------|
| `digital_beacon/config.py` | Added `HYBRID_*` constants (ports, depths, reference BPM) |
| `digital_beacon/osc_receiver.py` | Added `/sync/clock`, `/bridge/heartbeat`, `/spatial/imu`, `/hybrid/mode` handlers |
| `digital_beacon/state.py` | Added hybrid sensor state (`_hybrid_bpm`, `_hybrid_yaw`, etc.) + getters/setters |
| `digital_beacon/audio_engine.py` | Added HR → gain/shape modulation, IMU → pan/gain modulation in audio callback |
| `digital_beacon/api.py` | Added `/api/hybrid/*` endpoints (status, mode, imu, hr) |
| `beacon.scd` | Added 4 OSCdefs: `\hybrid_hr`, `\hybrid_imu`, `\hybrid_mode`, `\hybrid_sync` |
| `start.sh` | Added `--hybrid` flag, hybrid_bridge launch step, port 8081 cleanup |
| `README.md` | Added Hybrid Sensor Mode section with architecture diagram and usage |

### Modified Files (cymatic-control)

| File | Changes |
|------|---------|
| `hr_relay.py` | Added `--target digital-beacon` option — sends `/hybrid/hr` directly to sclang on 127.0.0.1:57120 |

### Modified Files (beacon-spatial)

| File | Changes |
|------|---------|
| `README.md` | Added migration note pointing to digital-beacon as successor |

## OSC Protocol

### Inbound to sclang (port 57120) — sent by hybrid_bridge.py or api.py

| Address | Args | Description |
|---------|------|-------------|
| `/hybrid/hr` | `[bpm, rr_ms, confidence]` | Heart rate data |
| `/hybrid/imu` | `[yaw, pitch, roll, ax, ay, az]` | IMU orientation + accel |
| `/hybrid/mode` | `[mode]` | 0=HR, 1=IMU, 2=Hybrid |
| `/hybrid/sync/clock` | `[timestamp, seq, bpm_ref]` | Temporal sync heartbeat (hybrid_bridge.py only) |

`hybrid_bridge.py` has no inbound OSC receiver — it only ever sources HR/IMU
itself (BLE/simulate modes, and its own `/imu` HTTP endpoint) and sends
outbound to sclang. `hr_relay.py --target digital-beacon` sends `/hybrid/hr`
straight to sclang too, independently of `hybrid_bridge.py`.

### HTTP Endpoints (direct to API, independent of hybrid_bridge.py)

| Method | Endpoint | Body | Description |
|--------|----------|------|-------------|
| GET | `/api/hybrid/status` | — | Current sensor state |
| POST | `/api/hybrid/mode` | `{"mode": 0｜1｜2}` | Switch mode |
| POST | `/api/hybrid/imu` | `{"yaw", "pitch", "roll", "ax", "ay", "az"}` | Direct IMU injection |
| POST | `/api/hybrid/hr` | `{"bpm", "rr_ms", "confidence"}` | Direct HR injection |

## Creative Mappings

### SuperCollider (beacon.scd)

| Sensor | Target | Effect | Range |
|--------|--------|--------|-------|
| HR BPM | Master gain | ±5% pulse around 72 BPM | 42–102 BPM → ±5% |
| HR BPM | Upper band gain (16–32) | Brighter/darker spectral shift | ±15% relative |
| IMU Yaw | Azimuth rotation | Head-tracking spatial rotation | ±180° → full rotation |
| IMU Pitch | Front band gain | Elevation tilt | ±90° → ±20% gain |
| IMU Accel | Master gain | Movement → brightness | 0–20 m/s² → 0–10% |

### Shaper Audio Engine (audio_engine.py)

| Sensor | Target | Effect | Range |
|--------|--------|--------|-------|
| HR BPM | Master gain | ±3% pulse (configurable depth) | `HYBRID_HR_GAIN_DEPTH` |
| HR BPM | Per-voice shape | Brightness/darkness per harmonic | `HYBRID_HR_SHAPE_DEPTH` |
| IMU Yaw | Per-voice pan | Spatial rotation per voice | ±90° → ±0.3 pan |
| IMU Accel | Per-voice gain | Movement → per-voice brightness | `HYBRID_ACCEL_GAIN_BOOST` |

## Configuration

All hybrid settings are in `digital_beacon/config.py`:

```python
# Hybrid Sensor Mode (v1.0)
HYBRID_ENABLED = True
HYBRID_IMU_PORT = 5051         # HTTP server for phone IMU (hybrid_bridge.py)
HYBRID_BPM_REF = 72.0          # Reference BPM for normalization
HYBRID_HR_GAIN_DEPTH = 0.30    # ±30% master gain at ±30 BPM
HYBRID_HR_SHAPE_DEPTH = 0.45   # ±45% shape modulation at ±30 BPM
HYBRID_ACCEL_GAIN_BOOST = 0.35 # 35% gain boost per 20 m/s²
```

## Running

### Full Stack with Hybrid Mode

```bash
cd ~/Documents/AlterMundi/Beacon/digital-beacon
./start.sh --hybrid --live    # Enable hybrid + live input
./start.sh --hybrid --file    # Enable hybrid + WAV file
```

### Manual Component Launch

```bash
# Terminal 1: Hybrid bridge only
python hybrid_bridge.py --target-host 127.0.0.1 --target-port 57120

# Terminal 2: HR relay (Fitbit BLE) — sends straight to sclang, independent
# of hybrid_bridge.py
cd ../cymatic-control
python hr_relay.py --mode ble --target digital-beacon --device "Charge 6"

# Terminal 3: Phone IMU (curl test, hits hybrid_bridge.py's Flask server)
curl -X POST http://localhost:5051/imu \
  -H "Content-Type: application/json" \
  -d '{"yaw": 15, "pitch": -5, "roll": 0, "ax": 0.1, "ay": 0, "az": 9.8}'
```

### Direct API Injection (bypass bridge)

```bash
# Inject HR directly
curl -X POST http://localhost:8080/api/hybrid/hr \
  -H "Content-Type: application/json" \
  -d '{"bpm": 85, "rr_ms": 705, "confidence": 0.95}'

# Inject IMU directly
curl -X POST http://localhost:8080/api/hybrid/imu \
  -H "Content-Type: application/json" \
  -d '{"yaw": 15, "pitch": -5, "roll": 0, "ax": 0.1, "ay": 0, "az": 9.8}'

# Check status
curl http://localhost:8080/api/hybrid/status
```

## Pair Mode (two-phone movement-only test case)

Everything above is the **single-device Hybrid mode** — HR + one phone's IMU
rotating/boosting the whole 32-band field — and it's the default, unaffected
by anything below. **Pair Mode** is a separate, opt-in mapping for the
two-phone hug/dance MVP: harmonics 1-10/11-20 become device A's/B's own
register (their sway energy → gain, zeroed yaw → pan, pitch → gain tilt),
21-28 is a shared band that only blooms when the pair is synchronized, and
HR is **excluded from the mapping entirely** — this test case is scoped to
phone movement only, no heart-rate hardware assumed or simulated.

**This iteration intentionally has no HR/HRV/RR involved at all** (the strap
hardware isn't ready, and mockup heart-rate data would misrepresent it) — Pair
Mode enforces that structurally (HR pulse is forced to 0 even if HR data
happens to be flowing), and the run procedure below never starts an HR
source in the first place.

To turn it on:

1. **Python** (`digital_beacon/config.py`): flip `HYBRID_PAIR_MODE_ENABLED = True`.
2. **SC** (`beacon.scd` or `beacon_simple.scd`): flip `var hybridPairModeEnabled = false;` to `true`, near the top of the `doWhenBooted` block. Restart sclang after editing — this is a "which test case am I running" choice, not a live-toggleable performance control.
3. Launch the bridge with HR fully off: `python hybrid_bridge.py --mode off` (IMU server still runs; no simulated or real HR is generated).
4. Open the sender page on both phones, pick "Phone A" / "Phone B", tap Start on each (this also zeros yaw/pitch — see IMU calibration below).

To turn it back off: flip both flags back (Python + the `.scd` file) and
restart. Nothing else needs to change — the existing single-device workflow
in "Running" above works exactly as it did before Pair Mode existed.

### Testing without two phones

`simulate_dance.py` (project root) POSTs synthetic device A/B IMU readings
to `hybrid_bridge.py`'s `/imu` endpoint — same wire format a real phone's
sender page uses — through a set of named stages (`STILL`, `SOLO_A`/`SOLO_B`,
`SYNCED_SWAY`/`DESYNCED_SWAY`, `SLOW_SPIN`, `EMBRACE_TIGHTEN`). Useful for
iterating on Pair Mode or the dashboard's Pair Mode Telemetry canvas without
needing two people and two phones every time:

```bash
python hybrid_bridge.py --mode off &
python simulate_dance.py --stages 4,5   # synced vs. desynced sway, to sanity-check synchrony
```

### Dashboard: Pair Mode Telemetry canvas

The dashboard's Hybrid Sensors panel includes a small canvas (`#pair-canvas`
in `static/index.html`, drawn by `drawPairViz`) showing each device's zeroed
facing direction (a needle), sway energy (halo size), pair synchrony
(connector thickness/glow), and pair energy (center pulse). It's
deliberately abstract — phone IMU alone can't give real body position, so
it never pretends to show where the two dancers actually are, only what's
actually measured. It's populated regardless of whether Pair Mode's sound
mapping is on, so it doubles as a way to sanity-check the derived-variable
math (see `digital_beacon/state.py`) before/without the audio changing.

## Future Work

- [ ] EEG integration (slot reserved in `config.py`)
- [ ] Continuous f1 modulation (12 discrete points → smooth interpolation)
- [x] IMU calibration (zero-offset for yaw/pitch) — done client-side in the
      sender page: yaw/pitch zero to whatever the wearer was facing/tilted
      at the first reading after Start (or after tapping Recalibrate), rather
      than reporting raw compass heading. Roll is still unzeroed (unused by
      any mapping so far).
- [ ] HR variability (HRV) analysis for deeper modulation
- [x] Multi-device IMU plumbing (two phones, tagged "A"/"B") — device id threads
      through the sender page's picker, `hybrid_bridge.py`'s `HybridSensorHub`,
      `digital_beacon/state.py`'s per-device store, `/hybrid/imu[/B]` +
      `/hybrid/hr[/B]` OSC addressing, and both `.scd` engines.
- [x] Derived pair variables (sway energy, pair synchrony, relative facing
      angle, pair energy) — `VoiceParameterStore.get_hybrid_sway_energy/
      _synchrony/_relative_facing/_pair_energy` in `digital_beacon/state.py`,
      computed from a 2s rolling motion-history buffer, visible in the
      dashboard's Hybrid Sensors panel and `to_dict()["hybrid"]["derived"]`.
- [x] Per-person differentiated sound mapping, as an **opt-in Pair Mode**
      (`config.HYBRID_PAIR_MODE_ENABLED`, default `False`) — see the "Pair
      Mode" section above. Harmonics 1-10/11-20 are device A's/B's own
      register (their own sway energy → gain, zeroed yaw → pan/azimuth,
      pitch → gain tilt); 21-28 is a shared band gated by pair synchrony
      (silent until they move together); 29-32 stay untouched. Implemented
      in `digital_beacon/audio_engine.py` (`_hybrid_register`,
      `_person_pan_gain_mod`) and mirrored in both `.scd` engines
      (`applyHybridRegisters`/`applyMasterGain`, gated by each file's own
      `hybridPairModeEnabled` var), fed by a new `/hybrid/derived` OSC push
      from `digital_beacon/api.py`'s `_on_change` (SC has no rolling buffer
      of its own for sway/synchrony). All the `HYBRID_PAIR_*` depth/
      reference constants in `config.py` are first-guess values pending
      real slow-dance-motion calibration. With the flag at its default
      (off), the original single-device whole-field IMU mapping and
      HR-driven master pulse are exactly as they were before Pair Mode
      existed — see `tests/test_hybrid_pair_mapping.py`'s
      `TestLegacySingleDeviceModeUnaffected`.
- [ ] Multi-user HR (multiple Fitbits) — HR still assumes a single strap on device "A"

## License

GPL-3.0-or-later (matches digital-beacon).

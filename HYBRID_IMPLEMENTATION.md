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

```
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│  Fitbit Charge 6 (BLE)                                                          │
│      │                                                                          │
│      ▼                                                                          │
│  hr_relay.py --target digital-beacon                                             │
│      │                                                                          │
│      ▼  /bridge/heartbeat [bpm, rr_ms]                                         │
│  hybrid_bridge.py (port 9003)                                                   │
│      │                                                                          │
│      ▼  /hybrid/hr [bpm, rr_ms, confidence]                                     │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
                           ┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
                           │  SuperCollider (sclang:57120)                                                   │
                           │      │                                                                          │
                           │      ▼  OSCdef(\hybrid_hr) → master gain + band gain                            │
                           │      ▼  OSCdef(\hybrid_imu) → azimuth rotation + front gain                     │
                           │      ▼  OSCdef(\hybrid_mode) → mode switch                                       │
                           │      ▼  OSCdef(\hybrid_sync) → temporal sync                                      │
                           └──────────────────────────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
                           ┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
                           │  Shaper audio_engine.py                                                           │
                           │      │                                                                          │
                           │      ▼  HR → master gain pulse (±5% around 72 BPM)                                │
                           │      ▼  HR → shape modulation (brightness/darkness)                             │
                           │      ▼  IMU → per-voice pan modulation (yaw → spatial)                         │
                           │      ▼  IMU → gain boost (acceleration → brightness)                             │
                           └──────────────────────────────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│  Phone (Web Sensors API)                                                         │
│      │                                                                          │
│      ▼  POST /imu {yaw, pitch, roll, ax, ay, az}                                │
│  hybrid_bridge.py (port 8081)                                                    │
│      │                                                                          │
│      ▼  /hybrid/imu [yaw, pitch, roll, ax, ay, az]                             │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼ (same SC + Shaper path as HR above)
```

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
| `hr_relay.py` | Added `--target digital-beacon` option (auto-configures port 9003) |

### Modified Files (beacon-spatial)

| File | Changes |
|------|---------|
| `README.md` | Added migration note pointing to digital-beacon as successor |

## OSC Protocol

### Inbound (to SC, from hybrid_bridge.py)

| Address | Args | Source | Description |
|---------|------|--------|-------------|
| `/hybrid/hr` | `[bpm, rr_ms, confidence]` | hybrid_bridge.py | Heart rate data |
| `/hybrid/imu` | `[yaw, pitch, roll, ax, ay, az]` | hybrid_bridge.py | IMU orientation + accel |
| `/hybrid/mode` | `[mode]` | hybrid_bridge.py | 0=HR, 1=IMU, 2=Hybrid |
| `/hybrid/sync/clock` | `[timestamp, seq, bpm_ref]` | hybrid_bridge.py | Temporal sync heartbeat |

### Inbound (to hybrid_bridge.py)

| Address | Args | Source | Description |
|---------|------|--------|-------------|
| `/bridge/heartbeat` | `[bpm, rr_ms]` | hr_relay.py | BLE heart rate from Fitbit |
| `/spatial/imu` | `[yaw, pitch, roll, ax, ay, az]` | Phone/HTTP | IMU data from phone sensors |

### HTTP Endpoints (direct to API, bypassing bridge)

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
HYBRID_OSC_PORT = 9003        # hybrid_bridge listens here
HYBRID_IMU_PORT = 8081        # HTTP server for phone IMU
HYBRID_BPM_REF = 72.0         # Reference BPM for normalization
HYBRID_HR_GAIN_DEPTH = 0.03   # ±3% master gain at ±30 BPM
HYBRID_HR_SHAPE_DEPTH = 0.05  # ±5% shape modulation at ±30 BPM
HYBRID_ACCEL_GAIN_BOOST = 0.02 # 2% gain boost per 20 m/s²
```

## Running

### Full Stack with Hybrid Mode

```bash
cd ~/Projects/digital-beacon
./start.sh --hybrid --live    # Enable hybrid + live input
./start.sh --hybrid --file    # Enable hybrid + WAV file
```

### Manual Component Launch

```bash
# Terminal 1: Hybrid bridge only
python hybrid_bridge.py --target-host 127.0.0.1 --target-port 57120

# Terminal 2: HR relay (Fitbit BLE)
cd ../cymatic-control
python hr_relay.py --mode ble --target digital-beacon --device "Charge 6"

# Terminal 3: Phone IMU (curl test)
curl -X POST http://localhost:8081/imu \
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

## Future Work

- [ ] EEG integration (slot reserved in `config.py`)
- [ ] Continuous f1 modulation (12 discrete points → smooth interpolation)
- [ ] IMU calibration (zero-offset for yaw/pitch/roll)
- [ ] HR variability (HRV) analysis for deeper modulation
- [ ] Multi-user hybrid mode (multiple Fitbits + phones)

## License

GPL-3.0-or-later (matches digital-beacon).

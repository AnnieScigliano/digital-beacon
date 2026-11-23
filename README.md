# digital-beacon / NaturalHarmony Toolkit

## Two-phone rehearsal candidate

For the movement-only Living Lab profile on this experimental branch, read
[PAIR_REHEARSAL.md](PAIR_REHEARSAL.md). It reuses the existing additive Pair
Mode mapping with explicit source freshness, phone permissions and an isolated
foreground launcher. It does not require ECG/EEG, SC or the legacy start script.


**The Harmonic Beacon as a modular software toolkit.** Natural harmonics, end-to-end.

This repo is the new home of a unified toolkit built from:

- `digital-beacon` (32-band SC binaural spatializer + Shaper additive synth)
- `NaturalHarmony` (MIDI middleware, key mapping, harmonic math)
- `beacon-spatial` (13-band ATK spatializer + phone sensor UI)
- `EEG-Game` (Muse neurofeedback + concentration estimator)
- `Phideus` (H-series, V4-lin, A4-16k harmonic descriptors)

> **Scope:** Software-only. Physical tines / hardware are out of scope for this foundational milestone. Backward compatibility is not preserved when it complicates the architecture.

## Packages

| Package | Purpose |
|---|---|
| `nh-core` | Renderer-neutral `HarmonicField`, `Partial`, `Residual`, `RendererCapabilities`, math helpers |
| `nh-presets` | Versioned preset schema + migrations from legacy 13/32-band JSON |
| `nh-model` | Portable runtime state with snapshots and modulation |
| `nh-control` | Normalized `ControlEvent`, mapping graph, Launchpad adapter |
| `nh-analysis` | F0, harmonicity, harmonic mask, Phideus descriptors (NumPy-first) |
| `nh-sensors` | EEG processor / Muse OSC adapter, phone IMU adapter, simulators |
| `nh-renderers` | Python/sounddevice (reference), WebAudio AudioWorklet, SuperCollider OSC |
| `nh-runtime` | WebSocket base-field server + local model client |

## Hybrid Sensor Mode (v1.0) — NEW

`digital-beacon` now supports a **Hybrid Sensor Mode** that combines:
- **Fitbit Charge 6** heart rate (via BLE real-time beat events)
- **Phone IMU** (gyroscope + accelerometer via HTTP POST)
- **Temporal sync** between both sensor streams

### How it works

```
Fitbit Charge 6 (BLE) ──► hr_relay.py ──► hybrid_bridge.py ──► /hybrid/hr ──► SC
Phone IMU (HTTP) ───────► POST /imu ───► hybrid_bridge.py ──► /hybrid/imu ──► SC
                                              │
                                              ▼
                                    temporal sync + beat clock
                                              │
                                              ▼
                              Shaper audio_engine.py (HR→gain/shape, IMU→pan)
```

### Creative mappings

| Sensor | Target | Effect |
|--------|--------|--------|
| HR BPM | Master gain | ±5% pulse around 72 BPM reference |
| HR BPM | Band gain (upper) | Brighter/darker spectral shift |
| IMU Yaw | Azimuth rotation | Head-tracking spatial rotation |
| IMU Pitch | Front band gain | Elevation tilt (front/back) |
| IMU Accel | Master gain | Movement → brightness |

### Running with Hybrid Mode

```bash
./start.sh --hybrid --live    # Enable hybrid sensors + live input
./start.sh --hybrid --file    # Enable hybrid sensors + WAV file
```

The hybrid bridge runs on port 5051 (IMU HTTP endpoint) and forwards
temporal-sync OSC to the SC engine on port 57120.

### Direct HTTP endpoints (bypass bridge)

```bash
curl -X POST http://localhost:8080/api/hybrid/hr \
  -H "Content-Type: application/json" \
  -d '{"bpm": 85, "rr_ms": 705, "confidence": 0.95}'

curl -X POST http://localhost:8080/api/hybrid/imu \
  -H "Content-Type: application/json" \
  -d '{"yaw": 15, "pitch": -5, "roll": 0, "ax": 0.1, "ay": 0, "az": 9.8}'

curl http://localhost:8080/api/hybrid/status
```

## Protocols

- `docs/protocols/websocket-messages.md` — canonical internal control plane
- `docs/protocols/sensor-event.md` — normalized sensor event schema
- `docs/protocols/osc-bridge.md` — OSC edge/gateway compatibility layer

## Stack

- `scsynth` + `sclang` + ATK (Ambisonic Toolkit) — binaural engine (falls back
  to `beacon_simple.scd`, no ATK required, when the quark isn't installed)
- `pw-jack` (PipeWire) — audio backend on Linux; macOS uses CoreAudio directly,
  no JACK wrapper needed
- `sounddevice` (PortAudio) — Shaper audio engine
- `python-osc` — control bus
- `mido` — Launchpad Mini + Minilab3 MIDI
- `fastapi` + WebSocket — optional web control surface

## Roadmap

See `docs/ROADMAP_foundational_toolkit_2026-07-06.md`.

## How to run tests

```bash
# Run package tests
cd ~/Projects/digital-beacon
python3 -m venv .venv
source .venv/bin/activate
pip install -e packages/nh-core -e packages/nh-presets -e packages/nh-model \
    -e packages/nh-control -e packages/nh-analysis -e packages/nh-sensors \
    -e packages/nh-renderers -e packages/nh-runtime
pytest packages/

# Run the legacy instrument end-to-end
cd ~/Documents/AlterMundi/Beacon/digital-beacon
./start.sh --file                  # beacon + shaper + bridge, source = WAV
./start.sh --live                  # beacon source = SoundIn(0) [R24 CH1]
```

## Legacy architecture

The old 32-band instrument stack (SuperCollider + ATK + Shaper + Launchpad) is still in `digital_beacon/` and `beacon.scd`. It will be migrated to the new renderer architecture incrementally rather than replaced in one step.

## License

GPL-3.0-or-later (matches NaturalHarmony).

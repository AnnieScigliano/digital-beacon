"""Hybrid Sensor Bridge for digital-beacon.

Combines:
  - Fitbit Charge 6 heart rate via BLE (real-time beat events)
  - Phone IMU (gyroscope + accelerometer) via HTTP POST /imu
  - Temporal synchronization protocol with timestamps and sequence numbers
  - OSC output to the digital-beacon engine (sclang :57120)

Protocol OSC (addresses/arg shapes match the OSCdefs registered in beacon.scd):
  /hybrid/sync/clock [timestamp, seq, bpm_ref]          — sync heartbeat
  /hybrid/hr         [bpm, rr_ms, confidence]           — HR data
  /hybrid/imu        [yaw, pitch, roll, ax, ay, az]      — IMU frame
  /hybrid/mode       [mode]                             — 0=HR, 1=IMU, 2=Hybrid

Creative mappings (enforced by beacon.scd + Shaper):
  HR (bpm)    → subtle master gain pulse + Shaper shape modulation + breathing LFO
  Yaw         → global FoaRotate azimuth rotation (all bands rotate together)
  Pitch       → band distance modulation (closer/farther)
  Accel (mag) → per-band gain boost (energy → loudness)

Usage:
  python hybrid_bridge.py                          # auto mode
  python hybrid_bridge.py --mode simulate --bpm 72   # simulate HR
  python hybrid_bridge.py --mode ble --device "Charge 6"  # real Fitbit
  python hybrid_bridge.py --imu-port 5051          # custom IMU endpoint port

The bridge runs as a standalone process alongside the digital-beacon stack.
It is launched automatically by start.sh when --hybrid is passed.
"""

import argparse
import asyncio
import json
import logging
import math
import random
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

try:
    from pythonosc.udp_client import SimpleUDPClient
    HAS_OSC = True
except ImportError:
    HAS_OSC = False

# Import digital-beacon config for OSC targets
try:
    from digital_beacon import config
except ImportError:
    # Fallback config when running standalone
    class _FallbackConfig:
        SCLANG_HOST = "127.0.0.1"
        SCLANG_OSC_PORT = 57120
    config = _FallbackConfig()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | hybrid_bridge | %(message)s",
)
log = logging.getLogger("hybrid_bridge")

# ─── Configuration ───────────────────────────────────────────────────────────

DEFAULT_IMU_HTTP_PORT = 5051            # phone IMU posts here
DEFAULT_BPM_REF = 72.0                  # reference BPM for sync
HR_TO_GAIN_DEPTH = 0.15                 # max ±gain modulation from HR
HR_TO_SHAPE_DEPTH = 0.25                # max shape modulation from HR
IMU_YAW_TO_AZ_RANGE = 90.0              # yaw ±180° maps to az ±90°
IMU_PITCH_TO_DIST_RANGE = 5.0           # pitch ±90° maps to dist ±5
ACCEL_TO_GAIN_BOOST = 0.3               # accel magnitude → gain boost

# BLE HR service/characteristic UUIDs
HR_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
HR_CHAR_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

# Phone IMU sender page (served at GET /) — fetch()es /imu relative to its own
# origin, so tunneling this one port (cloudflared/ngrok) is enough: no mixed
# HTTPS-page/HTTP-fetch content, and no IP to type in on the phone.
IMU_SENDER_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Beacon IMU</title>
  <style>
    body { font-family: sans-serif; background: #111; color: #0f0; padding: 20px; }
    button { font-size: 18px; padding: 15px; margin: 10px 0; width: 100%; }
    #status { font-size: 14px; margin-top: 20px; }
    .value { font-family: monospace; font-size: 16px; }
  </style>
</head>
<body>
  <h2>Beacon IMU Sender</h2>
  <button id="startBtn" onclick="start()">Start Sensors</button>
  <button id="stopBtn" onclick="stop()" disabled>Stop</button>
  <div id="status">Status: idle</div>
  <div id="data"></div>

<script>
let interval = null;
const status = document.getElementById('status');
const dataDiv = document.getElementById('data');

async function start() {
  if (typeof DeviceMotionEvent !== 'undefined' &&
      typeof DeviceMotionEvent.requestPermission === 'function') {
    // iOS 13+ requires an explicit user-gesture permission request.
    const resp = await DeviceMotionEvent.requestPermission().catch(() => 'denied');
    if (resp !== 'granted') {
      status.textContent = 'Status: motion permission denied';
      return;
    }
  }
  window.addEventListener('deviceorientation', handleOrientation);
  window.addEventListener('devicemotion', handleMotion);
  interval = setInterval(sendData, 100); // 10 Hz
  document.getElementById('startBtn').disabled = true;
  document.getElementById('stopBtn').disabled = false;
  status.textContent = 'Status: streaming @ 10Hz';
}

function stop() {
  window.removeEventListener('deviceorientation', handleOrientation);
  window.removeEventListener('devicemotion', handleMotion);
  clearInterval(interval);
  document.getElementById('startBtn').disabled = false;
  document.getElementById('stopBtn').disabled = true;
  status.textContent = 'Status: stopped';
}

let yaw = 0, pitch = 0, roll = 0, ax = 0, ay = 0, az = 9.8;

function handleOrientation(e) {
  yaw = e.alpha || 0;
  pitch = e.beta || 0;
  roll = e.gamma || 0;
}

function handleMotion(e) {
  const a = e.acceleration || {};
  ax = a.x || 0;
  ay = a.y || 0;
  az = a.z || 9.8;
}

function sendData() {
  const payload = { yaw, pitch, roll, ax, ay, az };
  fetch('/imu', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  }).then(r => r.json())
    .then(j => {
      dataDiv.innerHTML = `<span class="value">Y:${yaw.toFixed(1)} P:${pitch.toFixed(1)} R:${roll.toFixed(1)} | A:${ax.toFixed(2)},${ay.toFixed(2)},${az.toFixed(2)}</span>`;
    })
    .catch(e => { status.textContent = 'Error: ' + e.message; });
}
</script>
</body>
</html>
"""


# ─── Data structures ───────────────────────────────────────────────────────

@dataclass
class SensorState:
    """Thread-safe container for latest sensor readings."""
    # HR
    bpm: float = 0.0
    rr_ms: float = 0.0
    confidence: float = 0.0
    last_hr_time: float = 0.0
    # IMU
    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0
    ax: float = 0.0
    ay: float = 0.0
    az: float = 0.0
    last_imu_time: float = 0.0
    # Mode
    mode: int = 0  # 0=HR, 1=IMU, 2=Hybrid
    # Sequence
    seq: int = 0
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def next_seq(self) -> int:
        with self._lock:
            self.seq += 1
            return self.seq

    def update_hr(self, bpm: float, rr_ms: float, confidence: float = 1.0):
        with self._lock:
            self.bpm = bpm
            self.rr_ms = rr_ms
            self.confidence = confidence
            self.last_hr_time = time.time()
        self.next_seq()

    def update_imu(self, yaw: float, pitch: float, roll: float,
                   ax: float, ay: float, az: float):
        with self._lock:
            self.yaw = yaw
            self.pitch = pitch
            self.roll = roll
            self.ax = ax
            self.ay = ay
            self.az = az
            self.last_imu_time = time.time()
        self.next_seq()

    def set_mode(self, mode: int):
        with self._lock:
            self.mode = mode

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "bpm": self.bpm,
                "rr_ms": self.rr_ms,
                "confidence": self.confidence,
                "yaw": self.yaw,
                "pitch": self.pitch,
                "roll": self.roll,
                "ax": self.ax,
                "ay": self.ay,
                "az": self.az,
                "mode": self.mode,
                "seq": self.seq,
                "last_hr_age_s": round(time.time() - self.last_hr_time, 2) if self.last_hr_time else None,
                "last_imu_age_s": round(time.time() - self.last_imu_time, 2) if self.last_imu_time else None,
            }


# ─── OSC Protocol ──────────────────────────────────────────────────────────

class HybridOSCProtocol:
    """Sends temporal-sync OSC messages to the digital-beacon engine."""

    def __init__(self, host: str = "127.0.0.1", port: int = 57120):
        if not HAS_OSC:
            raise ImportError("python-osc is required for OSC protocol.")
        self._client = SimpleUDPClient(host, port)
        self._host = host
        self._port = port
        self._seq = 0
        self._lock = threading.Lock()

    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def _now(self) -> float:
        return time.time()

    def send_clock(self, bpm_ref: float = DEFAULT_BPM_REF):
        """Broadcast sync clock with reference BPM."""
        seq = self._next_seq()
        ts = self._now()
        self._client.send_message("/hybrid/sync/clock", [ts, seq, bpm_ref])
        log.debug("/hybrid/sync/clock seq=%d bpm_ref=%.1f", seq, bpm_ref)

    def send_heartbeat(self, bpm: float, rr_ms: float, confidence: float = 1.0):
        """Send heart rate data. Address/arg shape matches beacon.scd's \\hybrid_hr OSCdef."""
        seq = self._next_seq()
        self._client.send_message("/hybrid/hr", [bpm, rr_ms, confidence])
        log.debug("/hybrid/hr seq=%d bpm=%.1f rr=%.0fms", seq, bpm, rr_ms)

    def send_imu(self, yaw: float, pitch: float, roll: float,
                 ax: float, ay: float, az: float):
        """Send IMU frame. Address/arg shape matches beacon.scd's \\hybrid_imu OSCdef."""
        seq = self._next_seq()
        self._client.send_message("/hybrid/imu", [yaw, pitch, roll, ax, ay, az])
        log.debug("/hybrid/imu seq=%d yaw=%.1f pitch=%.1f roll=%.1f", seq, yaw, pitch, roll)

    def send_mode(self, mode: int):
        """Announce hybrid mode change. Arg shape matches beacon.scd's \\hybrid_mode OSCdef."""
        seq = self._next_seq()
        self._client.send_message("/hybrid/mode", [mode])
        log.info("/hybrid/mode seq=%d mode=%d (%s)", seq, mode,
                 ["HR", "IMU", "Hybrid"][mode])

    def send_imu_bundle(self, yaw: float, pitch: float, roll: float,
                        ax: float, ay: float, az: float):
        """Send IMU as OSC bundle (placeholder for future optimization)."""
        self.send_imu(yaw, pitch, roll, ax, ay, az)


# ─── BLE Heart Rate ────────────────────────────────────────────────────────

def parse_hr_measurement(data: bytearray) -> tuple[int, list[float]]:
    """Parse BLE Heart Rate Measurement characteristic (GATT spec)."""
    flags = data[0]
    hr_16bit = flags & 0x01
    rr_present = (flags >> 4) & 0x01
    offset = 1
    if hr_16bit:
        bpm = int.from_bytes(data[offset:offset + 2], byteorder="little")
        offset += 2
    else:
        bpm = data[offset]
        offset += 1
    # Skip energy expended if present
    if (flags >> 3) & 0x01:
        offset += 2
    rr_intervals = []
    if rr_present:
        while offset + 1 < len(data):
            rr_raw = int.from_bytes(data[offset:offset + 2], byteorder="little")
            rr_intervals.append(rr_raw * 1000.0 / 1024.0)
            offset += 2
    return bpm, rr_intervals


async def run_ble_async(state: SensorState, protocol: HybridOSCProtocol, args):
    """Async BLE scanner + HR notification handler."""
    try:
        import bleak
    except ImportError:
        log.error("bleak is required for BLE mode. Install: pip install bleak")
        return

    device_filter = args.device
    log.info("Scanning for BLE Heart Rate sensors...")
    scanner = bleak.BleakScanner()
    devices = await scanner.discover(timeout=args.scan_timeout)

    hr_devices = []
    for d in devices:
        name = d.name or ""
        if device_filter and device_filter.lower() not in name.lower():
            continue
        hr_devices.append(d)

    if not hr_devices:
        log.warning("No devices matching '%s' found. Trying all...", device_filter or "(any)")
        hr_devices = devices

    target = None
    for d in hr_devices:
        try:
            async with bleak.BleakClient(d) as client:
                services = client.services
                for s in services:
                    if s.uuid.lower() == HR_SERVICE_UUID:
                        target = d
                        break
                if target:
                    break
        except Exception:
            continue

    if not target:
        log.error("No BLE device with Heart Rate Service found.")
        log.error("Make sure your Fitbit is in exercise mode and nearby.")
        return

    name = target.name or target.address
    log.info("Found HR sensor: %s (%s)", name, target.address)

    beat_count = 0

    def notification_handler(sender, data):
        nonlocal beat_count
        bpm, rr_intervals = parse_hr_measurement(bytearray(data))
        if rr_intervals:
            for rr_ms in rr_intervals:
                state.update_hr(bpm, rr_ms)
                protocol.send_heartbeat(bpm, rr_ms)
                beat_count += 1
                log.info("♥ beat #%d  %d BPM  (RR: %.0fms)", beat_count, bpm, rr_ms)
        else:
            rr_ms = 60000.0 / max(bpm, 1)
            state.update_hr(bpm, rr_ms)
            protocol.send_heartbeat(bpm, rr_ms)
            beat_count += 1
            log.info("♥ beat #%d  %d BPM", beat_count, bpm)

    try:
        async with bleak.BleakClient(target) as client:
            await client.start_notify(HR_CHAR_UUID, notification_handler)
            log.info("Streaming heart rate from %s...", name)
            while True:
                await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log.error("BLE error: %s", e)


def run_ble(state: SensorState, protocol: HybridOSCProtocol, args):
    """Entry point for BLE mode (wraps async)."""
    try:
        import bleak  # noqa: F401
    except ImportError:
        log.error("bleak is required. Install: pip install bleak")
        return
    asyncio.run(run_ble_async(state, protocol, args))


# ─── Simulate Mode ─────────────────────────────────────────────────────────

def run_simulate(state: SensorState, protocol: HybridOSCProtocol, args):
    """Generate synthetic heartbeats with optional BPM drift."""
    base_bpm = args.bpm
    variation = args.variation
    current_bpm = base_bpm
    beat_count = 0

    log.info("Generating simulated beats at ~%.0f BPM (+/-%.0f)...", base_bpm, variation)

    try:
        while True:
            current_bpm = base_bpm + (args.variation * (2 * (beat_count % 2) - 1))
            # Add some randomness
            current_bpm += (random.random() - 0.5) * variation * 0.5
            current_bpm = max(30.0, min(200.0, current_bpm))
            rr_ms = 60000.0 / current_bpm
            interval = rr_ms / 1000.0

            state.update_hr(current_bpm, rr_ms)
            protocol.send_heartbeat(current_bpm, rr_ms)
            beat_count += 1

            bar = "♥" if beat_count % 2 == 0 else "♡"
            log.info("%s beat #%d  %.1f BPM  (RR: %.0fms)", bar, beat_count, current_bpm, rr_ms)

            time.sleep(interval)
    except KeyboardInterrupt:
        pass

    log.info("Stopped after %d beats.", beat_count)


# ─── IMU HTTP Server (Flask) ───────────────────────────────────────────────

def run_imu_server(state: SensorState, protocol: HybridOSCProtocol, port: int):
    """Start a lightweight HTTP server to receive phone IMU data."""
    try:
        from flask import Flask, request, jsonify
    except ImportError:
        log.error("flask is required for IMU server. Install: pip install flask")
        return

    app = Flask(__name__)

    @app.route("/", methods=["GET"])
    def sender_page():
        """Serve the phone IMU sender page from this same origin.

        DeviceOrientation/DeviceMotion only work in a secure context (HTTPS)
        on modern browsers. Tunnel this port (e.g. `cloudflared tunnel --url
        http://localhost:5051`) and open the tunnel's HTTPS URL on the phone
        — since the page and /imu are same-origin, the fetch() below never
        crosses HTTPS->HTTP (which browsers block as mixed content).
        """
        return IMU_SENDER_HTML

    @app.route("/imu", methods=["POST"])
    def imu_endpoint():
        """Receive IMU data from phone browser sensors.

        Expected JSON:
          {"yaw": 45.0, "pitch": 10.0, "roll": 0.0,
           "ax": 0.1, "ay": 0.0, "az": 9.8}
        """
        try:
            data = request.get_json(force=True)
            yaw = float(data.get("yaw", 0.0))
            pitch = float(data.get("pitch", 0.0))
            roll = float(data.get("roll", 0.0))
            ax = float(data.get("ax", 0.0))
            ay = float(data.get("ay", 0.0))
            az = float(data.get("az", 0.0))

            state.update_imu(yaw, pitch, roll, ax, ay, az)
            protocol.send_imu(yaw, pitch, roll, ax, ay, az)

            return jsonify({"ok": True, "seq": state.seq})
        except Exception as e:
            log.warning("IMU parse error: %s", e)
            return jsonify({"ok": False, "error": str(e)}), 400

    @app.route("/hybrid/status", methods=["GET"])
    def status_endpoint():
        """Return current sensor state."""
        return jsonify(state.to_dict())

    @app.route("/hybrid/mode", methods=["POST"])
    def mode_endpoint():
        """Set hybrid mode: 0=HR, 1=IMU, 2=Hybrid."""
        try:
            data = request.get_json(force=True)
            mode = int(data.get("mode", 0))
            state.set_mode(mode)
            protocol.send_mode(mode)
            return jsonify({"ok": True, "mode": mode})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400

    log.info("IMU HTTP server on http://0.0.0.0:%d", port)
    log.info("  GET  /              — phone sender page (tunnel this for HTTPS)")
    log.info("  POST /imu           — receive phone IMU data")
    log.info("  GET  /hybrid/status — sensor state")
    log.info("  POST /hybrid/mode   — set mode (0=HR, 1=IMU, 2=Hybrid)")
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)


# ─── Clock Sync Thread ─────────────────────────────────────────────────────

def clock_sync_thread(protocol: HybridOSCProtocol, interval: float = 1.0):
    """Periodically broadcast /sync/clock for temporal alignment."""
    while True:
        protocol.send_clock()
        time.sleep(interval)


# ─── Main ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Hybrid Sensor Bridge for digital-beacon")
    parser.add_argument("--mode", choices=["simulate", "ble", "off"],
                        default="simulate",
                        help="HR source mode (default: simulate)")
    parser.add_argument("--bpm", type=float, default=72.0,
                        help="Base BPM for simulate mode (default: 72)")
    parser.add_argument("--variation", type=float, default=3.0,
                        help="BPM variation for simulate (default: 3)")
    parser.add_argument("--device", default=None,
                        help="BLE device name filter (default: any HR sensor)")
    parser.add_argument("--scan-timeout", type=float, default=10.0,
                        help="BLE scan timeout (default: 10s)")
    parser.add_argument("--imu-port", type=int, default=DEFAULT_IMU_HTTP_PORT,
                        help=f"HTTP port for IMU endpoint (default: {DEFAULT_IMU_HTTP_PORT})")
    parser.add_argument("--target-host", default="127.0.0.1",
                        help="Target OSC host (default: 127.0.0.1)")
    parser.add_argument("--target-port", type=int, default=57120,
                        help="Target OSC port (default: 57120)")
    parser.add_argument("--clock-interval", type=float, default=1.0,
                        help="Sync clock broadcast interval in seconds (default: 1.0)")
    args = parser.parse_args()

    # State + Protocol
    state = SensorState()
    protocol = HybridOSCProtocol(host=args.target_host, port=args.target_port)

    log.info("=" * 55)
    log.info("  Hybrid Sensor Bridge v1.0")
    log.info("  digital-beacon core integration")
    log.info("=" * 55)
    log.info("  HR mode:  %s", args.mode)
    log.info("  IMU port: %d", args.imu_port)
    log.info("  OSC out:  %s:%d  ->  sclang", args.target_host, args.target_port)
    log.info("  Protocol: /hybrid/sync/clock, /hybrid/hr, /hybrid/imu, /hybrid/mode")
    log.info("=" * 55)

    # Start clock sync thread
    clock_t = threading.Thread(target=clock_sync_thread, args=(protocol, args.clock_interval),
                               name="hybrid-clock", daemon=True)
    clock_t.start()

    # Start IMU HTTP server thread
    imu_t = threading.Thread(target=run_imu_server, args=(state, protocol, args.imu_port),
                             name="hybrid-imu", daemon=True)
    imu_t.start()

    # Start HR mode
    if args.mode == "simulate":
        run_simulate(state, protocol, args)
    elif args.mode == "ble":
        run_ble(state, protocol, args)
    elif args.mode == "off":
        log.info("HR mode is OFF. IMU server still running.")
        while True:
            time.sleep(1)

    log.info("Hybrid bridge stopped.")


if __name__ == "__main__":
    main()

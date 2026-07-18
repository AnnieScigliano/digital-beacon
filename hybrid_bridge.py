"""Hybrid Sensor Bridge for digital-beacon.

Combines:
  - Fitbit Charge 6 heart rate via BLE (real-time beat events)
  - Phone IMU (gyroscope + accelerometer) via HTTP POST /imu
  - Temporal synchronization protocol with timestamps and sequence numbers
  - OSC output to the digital-beacon engine (sclang :57120)

Protocol OSC (addresses/arg shapes match the OSCdefs registered in beacon.scd):
  /hybrid/sync/clock [timestamp, seq, bpm_ref]          — sync heartbeat
  /hybrid/hr         [bpm, rr_ms, confidence]           — HR data, device "A" (legacy address)
  /hybrid/hr/<dev>   [bpm, rr_ms, confidence]           — HR data, any other device
  /hybrid/imu        [yaw, pitch, roll, ax, ay, az]      — IMU frame, device "A" (legacy address)
  /hybrid/imu/<dev>  [yaw, pitch, roll, ax, ay, az]      — IMU frame, any other device
  /hybrid/mode       [mode]                             — 0=HR, 1=IMU, 2=Hybrid (global, not per-device)

Multi-device: the phone sender page lets the wearer pick "Phone A" or
"Phone B" before starting; that tag rides along in the /imu POST body and
is used both to key server-side state (SensorState per device, in
HybridSensorHub below) and to pick the OSC address above. Device "A" is
the default everywhere, so single-phone setups behave exactly as before
multi-device support existed.

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
import urllib.error
import urllib.request
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
DEFAULT_DASHBOARD_PORT = 8080            # digital_beacon.main's dashboard (0 disables sync)
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
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
  <title>Beacon IMU</title>
  <style>
    :root {
      --color-a: #3498db;   /* same blue drawPairViz uses for device A */
      --color-b: #e67e22;   /* same orange drawPairViz uses for device B */
      --ok: #2ecc71;
      --warn: #e67e22;
      --err: #e74c3c;
      --idle: #666;
      --device-color: var(--color-a);
    }
    * { box-sizing: border-box; }
    body {
      font-family: -apple-system, "SF Pro Text", "Segoe UI", Roboto, sans-serif;
      background: #0a0a0a; color: #eee; padding: 24px 20px 40px;
      min-height: 100vh; margin: 0;
    }
    h2 { font-weight: 600; letter-spacing: 0.02em; margin: 0 0 20px; }
    button {
      font-size: 17px; font-weight: 600; padding: 16px; margin: 8px 0;
      width: 100%; border-radius: 14px; border: 2px solid #333;
      background: #1a1a1a; color: #eee; -webkit-tap-highlight-color: transparent;
      transition: transform 0.08s ease, opacity 0.15s ease;
    }
    button:active { transform: scale(0.97); }
    button:disabled { opacity: 0.35; }
    #devicePicker button:nth-of-type(1) { border-color: var(--color-a); color: var(--color-a); }
    #devicePicker button:nth-of-type(2) { border-color: var(--color-b); color: var(--color-b); }
    #startBtn:not(:disabled) { border-color: var(--ok); color: var(--ok); }
    #stopBtn:not(:disabled) { border-color: var(--err); color: var(--err); }
    #deviceLabel {
      font-size: 15px; font-weight: 600; margin-bottom: 14px;
      color: var(--device-color); letter-spacing: 0.04em; text-transform: uppercase;
    }
    #statusRow { display: flex; align-items: center; gap: 8px; margin-top: 22px; font-size: 14px; color: #999; }
    #statusDot {
      width: 10px; height: 10px; border-radius: 50%; background: var(--idle);
      flex-shrink: 0; transition: background 0.2s ease;
    }
    #wakeWarning {
      display: none; margin-top: 10px; padding: 10px 12px; border-radius: 10px;
      background: rgba(230,126,34,0.12); border: 1px solid var(--warn);
      color: var(--warn); font-size: 13px; line-height: 1.4;
    }
    #motionCanvas {
      display: block; width: 100%; max-width: 260px; aspect-ratio: 1;
      margin: 18px auto 4px; border-radius: 16px; background: #111;
    }
    .readout {
      display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px;
      margin-top: 18px;
    }
    .stat {
      background: #161616; border-radius: 12px; padding: 10px 6px; text-align: center;
    }
    .stat .label { display: block; font-size: 10px; letter-spacing: 0.08em; color: #777; text-transform: uppercase; }
    .stat .val { display: block; font-family: "SF Mono", ui-monospace, monospace; font-size: 17px; margin-top: 4px; color: var(--device-color); }
  </style>
</head>
<body>
  <h2>Beacon IMU Sender</h2>
  <div id="devicePicker">
    <p>Which phone is this?</p>
    <button onclick="pickDevice('A')">Phone A</button>
    <button onclick="pickDevice('B')">Phone B</button>
  </div>
  <div id="controls" style="display:none;">
    <div id="deviceLabel"></div>
    <button id="startBtn" onclick="start()">Start Sensors</button>
    <button id="stopBtn" onclick="stop()" disabled>Stop</button>
    <button id="recalBtn" onclick="recalibrate()" disabled>Recalibrate (face partner, stand still)</button>
  </div>
  <canvas id="motionCanvas"></canvas>
  <div id="statusRow"><span id="statusDot"></span><span id="statusText">idle</span></div>
  <div id="wakeWarning">
    This phone can't be kept awake automatically — please disable auto-lock
    (Settings) or it will stop sending data once the screen locks.
  </div>
  <div class="readout">
    <div class="stat"><span class="label">Yaw</span><span class="val" id="valYaw">0.0°</span></div>
    <div class="stat"><span class="label">Pitch</span><span class="val" id="valPitch">0.0°</span></div>
    <div class="stat"><span class="label">Roll</span><span class="val" id="valRoll">0.0°</span></div>
    <div class="stat"><span class="label">Ax</span><span class="val" id="valAx">0.00</span></div>
    <div class="stat"><span class="label">Ay</span><span class="val" id="valAy">0.00</span></div>
    <div class="stat"><span class="label">Az</span><span class="val" id="valAz">9.80</span></div>
  </div>

<script>
let device = new URLSearchParams(location.search).get('device');
let interval = null;
let streaming = false;
const statusText = document.getElementById('statusText');
const statusDot = document.getElementById('statusDot');
const wakeWarning = document.getElementById('wakeWarning');

// Same hex values as --color-a/--color-b above (and drawPairViz on the
// dashboard) — duplicated as RGB triples because canvas fillStyle needs an
// alpha-blendable color and reading resolved CSS custom properties back
// out in JS is unreliable across browsers.
const COLOR_A_RGB = '52,152,219';
const COLOR_B_RGB = '230,126,34';
let deviceRGB = COLOR_A_RGB;

function setStatus(text, color) {
  statusText.textContent = text;
  statusDot.style.background = color || 'var(--idle)';
}

function pickDevice(d) {
  device = d;
  document.getElementById('devicePicker').style.display = 'none';
  document.getElementById('controls').style.display = 'block';
  document.getElementById('deviceLabel').textContent = 'Phone ' + device;
  document.documentElement.style.setProperty(
    '--device-color', d === 'B' ? 'var(--color-b)' : 'var(--color-a)'
  );
  deviceRGB = d === 'B' ? COLOR_B_RGB : COLOR_A_RGB;
}
if (device) { pickDevice(device); }

// ─── Local motion visualization (Phase 2) ──────────────────────────────
// Purely local rendering — no extra network calls. A needle shows zeroed
// yaw (the same "facing" concept the dashboard's pair canvas uses) and a
// halo pulses with sway energy, computed here the same way
// VoiceParameterStore.get_hybrid_sway_energy does server-side (RMS of
// accel magnitude over a rolling window) so this is a faithful preview of
// what the pair canvas will show, not just a decoration.
const MOTION_WINDOW_MS = 2000;  // must match state.py's _HYBRID_MOTION_WINDOW_S
const SWAY_REF = 3.0;           // must match config.HYBRID_PAIR_ENERGY_REF
let motionHistory = [];

function pushMotionSample(mag) {
  const now = performance.now();
  motionHistory.push([now, mag]);
  const cutoff = now - MOTION_WINDOW_MS;
  while (motionHistory.length && motionHistory[0][0] < cutoff) motionHistory.shift();
}

function localSwayEnergy() {
  if (!motionHistory.length) return 0;
  const sumSq = motionHistory.reduce((s, [, v]) => s + v * v, 0);
  return Math.sqrt(sumSq / motionHistory.length);
}

let motionCtx = null, motionSize = 260;

function setupCanvas() {
  const canvas = document.getElementById('motionCanvas');
  const dpr = window.devicePixelRatio || 1;
  const cssSize = canvas.getBoundingClientRect().width || 260;
  canvas.width = cssSize * dpr;
  canvas.height = cssSize * dpr;
  motionCtx = canvas.getContext('2d');
  motionCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
  motionSize = cssSize;
}
window.addEventListener('resize', setupCanvas);
window.addEventListener('orientationchange', setupCanvas);
setupCanvas();

function drawMotion() {
  if (!motionCtx) return;
  const ctx = motionCtx, s = motionSize, cx = s / 2, cy = s / 2;
  ctx.clearRect(0, 0, s, s);
  ctx.fillStyle = '#111';
  ctx.fillRect(0, 0, s, s);

  const swayNorm = Math.min(localSwayEnergy() / SWAY_REF, 1.0);
  const baseR = s * 0.28;
  const haloR = baseR + swayNorm * s * 0.18;

  // Halo: grows and brightens with sway energy (0 = still, 1 = full-scale motion)
  ctx.beginPath();
  ctx.fillStyle = `rgba(${deviceRGB},${(0.10 + swayNorm * 0.28).toFixed(3)})`;
  ctx.arc(cx, cy, haloR, 0, Math.PI * 2);
  ctx.fill();

  // Static ring + a top tick marking the calibrated "forward" reference (yaw=0)
  ctx.beginPath();
  ctx.strokeStyle = `rgba(${deviceRGB},0.45)`;
  ctx.lineWidth = 2;
  ctx.arc(cx, cy, baseR, 0, Math.PI * 2);
  ctx.stroke();
  ctx.beginPath();
  ctx.strokeStyle = 'rgba(255,255,255,0.18)';
  ctx.lineWidth = 1;
  ctx.moveTo(cx, cy - baseR - 6);
  ctx.lineTo(cx, cy - baseR + 6);
  ctx.stroke();

  // Needle: rotates with zeroed yaw — snaps back to 12 o'clock on Recalibrate
  ctx.save();
  ctx.translate(cx, cy);
  ctx.rotate((yaw * Math.PI) / 180);
  const needleLen = baseR * 0.9;
  ctx.beginPath();
  ctx.moveTo(0, 0);
  ctx.lineTo(0, -needleLen);
  ctx.strokeStyle = `rgb(${deviceRGB})`;
  ctx.lineWidth = 4;
  ctx.lineCap = 'round';
  ctx.stroke();
  ctx.beginPath();
  ctx.arc(0, -needleLen, 5, 0, Math.PI * 2);
  ctx.fillStyle = `rgb(${deviceRGB})`;
  ctx.fill();
  ctx.restore();

  ctx.beginPath();
  ctx.fillStyle = '#eee';
  ctx.arc(cx, cy, 3, 0, Math.PI * 2);
  ctx.fill();

  requestAnimationFrame(drawMotion);
}
requestAnimationFrame(drawMotion);

// ─── Screen Wake Lock ───────────────────────────────────────────────────
// Without this, iOS/Android will dim then lock the screen after their
// normal inactivity timeout — and on iOS, a locked/backgrounded Safari tab
// stops firing deviceorientation/devicemotion entirely (and pauses the
// sendData() interval), so the phone silently stops sending data mid-dance.
// The Wake Lock API keeps the screen ON (not off — there's no web API to
// keep sensors alive with the screen off) for as long as this tab stays
// foregrounded. It requires a secure (HTTPS) context, same as the iOS
// motion-permission prompt above — already satisfied by the cloudflared
// tunnel this page is served through.
let wakeLock = null;

async function requestWakeLock() {
  if (!('wakeLock' in navigator)) {
    wakeWarning.style.display = 'block';
    return;
  }
  try {
    wakeLock = await navigator.wakeLock.request('screen');
    wakeWarning.style.display = 'none';
    wakeLock.addEventListener('release', () => {
      // Fires when the OS/browser reclaims it (e.g. tab backgrounded) —
      // re-requested by the visibilitychange handler below once we're
      // foregrounded again, not from here (this listener has no gesture).
      wakeLock = null;
    });
  } catch (err) {
    // Most common cause: non-secure context, or the page lost visibility
    // between the call starting and resolving.
    wakeWarning.style.display = 'block';
  }
}

document.addEventListener('visibilitychange', async () => {
  if (streaming && document.visibilityState === 'visible' && wakeLock === null) {
    await requestWakeLock();
  }
});

async function start() {
  if (typeof DeviceMotionEvent !== 'undefined' &&
      typeof DeviceMotionEvent.requestPermission === 'function') {
    // iOS 13+ requires an explicit user-gesture permission request.
    const resp = await DeviceMotionEvent.requestPermission().catch(() => 'denied');
    if (resp !== 'granted') {
      setStatus('motion permission denied', 'var(--err)');
      return;
    }
  }
  window.addEventListener('deviceorientation', handleOrientation);
  window.addEventListener('devicemotion', handleMotion);
  interval = setInterval(sendData, 100); // 10 Hz
  streaming = true;
  await requestWakeLock();
  document.getElementById('startBtn').disabled = true;
  document.getElementById('stopBtn').disabled = false;
  document.getElementById('recalBtn').disabled = false;
  setStatus('streaming @ 10Hz (calibrating...)', 'var(--ok)');
}

function stop() {
  window.removeEventListener('deviceorientation', handleOrientation);
  window.removeEventListener('devicemotion', handleMotion);
  clearInterval(interval);
  streaming = false;
  if (wakeLock) { wakeLock.release().catch(() => {}); wakeLock = null; }
  document.getElementById('startBtn').disabled = false;
  document.getElementById('stopBtn').disabled = true;
  document.getElementById('recalBtn').disabled = true;
  setStatus('stopped', 'var(--idle)');
}

// Yaw/pitch are reported relative to wherever the wearer was facing/tilted
// at calibration time (first reading after Start, or after Recalibrate) —
// not raw compass heading, which drifts and is noisy indoors. Recalibrating
// mid-session (e.g. "now stand facing your partner") just re-zeros both.
let yawZero = null, pitchZero = null;
function wrap180(deg) {
  let d = deg % 360;
  if (d > 180) d -= 360;
  if (d < -180) d += 360;
  return d;
}
function recalibrate() {
  yawZero = null;
  pitchZero = null;
  setStatus('recalibrating on next reading...', 'var(--warn)');
}

let yaw = 0, pitch = 0, roll = 0, ax = 0, ay = 0, az = 9.8;

function handleOrientation(e) {
  const rawYaw = e.alpha || 0;
  const rawPitch = e.beta || 0;
  if (yawZero === null) { yawZero = rawYaw; pitchZero = rawPitch; }
  yaw = wrap180(rawYaw - yawZero);
  pitch = rawPitch - pitchZero;
  roll = e.gamma || 0;
}

function handleMotion(e) {
  const a = e.acceleration || {};
  ax = a.x || 0;
  ay = a.y || 0;
  az = a.z || 9.8;
  pushMotionSample(Math.sqrt(ax * ax + ay * ay + az * az));
}

function sendData() {
  const payload = { device, yaw, pitch, roll, ax, ay, az };
  fetch('/imu', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  }).then(r => r.json())
    .then(j => {
      document.getElementById('valYaw').textContent = yaw.toFixed(1) + '°';
      document.getElementById('valPitch').textContent = pitch.toFixed(1) + '°';
      document.getElementById('valRoll').textContent = roll.toFixed(1) + '°';
      document.getElementById('valAx').textContent = ax.toFixed(2);
      document.getElementById('valAy').textContent = ay.toFixed(2);
      document.getElementById('valAz').textContent = az.toFixed(2);
      setStatus('streaming @ 10Hz' + (wakeLock ? '' : ' (screen-lock not held)'),
                 wakeLock ? 'var(--ok)' : 'var(--warn)');
    })
    .catch(e => { setStatus('error: ' + e.message, 'var(--err)'); });
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


class HybridSensorHub:
    """Holds one SensorState per connected device (e.g. "A", "B").

    HR (BLE/simulate) always addresses device "A" — this bridge only talks
    to one heart-rate strap at a time. IMU is where multi-device matters:
    each phone tags its POSTs with a device id and gets its own SensorState.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._devices: dict[str, SensorState] = {}
        self.mode: int = 0  # 0=HR, 1=IMU, 2=Hybrid — global, not per-device

    def get(self, device: str) -> SensorState:
        with self._lock:
            if device not in self._devices:
                self._devices[device] = SensorState()
            return self._devices[device]

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "mode": self.mode,
                "devices": {dev: state.to_dict() for dev, state in self._devices.items()},
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

    def send_heartbeat(self, bpm: float, rr_ms: float, confidence: float = 1.0,
                       device: str = "A"):
        """Send heart rate data. Device "A" uses the legacy bare address
        (matches beacon.scd's \\hybrid_hr OSCdef); any other device gets
        /hybrid/hr/<device>."""
        seq = self._next_seq()
        addr = "/hybrid/hr" if device == "A" else f"/hybrid/hr/{device}"
        self._client.send_message(addr, [bpm, rr_ms, confidence])
        log.debug("%s seq=%d bpm=%.1f rr=%.0fms", addr, seq, bpm, rr_ms)

    def send_imu(self, yaw: float, pitch: float, roll: float,
                 ax: float, ay: float, az: float, device: str = "A"):
        """Send IMU frame. Device "A" uses the legacy bare address (matches
        beacon.scd's \\hybrid_imu OSCdef); any other device gets
        /hybrid/imu/<device>."""
        seq = self._next_seq()
        addr = "/hybrid/imu" if device == "A" else f"/hybrid/imu/{device}"
        self._client.send_message(addr, [yaw, pitch, roll, ax, ay, az])
        log.debug("%s seq=%d yaw=%.1f pitch=%.1f roll=%.1f", addr, seq, yaw, pitch, roll)

    def send_mode(self, mode: int):
        """Announce hybrid mode change. Arg shape matches beacon.scd's \\hybrid_mode OSCdef."""
        seq = self._next_seq()
        self._client.send_message("/hybrid/mode", [mode])
        log.info("/hybrid/mode seq=%d mode=%d (%s)", seq, mode,
                 ["HR", "IMU", "Hybrid"][mode])

    def send_imu_bundle(self, yaw: float, pitch: float, roll: float,
                        ax: float, ay: float, az: float, device: str = "A"):
        """Send IMU as OSC bundle (placeholder for future optimization)."""
        self.send_imu(yaw, pitch, roll, ax, ay, az, device=device)


def sync_dashboard(path: str, payload: dict, host: str, port: int) -> None:
    """Best-effort mirror of a sensor update into digital_beacon's dashboard.

    This bridge sends HR/IMU straight to sclang via OSC (see HybridOSCProtocol
    above) — that's the audio-critical path and doesn't touch this at all.
    But digital_beacon.main's web dashboard (:8080) only knows about sensor
    values that went through its own /api/hybrid/* endpoints, so without this
    the dashboard's "Hybrid Sensors" panel stays at zero no matter what this
    bridge is doing. The /sync variants below update the dashboard's state
    only — they don't re-forward to sclang, since that already happened.
    Failures (dashboard not running, etc.) are swallowed — this is a nice-to-
    have status mirror, not required for the audio path to work.
    """
    if port <= 0:
        return
    try:
        req = urllib.request.Request(
            f"http://{host}:{port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=0.5).close()
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        log.debug("Dashboard sync %s failed: %s", path, e)


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
                sync_dashboard("/api/hybrid/hr/sync",
                               {"device": "A", "bpm": bpm, "rr_ms": rr_ms, "confidence": 1.0},
                               args.dashboard_host, args.dashboard_port)
                beat_count += 1
                log.info("♥ beat #%d  %d BPM  (RR: %.0fms)", beat_count, bpm, rr_ms)
        else:
            rr_ms = 60000.0 / max(bpm, 1)
            state.update_hr(bpm, rr_ms)
            protocol.send_heartbeat(bpm, rr_ms)
            sync_dashboard("/api/hybrid/hr/sync",
                           {"device": "A", "bpm": bpm, "rr_ms": rr_ms, "confidence": 1.0},
                           args.dashboard_host, args.dashboard_port)
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
            sync_dashboard("/api/hybrid/hr/sync",
                           {"device": "A", "bpm": current_bpm, "rr_ms": rr_ms, "confidence": 1.0},
                           args.dashboard_host, args.dashboard_port)
            beat_count += 1

            bar = "♥" if beat_count % 2 == 0 else "♡"
            log.info("%s beat #%d  %.1f BPM  (RR: %.0fms)", bar, beat_count, current_bpm, rr_ms)

            time.sleep(interval)
    except KeyboardInterrupt:
        pass

    log.info("Stopped after %d beats.", beat_count)


# ─── IMU HTTP Server (Flask) ───────────────────────────────────────────────

def run_imu_server(hub: HybridSensorHub, protocol: HybridOSCProtocol, port: int,
                    dashboard_host: str = "127.0.0.1", dashboard_port: int = DEFAULT_DASHBOARD_PORT):
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
          {"device": "A", "yaw": 45.0, "pitch": 10.0, "roll": 0.0,
           "ax": 0.1, "ay": 0.0, "az": 9.8}
        "device" defaults to "A" so single-phone senders (or any client that
        predates multi-device support) need no changes.
        """
        try:
            data = request.get_json(force=True)
            device = str(data.get("device") or "A")
            yaw = float(data.get("yaw", 0.0))
            pitch = float(data.get("pitch", 0.0))
            roll = float(data.get("roll", 0.0))
            ax = float(data.get("ax", 0.0))
            ay = float(data.get("ay", 0.0))
            az = float(data.get("az", 0.0))

            state = hub.get(device)
            state.update_imu(yaw, pitch, roll, ax, ay, az)
            protocol.send_imu(yaw, pitch, roll, ax, ay, az, device=device)
            sync_dashboard("/api/hybrid/imu/sync",
                           {"device": device, "yaw": yaw, "pitch": pitch, "roll": roll,
                            "ax": ax, "ay": ay, "az": az},
                           dashboard_host, dashboard_port)

            return jsonify({"ok": True, "device": device, "seq": state.seq})
        except Exception as e:
            log.warning("IMU parse error: %s", e)
            return jsonify({"ok": False, "error": str(e)}), 400

    @app.route("/hybrid/status", methods=["GET"])
    def status_endpoint():
        """Return current sensor state for every device seen so far."""
        return jsonify(hub.to_dict())

    @app.route("/hybrid/mode", methods=["POST"])
    def mode_endpoint():
        """Set hybrid mode: 0=HR, 1=IMU, 2=Hybrid (global, not per-device)."""
        try:
            data = request.get_json(force=True)
            mode = int(data.get("mode", 0))
            hub.mode = mode
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
    parser.add_argument("--dashboard-host", default="127.0.0.1",
                        help="digital_beacon dashboard host, for mirroring live sensor "
                             "values into its web UI (default: 127.0.0.1)")
    parser.add_argument("--dashboard-port", type=int, default=DEFAULT_DASHBOARD_PORT,
                        help=f"digital_beacon dashboard port (default: {DEFAULT_DASHBOARD_PORT}); "
                             "0 disables the mirror")
    args = parser.parse_args()

    # State (one SensorState per device, HR always on device "A") + Protocol
    hub = HybridSensorHub()
    protocol = HybridOSCProtocol(host=args.target_host, port=args.target_port)

    log.info("=" * 55)
    log.info("  Hybrid Sensor Bridge v1.0")
    log.info("  digital-beacon core integration")
    log.info("=" * 55)
    log.info("  HR mode:  %s", args.mode)
    log.info("  IMU port: %d", args.imu_port)
    log.info("  OSC out:  %s:%d  ->  sclang", args.target_host, args.target_port)
    log.info("  Protocol: /hybrid/sync/clock, /hybrid/hr[/<dev>], /hybrid/imu[/<dev>], /hybrid/mode")
    log.info("=" * 55)

    # Start clock sync thread
    clock_t = threading.Thread(target=clock_sync_thread, args=(protocol, args.clock_interval),
                               name="hybrid-clock", daemon=True)
    clock_t.start()

    # Start IMU HTTP server thread
    imu_t = threading.Thread(
        target=run_imu_server,
        args=(hub, protocol, args.imu_port, args.dashboard_host, args.dashboard_port),
        name="hybrid-imu", daemon=True)
    imu_t.start()

    # Start HR mode (always device "A" — this bridge talks to one HR strap)
    if args.mode == "simulate":
        run_simulate(hub.get("A"), protocol, args)
    elif args.mode == "ble":
        run_ble(hub.get("A"), protocol, args)
    elif args.mode == "off":
        log.info("HR mode is OFF. IMU server still running.")
        while True:
            time.sleep(1)

    log.info("Hybrid bridge stopped.")


if __name__ == "__main__":
    main()

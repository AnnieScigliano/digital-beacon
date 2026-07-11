"""Two-Phone Dance Simulator — synthetic IMU streams for the hug/dance MVP.

Mirrors cymatic-control/simulate_tilt.py's pattern (stage-based mock sensor
data, live status printout) but for hybrid_bridge.py's /imu endpoint instead
of raw OSC: it POSTs synthetic device "A" and "B" readings so the pair
mapping (Pair Mode), the derived variables (sway energy, synchrony, pair
energy), and the dashboard's Pair Mode Telemetry canvas can all be exercised
without two real phones and two people every iteration.

Stages cover the scenarios the hug/dance roadmap's Phase 5 test plan calls
for: a still baseline, each person moving solo, synced vs. desynced sway
(to sanity-check the synchrony math), a slow opposite-direction spin, and a
"tightening embrace" ramp.

Usage:
    python simulate_dance.py                          # all stages, looping
    python simulate_dance.py --stages 4,5              # just synced vs desynced
    python simulate_dance.py --speed 2.0                # run stages 2x faster
    python simulate_dance.py --port 5051 --no-loop      # run once, then stop

This only talks to hybrid_bridge.py's /imu endpoint (same as a real phone's
sender page) — it does not touch HR at all, matching Pair Mode's "movement
only" scope. Run hybrid_bridge.py with `--mode off` alongside this so no HR
data is generated either.
"""

import argparse
import json
import math
import time
import urllib.error
import urllib.request

DEFAULT_PORT = 5051
DEFAULT_DASHBOARD_PORT = 8080

STAGES = [
    {
        "id": 1,
        "name": "STILL",
        "description": "Both dancers stand still — baseline/calibration check",
        "duration": 8.0,
        "yaw_amp_a": 0.0, "yaw_amp_b": 0.0, "yaw_freq": 0.0,
        "sway_amp_a": 0.0, "sway_amp_b": 0.0, "sway_freq": 0.0,
        "pitch_amp_a": 0.0, "pitch_amp_b": 0.0,
    },
    {
        "id": 2,
        "name": "SOLO_A",
        "description": "Only A sways and turns — B stays still (Phase 5's solo test step)",
        "duration": 15.0,
        "yaw_amp_a": 45.0, "yaw_amp_b": 0.0, "yaw_freq": 0.05,
        "sway_amp_a": 2.5, "sway_amp_b": 0.0, "sway_freq": 0.3,
        "pitch_amp_a": 10.0, "pitch_amp_b": 0.0,
    },
    {
        "id": 3,
        "name": "SOLO_B",
        "description": "Only B sways and turns — A stays still",
        "duration": 15.0,
        "yaw_amp_a": 0.0, "yaw_amp_b": 45.0, "yaw_freq": 0.05,
        "sway_amp_a": 0.0, "sway_amp_b": 2.5, "sway_freq": 0.3,
        "pitch_amp_a": 0.0, "pitch_amp_b": 10.0,
    },
    {
        "id": 4,
        "name": "SYNCED_SWAY",
        "description": "Both sway in phase together — should read as high synchrony",
        "duration": 20.0,
        "yaw_amp_a": 20.0, "yaw_amp_b": 20.0, "yaw_freq": 0.04,
        "sway_amp_a": 2.0, "sway_amp_b": 2.0, "sway_freq": 0.25,
        "pitch_amp_a": 5.0, "pitch_amp_b": 5.0,
    },
    {
        "id": 5,
        "name": "DESYNCED_SWAY",
        "description": "Both move, but at different rates — low synchrony",
        "duration": 20.0,
        "yaw_amp_a": 20.0, "yaw_amp_b": -20.0, "yaw_freq": 0.04,
        # Different frequencies, NOT a phase offset: sway_energy/synchrony are
        # computed from accel MAGNITUDE (sqrt(ax^2+ay^2+az^2)), and magnitude
        # is insensitive to sign — |sin(x+pi)| == |sin(x)| exactly, so a pure
        # antiphase pair actually reads as perfectly IN sync, not out of it
        # (caught via a real running dry-run session, not just unit tests —
        # an earlier version of this stage used sway_phase_offset=pi and
        # measured synchrony=1.0). Distinct, non-simply-related rates are
        # what actually decorrelates the magnitude series.
        "sway_amp_a": 2.0, "sway_amp_b": 2.0, "sway_freq_a": 0.37, "sway_freq_b": 0.53,
        "pitch_amp_a": 5.0, "pitch_amp_b": -5.0,
    },
    {
        "id": 6,
        "name": "SLOW_SPIN",
        "description": "Both slowly rotate in opposite directions, as if turning during a hug",
        "duration": 20.0,
        "yaw_amp_a": 90.0, "yaw_amp_b": -90.0, "yaw_freq": 0.03,
        "sway_amp_a": 1.0, "sway_amp_b": 1.0, "sway_freq": 0.2,
        "pitch_amp_a": 0.0, "pitch_amp_b": 0.0,
    },
    {
        "id": 7,
        "name": "EMBRACE_TIGHTEN",
        "description": "Synced sway that ramps from calm to intense — pair energy climbs",
        "duration": 20.0,
        "ramp": True,
        "yaw_amp_a": 10.0, "yaw_amp_b": 10.0, "yaw_freq": 0.05,
        "sway_amp_a": 3.0, "sway_amp_b": 3.0, "sway_freq": 0.3,
        "pitch_amp_a": 15.0, "pitch_amp_b": 15.0,
    },
]


def sample_stage(stage: dict, t_stage: float) -> dict:
    """Synthetic (yaw, pitch, roll, ax, ay, az) for both devices at t_stage
    seconds into `stage`. Values are the "already zeroed" readings a real
    sender page would produce after its own calibration — this simulator
    has no compass/browser layer to mimic, so it just emits what the server
    side actually consumes."""
    ramp = min(t_stage / stage["duration"], 1.0) if stage.get("ramp") else 1.0

    yaw_freq = stage.get("yaw_freq", 0.0)
    yaw_a = stage["yaw_amp_a"] * math.sin(2 * math.pi * yaw_freq * t_stage) if yaw_freq > 0 else 0.0
    yaw_b = stage["yaw_amp_b"] * math.sin(2 * math.pi * yaw_freq * t_stage) if yaw_freq > 0 else 0.0

    pitch_a = stage.get("pitch_amp_a", 0.0) * ramp * math.sin(2 * math.pi * 0.15 * t_stage)
    pitch_b = stage.get("pitch_amp_b", 0.0) * ramp * math.sin(2 * math.pi * 0.15 * t_stage + 0.3)

    # Per-device sway frequency (falls back to a shared "sway_freq" for
    # stages that don't need to desynchronize, e.g. SYNCED_SWAY). Using
    # DIFFERENT rates — not a phase offset — is what actually produces two
    # decorrelated accel-MAGNITUDE series; see DESYNCED_SWAY's comment above.
    sway_freq_a = stage.get("sway_freq_a", stage.get("sway_freq", 0.0))
    sway_freq_b = stage.get("sway_freq_b", stage.get("sway_freq", 0.0))
    az_a = stage["sway_amp_a"] * ramp * math.sin(2 * math.pi * sway_freq_a * t_stage) if sway_freq_a > 0 else 0.0
    az_b = stage["sway_amp_b"] * ramp * math.sin(2 * math.pi * sway_freq_b * t_stage) if sway_freq_b > 0 else 0.0
    ax_a = az_a * 0.15 * math.cos(2 * math.pi * sway_freq_a * t_stage * 0.5) if sway_freq_a > 0 else 0.0
    ax_b = az_b * 0.15 * math.cos(2 * math.pi * sway_freq_b * t_stage * 0.5) if sway_freq_b > 0 else 0.0

    return {
        "A": {"yaw": yaw_a, "pitch": pitch_a, "roll": 0.0, "ax": ax_a, "ay": 0.0, "az": az_a},
        "B": {"yaw": yaw_b, "pitch": pitch_b, "roll": 0.0, "ax": ax_b, "ay": 0.0, "az": az_b},
    }


def post_imu(host: str, port: int, device: str, reading: dict, timeout: float = 0.5) -> bool:
    payload = {"device": device, **reading}
    try:
        req = urllib.request.Request(
            f"http://{host}:{port}/imu",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=timeout).close()
        return True
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def fetch_server_derived(dashboard_host: str, dashboard_port: int, timeout: float = 0.5):
    """Best-effort GET of digital_beacon's /api/hybrid/status, so this
    simulator can print the SERVER's own computed sway/synchrony next to
    the values it's intentionally generating — a live sanity check that
    the rolling-buffer math (digital_beacon/state.py) is doing what the
    scenario expects. Returns None if the dashboard isn't running; this
    tool works fine without it (hybrid_bridge.py's /imu is all it needs)."""
    try:
        req = urllib.request.Request(f"http://{dashboard_host}:{dashboard_port}/api/hybrid/status")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return None


def bar(value: float, ref: float, width: int = 8) -> str:
    fill = int(max(0.0, min(1.0, value / ref)) * width) if ref > 0 else 0
    return "█" * fill + "░" * (width - fill)


def main():
    parser = argparse.ArgumentParser(
        description="Two-phone dance simulator — synthetic IMU for hybrid_bridge.py's /imu"
    )
    parser.add_argument("--host", default="127.0.0.1", help="hybrid_bridge.py host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"hybrid_bridge.py IMU port (default: {DEFAULT_PORT})")
    parser.add_argument("--dashboard-host", default="127.0.0.1", help="digital_beacon dashboard host, for the server-side verification readout")
    parser.add_argument("--dashboard-port", type=int, default=DEFAULT_DASHBOARD_PORT,
                        help=f"digital_beacon dashboard port (default: {DEFAULT_DASHBOARD_PORT}); 0 disables the readout")
    parser.add_argument("--rate", type=float, default=10.0, help="Send rate in Hz, matching the real sender page (default: 10)")
    parser.add_argument("--speed", type=float, default=1.0, help="Stage-time speed multiplier (default: 1.0)")
    parser.add_argument("--stages", type=str, default=None, help="Comma-separated stage IDs to run (default: all)")
    parser.add_argument("--loop", action="store_true", default=True, help="Loop through stages (default: true)")
    parser.add_argument("--no-loop", action="store_true", default=False, help="Run through stages once then stop")
    args = parser.parse_args()

    do_loop = args.loop and not args.no_loop

    if args.stages:
        stage_ids = [int(x) for x in args.stages.split(",")]
        stages = [s for s in STAGES if s["id"] in stage_ids]
    else:
        stages = list(STAGES)

    if not stages:
        print("ERROR: No valid stages selected")
        return

    dt = 1.0 / args.rate

    print("=" * 70)
    print("  Two-Phone Dance Simulator")
    print("=" * 70)
    print(f"  Target:     {args.host}:{args.port}/imu  (device A + B)")
    print(f"  Rate:       {args.rate} Hz  |  Speed: {args.speed}x")
    print(f"  Stages:     {len(stages)}  |  Loop: {do_loop}")
    if args.dashboard_port:
        print(f"  Dashboard:  {args.dashboard_host}:{args.dashboard_port} (server-side verification readout)")
    print("=" * 70)
    for s in stages:
        print(f"    [{s['id']}] {s['name']:16s} — {s['description']}")
    print("=" * 70)
    print()

    stage_idx = 0
    t_stage = 0.0
    tick = 0

    try:
        while True:
            stage = stages[stage_idx]
            stage_duration = stage["duration"] / args.speed

            reading = sample_stage(stage, t_stage * args.speed)
            post_imu(args.host, args.port, "A", reading["A"])
            post_imu(args.host, args.port, "B", reading["B"])

            if tick % max(int(args.rate), 1) == 0:  # ~once per second
                derived = None
                if args.dashboard_port:
                    status = fetch_server_derived(args.dashboard_host, args.dashboard_port)
                    if status:
                        derived = status.get("devices", {})
                pct = int(min(t_stage / stage_duration, 1.0) * 100)
                line = (f"\r  [{stage['id']}] {stage['name']:16s} {pct:3d}%  "
                        f"yawA={reading['A']['yaw']:+6.1f} yawB={reading['B']['yaw']:+6.1f}  "
                        f"swayA[{bar(abs(reading['A']['az']), 3.0)}] swayB[{bar(abs(reading['B']['az']), 3.0)}]")
                if derived is not None:
                    line += "  (server responding)"
                elif args.dashboard_port:
                    line += "  (dashboard unreachable — /imu still sent fine)"
                print(line, end="", flush=True)

            t_stage += dt
            tick += 1

            if t_stage >= stage_duration:
                t_stage = 0.0
                stage_idx += 1
                if stage_idx >= len(stages):
                    if not do_loop:
                        break
                    stage_idx = 0
                    print("\n\n  --- Looping ---")
                next_stage = stages[stage_idx]
                print(f"\n\n  >>> Stage {next_stage['id']}: {next_stage['name']}")
                print(f"      {next_stage['description']}")
                print()

            time.sleep(dt)

    except KeyboardInterrupt:
        pass

    print("\n\n  Done.\n")


if __name__ == "__main__":
    main()

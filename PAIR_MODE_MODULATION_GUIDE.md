# Pair Mode: How Movement Becomes Sound

This is a plain-language map of exactly what each phone measures, what
that becomes inside `digital_beacon`, and what you actually hear — for
Pair Mode (the two-phone hug/dance MVP), not the older single-device
Hybrid mode. Every formula below is quoted from the real code
(`digital_beacon/state.py`, `digital_beacon/audio_engine.py`,
`digital_beacon/config.py`) so this stays accurate as the constants get
tuned — if you change a constant, update the number here too.

Pair Mode only runs when `config.HYBRID_PAIR_MODE_ENABLED` (Python) and
`hybridPairModeEnabled` (SuperCollider) are both on. It deliberately has
no heart-rate input at all — everything here comes from each phone's
orientation and accelerometer only.

## The raw ingredients (per phone)

Each phone streams six numbers at 10 Hz: `yaw, pitch, roll, ax, ay, az`.

- **yaw** — which way the wearer is facing, *zeroed to whatever direction
  they were facing when they tapped Start* (or Recalibrate). So yaw isn't
  a compass heading — it's "how far have I turned since I began,"
  which is what actually matters for two people rotating around each
  other during a slow dance.
- **pitch** — forward/backward lean, also zeroed at Start.
- **roll** — currently measured but not used by any mapping yet.
- **ax, ay, az** — linear acceleration on each axis. Not used directly;
  they're combined into **accel magnitude** = `sqrt(ax² + ay² + az²)`,
  which is what everything downstream actually consumes.

## Two derived signals per person, one shared between them

Before any sound mapping happens, `state.py` keeps a rolling 2-second
window of each phone's accel magnitude and derives:

- **Sway energy** (per person) — the RMS (root-mean-square) of that
  person's accel magnitude over the last 2 seconds. In plain terms:
  "how much is this person moving *right now*," smoothed so a single
  jolt doesn't spike it and stillness settles it back down within ~2s.
- **Synchrony** (shared, 0 to 1) — the Pearson correlation between the
  two people's accel-magnitude histories over that same 2-second window.
  This is not "are they close together" — it's "are they moving in the
  same rhythm at the same time," regardless of position. Swaying in
  unison scores high; one person still while the other moves scores near
  zero; moving in an opposite/uncorrelated rhythm can even go negative
  (clamped to 0 for audio purposes).
- **Pair energy** (shared) — simply the average of both people's sway
  energy. "How alive is the room right now," combining both bodies.
- **Relative facing** (shared, informational) — the difference between
  the two zeroed yaws, wrapped to -180°..180°. Tracked and shown on the
  dashboard, not currently wired to any sound parameter.

## Where the sound lives: three registers across 32 harmonics

The 32-harmonic field is split into three zones. This is the core idea of
Pair Mode: instead of one whole-field mix that changes for everyone, each
dancer owns a slice of the sound that answers mostly to *their own* body.

| Harmonics | Register | Driven by |
|-----------|----------|-----------|
| 1–10 | Person A | A's own yaw, pitch, sway |
| 11–20 | Person B | B's own yaw, pitch, sway |
| 21–28 | Shared "bloom" | Pair synchrony only |
| 29–32 | Untouched | Manual/default — no sensor |

### Your own register: pan, gain, lean

For each person, from their own yaw/pitch/sway:

- **Stereo position (pan)**: `yaw ÷ 180°`, clamped to ±1, then scaled by
  `HYBRID_PAIR_YAW_PAN_RANGE` (0.6) *and* the live dashboard pan-sensitivity
  slider (0–300%, default 100%). Turn to face away from your starting
  point and your register's sound audibly rotates toward that side —
  spin all the way around and it sweeps hard-left → center → hard-right
  → center. This is the control the first live test tuned via the
  slider — turn it up if the pan feels too subtle for the room, down if
  it feels disorienting.
- **Presence (gain)**: `sway_energy ÷ 3.0` (the reference "full-scale"
  motion level, `HYBRID_PAIR_ENERGY_REF`), capped at 1.0, times
  `HYBRID_PAIR_SWAY_GAIN_DEPTH` (0.5) → up to **+50% louder** in your own
  register the more you move. On top of that, forward/backward lean adds
  `pitch ÷ 90° × HYBRID_PAIR_LEAN_GAIN_DEPTH (0.2)` → up to **±20%** more.
  Standing still and upright leaves your register at its unboosted
  baseline; swaying vigorously while leaning in pushes it up to roughly
  +70% combined.

### Feeling your partner: cross-bleed

A trace of your partner's *own* gain swing (their sway+lean, not yours)
leaks into your register too — but only in proportion to how synced you
currently are:

`your_gain = 1 + your_gain_mod + (0.35 × synchrony) × their_gain_mod`

At zero synchrony you hear only yourself. At perfect synchrony, up to 35%
of their gain motion rides along inside your own register — deliberately
gain-only (never pan), so your register still sounds like it's *yours*,
just carrying a trace of "I can feel them" underneath. This is the
mechanism that makes falling into sync feel like something, before the
shared band even kicks in.

### The shared band: silence until you move as one

Harmonics 21–28 have no baseline volume of their own — their gain **is**
the synchrony value, directly:

- Synchrony 0.0 → silent.
- Synchrony 1.0 → full base gain.

There's no separate depth constant here on purpose: synchrony is already
0–1, so it maps straight through. This band is the most legible "reward"
signal in the whole system — it's silent when you're moving independently
and blooms in as your motion locks together, with no dashboard tweak
needed to hear it.

### The room breathing: pair energy → master pulse

Combined pair energy (average of both people's sway) also pulses the
**overall master gain** very slightly:

`pair_energy ÷ 3.0` (capped at 1.0) `× HYBRID_PAIR_ENERGY_GAIN_DEPTH (0.25)`
→ up to **+25%** overall loudness when both people are moving a lot,
easing back down as everyone settles. This is deliberately the smallest,
slowest-feeling change in the mapping — a "the room is alive" breath, not
a per-person cue.

## Quick reference: all the numbers in one place

| Constant | Value | Effect |
|---|---|---|
| `HYBRID_PAIR_ENERGY_REF` | 3.0 m/s² | Accel magnitude treated as "full-scale" (1.0) motion |
| `HYBRID_PAIR_SWAY_GAIN_DEPTH` | 0.5 | Your own sway → up to +50% gain in your register |
| `HYBRID_PAIR_LEAN_GAIN_DEPTH` | 0.2 | Your own forward lean → up to ±20% gain |
| `HYBRID_PAIR_YAW_PAN_RANGE` | 0.6 | Your own zeroed yaw → up to ±0.6 pan (×0–3 live slider) |
| `HYBRID_PAIR_CROSS_BLEED_DEPTH` | 0.35 | At full sync, up to 35% of partner's gain swing bleeds into yours |
| `HYBRID_PAIR_ENERGY_GAIN_DEPTH` | 0.25 | Combined pair energy → up to +25% master gain |
| (shared band gain) | = synchrony directly | 0 = silent, 1 = full |
| Motion window | 2.0 seconds | Rolling window behind sway energy & synchrony |

**Important**: every one of these depth/reference numbers is a first
guess, set before any real slow-dance test. The first live test (which
led to adding the pan-sensitivity slider) already showed the pan range
needed live adjustment — expect the gain-related constants to need the
same kind of tuning once more sessions happen. If a number here stops
matching `config.py`, trust the code and fix this doc, not the other way
around.

## Everything the two phones never affect

- No heart-rate input of any kind — Pair Mode structurally zeroes it even
  if HR data happens to be flowing from elsewhere.
- `roll` is measured but not mapped to anything yet.
- Harmonics 29–32 are permanently outside this system.
- Absolute compass direction is never used — only each phone's own
  zeroed yaw, which is why standing still after Recalibrate always
  reads 0° regardless of which way you're actually facing in the room.

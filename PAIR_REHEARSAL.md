# Two-phone rehearsal for the Living Lab

This movement-only rehearsal uses two phones. The experimental branch builds
on the Pair Mode source `ff9ed54598328e3fe1956f97f8c71620fa096d58`.

## What the audience can explore

Each person wears a phone on the torso or waist, with the screen visible and
awake during setup. A controls harmonics1–10; B controls11–20. Relative yaw
pans that person's register. Lean and acceleration magnitude vary its gain.
Correlation between changing acceleration magnitudes adds a shared high
register21–28 and the original partner gain contribution. There is no relation
score to attain: begin with separate voices, explore together, pause and return.

The rehearsal uses the existing Python additive `AudioEngine` and its original
Pair Mode mapping. A small authored tone bank makes the experiment audible
without a recorded input, MIDI controller, NaturalHarmony broadcast or SC
sample source. The bank has one sine per harmonic, a descending rolloff within
each register and a quieter shared layer. It is not the SuperCollider spatial
sample-processing path, nor the current `harmonic-shaper` process.

Latido contributes a useful artistic direction: two bodily contributions can
change one sonic field, and phase relationships can change its geometric form.
This candidate preserves the phone instrument rather than silently turning its
IMU values into HarMoCAP posture features. A new phase scene or phone-to-Weaver
adapter remains a later explicit mapping decision. No ECG/EEG driver, generated
heartbeats, physical laser calibration or camera control is part of this runner.

## Changed behavior

- The phone preserves valid zero acceleration and waits for actual finite
  linear-acceleration/orientation events. Missing readings are not gravity or
  observed stillness. Motion and orientation permissions are both requested
  where the browser exposes them. Start takes a new relative reference.
- Sender frames have per-Start identity and increasing sequence. They are not
  sent until both sensor event types are recent. There is at most one in-flight
  post, with a one-second request deadline; acknowledgments are checked.
- The opt-in store expires a phone after one second without accepted frames.
  Getter/audio-callback freshness does not depend on another packet arriving.
  A stopped stream is closed, so a late packet cannot reopen it. A competing
  fresh stream cannot silently take the same role.
- Correlation uses a common10Hz receiver-time grid over at most two seconds,
  with six supported pairs required and no interpolation across gaps over350ms.
  Missing, insufficient and flat support remain distinct from observed zero
  correlation. It does not synchronize phone acquisition clocks or compensate
  unknown network delay. This is still magnitude-only motion correlation.
- Only fresh phone registers enter the audio snapshot. Their disappearance
  uses the existing500ms release ramp. Pause releases voices; Cut/Panic is an
  immediate master mute and invalidates the current phone streams.
- The operator page shows both connections, the shared-layer state and explicit
  output enable/pause/cut controls. All visible controls remain readable on a
  narrow screen. Screen lock/backgrounding ends sending; foreground return
  requires Start again rather than quietly reusing a stale reference.

These freshness semantics belong to `PairRehearsalStore`; the original store
and SC receive path were not migrated. The original bridge's updated phone page
is backward compatible, but its optional partner status/explicit-stop support
requires this new rehearsal receiver. Legacy receivers do not acquire the new
release contract merely by receiving the new phone page.

## Run on the Mac

Use the experimental branch with a Python 3.10+ environment, such as
`../harmonic-weaver/.venv`. The silent server uses only the standard library.
Audio uses NumPy and sounddevice/PortAudio. Check the output device on the
receiving computer before enabling sound.

```sh
# From digital-beacon; inspect receiving dependencies without opening audio.
../harmonic-weaver/.venv/bin/python -c 'import numpy, sounddevice; print(sounddevice.query_devices())'

# Start with a silent setup check, on an unused port.
../harmonic-weaver/.venv/bin/python -B scripts/pair_rehearsal.py --port 5052
```

Open `http://127.0.0.1:5052/pair` on the Mac. Phones need the same sender/receiver
origin over HTTPS; Mac localhost is not the phone's localhost. Reuse the selected
working HTTPS delivery route after checking it on the Mac. If the existing
cloudflared tool is available and selected, its historical route is:

```sh
cloudflared tunnel --url http://127.0.0.1:5052
```

Use the actual emitted HTTPS URL in both phones, with `?device=A` and
`?device=B`. A reverse proxy must preserve the browser Host/Origin relationship.
This prototype has no public multi-user authentication/session service; keep
the rehearsal URL in the selected group. No tunnel was started by preparation.

After stopping only the task-owned silent process with Ctrl-C, select audible
rehearsal explicitly (optionally add `--device` with the actual output name/ID):

```sh
../harmonic-weaver/.venv/bin/python -B scripts/pair_rehearsal.py --port 5052 --audio
```

Enable sound on `/pair`, starting at the low default level. The runner does not
open sound at all without `--audio`, and opens a muted/unarmed instrument with
it. It binds its own TCP port first and fails on conflict. It starts no OSC/MIDI
listeners, kills no existing process and changes no current lab configuration.
Do not use the historical `start.sh` for this candidate: it contains broad
pre-kill behavior and unrelated local audio assumptions.

## Rehearse before selecting it for the demonstration

1. Confirm real sensor permission and live readings on both phones, using the
   intended browser, mounting, network and HTTPS URL.
2. Listen to A alone, then B alone; recognize each register and its yaw/lean
   response. Set the listening level on the real output.
3. Explore repeated shared movement and distinct rhythms. Observe the shared
   layer without treating its correlation as affection, distance or intention.
4. Stop A, background B and disconnect the network. Verify actual sound releases
   and that restarting takes a new reference. Try Pause and Cut from the Mac.
5. Reconnect and play a short complete scene. If using a membrane/mirror/laser,
   calibrate it physically; synthetic audio checks do not establish its shape.

## Verification

The original Pair Mode source passed 29 of 31 hybrid tests,
with two contradictory legacy-default expectations. No production flag was
flipped. Configuration still selects Pair Mode=True in Python and both SC files;
comments and tests now say that explicitly. Legacy sound tests explicitly select
legacy mode, while a separate check protects the actual three-file Pair setting.
All31 hybrid cases now pass, alongside14 new profile/HTTP/audio cases.

The optional `tools/check_pair_phone_browser.py` verifies two virtual phone
contexts, both permission paths, no sends before events, legitimate zero,
partner state, reference reset, Stop/restart, missing-data expiry, orientation
denial and narrow/operator layout. It uses a silent test receiver and existing
Playwright development tooling; no physical phone/audio hardware is exercised.

The physical rehearsal above checks the phone permissions, sound and devices
that the automated tests simulate.

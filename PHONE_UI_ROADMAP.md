# Phone Sender Page — Visual Roadmap

The current sender page (`IMU_SENDER_HTML` in `hybrid_bridge.py`) is a
functional debug UI: black background, green monospace text, two plain
buttons, raw numbers. It did its job for the first two-phone test — this
roadmap is about making it feel like part of the instrument instead of a
diagnostics console, without touching the sensor/OSC pipeline underneath.

Everything below only changes what's inside `IMU_SENDER_HTML` (markup, CSS,
a little JS for animation). No changes to `/imu`'s payload shape, the OSC
protocol, `state.py`, or `audio_engine.py` — this is presentation-only.

## Constraints to design within

- **One-handed, mid-dance.** The wearer taps Start once, then the phone
  goes on their chest/waist. Nothing after that should need precise
  taps — screen may go dim/lock, be under a shirt, etc.
- **Outdoor/low-light legibility.** Sessions happen in a dim room; the
  current near-black background is intentional and should stay. High
  contrast, big hit targets.
- **No new dependencies.** `IMU_SENDER_HTML` is an inline Python triple-
  quoted string served with zero build step — keep it that way (inline
  `<style>`/`<script>`, no CDN fetch, no bundler). This also matches
  `aad4022`'s same-origin constraint (mixed-content blocking on phones).
- **Truth over decoration.** Anything animated on screen should reflect
  what's actually being measured (yaw/pitch/sway), the same principle
  already applied to the dashboard's `drawPairViz` — never show motion
  that isn't real.

## Phase 1 — Visual polish, no new information — DONE

Goal: same three screens (device picker → controls → live readout), but
looking designed instead of default browser chrome.

- [x] System font stack switched to `-apple-system`/"SF Pro Text" with
  sane fallbacks — no font files hosted.
- [x] Real button styling: rounded corners, pressed/active scale-down
  state, clearly greyed-out `:disabled` state.
- [x] Device A/B color-coded with the *same* hex values `drawPairViz` uses
  on the dashboard (`--color-a: #3498db`, `--color-b: #e67e22`) — carried
  through the picker buttons, the device label, and the readout numerals
  via a `--device-color` CSS var set on pick.
- [x] Raw `Y:12.3 P:-4.1 ...` text line replaced with a 6-cell labeled
  grid (Yaw/Pitch/Roll/Ax/Ay/Az), monospace numerals, small-caps labels.
- [x] Status line is now a colored dot + text (grey=idle, green=streaming,
  amber=warning, red=error), matching the dashboard's own status-dot
  convention.

Also fixed alongside Phase 1 (functional, not visual, but same file/PR):
**Screen Wake Lock.** The phone screen was locking after the OS's normal
inactivity timeout, which on iOS Safari fully suspends the tab — no more
`deviceorientation`/`devicemotion` events, no more `sendData()` interval —
so the phone silently stopped sending data mid-dance. `start()` now calls
`navigator.wakeLock.request('screen')` (requires the same secure/HTTPS
context the cloudflared tunnel already provides for the motion-permission
prompt), re-requests it on `visibilitychange` if the OS reclaimed it, and
releases it on `stop()`. If the Wake Lock API isn't available at all
(older iOS, or an insecure/non-tunneled connection), a visible amber
warning tells the wearer to disable auto-lock manually in Settings instead
of failing silently.

**Important ceiling, not a bug**: Wake Lock keeps the screen *on*, not
off — there is no web API that lets a page keep receiving sensor data
with the screen truly off/locked on iOS. The near-black UI already
minimizes visible glow and (on OLED iPhones) backlight draw, but the
phone will stay lit for the whole session. A manually pressed side button,
or Safari being swiped to the background, still stops data (Wake Lock
only defeats the automatic inactivity timeout) — worth pairing with
Guided Access (Settings → Accessibility → Guided Access) if a field test
shows accidental taps/backgrounding is a real problem, but don't build
that speculatively before confirming it's needed.

## Phase 2 — A live motion visualization on the phone itself — DONE

Goal: give the wearer (and anyone glancing at their phone before it goes
under a shirt) direct visual feedback that the sensors are alive and
moving correctly, mirroring `drawPairViz` but scoped to just this one
phone.

- [x] `#motionCanvas`: a needle rotates with zeroed yaw (0° = 12 o'clock,
  matching the dim tick mark drawn at the top of the ring), and a soft
  halo grows/brightens with local sway energy.
- [x] Sway energy is computed **locally in JS** with the same formula as
  `VoiceParameterStore.get_hybrid_sway_energy` — RMS of accel magnitude
  over a rolling window — rather than trusting a single instantaneous
  reading. `MOTION_WINDOW_MS` (2000) and `SWAY_REF` (3.0) mirror
  `state.py`'s `_HYBRID_MOTION_WINDOW_S` and `config.HYBRID_PAIR_ENERGY_REF`
  — if either constant is retuned server-side, update the matching JS
  constant too, or the phone's preview and the dashboard's pair canvas
  will visibly disagree.
- [x] Purely local rendering (`requestAnimationFrame` loop reading the
  page's own `yaw`/`ax,ay,az` state) — no new network calls, so it costs
  nothing extra on the `/imu` POST loop.
- [x] Recalibrating snaps the needle back to the top tick within one
  orientation-event tick, confirming the zero actually took instead of
  just trusting the status text.

## Phase 3 — Session framing (before/after the dance)

Goal: make the start and end of a session feel intentional, not just a
button tap.

- A short "ready" screen between device-picking and Start: a breathing
  animation (2-3s) while the phone confirms it's getting nonzero sensor
  events, so the wearer isn't guessing whether motion permission actually
  got granted before they start dancing.
- On Stop, a brief "session ended" state (elapsed time, maybe a summary
  stat like peak sway) instead of instantly reverting to the idle screen.
- Optional: a subtle ambient animation on the idle/controls screen (slow
  gradient drift) so the phone doesn't look "frozen" if the wearer glances
  at it while waiting for their partner to also connect.

## Phase 4 — Two-phone pairing awareness (stretch)

Goal: let each phone know its partner is connected, without adding new
server endpoints beyond what already exists.

- Poll `/hybrid/status` (already exists, returns both devices' `to_dict()`
  including `last_imu_age_s`) every couple seconds while on the controls
  screen, and show a small "Partner: connected / waiting..." line. Purely
  additive polling — doesn't change anything the audio engine reads.
- This is a stretch goal specifically because it adds a recurring fetch
  loop; only worth it if Phase 1-3 feel done and there's still appetite for
  more phone-side work before moving on to dashboard/engine work.

## What NOT to do here

- Don't add a settings/config UI to the phone page (pan sensitivity etc.
  already lives on the dashboard slider, on purpose — one phone shouldn't
  be able to silently change the whole mix while strapped to a moving
  body).
- Don't try to show the *other* phone's motion on this phone's screen —
  that's what the dashboard's pair canvas is for; duplicating it here adds
  a second real-time sync path for no benefit to the wearer, who can't
  usefully watch their own chest anyway.
- Don't reach for a JS framework/build step for this — it's one HTML
  string served by Flask; keep it that way until there's a concrete reason
  not to.

## Suggested order

Phase 1 is the highest value-per-effort (it's what makes the page feel
"done" in a screenshot/demo) and has zero risk to the working pipeline.
Phase 2 is next because it directly helps debugging in the field (a bad
sensor reading becomes visually obvious, not just a frozen number). Phase
3 and 4 are polish/stretch — do them if there's time before the next real
test, not before.

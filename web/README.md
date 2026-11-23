# Pair Mode in Home

The existing Home service checks the shared resource audience before dispatch.
Biblio keeps its own session and CSRF checks for catalogue writes. The wrapper
checks the separately selected Rostro account before dispatching its routes.
Configure the exact HTTPS origin in `PAIR_LAB_ORIGIN` and the permitted human
reference in `PAIR_LAB_HUMAN_REF` through the deployment environment. With
either setting absent, the wrapper returns 503 for Rostro. Pair and Biblio
continue through their existing service routes and access checks.

This browser renderer belongs to the earlier movement-only rehearsal on
`oliva/pair-mode-rehearsal-20261113`. For the later native phone routing through
Weaver and Shaper, see the [Phone Pair integration](https://github.com/AlterMundi/harmonic-weaver/blob/oliva/pair-mode-native-20261113/docs/laboratory/integrations/PHONE_PAIR.md).

## Rehearsal

Open the operator page on the computer that should sound, then open its A and
B links on two phones. Each device needs a Home session for an account in the
shared resource audience. Press **Start Sensors**
on each phone, allow motion and orientation, and hold the reference posture.
Keep phone pages visible and screens awake. Press **Activar sonido** on the
listening computer. Start at the default 6% level with a modest speaker level.

A controls harmonics 1–10, B controls 11–20, and positively correlated recent
movement introduces 21–28 and the partner-gain contribution. Compare each voice
alone, then move together, differently, and stop. This is an exploration of
movement and sound, not a physiological or emotional measurement. **Pausar**
silences this listening browser; **Cortar** also closes the shared sending
streams. To restart after a cut, press Stop and Start on the phones. A shared
room has one A and one B; another active phone cannot silently take a role.

The browser generates audio on the listening computer. Phone readings travel
over the selected same-origin HTTPS Home route to a small Node receiver. Network
delay therefore affects responsiveness; it is not a local acquisition clock.
No sensor samples are written to disk. The receiver retains at most two
64-frame histories over a two-second window and 64 closed-stream identifiers.
An idle phone expires after one second. A fresh stream must carry finite
readings, a bounded identifier and a monotonically increasing sequence.

The browser starts silent, requires a user gesture, and pauses on a failed
status read, backgrounding, changed server epoch/reset or stale reading.
Recovery requires explicitly enabling sound again. Web Audio schedules a short
finite gain lease on its audio clock; silence follows even if JavaScript stops
renewing it. Missing voices release over 500ms. The master starts at 0.06, is
bounded to 0.3 and feeds a compressor. The gain-lease pattern comes from the
MIT-licensed Weaver FaceSound implementation, attributed in NOTICE.txt.

The port uses the Python rehearsal's mapping and receiver-time resampling.
The committed reference fixture covers 42 states produced by the real Python
store and existing pan/gain function. Web Audio is a separate renderer, not
bit-identical PortAudio output. Physical phone permissions, real network latency,
Mac output and hearing must still be tried on the demonstration devices.

## Composition and validation

`tools/build_pair_home.py --base <exact-workshop.tar> --expected-base <sha256>
--out <new-directory>` verifies an immutable workshop inventory, preserves all
existing Biblio/Rostro files, extracts the selected phone sender and adds the
Pair route and wrapper. It keeps the existing health response and `web-v1`
state contract. The wrapper must be named `pair-workshop-entry.cjs`; its base
remains `workshop-entry.cjs`. A build does not publish or deploy anything.
Future workshop builders must preserve this additive namespace or explicitly
recompose it from the same source; do not rebuild from a stale Biblio-only base.

Use the Home package verifier and the managed service publisher with the
selected predecessor, rollback package and audience for the deployment.

Run `node --test tests/pair_web.test.mjs` for independent Python parity, bounded
long sessions, stream isolation and actual HTTP validation. The optional
`tools/check_pair_home_browser.py --base http://127.0.0.1:<test-port> --out <dir>`
exercises two real sender pages with virtual events, real Web Audio output,
lease expiry, explicit recovery, cut and responsive layouts. Use only a
task-owned local candidate, never virtual events in a participant's live room.
The browser tool needs the optional Playwright development package and Chromium.
`tools/export_pair_parity.py` regenerates the fixture intentionally; ordinary
checks must compare against the committed fixture rather than regenerating it.

Motion permissions require an explicit gesture and the selected secure origin;
see the [W3C orientation contract](https://www.w3.org/TR/orientation-event/).
The gain scheduler includes a fallback for browsers without
[`cancelAndHoldAtTime`](https://developer.mozilla.org/en-US/docs/Web/API/AudioParam/cancelAndHoldAtTime).

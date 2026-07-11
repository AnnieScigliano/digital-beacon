// beacon_simple.scd
// digital-beacon — 32-band spatializer (SIMPLE STEREO MODE)
//
// This version works WITHOUT ATK quarks (Ambisonic Toolkit).
// It uses simple stereo panning instead of binaural HRTF spatialization.
// Full 3D binaural mode requires: Quarks.install("atk-sc3")
//
// 32 BPF, one per natural harmonic of f1 (default 40 Hz).
// Band N center frequency = f1 * N for N = 1..32.
// Source: PlayBuf (looping WAV) or SoundIn(0), varispeed by \vsrate.
// Per-band: gain, azimuth (mapped to L/R pan), on/off.
//
// OSC (sclang :57120):
//   /beacon/vsource [rate]   - sample rate multiplier
//   /beacon/f1 [hz]          - update f1
//   /beacon/gain/N [1..32] [0..3]
//   /beacon/az/N [1..32] [-180..180]  (mapped to L/R pan)
//   /beacon/on/N [1..32] [0|1]
//   /beacon/master [0..3]
//   /beacon/panic            - all bands off
//
// Outbound (sclang broadcasts to :9001):
//   /beacon/f1 [hz]
//   /beacon/band/N/active [1..32] [0|1]
//
// Server: 2in/2out, 48000 Hz, block 512.

(
var nBands = 32;
var defaultF1 = 40.0;
var buf, synth, oscdefs;
var audioFilePath = "AUDIO_FILE".getenv ?? (Platform.resourceDir +/+ "sounds/harmonic_beacon_2026_05_13_session.wav");
var useFile = "BEACON_SOURCE".getenv == "file";
var defName = if (useFile) { \digital_beacon_file } { \digital_beacon_live };

// Pre-started scsynth on 57110
var server = Server(\digital_beacon, NetAddr("127.0.0.1", 57110));
Server.default = server;

server.options.numInputBusChannels = 2;
server.options.numOutputBusChannels = 2;
server.options.sampleRate = 48000;
server.options.memSize = 65536;
server.options.blockSize = 64;

"".postln;
"========================================".postln;
"  DIGITAL BEACON — 32 BPF 1:1 with f1·N  ".postln;
"  SIMPLE STEREO MODE (no ATK required)     ".postln;
"========================================".postln;
"SC: %  |  bands: %  |  source: %".format(Main.version, nBands,
  if (useFile) { "FILE (% — share with beacon-spatial)".format(audioFilePath.basename) } { "LIVE (SoundIn.ar(0))" }
).postln;
"".postln;

server.startAliveThread(0.4);
server.doWhenBooted({
  var cond = Condition.new;
  var applyHybridHr, applyHybridImu, applyHybridRegisters, applyMasterGain;
  // Hug/dance PAIR MODE (Phase 2) — OPT-IN, off by default. Must match
  // config.HYBRID_PAIR_MODE_ENABLED in digital_beacon/config.py: when false
  // (the default), IMU behaves exactly as it did before pair mode existed
  // (whole-field yaw rotation + HR gain/shape) — completely unaffected by
  // any of this. Flip to true (and restart sclang) only for the two-phone
  // movement-only test case; there's no live OSC toggle by design, same as
  // Python's config constant — this is a "which test case am I running"
  // choice, not a runtime performance control.
  var hybridPairModeEnabled = true;
  // Must match config.HYBRID_HARMONICS_A/B/SHARED and config.HYBRID_PAIR_*
  // in digital_beacon/config.py exactly, so this engine and the Python
  // Shaper behave the same way for the same motion, when pair mode is on.
  var hybridHarmonicsA = (1..10);
  var hybridHarmonicsB = (11..20);
  var hybridHarmonicsShared = (21..28);
  var hybridPairEnergyRef = 3.0;
  var hybridSwayGainDepth = 0.5;
  var hybridLeanGainDepth = 0.2;
  var hybridYawAzRange = 90.0;         // SC's own unit (az degrees); Python's
                                        // equivalent is HYBRID_PAIR_YAW_PAN_RANGE (pan units)
  var hybridPairEnergyGainDepth = 0.25;
  // Fraction of your partner's OWN gain modulation that bleeds into your own
  // register's gain, scaled by current synchrony — gain-only, never pan.
  // Must match config.HYBRID_PAIR_CROSS_BLEED_DEPTH.
  var hybridCrossBleedDepth = 0.35;
  // Latest known per-device / pair-derived values — each arrives on its own
  // OSC message at its own rate, so we cache and reapply together rather
  // than compute directly inside each OSCdef.
  var yawA = 0.0, pitchA = 0.0, yawB = 0.0, pitchB = 0.0;
  var swayA = 0.0, swayB = 0.0, synchrony = 0.0, pairEnergy = 0.0;
  var hrPulse = 0.0;
  // Live control knob (dashboard slider), not derived from motion — lets
  // pan sensitivity be dialed during a live test with no restart.
  var panSensitivity = 1.0;

  // Defaults: uniform gain, az array spreads bands around the head.
  var defaultGains = { |i| 0.8 } ! nBands;
  // Default azimuths: 32 angles spread around the head.
  var defaultAz = {
    var low = [-90, -45, 0, 45, 90, 135];
    var mid = [-60, -30, 0, 30, 60, 90];
    var high = [-180, -135, -90, 90, 135, 180, -45, 45];
    var top = [-90, 0, 90, -45, 45, -135, 135, 180, 0, -180, 45, -45];
    Array.fill(nBands, { |i|
      if (i < 6) { low[i] }
      { if (i < 12) { mid[i - 6] }
        { if (i < 20) { high[i - 12] }
          { top[(i - 20) % top.size] }
        }
      };
    });
  }.value;
  var defaultOns = { |i| 1.0 } ! nBands;
  var master0 = 0.9;

  var makeBeaconDef = { |name, srcFunc|
    SynthDef(name, {
      arg out = 0, bufnum = 0;

      var vsrate = \vsrate.kr(1.0).lag(0.05);
      var src = srcFunc.value(bufnum, vsrate);

      var f1 = \f1.kr(defaultF1).lag(0.05);

      var gains = Array.fill(nBands, { |i|
        ("gain" ++ (i+1)).asSymbol.kr(defaultGains[i]).lag(0.05)
      });
      var azs = Array.fill(nBands, { |i|
        ("az" ++ (i+1)).asSymbol.kr(defaultAz[i]).lag(0.05)
      });
      var ons = Array.fill(nBands, { |i|
        ("on" ++ (i+1)).asSymbol.kr(defaultOns[i])
      });
      var master = \master.kr(master0);
      var q32 = \q32.kr(1.0).lag(0.05);

      // Build 31 BPFs + 1 HPF (last band)
      var bpfs = Array.fill(nBands, { |i|
        var freq = (f1 * (i+1)).clip(20.0, 18000.0);
        var sig;
        if (i == (nBands - 1)) {
          var cutoff = (freq / q32).clip(20.0, 18000.0);
          sig = HPF.ar(src, cutoff);
        } {
          sig = BPF.ar(src, freq, 0.5);
        };
        sig * gains[i] * ons[i]
      });

      // SIMPLE STEREO: map azimuth to L/R pan instead of ATK binaural
      // az = -90 = full left, az = 0 = center, az = 90 = full right
      // az beyond ±90 wraps to the back (still panned L/R)
      var panned = bpfs.collect { |bpfSig, i|
        var az = azs[i];
        // Normalize az to pan position: -90..90 → -1..1
        // For az > 90 or < -90 (back), we still pan but reduce volume slightly
        var isBack = (az.abs > 90);
        var pan = (az / 90).clip(-1.0, 1.0);
        // az is a UGen (kr control), so isBack is too — sclang's `if` needs a
        // real Boolean at compile time and would raise "Non Boolean in test"
        // here. Select.kr picks array[isBack] at audio-graph run time instead.
        var backAtten = Select.kr(isBack, [1.0, 0.7]);
        Pan2.ar(bpfSig, pan, backAtten)
      };

      var stereoMix = Mix(panned);
      var nActive = 32;
      var wet = stereoMix / nActive.sqrt;

      // Soft limiter
      wet = tanh(wet * 1.2) * 0.9;

      Out.ar(out, wet * master);
    }).add;
  };

  "Server booted (2in/2out @ 48000Hz).".postln;
  server.freeAll;

  if (useFile) {
    "Loading buffer: % ...".format(audioFilePath.basename).postln;
    buf = Buffer.read(server, audioFilePath, action: {
      "  Buffer ready: bufnum=%, % frames, %s".format(
        buf.bufnum, buf.numFrames, buf.duration.round(0.01)
      ).postln;
      cond.unhang;
    });
    cond.hang;
  };

  "Defining SynthDef \\% ...".format(defName).postln;
  if (useFile) {
    makeBeaconDef.value(defName, { |bufnum, vsrate|
      PlayBuf.ar(1, bufnum, BufRateScale.kr(bufnum) * vsrate, loop: 1)
    });
  } {
    makeBeaconDef.value(defName, { |bufnum, vsrate|
      SoundIn.ar(0)
    });
  };
  server.sync;
  "SynthDef \\% added.".format(defName).postln;

  // Instantiate
  if (useFile) {
    synth = Synth(defName, [\bufnum, buf.bufnum]);
  } {
    synth = Synth(defName);
  };
  "Synth active: nodeID=%  (headphones recommended)".format(synth.nodeID).postln;

  // ─── OSCdefs ─────────────────────────────────────────────────────────────

  // 32x gain
  nBands.do { |i|
    var n = i + 1;
    oscdefs = oscdefs.add(OSCdef(
      ("db_gain_" ++ n).asSymbol,
      { |msg| synth.set(("gain" ++ n).asSymbol, msg[1].asFloat) },
      "/beacon/gain/" ++ n
    ));
    // 32x az
    oscdefs = oscdefs.add(OSCdef(
      ("db_az_" ++ n).asSymbol,
      { |msg| synth.set(("az" ++ n).asSymbol, msg[1].asFloat) },
      "/beacon/az/" ++ n
    ));
    // 32x on/off
    oscdefs = oscdefs.add(OSCdef(
      ("db_on_" ++ n).asSymbol,
      { |msg| synth.set(("on" ++ n).asSymbol, msg[1].asInteger) },
      "/beacon/on/" ++ n
    ));
  };

  // Master gain
  oscdefs = oscdefs.add(OSCdef(\db_master, { |msg|
    synth.set(\master, msg[1].asFloat);
  }, "/beacon/master"));

  // Panic
  oscdefs = oscdefs.add(OSCdef(\db_panic, {
    nBands.do { |i| synth.set(("on" ++ (i+1)).asSymbol, 0) };
    "  [PANIC] All bands off.".postln;
  }, "/beacon/panic"));

  // f1 retune
  oscdefs = oscdefs.add(OSCdef(\db_f1, { |msg|
    var newF1 = msg[1].asFloat;
    synth.set(\f1, newF1);
    "  [F1] % Hz".format(newF1).postln;
  }, "/beacon/f1"));

  // Varispeed (only for file source)
  oscdefs = oscdefs.add(OSCdef(\db_vsource, { |msg|
    var rate = msg[1].asFloat;
    synth.set(\vsrate, rate);
    "  [VSOURCE] rate=%".format(rate).postln;
  }, "/beacon/vsource"));

  // ─── Outbound broadcast (to Shaper on :9001) ─────────────────────────────
  // Send current f1 and band states so the Shaper can sync its analysis.
  // We send on boot and on any /beacon/f1 change.
  oscdefs = oscdefs.add(OSCdef(\db_f1_broadcast, { |msg|
    var f1 = msg[1].asFloat;
    NetAddr("127.0.0.1", 9001).sendMsg("/beacon/f1", f1);
    "  [BROADCAST] f1=% Hz → :9001".format(f1).postln;
  }, "/beacon/f1"));

  // Also broadcast band on/off states so the Shaper knows which are active
  nBands.do { |i|
    var n = i + 1;
    oscdefs = oscdefs.add(OSCdef(
      ("db_on_broadcast_" ++ n).asSymbol,
      { |msg|
        var state = msg[1].asInteger;
        NetAddr("127.0.0.1", 9001).sendMsg("/beacon/band/" ++ n ++ "/active", state);
      },
      "/beacon/on/" ++ n
    ));
  };

  // ─── Hybrid Sensor Mode OSCdefs (Phase 2: per-person pair mapping) ───────
  // Splits the 32 harmonics into per-person registers plus a shared
  // synchrony-gated "bloom" band — mirrors digital_beacon/audio_engine.py's
  // mapping exactly (same harmonic ranges, same HYBRID_PAIR_* depths) so the
  // Shaper and this SC engine respond the same way to the same motion.
  // Device A/B's own yaw/pitch arrive via /hybrid/imu[/B]; the sway-energy/
  // synchrony/pair-energy values need a rolling buffer no single OSC message
  // carries, so they're computed in Python (digital_beacon/state.py) and
  // pushed here via /hybrid/derived (see digital_beacon/api.py's _on_change).

  // Recombines HR pulse + pair-energy pulse into master gain — both target
  // the same scalar, so they're composed here rather than each calling
  // synth.set(\master, ...) directly (which would just have whichever
  // OSCdef fires last silently overwrite the other's effect).
  applyMasterGain = {
    var pairEnergyNorm = (pairEnergy / hybridPairEnergyRef).clip(0.0, 1.0);
    var pairEnergyPulse = pairEnergyNorm * hybridPairEnergyGainDepth;
    synth.set(\master, (master0 * (1.0 + hrPulse) * (1.0 + pairEnergyPulse)).clip(0.0, 3.0));
  };

  // Reapplies the full per-register az/gain from whatever's currently
  // cached (yawA/pitchA/swayA/..., synchrony) — called after ANY of the
  // underlying OSC messages updates one piece of it. No-op unless pair
  // mode is on (harmless if /hybrid/derived arrives regardless of mode).
  applyHybridRegisters = {
    if (hybridPairModeEnabled) {
      var yawNormA, pitchNormA, swayNormA, panModA, gainModA;
      var yawNormB, pitchNormB, swayNormB, panModB, gainModB;
      var crossBleed, gainMultA, gainMultB;

      yawNormA = (yawA / 180.0).clip(-1.0, 1.0);
      pitchNormA = (pitchA / 90.0).clip(-1.0, 1.0);
      swayNormA = (swayA / hybridPairEnergyRef).clip(0.0, 1.0);
      panModA = yawNormA * hybridYawAzRange * panSensitivity;
      gainModA = (swayNormA * hybridSwayGainDepth) + (pitchNormA * hybridLeanGainDepth);

      yawNormB = (yawB / 180.0).clip(-1.0, 1.0);
      pitchNormB = (pitchB / 90.0).clip(-1.0, 1.0);
      swayNormB = (swayB / hybridPairEnergyRef).clip(0.0, 1.0);
      panModB = yawNormB * hybridYawAzRange * panSensitivity;
      gainModB = (swayNormB * hybridSwayGainDepth) + (pitchNormB * hybridLeanGainDepth);

      // Partner cross-bleed: a synchrony-scaled trace of the OTHER
      // person's own gain modulation, gain-only (pan stays purely
      // self-driven so each register's stereo position stays legible).
      crossBleed = hybridCrossBleedDepth * synchrony;
      gainMultA = 1.0 + gainModA + (crossBleed * gainModB);
      gainMultB = 1.0 + gainModB + (crossBleed * gainModA);

      hybridHarmonicsA.do { |n|
        var baseAz = defaultAz[n - 1];
        var baseGain = defaultGains[n - 1];
        synth.set(("az" ++ n).asSymbol, (baseAz + panModA).wrap(-180, 180));
        synth.set(("gain" ++ n).asSymbol, (baseGain * gainMultA).clip(0.0, 3.0));
      };
      hybridHarmonicsB.do { |n|
        var baseAz = defaultAz[n - 1];
        var baseGain = defaultGains[n - 1];
        synth.set(("az" ++ n).asSymbol, (baseAz + panModB).wrap(-180, 180));
        synth.set(("gain" ++ n).asSymbol, (baseGain * gainMultB).clip(0.0, 3.0));
      };
      hybridHarmonicsShared.do { |n|
        var baseGain = defaultGains[n - 1];
        // Gated by synchrony, not boosted from a baseline: silent until the
        // pair is moving together, then it blooms.
        synth.set(("gain" ++ n).asSymbol, (baseGain * synchrony).clip(0.0, 3.0));
      };

      applyMasterGain.value;
    };
  };

  // /hybrid/hr — pair mode ON: HR is intentionally excluded (movement-only
  // test case) — hrPulse forced to 0, no band gains touched (those are
  // fully owned by applyHybridRegisters). Pair mode OFF (default):
  // unchanged since before pair mode existed — HR alone drives master
  // pulse + upper-band (17-32) brightness shift.
  applyHybridHr = { |msg, tag|
    var bpm = msg[1].asFloat;
    var rr_ms = msg[2].asFloat;
    var confidence = msg[3].asFloat;
    var bpm_norm = ((bpm - 72) / 30).clip(-1.0, 1.0);
    var hr_shape = bpm_norm * 0.45;

    if (hybridPairModeEnabled) {
      hrPulse = 0.0;
      applyMasterGain.value;
    } {
      var hr_pulse = bpm_norm * 0.30;
      synth.set(\master, (master0 * (1.0 + hr_pulse)).clip(0.0, 3.0));
      nBands.do { |i|
        var n = i + 1;
        var current_gain = defaultGains[i];
        var band_shift = if (i > 15) { hr_shape } { 0 };
        synth.set(("gain" ++ n).asSymbol, (current_gain + band_shift).clip(0.0, 3.0));
      };
    };

    "  [HYBRID HR %] BPM=%  RR=%ms  conf=%  pairMode=%".format(
      tag, bpm.round(0.1), rr_ms.round(0.1), confidence.round(0.01), hybridPairModeEnabled
    ).postln;
  };
  oscdefs = oscdefs.add(OSCdef(\hybrid_hr_a, { |msg| applyHybridHr.value(msg, "A") }, "/hybrid/hr"));
  oscdefs = oscdefs.add(OSCdef(\hybrid_hr_b, { |msg| applyHybridHr.value(msg, "B") }, "/hybrid/hr/B"));

  // /hybrid/imu — pair mode ON: yaw/pitch update this device's cached
  // values, then reapply the full per-register mapping (accel's role in
  // pair mode is the rolling-window pair energy from /hybrid/derived, not
  // an instantaneous per-message reading). Pair mode OFF (default):
  // unchanged since before pair mode existed — yaw rotates the whole
  // field, pitch tilts front-band gain, accel boosts master gain directly.
  applyHybridImu = { |msg, tag|
    var yaw = msg[1].asFloat;
    var pitch = msg[2].asFloat;
    var roll = msg[3].asFloat;
    var ax = msg[4].asFloat;
    var ay = msg[5].asFloat;
    var az = msg[6].asFloat;

    if (hybridPairModeEnabled) {
      if (tag == "A") {
        yawA = yaw; pitchA = pitch;
      } {
        yawB = yaw; pitchB = pitch;
      };
      applyHybridRegisters.value;
    } {
      var yaw_offset = yaw.clip(-180, 180);
      var pitch_mod = (pitch / 90).clip(-1.0, 1.0) * 0.4;
      var accel_mag = (ax.squared + ay.squared + az.squared).sqrt;
      var accel_boost = (accel_mag / 20).clip(0, 1) * 0.35;

      nBands.do { |i|
        var n = i + 1;
        var base_az = defaultAz[i];
        var new_az = (base_az + yaw_offset).wrap(-180, 180);
        synth.set(("az" ++ n).asSymbol, new_az);
      };
      nBands.do { |i|
        var n = i + 1;
        var base_az = defaultAz[i];
        var is_front = (base_az.abs < 45);
        if (is_front) {
          var current_gain = defaultGains[i];
          synth.set(("gain" ++ n).asSymbol, (current_gain + pitch_mod).clip(0.0, 3.0));
        };
      };
      synth.set(\master, (master0 * (1.0 + accel_boost)).clip(0.0, 3.0));
    };

    "  [HYBRID IMU %] yaw=%°  pitch=%°  roll=%°  pairMode=%".format(
      tag, yaw.round(0.1), pitch.round(0.1), roll.round(0.1), hybridPairModeEnabled
    ).postln;
  };
  oscdefs = oscdefs.add(OSCdef(\hybrid_imu_a, { |msg| applyHybridImu.value(msg, "A") }, "/hybrid/imu"));
  oscdefs = oscdefs.add(OSCdef(\hybrid_imu_b, { |msg| applyHybridImu.value(msg, "B") }, "/hybrid/imu/B"));

  // /hybrid/derived — [sway_a, sway_b, synchrony, pair_energy], pushed by
  // digital_beacon/api.py on every store change.
  oscdefs = oscdefs.add(OSCdef(\hybrid_derived, { |msg|
    swayA = msg[1].asFloat;
    swayB = msg[2].asFloat;
    synchrony = msg[3].asFloat;
    pairEnergy = msg[4].asFloat;
    applyHybridRegisters.value;
  }, "/hybrid/derived"));

  // /hybrid/pan_sensitivity — [value], live control knob (dashboard slider),
  // pushed by digital_beacon/api.py's POST /api/hybrid/pan_sensitivity.
  oscdefs = oscdefs.add(OSCdef(\hybrid_pan_sensitivity, { |msg|
    panSensitivity = msg[1].asFloat;
    applyHybridRegisters.value;
    "  [HYBRID PAN SENSITIVITY] % (%pct)".format(panSensitivity, (panSensitivity * 100).round(1)).postln;
  }, "/hybrid/pan_sensitivity"));

  // /hybrid/mode — switch between HR-only, IMU-only, or Hybrid
  oscdefs = oscdefs.add(OSCdef(\hybrid_mode, { |msg|
    var mode = msg[1].asInteger;
    var mode_name = ["HR-ONLY", "IMU-ONLY", "HYBRID"][mode.clip(0, 2)];
    "  [HYBRID MODE] %".format(mode_name).postln;
  }, "/hybrid/mode"));

  // /hybrid/clock — temporal sync (broadcast by bridge every second)
  oscdefs = oscdefs.add(OSCdef(\hybrid_clock, { |msg|
    var timestamp = msg[1].asFloat;
    var source = msg[2].asString;
    // Clock received — can be used for time-based effects
    // Currently just logs for debugging
    // "  [HYBRID CLOCK] t=% from=%".format(timestamp, source).postln;
  }, "/hybrid/clock"));

  "  % OSCdefs registered.".format(oscdefs.size).postln;
  "  Listening on :57120".postln;
  "  Broadcasting to :9001".postln;
  "".postln;
  "  Commands:".postln;
  "    s.sendMsg(\"/beacon/f1\", 55);        // retune".postln;
  "    s.sendMsg(\"/beacon/gain/1\", 1.5);  // boost band 1".postln;
  "    s.sendMsg(\"/beacon/az/1\", -45);    // pan band 1 left".postln;
  "    s.sendMsg(\"/beacon/panic\");         // all off".postln;
  "    s.sendMsg(\"/hybrid/hr\", 85, 700, 0.95);  // simulate HR (device A)".postln;
  "    s.sendMsg(\"/hybrid/imu\", 15, -5, 0, 0.1, 0, 9.8);  // simulate IMU (device A)".postln;
  "    s.sendMsg(\"/hybrid/imu/B\", -20, 8, 0, 0.2, 0, 9.8);  // simulate IMU (device B)".postln;
  "    s.sendMsg(\"/hybrid/derived\", 1.5, 2.0, 0.8, 1.7);  // sway_a sway_b synchrony pair_energy".postln;
  "".postln;

  // ─── MIDI (optional) ───────────────────────────────────────────────────────
  // MIDI control is available if the Shaper sends MIDI to the IAC bus.
  // For now, we leave MIDI unmapped to avoid conflicts with the Shaper.
  // MIDIdef.noteOn({ |vel, nn| "MIDI noteOn % vel=%".format(nn, vel).postln });

});

// Keep sclang alive
loop { 1.0.wait };
)

// beacon.scd
// digital-beacon — 32-band binaural spatializer
//
// 32 BPF, one per natural harmonic of f1 (default 40 Hz).
// Band N center frequency = f1 * N for N = 1..32.
// Source: PlayBuf (looping WAV) or SoundIn(0), varispeed by \vsrate.
// Per-band: gain, azimuth, on/off (replaces wet/dry mix).
// FoaPanB → sum B-format → FoaDecode(Listen HRTF) → stereo out.
//
// OSC (sclang :57120):
//   /beacon/vsource [rate]   - sample rate multiplier (1.0 = native, 0.5 = -1 oct, 2.0 = +1 oct)
//   /beacon/f1 [hz]          - update f1 (band centers retune + broadcast vsrate if file source)
//   /beacon/gain/N [1..32] [0..3]
//   /beacon/az/N [1..32] [-180..180]
//   /beacon/on/N [1..32] [0|1]
//   /beacon/master [0..3]
//   /beacon/panic            - all bands off
//
// Outbound (sclang broadcasts to :9001):
//   /beacon/f1 [hz]
//   /beacon/band/N/active [1..32] [0|1]
//
// Server: 2in/2out, 48000 Hz, block 512 (HRTF kernel latency).
// ATK Listen kernels at ~/.local/share/ATK/kernels (subject 1002).

(
var nBands = 32;
var defaultF1 = 40.0;
var buf, decoder, synth, oscdefs;
var audioFilePath = "AUDIO_FILE".getenv ?? (Platform.resourceDir +/+ "sounds/harmonic_beacon_2026_05_13_session.wav");
var useFile = "BEACON_SOURCE".getenv == "file";
var defName = if (useFile) { \digital_beacon_file } { \digital_beacon_live };

// Pre-started scsynth on 57110 (from start.sh via pw-jack).
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
"  ATK FOA binaural (Listen HRTF)         ".postln;
"========================================".postln;
"SC: %  |  ATK kernels: %".format(Main.version, Atk.userKernelDir).postln;
"f1 default: % Hz  |  bands: %  |  source: %".format(defaultF1, nBands,
  if (useFile) { "FILE (% — share with beacon-spatial)".format(audioFilePath.basename) } { "LIVE (SoundIn.ar(0))" }
).postln;
"".postln;

server.startAliveThread(0.4);
server.doWhenBooted({
  var cond = Condition.new;

  // Defaults: uniform gain, az array spreads bands around the head.
  // Bands 1-6 close in, 7-12 mid, 13-20 wider, 21-32 back to center.
  var defaultGains = { |i| 0.8 } ! nBands;
  // Default azimuths: 32 angles spread around the head.
  // Build it with a small table per band.
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
  var defaultOns = { |i| 1.0 } ! nBands;   // all on by default
  var master0 = 0.9;

  var makeBeaconDef = { |name, srcFunc|
    SynthDef(name, {
      arg out = 0, bufnum = 0;

      // Source (file or live) with varispeed via \vsrate
      var vsrate = \vsrate.kr(1.0).lag(0.05);
      var src = srcFunc.value(bufnum, vsrate);

      // 32 BPF (one per harmonic N=1..32)
      // bandFreqs are derived at SynthDef build time, but we re-read f1 dynamically
      // via 32 NamedControls. Per design rule: BPF center = f1 * N.
      // The NamedControl \f1 carries the current f1 (with lag).
      var f1 = \f1.kr(defaultF1).lag(0.05);

      // 32 gains + 32 az + 32 on (default 1)
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
      // Band 32 (index 31) is a High-Pass Filter, not a BPF. Its center
      // frequency is the cutoff. The \q32 control scales the cutoff: small
      // q32 = high cutoff (above the band), large q32 = low cutoff.
      // Default q32=1.0 means cutoff = f1*32 = 1280 Hz.
      var q32 = \q32.kr(1.0).lag(0.05);

      // Build 31 BPFs + 1 HPF (last band)
      var bpfs = Array.fill(nBands, { |i|
        var freq = (f1 * (i+1)).clip(20.0, 18000.0);
        var sig;
        if (i == (nBands - 1)) {
          // Band 32: HPF with cutoff at f1*32 (scaled by 1/q32)
          var cutoff = (freq / q32).clip(20.0, 18000.0);
          sig = HPF.ar(src, cutoff);
        } {
          sig = BPF.ar(src, freq, 0.5);  // Q=0.5 (1 octave bandwidth) per band
        };
        sig * gains[i] * ons[i]
      });

      // Per-band FoaPanB
      var foas = bpfs.collect { |bpfSig, i|
        var azRad = azs[i].degrad;
        FoaPanB.ar(bpfSig, azRad, 0)
      };
      var bfmt = Mix(foas);

      // Decode + master + soft limiter
      // Per-band normalization: divide the mix by sqrt(N active) so adding
      // more bands doesn't blow up. Then a tanh soft clip as the final stage.
      var nActive = 32;  // 32 bands total (we can be smarter later if needed)
      var wet = FoaDecode.ar(bfmt, decoder) / nActive.sqrt;
      // Soft limiter: tanh saturates at +/-1 with a smooth knee.
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

  "Loading FoaDecoderKernel.newListen(1002, 48000Hz, 512-sample)...".postln;
  decoder = FoaDecoderKernel.newListen(1002, server, 48000);
  2.0.wait;
  "  Decoder kernel ready (~11ms latency).".postln;

  "Defining SynthDef \\% ...".format(defName).postln;
  if (useFile) {
    makeBeaconDef.value(defName, { |bufnum, vsrate|
      PlayBuf.ar(1, bufnum, BufRateScale.kr(bufnum) * vsrate, loop: 1)
    });
  } {
    makeBeaconDef.value(defName, { |bufnum, vsrate|
      // Live input — vsrate not applicable to SoundIn (would need granular).
      // We still expose the slot so OSC schema is uniform; vsrate is ignored.
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
  "Synth active: nodeID=%  (headphones required for binaural)".format(synth.nodeID).postln;

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
      { |msg|
        var v = msg[1].asFloat.clip(0.0, 1.0);
        synth.set(("on" ++ n).asSymbol, v);
        // Broadcast activity (so external UI can show)
        server.sendMsg("/beacon/band/" ++ n ++ "/active", v);
      },
      "/beacon/on/" ++ n
    ));
  };

  // /beacon/vsource — varispeed rate multiplier
  oscdefs = oscdefs.add(OSCdef(
    \db_vsource,
    { |msg| synth.set(\vsrate, msg[1].asFloat.clip(0.1, 4.0)) },
    "/beacon/vsource"
  ));

  // /beacon/f1 — update f1 (band centers retune)
  oscdefs = oscdefs.add(OSCdef(
    \db_f1,
    { |msg|
      var hz = msg[1].asFloat.clip(20.0, 200.0);
      synth.set(\f1, hz);
      "  f1 -> % Hz".format(hz).postln;
    },
    "/beacon/f1"
  ));

  // /beacon/master
  oscdefs = oscdefs.add(OSCdef(\db_master,
    { |msg| synth.set(\master, msg[1].asFloat.clip(0.0, 3.0)) },
    "/beacon/master"
  ));

  // /beacon/panic — all bands off
  oscdefs = oscdefs.add(OSCdef(\db_panic, {
    nBands.do { |i|
      synth.set(("on" ++ (i+1)).asSymbol, 0.0);
    };
    "[PANIC] All bands off.".postln;
  }, "/beacon/panic"));

  // /beacon/record/start — start SC Server.record (captures the binaural
  // engine output directly, no PipeWire monitoring needed).
  oscdefs = oscdefs.add(OSCdef(\db_record_start, { |msg|
    var path = msg[1].asString;
    if(path == "", {
      path = "/home/nicolas/Music/beacon/sc_record_%.wav".format(Date.localtime.stamp);
    });
    server.record(path: path, numChannels: 2);
    "[RECORD START] %".format(path).postln;
  }, "/beacon/record/start"));

  // /beacon/record/stop — stop SC recording, free the recording node.
  oscdefs = oscdefs.add(OSCdef(\db_record_stop, { |msg|
    server.stopRecording;
    "[RECORD STOP]".postln;
  }, "/beacon/record/stop"));

  // /beacon/reset — restore defaults
  oscdefs = oscdefs.add(OSCdef(\db_reset, {
    synth.set(\vsrate, 1.0);
    synth.set(\f1, defaultF1);
    synth.set(\master, master0);
    nBands.do { |i|
      var n = i + 1;
      synth.set(("gain" ++ n).asSymbol, defaultGains[i]);
      synth.set(("az" ++ n).asSymbol, defaultAz[i]);
      synth.set(("on" ++ n).asSymbol, 1.0);
    };
    "[RESET] All bands restored.".postln;
  }, "/beacon/reset"));

  // ─── Hybrid sensor OSCdefs ────────────────────────────────────────────────
  // These receive on this sclang's own OSC port (57120) from either
  // hybrid_bridge.py's internal BLE/simulate capture or api.py's
  // /api/hybrid/* HTTP endpoints, and modulate the binaural spatializer
  // in real-time with body sensor data.

  // /hybrid/hr — Heart Rate → master gain pulse + band shape modulation
  // BPM > 72 → brighter (more shape), BPM < 72 → darker (less shape)
  oscdefs = oscdefs.add(OSCdef(\hybrid_hr, { |msg|
    var bpm = msg[1].asFloat;
    var rr_ms = msg[2].asFloat;
    var confidence = msg[3].asFloat;
    var bpm_norm = ((bpm - 72) / 30).clip(-1.0, 1.0);
    var hr_pulse = bpm_norm * 0.05;  // ±5% master gain
    var hr_shape = bpm_norm * 0.15;  // ±15% shape modulation
    var current_master, band_shift;

    // Apply master gain pulse
    current_master = master0;  // base
    synth.set(\master, (current_master * (1.0 + hr_pulse)).clip(0.0, 3.0));

    // Modulate shape of all active bands
    nBands.do { |i|
      var n = i + 1;
      var current_gain = defaultGains[i];
      // Subtle brightness shift: higher BPM → slightly higher gain on upper bands
      band_shift = if (i > 15) { hr_shape * 0.3 } { 0 };
      synth.set(("gain" ++ n).asSymbol, (current_gain + band_shift).clip(0.0, 3.0));
    };

    "  [HYBRID HR] BPM=%  RR=%ms  conf=%  pulse=%  shape=%".format(
      bpm.round(0.1), rr_ms.round(0.1), confidence.round(2), hr_pulse.round(3), hr_shape.round(3)
    ).postln;
  }, "/hybrid/hr"));

  // /hybrid/imu — IMU orientation → azimuth rotation + elevation tilt
  // Yaw → rotate all azimuths, Pitch → elevation (not directly in FoaPanB,
  // so we map to subtle gain modulation on front/back bands)
  oscdefs = oscdefs.add(OSCdef(\hybrid_imu, { |msg|
    var yaw = msg[1].asFloat;    // -180..180
    var pitch = msg[2].asFloat;  // -90..90
    var roll = msg[3].asFloat;   // -180..180
    var ax = msg[4].asFloat;
    var ay = msg[5].asFloat;
    var az = msg[6].asFloat;
    var yaw_offset, pitch_mod, accel_mag, accel_boost;

    // Yaw → rotate all azimuths by yaw offset
    yaw_offset = yaw.clip(-180, 180);
    nBands.do { |i|
      var n = i + 1;
      var base_az = defaultAz[i];
      var new_az = (base_az + yaw_offset).wrap(-180, 180);
      synth.set(("az" ++ n).asSymbol, new_az);
    };

    // Pitch → subtle elevation: tilt forward (pitch > 0) → boost front bands
    // We approximate by modulating gain on bands with az near 0°
    pitch_mod = (pitch / 90).clip(-1.0, 1.0) * 0.2;
    nBands.do { |i|
      var n = i + 1;
      var base_az = defaultAz[i];
      var is_front = (base_az.abs < 45);
      if (is_front) {
        var current_gain = defaultGains[i];
        synth.set(("gain" ++ n).asSymbol, (current_gain + pitch_mod).clip(0.0, 3.0));
      };
    };

    // Acceleration magnitude → subtle global brightness
    accel_mag = (ax.squared + ay.squared + az.squared).sqrt;
    accel_boost = (accel_mag / 20).clip(0, 1) * 0.1;
    synth.set(\master, (master0 * (1.0 + accel_boost)).clip(0.0, 3.0));

    "  [HYBRID IMU] yaw=%°  pitch=%°  roll=%°  accel=%  boost=%".format(
      yaw.round(0.1), pitch.round(0.1), roll.round(0.1), accel_mag.round(1), accel_boost.round(3)
    ).postln;
  }, "/hybrid/imu"));

  // /hybrid/mode — switch between HR-only, IMU-only, or Hybrid
  oscdefs = oscdefs.add(OSCdef(\hybrid_mode, { |msg|
    var mode = msg[1].asInteger;  // 0=HR, 1=IMU, 2=Hybrid
    var mode_name = ["HR-ONLY", "IMU-ONLY", "HYBRID"][mode.clip(0, 2)];
    "  [HYBRID MODE] %".format(mode_name).postln;
  }, "/hybrid/mode"));

  // /hybrid/sync/clock — temporal sync pulse (for visual or rhythmic feedback)
  oscdefs = oscdefs.add(OSCdef(\hybrid_sync, { |msg|
    var timestamp = msg[1].asFloat;
    var seq = msg[2].asInteger;
    var bpm_ref = msg[3].asFloat;
    // Optional: flash a brief visual cue or trigger a one-shot event
    // For now, just log at debug level
    // "  [HYBRID SYNC] ts=%  seq=%  bpm_ref=%".format(timestamp, seq, bpm_ref).postln;
  }, "/hybrid/sync/clock"));

  "OSCdefs registered: 32 gain + 32 az + 32 on + vsource + f1 + master + panic + reset + hybrid (102 total).".postln;

  // Cleanup
  ServerQuit.add({
    "ServerQuit: freeing digital-beacon resources...".postln;
    oscdefs.do { |d| d.free };
    if (synth.notNil) { synth.free };
    if (decoder.notNil) { decoder.free };
    if (buf.notNil) { buf.free };
  }, server);

  CmdPeriod.add({
    "Cmd-. : audio stopped.".postln;
  });

  "".postln;
  "========================================".postln;
  "  DIGITAL BEACON READY                  ".postln;
  "  OSC to sclang:57120 | scsynth:57110   ".postln;
  "========================================".postln;
  "Bands: %  (1..% → f1·N)".format(nBands, nBands).postln;
  "f1 default: % Hz  |  vsrate default: 1.0".format(defaultF1).postln;
  "Default az (deg): %".format(defaultAz).postln;
  "".postln;
  if (useFile) {
    "Signal: PlayBuf (vsrate) -> 32 BPF(f1·N) -> FoaPanB -> B-format -> FoaDecode(Listen)".postln;
  } {
    "Signal: SoundIn.ar(0) -> 32 BPF(f1·N) -> FoaPanB -> B-format -> FoaDecode(Listen)".postln;
  };
  "        -> stereo out  (headphones required)".postln;
  "".postln;
  "Open NaturalHarmony to drive /beacon/voice/* + /beacon/f1 broadcasts.".postln;
  "Run f1_bridge.py to forward f1 to vsrate (file source only).".postln;
  "".postln;
  "sclang waiting for OSC...".postln;
});
)

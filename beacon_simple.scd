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

  // ─── Hybrid Sensor Mode OSCdefs ──────────────────────────────────────────
  // These receive data from hybrid_bridge.py and modulate the beacon
  // in real-time with body sensor data.

  // /hybrid/hr — Heart Rate → master gain pulse + band shape modulation
  oscdefs = oscdefs.add(OSCdef(\hybrid_hr, { |msg|
    var bpm = msg[1].asFloat;
    var rr_ms = msg[2].asFloat;
    var confidence = msg[3].asFloat;
    var bpm_norm = ((bpm - 72) / 30).clip(-1.0, 1.0);
    var hr_pulse = bpm_norm * 0.05;
    var hr_shape = bpm_norm * 0.15;
    var current_master, band_shift;

    current_master = master0;
    synth.set(\master, (current_master * (1.0 + hr_pulse)).clip(0.0, 3.0));

    nBands.do { |i|
      var n = i + 1;
      var current_gain = defaultGains[i];
      band_shift = if (i > 15) { hr_shape * 0.3 } { 0 };
      synth.set(("gain" ++ n).asSymbol, (current_gain + band_shift).clip(0.0, 3.0));
    };

    "  [HYBRID HR] BPM=%  RR=%ms  conf=%  pulse=%  shape=%".format(
      bpm.round(0.1), rr_ms.round(0.1), confidence.round(2), hr_pulse.round(3), hr_shape.round(3)
    ).postln;
  }, "/hybrid/hr"));

  // /hybrid/imu — IMU orientation → azimuth rotation + elevation tilt
  oscdefs = oscdefs.add(OSCdef(\hybrid_imu, { |msg|
    var yaw = msg[1].asFloat;
    var pitch = msg[2].asFloat;
    var roll = msg[3].asFloat;
    var ax = msg[4].asFloat;
    var ay = msg[5].asFloat;
    var az = msg[6].asFloat;
    var yaw_offset, pitch_mod, accel_mag, accel_boost;

    yaw_offset = yaw.clip(-180, 180);
    nBands.do { |i|
      var n = i + 1;
      var base_az = defaultAz[i];
      var new_az = (base_az + yaw_offset).wrap(-180, 180);
      synth.set(("az" ++ n).asSymbol, new_az);
    };

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

    accel_mag = (ax.squared + ay.squared + az.squared).sqrt;
    accel_boost = (accel_mag / 20).clip(0, 1) * 0.1;
    synth.set(\master, (master0 * (1.0 + accel_boost)).clip(0.0, 3.0));

    "  [HYBRID IMU] yaw=%°  pitch=%°  roll=%°  accel=%  boost=%".format(
      yaw.round(0.1), pitch.round(0.1), roll.round(0.1), accel_mag.round(1), accel_boost.round(3)
    ).postln;
  }, "/hybrid/imu"));

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
  "    s.sendMsg(\"/hybrid/hr\", 85, 700, 0.95);  // simulate HR".postln;
  "    s.sendMsg(\"/hybrid/imu\", 15, -5, 0, 0.1, 0, 9.8);  // simulate IMU".postln;
  "".postln;

  // ─── MIDI (optional) ───────────────────────────────────────────────────────
  // MIDI control is available if the Shaper sends MIDI to the IAC bus.
  // For now, we leave MIDI unmapped to avoid conflicts with the Shaper.
  // MIDIdef.noteOn({ |vel, nn| "MIDI noteOn % vel=%".format(nn, vel).postln });

});

// Keep sclang alive
loop { 1.0.wait };
)

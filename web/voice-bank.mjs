// Gain-lease pattern adapted from Harmonic Weaver FaceSound (MIT),
// src/face/sound.ts at bf1fe9ee. Extended here for 28 panned sine voices.
// The browser renderer is distinct from PortAudio; bit-identical output is not claimed.
export function hold(parameter,now) {
  if(parameter.cancelAndHoldAtTime)parameter.cancelAndHoldAtTime(now);
  else {parameter.cancelScheduledValues(now);parameter.setValueAtTime(parameter.value,now);}
}
export function leaseGain(parameter,target,now) {
  hold(parameter,now);parameter.linearRampToValueAtTime(target,now+.04);
  parameter.setValueAtTime(target,now+.28);parameter.linearRampToValueAtTime(0,now+.42);
}
export class VoiceBank {
  constructor() {
    const Context=globalThis.AudioContext||globalThis.webkitAudioContext;
    if(!Context)throw Error('Este navegador no tiene una salida de audio compatible.');
    this.context=new Context({latencyHint:'interactive'});this.master=this.context.createGain();this.master.gain.value=.06;
    this.compressor=this.context.createDynamicsCompressor();this.compressor.threshold.value=-15;this.compressor.ratio.value=8;
    this.master.connect(this.compressor);this.compressor.connect(this.context.destination);this.enabled=false;this.volume=.06;
    this.nodes=Array.from({length:28},(_,i)=>{const osc=this.context.createOscillator(),gain=this.context.createGain(),pan=this.context.createStereoPanner();osc.frequency.value=40.4*(i+1);gain.gain.value=0;osc.connect(gain);gain.connect(pan);pan.connect(this.master);osc.start();return {osc,gain,pan,last:0};});
  }
  async start(){await this.context.resume();if(this.context.state!=='running')throw Error('Tocá Activar sonido otra vez para habilitarlo.');this.enabled=true;this.setVolume(this.volume);}
  setVolume(value){this.volume=Math.max(0,Math.min(.3,value));this.master.gain.setTargetAtTime(this.volume,this.context.currentTime,.025);}
  update(voices){const now=this.context.currentTime;this.nodes.forEach((node,i)=>{const v=voices[i],target=this.enabled&&Number.isFinite(v?.gain)?Math.max(0,Math.min(1,v.gain)):0;node.pan.pan.setTargetAtTime(Math.max(-1,Math.min(1,v?.pan??0)),now,.04);if(target>0)leaseGain(node.gain.gain,target,now);else if(node.last>0){hold(node.gain.gain,now);node.gain.gain.linearRampToValueAtTime(0,now+.5);}node.last=target;});}
  mute(immediate=false){this.enabled=false;const now=this.context.currentTime;this.nodes.forEach(node=>{hold(node.gain.gain,now);if(immediate)node.gain.gain.setValueAtTime(0,now);else node.gain.gain.linearRampToValueAtTime(0,now+.04);node.last=0;});}
  async close(){this.mute(true);this.nodes.forEach(({osc})=>osc.stop());await this.context.close();}
}

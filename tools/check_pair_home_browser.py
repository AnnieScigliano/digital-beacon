"""Qualify the composed Home candidate with isolated virtual phones and Web Audio.

Requires the optional existing Playwright development package. Use a task-owned
loopback receiver, not a participant's active shared room. No real sensors open.
"""
import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

p=argparse.ArgumentParser(description=__doc__);p.add_argument('--base',default='http://127.0.0.1:19868');p.add_argument('--out',type=Path,required=True)
a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)
prefix='/taller-oliva/pair/';checks=[];errors=[]
init="""(() => {window.__permissions=[];for(const name of ['DeviceMotionEvent','DeviceOrientationEvent']){const cls=window[name]||class extends Event{};cls.requestPermission=async()=>{window.__permissions.push(name);return 'granted';};window[name]=cls;}Object.defineProperty(navigator,'wakeLock',{value:{request:async()=>({addEventListener(){},release:async()=>{}})}});})()"""
pump="""() => {window.__pump=setInterval(()=>{const o=new Event('deviceorientation');Object.assign(o,{alpha:20,beta:10,gamma:0});window.dispatchEvent(o);const m=new Event('devicemotion');Object.assign(m,{acceleration:{x:0,y:0,z:2+Math.sin(Date.now()/1000*4)}});window.dispatchEvent(m);},50);}"""
audio_init="""(() => {const C=window.AudioContext;window.__contexts=[];window.__outputs=[];window.AudioContext=class extends C{constructor(...args){super(...args);window.__contexts.push(this);}createDynamicsCompressor(){const c=super.createDynamicsCompressor();window.__outputs.push(c);return c;}};})()"""
with sync_playwright() as p:
 b=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
 try:
  contexts=[];phones=[]
  for d in ['A','B']:
   c=b.new_context(viewport={'width':390,'height':844});contexts.append(c);page=c.new_page();page.on('pageerror',lambda e:errors.append(str(e)));page.add_init_script(init);page.goto(a.base+prefix+'phone?device='+d);page.get_by_role('button',name='Start Sensors',exact=True).click();page.evaluate(pump);phones.append(page)
  phones[0].get_by_text('Phone A: receiving · Phone B: receiving',exact=True).wait_for()
  checks.append('Two real sender pages post virtual events through the selected same-origin Home routes')
  c=b.new_context(viewport={'width':1280,'height':600});contexts.append(c);op=c.new_page();op.on('pageerror',lambda e:errors.append(str(e)));op.add_init_script(audio_init);op.goto(a.base+prefix)
  op.locator('#a').get_by_text('Señal recibida',exact=True).wait_for();op.locator('#b').get_by_text('Señal recibida',exact=True).wait_for()
  assert op.evaluate('window.__contexts.length')==0
  for name in ['listen','pause','cut']:
   box=op.locator('#'+name).bounding_box();assert box and box['y']+box['height']<=600,(name,box)
  op.get_by_role('button',name='Activar sonido',exact=True).click()
  rms="""async()=>{const c=window.__contexts[0],out=window.__outputs[0],an=c.createAnalyser();out.connect(an);await new Promise(r=>setTimeout(r,450));const samples=new Float32Array(an.fftSize);an.getFloatTimeDomainData(samples);out.disconnect(an);return Math.sqrt(samples.reduce((s,v)=>s+v*v,0)/samples.length);}"""
  sound=op.evaluate(rms);assert .0001<sound<.2,sound
  checks.append('No autoplay; explicit listening creates bounded nonzero real browser audio')
  op.screenshot(path=str(a.out/'pair-home-desktop.png'),full_page=True)
  # Schedule the exact production lease with and without cancelAndHoldAtTime.
  lease=op.evaluate("""async()=>{const {leaseGain}=await import('./voice-bank.mjs');const result=[];for(const fallback of [false,true]){const c=new OfflineAudioContext(1,48000,48000),osc=c.createOscillator(),g=c.createGain();g.gain.value=0;if(fallback)Object.defineProperty(g.gain,'cancelAndHoldAtTime',{value:undefined});osc.connect(g);g.connect(c.destination);osc.start();leaseGain(g.gain,.2,0);const x=(await c.startRendering()).getChannelData(0);const rms=(a,b)=>Math.sqrt(x.slice(a*48000,b*48000).reduce((s,v)=>s+v*v,0)/((b-a)*48000));result.push({steady:rms(.1,.2),released:rms(.7,.9)});}return result;}""")
  assert all(r['steady']>.1 and r['released']<1e-7 for r in lease),lease
  checks.append('Native audio-clock leases release to silence, including the compatibility fallback')
  op.route('**/api/status',lambda route:route.fulfill(status=401,json={'error':'expired'}))
  op.get_by_text('Conexión interrumpida · sonido pausado.',exact=True).wait_for();assert op.evaluate(rms)<1e-7
  op.unroute('**/api/status');op.get_by_text('Conexión recuperada. Activá el sonido para escuchar.',exact=True).wait_for();assert op.evaluate(rms)<1e-7
  checks.append('Session/read failure pauses sound and recovery never resumes it automatically')
  op.get_by_role('button',name='Activar sonido',exact=True).click();assert op.evaluate(rms)>.0001
  op.get_by_role('button',name='Pausar',exact=True).click();assert op.evaluate(rms)<1e-7
  checks.append('Local pause and explicit resume preserve the live sensor feeds')
  for width in [320,390,768,1024,1440]:
   op.set_viewport_size({'width':width,'height':800});assert op.evaluate('document.documentElement.scrollWidth<=innerWidth'),width
  op.set_viewport_size({'width':390,'height':844});op.screenshot(path=str(a.out/'pair-home-mobile.png'),full_page=True)
  phones[0].screenshot(path=str(a.out/'pair-home-phone-A.png'),full_page=True)
  checks.append('Readable operator and phone views reflow from 320px; primary controls fit 1280x600')
  op.get_by_role('button',name='Cortar el ensayo',exact=True).click()
  op.get_by_text('El ensayo se cerró. En los teléfonos, tocá Stop y Start para volver.',exact=True).wait_for()
  state=c.request.get(a.base+prefix+'api/status').json();assert not any(v['online'] for v in state['devices'].values())
  for page in phones:page.get_by_role('button',name='Stop',exact=True).click()
  assert errors==[],errors
  checks.append('Cut closes the shared streams; virtual sensor state is cleared after qualification')
  result={'status':'passed','checks':checks,'page_errors':errors,'initial_audio_rms':sound,'leases':lease,'scope':'Isolated composed candidate, virtual phone events, real Web Audio; no human session or physical device/hearing.'}
  (a.out/'browser-checks.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
 finally:b.close()

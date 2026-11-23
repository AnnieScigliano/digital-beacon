"""Browser QA with virtual motion events, no phone sensors or audio device.

Requires a silent rehearsal receiver and the optional Playwright development
package. Pass --base and --out; runtime use does not depend on Playwright.
"""
import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--base',default='http://127.0.0.1:19867')
parser.add_argument('--out',type=Path,required=True)
args=parser.parse_args();args.out.mkdir(parents=True,exist_ok=True)
checks=[];errors=[]
init="""() => {
  window.__permissions=[];
  for (const name of ['DeviceMotionEvent','DeviceOrientationEvent']) {
    const cls=window[name] || class extends Event {};
    cls.requestPermission=async()=>{window.__permissions.push(name);return 'granted';};
    window[name]=cls;
  }
  Object.defineProperty(navigator,'wakeLock',{value:{request:async()=>({addEventListener(){},release:async()=>{}})}});
}"""
pump="""() => {
  window.__yaw=20;window.__z=0;
  window.__pump=setInterval(()=>{
    const o=new Event('deviceorientation');Object.assign(o,{alpha:window.__yaw,beta:10,gamma:0});window.dispatchEvent(o);
    const m=new Event('devicemotion');Object.assign(m,{acceleration:{x:0,y:0,z:window.__z}});window.dispatchEvent(m);
  },50);
}"""
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
    try:
        contexts=[browser.new_context(viewport={'width':390,'height':844}) for _ in range(2)]
        pages=[];sent=[]
        for context,device in zip(contexts,['A','B']):
            page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
            page.add_init_script("("+init+")()");page.goto(args.base+'/?device='+device)
            page.on('request',lambda r:sent.append(r.post_data_json) if r.method=='POST' and r.url.endswith('/imu') else None)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.get_by_role('button',name='Start Sensors',exact=True).click()
            pages.append(page)
        pages[0].wait_for_timeout(250)
        assert not sent,'No synthetic defaults may be sent before sensor events'
        checks.append('No IMU posts before actual event observations; both permissions requested')
        for page in pages:
            assert page.evaluate('window.__permissions')==['DeviceMotionEvent','DeviceOrientationEvent']
            page.evaluate(pump)
        pages[0].get_by_text('Phone A: receiving · Phone B: receiving',exact=True).wait_for()
        pages[0].wait_for_timeout(600)
        assert sent and all(frame['az']==0 for frame in sent)
        assert pages[0].locator('#valAz').inner_text()=='0.00'
        checks.append('Two independent browser phones report valid zero and real partner state')
        pages[0].evaluate('window.__yaw=55')
        pages[0].wait_for_function("document.getElementById('valYaw').textContent==='35.0°'")
        pages[0].get_by_role('button',name='Recalibrate (face partner, stand still)',exact=True).click()
        pages[0].wait_for_function("document.getElementById('valYaw').textContent==='0.0°'")
        checks.append('Explicit reference reset uses the next orientation observation')
        pages[0].screenshot(path=str(args.out/'phone-A.png'),full_page=True)
        for page in pages:page.get_by_role('button',name='Stop',exact=True).click()
        pages[0].wait_for_timeout(300)
        status=contexts[0].request.get(args.base+'/pair/status').json()
        assert not any(row['online'] for row in status['devices'].values())
        count=len(sent);pages[0].wait_for_timeout(300);assert len(sent)==count
        checks.append('Stop closes both roles and sends no further frames')
        # A returning page must take a new reference, not retain the prior yaw.
        page=pages[0];page.get_by_role('button',name='Start Sensors',exact=True).click()
        page.wait_for_function("document.getElementById('valYaw').textContent==='0.0°'")
        page.evaluate('clearInterval(window.__pump);handleMotion({acceleration:null})')
        page.wait_for_timeout(1400)
        assert not contexts[0].request.get(args.base+'/pair/status').json()['devices']['A']['online']
        page.get_by_role('button',name='Stop',exact=True).click()
        checks.append('Restart recalibrates; missing motion expires instead of becoming zero/gravity')
        operator=browser.new_page(viewport={'width':1440,'height':1000})
        operator.on('pageerror',lambda e:errors.append(str(e)))
        operator.goto(args.base+'/pair');operator.get_by_text('Ensayo silencioso: este proceso no abre una salida de audio.',exact=True).wait_for()
        assert operator.get_by_role('button',name='Activar sonido',exact=True).is_disabled()
        operator.screenshot(path=str(args.out/'operator-desktop.png'),full_page=True)
        operator.set_viewport_size({'width':320,'height':800})
        assert operator.evaluate('document.documentElement.scrollWidth<=innerWidth')
        operator.screenshot(path=str(args.out/'operator-mobile.png'),full_page=True)
        checks.append('Silent mode is stated honestly; operator reflows at 320px')
        denied=browser.new_page();denied.add_init_script("("+init+")();DeviceOrientationEvent.requestPermission=async()=> 'denied'")
        denied.goto(args.base+'/?device=A');denied.get_by_role('button',name='Start Sensors',exact=True).click()
        denied.get_by_text('Sensor permission denied.',exact=True).wait_for()
        assert denied.get_by_role('button',name='Start Sensors',exact=True).is_enabled()
        checks.append('Orientation permission denial does not start a stream')
        assert errors==[],errors
        report={'status':'passed','checks':checks,'page_errors':errors,'hardware':'virtual browser events only; silent receiver'}
        (args.out/'browser-checks.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report))
    finally:browser.close()

import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import os from 'node:os';
import {createRequire} from 'node:module';
import {PairCore} from '../web/pair-core.mjs';
import {createPairHandler,PREFIX} from '../web/home-handler.mjs';
const fixture=JSON.parse(fs.readFileSync(new URL('./fixtures/pair-web-parity.json',import.meta.url)));
function close(a,b,label='') {
  if(typeof b==='number'){assert.ok(Math.abs(a-b)<1e-8,`${label}: ${a} vs ${b}`);return;}
  if(Array.isArray(b)){assert.equal(a.length,b.length,label);b.forEach((v,i)=>close(a[i],v,label+'.'+i));return;}
  if(b&&typeof b==='object'){for(const [k,v] of Object.entries(b))close(a[k],v,label+'.'+k);return;}
  assert.equal(a,b,label);
}
test('42 states match the independent Python store and actual pan/gain mapper',()=>{
  let now=100;const core=new PairCore({clock:()=>now,epoch:'parity'});
  for(const op of fixture.operations){now=op.t;if(op.frame)core.ingest(op.frame);if(op.stop)core.stop(...op.stop);if(op.panic)core.clear();close(core.status(),op.expected,'t='+op.t);}
});
test('source validation, closed streams and role conflicts preserve accepted state',()=>{
  let now=10;const core=new PairCore({clock:()=>now,epoch:'test'}),frame={device:'A',stream:'a',seq:0,yaw:0,pitch:0,roll:0,ax:0,ay:0,az:0};
  core.ingest(frame);assert.equal(core.sway('A'),0);
  for(const change of [{az:null},{ax:NaN},{seq:0},{stream:'other',seq:1},{az:201}])assert.throws(()=>core.ingest({...frame,...change}));
  core.stop('A','a');assert.throws(()=>core.ingest({...frame,seq:1}));
  core.ingest({...frame,stream:'new'});now+=1.01;assert.equal(core.status().devices.A.online,false);assert.ok(core.status().voices.every(v=>v.gain===0));
});
test('buffers and closed identities stay bounded across long sessions',()=>{
  let now=0;const core=new PairCore({clock:()=>now,epoch:'test'});
  for(let stream=0;stream<70;stream++){
    for(let seq=0;seq<100;seq++){now+=.01;core.ingest({device:'A',stream:'s'+stream,seq,yaw:0,pitch:0,roll:0,ax:0,ay:0,az:seq%2});}
    assert.ok(core.rows.get('A').history.length<=64);core.stop('A','s'+stream);
  }
  assert.equal(core.closed.length,64);assert.equal(core.rows.size,1);
});
test('HTTP sensor/control endpoints require same-origin bounded JSON',async()=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'pair-http-')),dir=path.join(root,'pair-mode');fs.mkdirSync(dir);
  for(const name of ['index.html','pair.css','app.mjs','voice-bank.mjs','phone.html','phone-sender.js','NOTICE.txt'])fs.writeFileSync(path.join(dir,name),'fixture');
  const handler=createPairHandler(root);const server=http.createServer((req,res)=>{if(!handler(req,res)){res.writeHead(404);res.end();}});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const base='http://127.0.0.1:'+server.address().port;
  const frame={device:'A',stream:'a',seq:0,yaw:0,pitch:0,roll:0,ax:0,ay:0,az:0};
  const post=(route,body,headers={})=>fetch(base+PREFIX+route,{method:'POST',headers:{Origin:base,'Content-Type':'application/json',...headers},body:JSON.stringify(body)});
  try{
    assert.equal((await post('api/imu',frame)).status,200);
    assert.equal((await post('api/imu',{...frame,seq:1},{Origin:'https://unrelated.example'})).status,403);
    assert.equal((await post('api/imu',{...frame,seq:1},{'Sec-Fetch-Site':'cross-site'})).status,403);
    assert.equal((await post('api/imu',{padding:'x'.repeat(5000)})).status,413);
    const status=await(await fetch(base+PREFIX+'api/status')).json();assert.equal(status.devices.A.online,true);
    assert.equal((await post('api/control',{action:'panic'})).status,200);
    assert.equal((await post('api/imu',{...frame,seq:1})).status,400);
    const page=await fetch(base+PREFIX);assert.match(page.headers.get('permissions-policy'),/accelerometer=\(self\)/);assert.match(page.headers.get('permissions-policy'),/microphone=\(\)/);
    assert.equal((await fetch(base+PREFIX+'unknown.file')).status,404);
  }finally{await new Promise(resolve=>server.close(resolve));fs.rmSync(root,{recursive:true,force:true});}
});

test('workshop routes use the configured Rostro gate and refuse access without it', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'pair-workshop-'));
  const pairDir = path.join(root, 'pair-mode');
  fs.mkdirSync(pairDir);
  for (const name of ['index.html', 'pair.css', 'app.mjs', 'voice-bank.mjs', 'phone.html', 'phone-sender.js', 'NOTICE.txt']) {
    fs.writeFileSync(path.join(pairDir, name), 'pair fixture');
  }
  for (const name of ['home-handler.mjs', 'pair-core.mjs']) {
    fs.copyFileSync(new URL('../web/' + name, import.meta.url), path.join(pairDir, name));
  }
  fs.copyFileSync(new URL('../web/workshop-entry.cjs', import.meta.url), path.join(root, 'pair-workshop-entry.cjs'));
  fs.writeFileSync(path.join(root, 'workshop-entry.cjs'), `
    module.exports.createServer = () => require('node:http').createServer(
      (_request, response) => response.end('existing workshop route')
    );
  `);
  fs.writeFileSync(path.join(root, 'home-auth.mjs'), `
    export function createHomeAuth({ origin, humanRef }) {
      if (origin !== 'https://workshop.example' || humanRef !== 'human:telegram:12345') {
        throw new Error('Expected the receiving deployment settings');
      }
      return { async authenticate(request) {
        if (request.headers['x-test-session'] !== 'selected') {
          throw Object.assign(new Error('Denied'), { status: 403 });
        }
      }};
    }
  `);
  const {createServer} = createRequire(import.meta.url)(path.join(root, 'pair-workshop-entry.cjs'));
  const lab = '/taller-oliva/living-lab';
  let server;
  try {
    for (const configured of [false, true]) {
      server = await createServer(root, {
        labOrigin: configured ? 'https://workshop.example' : '',
        labHumanRef: 'human:telegram:12345',
      });
      await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
      const base = 'http://127.0.0.1:' + server.address().port;
      assert.equal(await (await fetch(base + PREFIX)).text(), 'pair fixture');
      assert.equal(await (await fetch(base + '/taller-oliva/')).text(), 'existing workshop route');
      assert.equal((await fetch(base + lab, {method: 'HEAD'})).status, configured ? 403 : 503);
      assert.equal((await fetch(base + lab + '/api/state')).status, configured ? 403 : 503);
      const allowed = await fetch(base + lab + '/api/state', {headers: {'X-Test-Session': 'selected'}});
      assert.equal(allowed.status, configured ? 200 : 503);
      await allowed.text();
      await new Promise(resolve => server.close(resolve));
      server = undefined;
    }
  } finally {
    if (server?.listening) await new Promise(resolve => server.close(resolve));
    fs.rmSync(root, {recursive: true, force: true});
  }
});

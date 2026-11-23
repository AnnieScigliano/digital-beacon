import fs from 'node:fs';
import path from 'node:path';
import {PairCore} from './pair-core.mjs';
export const PREFIX='/taller-oliva/pair/';
const CSP="default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self' blob:; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'";
export function createPairHandler(root,{core=new PairCore()}={}) {
  const dir=path.join(root,'pair-mode');
  const publicFiles=new Map(['index.html','pair.css','app.mjs','voice-bank.mjs','phone.html','phone-sender.js','NOTICE.txt'].map(n=>[n,fs.readFileSync(path.join(dir,n))]));
  const types={html:'text/html; charset=utf-8',css:'text/css; charset=utf-8',mjs:'text/javascript; charset=utf-8',js:'text/javascript; charset=utf-8',txt:'text/plain; charset=utf-8'};
  function send(req,res,code,body,type='application/json; charset=utf-8',extra={}) {
    if(!Buffer.isBuffer(body))body=Buffer.from(JSON.stringify(body));
    res.writeHead(code,{'Content-Type':type,'Content-Length':body.length,'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','Content-Security-Policy':CSP,'Permissions-Policy':'accelerometer=(self), gyroscope=(self), magnetometer=(self), camera=(), microphone=(), geolocation=()',...extra});res.end(req.method==='HEAD'?undefined:body);
  }
  return function handle(req,res) {
    let url;try{url=new URL(req.url,'http://localhost');}catch{return false;}
    if(!url.pathname.startsWith(PREFIX))return false;
    const name=url.pathname.slice(PREFIX.length)||'index.html';
    if(req.method==='GET'||req.method==='HEAD') {
      if(name==='api/status'){send(req,res,200,core.status());return true;}
      const file=publicFiles.get(name==='phone'?'phone.html':name);
      if(!file){send(req,res,404,{error:'No se encontró esa página.'});return true;}
      send(req,res,200,file,types[(name==='phone'?'phone.html':name).split('.').at(-1)]);return true;
    }
    if(req.method!=='POST'||!['api/imu','api/disconnect','api/control'].includes(name)){send(req,res,405,{error:'Acción no disponible.'});return true;}
    // Home authenticates this entire workshop before dispatch. These additional
    // checks protect its browser writes without minting another identity store.
    let origin;try{origin=new URL(req.headers.origin||'');}catch{}
    if(!origin||!['http:','https:'].includes(origin.protocol)||origin.host!==req.headers.host||req.headers['sec-fetch-site']==='cross-site'||req.headers['content-type']?.split(';')[0]!=='application/json') {
      send(req,res,403,{error:'Volvé a abrir el ensayo desde nuestro Home.'});return true;
    }
    let size=0,chunks=[],done=false;
    const fail=(status,message)=>{if(done)return;done=true;clearTimeout(timer);send(req,res,status,{error:message});};
    const timer=setTimeout(()=>fail(408,'El envío tardó demasiado.'),1500);
    req.on('data',chunk=>{size+=chunk.length;if(size>4096){chunks=[];fail(413,'El envío supera el tamaño permitido.');}else if(!done)chunks.push(chunk);});
    req.on('error',()=>{done=true;clearTimeout(timer);});
    req.on('end',()=>{
      if(done)return;clearTimeout(timer);
      try {
        const value=JSON.parse(Buffer.concat(chunks).toString('utf8'));
        if(!value||typeof value!=='object'||Array.isArray(value))throw Error('Se esperaba una lectura JSON.');
        let result={ok:true};
        if(name==='api/imu')result=core.ingest(value);
        else if(name==='api/disconnect')core.stop(value.device,value.stream);
        else {if(value.action!=='panic')throw Error('Acción desconocida.');core.clear();}
        done=true;send(req,res,200,result);
      } catch(error){fail(400,error.message);}
    });return true;
  };
}

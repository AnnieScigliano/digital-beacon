// Browser-public math shared with the Node relay. Semantics are checked against
// fixtures produced by the original Python PairRehearsalStore and mapping.
const clamp = (v,a,b) => Math.max(a,Math.min(b,v));
export class PairCore {
  constructor({clock=()=>performance.now()/1000,epoch=globalThis.crypto.randomUUID()}={}) {
    this.clock=clock;this.epoch=epoch;this.reset=0;this.rows=new Map();this.closed=[];
  }
  fresh(d) {const row=this.rows.get(d);return !!row&&row.arrival!==null&&this.clock()-row.arrival>=0&&this.clock()-row.arrival<=1;}
  ingest(v) {
    if(!v||!['A','B'].includes(v.device))throw Error('Elegí el teléfono A o B.');
    for(const k of ['yaw','pitch','roll','ax','ay','az'])if(typeof v[k]!=='number'||!Number.isFinite(v[k]))throw Error('Falta una lectura válida de '+k+'.');
    if(['ax','ay','az'].some(k=>Math.abs(v[k])>200))throw Error('Aceleración fuera del rango de ensayo.');
    const stream=v.stream??'legacy';if(typeof stream!=='string'||!/^[a-zA-Z0-9-]{1,64}$/.test(stream))throw Error('Identificador de envío inválido.');
    if(this.closed.some(([d,s])=>d===v.device&&s===stream))throw Error('Este envío terminó. Tocá Stop y Start para volver.');
    const old=this.rows.get(v.device),seq=v.seq??((old?.seq??-1)+1);
    if(!Number.isSafeInteger(seq)||seq<0)throw Error('Secuencia inválida.');
    if(old?.stream===stream&&seq<=old.seq)throw Error('Lectura repetida o anterior.');
    if(old&&old.stream!==stream&&this.fresh(v.device))throw Error('Este rol ya está conectado. Detenelo antes de cambiar.');
    const history=old?.stream===stream&&this.fresh(v.device)?old.history:[];
    const now=this.clock();history.push([now,Math.hypot(v.ax,v.ay,v.az)]);
    while(history.length>64||history[0][0]<now-2)history.shift();
    this.rows.set(v.device,{...v,stream,seq,arrival:now,history});return {ok:true,device:v.device,seq};
  }
  stop(device,stream) {
    const row=this.rows.get(device);if(!row||row.stream!==stream)throw Error('Este envío no controla ese rol.');
    row.arrival=null;row.history=[];this.closed.push([device,stream]);this.closed=this.closed.slice(-64);
  }
  clear() {for(const [d,row] of this.rows)this.closed.push([d,row.stream]);this.closed=this.closed.slice(-64);this.rows.clear();this.reset++;}
  history(d) {const h=this.rows.get(d)?.history??[];while(h.length&&h[0][0]<this.clock()-2)h.shift();return h;}
  sway(d) {const h=this.history(d);return this.fresh(d)&&h.length?Math.sqrt(h.reduce((s,[,v])=>s+v*v,0)/h.length):0;}
  at(h,t) {
    let i=0;while(i<h.length&&h[i][0]<t)i++;
    if(i<h.length&&Math.abs(h[i][0]-t)<1e-8)return h[i][1];
    if(i===0||i===h.length)return null;const [a,b]=[h[i-1],h[i]];
    return b[0]-a[0]>.35?null:a[1]+(b[1]-a[1])*(t-a[0])/(b[0]-a[0]);
  }
  relation() {
    const a=this.history('A'),b=this.history('B');
    if(!this.fresh('A')||!this.fresh('B'))return {state:'waiting',value:null,samples:0};
    if(!a.length||!b.length)return {state:'insufficient',value:null,samples:0};
    const first=Math.ceil((Math.max(a[0][0],b[0][0])-1e-9)/.1),last=Math.floor((Math.min(a.at(-1)[0],b.at(-1)[0])+1e-9)/.1);
    const pairs=[];for(let i=first;i<=last;i++){const x=this.at(a,i*.1),y=this.at(b,i*.1);if(x!==null&&y!==null)pairs.push([x,y]);}
    if(pairs.length<6)return {state:'insufficient',value:null,samples:pairs.length};
    const n=pairs.length,mx=pairs.reduce((s,p)=>s+p[0],0)/n,my=pairs.reduce((s,p)=>s+p[1],0)/n;
    const vx=pairs.reduce((s,p)=>s+(p[0]-mx)**2,0),vy=pairs.reduce((s,p)=>s+(p[1]-my)**2,0);
    if(vx<1e-9||vy<1e-9)return {state:'flat',value:null,samples:n};
    const cov=pairs.reduce((s,p)=>s+(p[0]-mx)*(p[1]-my),0),denom=Math.sqrt(vx*vy);
    return {state:'observed',value:denom<1e-9?0:clamp(cov/denom,0,1),samples:n};
  }
  status() {
    const devices=Object.fromEntries(['A','B'].map(d=>[d,{online:this.fresh(d),age_s:this.rows.get(d)?.arrival==null?null:Math.max(0,this.clock()-this.rows.get(d).arrival),sway:this.sway(d)}]));
    const relation=this.relation(),sync=relation.value??0,energy=(devices.A.sway+devices.B.sway)/2;
    const mods={};for(const d of ['A','B']){const row=this.fresh(d)?this.rows.get(d):{};mods[d]={pan:clamp((row.yaw??0)/180,-1,1)*.6,gain:Math.min(devices[d].sway/3,1)*.5+clamp((row.pitch??0)/90,-1,1)*.2};}
    const active=(devices.A.online?10:0)+(devices.B.online?10:0)+(devices.A.online&&devices.B.online?8:0);
    const voices=Array.from({length:28},(_,i)=>{
      const n=i+1,role=n<=10?'A':n<=20?'B':'shared',index=n<=10?n:n<=20?n-10:n-20;
      const valid=role==='shared'?devices.A.online&&devices.B.online:devices[role].online;
      const scale=role==='shared'?sync:1+mods[role].gain+.35*sync*mods[role==='A'?'B':'A'].gain;
      return {harmonic_n:n,frequency_hz:40.4*n,pan:role==='shared'?0:mods[role].pan,gain:valid?Math.min(1,(role==='shared'?.12:.22)/Math.sqrt(index)*scale)/Math.sqrt(active)*(1+Math.min(energy/3,1)*.25):0};
    });
    return {epoch:this.epoch,reset:this.reset,devices,relation,voices,input:'phone movement',render:'Web Audio in the listening browser'};
  }
}

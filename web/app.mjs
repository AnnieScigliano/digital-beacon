import {VoiceBank} from './voice-bank.mjs';
const $=id=>document.getElementById(id);
let bank=null,wanted=false,snapshot=null,received=-Infinity,busy=false,fence=0,usable=false,lost=false;
const api=(name,options={})=>fetch(new URL('./api/'+name,location.href),{cache:'no-store',redirect:'error',...options});
function paused(message='El sonido está pausado.') {wanted=false;bank?.mute();$('output').textContent=message;}
for(const d of ['a','b']) {
  const url=new URL('./phone?device='+d.toUpperCase(),location.href);$('link-'+d).href=url.href;
  $('copy-'+d).onclick=async()=>{try{await navigator.clipboard.writeText(url.href);$('notice').textContent='Enlace '+d.toUpperCase()+' copiado. Abrilo en el teléfono.';}catch{$('notice').textContent='Copiá el enlace desde “Abrir '+d.toUpperCase()+'”.';}};
}
$('listen').onclick=async()=>{
  try{bank??=new VoiceBank();await bank.start();wanted=true;bank.setVolume(Number($('volume').value));if(snapshot&&usable&&performance.now()-received<250)bank.update(snapshot.voices);$('output').textContent='Sonido habilitado en este navegador.';$('notice').textContent='';}
  catch(error){paused();$('notice').textContent=error.message;}
};
$('pause').onclick=()=>paused();
$('volume').oninput=()=>{const v=Number($('volume').value);$('level').textContent=Math.round(v*100)+'%';bank?.setVolume(v);};
$('cut').onclick=async()=>{
  fence++;wanted=false;bank?.mute(true);snapshot=null;$('output').textContent='Sonido cortado.';
  try{const r=await api('control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'panic'})});if(!r.ok)throw Error('No pudimos confirmar el cierre de los envíos. La escucha local está cortada.');$('notice').textContent='El ensayo se cerró. En los teléfonos, tocá Stop y Start para volver.';}
  catch(error){$('notice').textContent=error.message;}
};
async function poll() {
  if(busy||document.visibilityState==='hidden')return;
  busy=true;const version=fence,started=performance.now(),abort=new AbortController(),deadline=setTimeout(()=>abort.abort(),1200);
  try{
    const response=await api('status',{signal:abort.signal});if(!response.ok)throw Error('No se pudo actualizar el ensayo. Revisá tu conexión o volvé a entrar a Home.');
    const value=await response.json();if(version!==fence)return;
    if(snapshot&&(snapshot.epoch!==value.epoch||snapshot.reset!==value.reset)){paused('El ensayo se reinició. Activá la escucha para volver.');}
    const transit=(performance.now()-started)/1000;
    const late=Object.values(value.devices).some(d=>d.online&&d.age_s+transit>1);
    if(late){bank?.mute();wanted=false;$('output').textContent='Lectura tardía. Activá el sonido cuando vuelva la señal.';}
    snapshot=value;received=performance.now();usable=!late;$('signin').hidden=true;if(lost){lost=false;$('output').textContent='Conexión recuperada. Activá el sonido para escuchar.';$('notice').textContent='';}
    for(const d of ['A','B'])$(d.toLowerCase()).textContent=value.devices[d].online?'Señal recibida':'Esperando señal';
    $('relation').textContent=({waiting:'Esperando los dos teléfonos',insufficient:'Reuniendo movimiento reciente',flat:'Sin variación suficiente para la capa compartida',observed:value.relation.value>0?'La capa compartida acompaña estas variaciones':'Las voces conservan sus movimientos propios'})[value.relation.state];
    $('readings').textContent=JSON.stringify({devices:value.devices,relation:value.relation,render:value.render},null,2);
    if(wanted&&!late)bank.update(value.voices);
  }catch(error){if(version!==fence)return;paused('Conexión interrumpida · sonido pausado.');snapshot=null;usable=false;lost=true;$('a').textContent=$('b').textContent='Estado sin confirmar';$('relation').textContent='Esperando datos recientes';$('notice').textContent=error.message;$('signin').hidden=false;}
  finally{clearTimeout(deadline);busy=false;}
}
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='hidden')paused('Escucha pausada al salir de la página.');else void poll();});
window.addEventListener('pagehide',()=>{wanted=false;void bank?.close();});
void poll();setInterval(poll,100);

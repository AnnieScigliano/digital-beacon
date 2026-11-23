"""Compose the selected immutable workshop with a private Node/browser Pair app.

Requires only Python's standard library. Never copy a mutable Biblio/Rostro tree
or private state. Phone code is extracted from the existing selected sender.
"""
import argparse
import ast
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile

ROOT=Path(__file__).resolve().parents[1]
sha=lambda raw:hashlib.sha256(raw).hexdigest()
canonical=lambda value:json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()

def phone_assets():
    tree=ast.parse((ROOT/'hybrid_bridge.py').read_text())
    html=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='IMU_SENDER_HTML' for t in n.targets))
    found=re.findall(r'<script>([\s\S]*?)</script>',html)
    if len(found)!=1:raise ValueError('Expected the selected sender script')
    script=found[0]
    for before,after in [("'/imu'","'./api/imu'"),("'/pair/disconnect'","'./api/disconnect'"),("'/pair/status'","'./api/status'")]:
        if script.count(before)!=1:raise ValueError('Sender routes changed; reconcile explicitly')
        script=script.replace(before,after)
    html=html.replace('<script>'+found[0]+'</script>','<script src="./phone-sender.js" defer></script>')
    for d in ['A','B']:html=html.replace('onclick="pickDevice(\''+d+'\')"','data-device="'+d+'"')
    for call in ['start()','stop()','recalibrate()']:html=html.replace(' onclick="'+call+'"','')
    if 'onclick=' in html:raise ValueError('Unexpected inline sender handler')
    script+='''
document.querySelectorAll('[data-device]').forEach(button=>button.addEventListener('click',()=>pickDevice(button.dataset.device)));
document.getElementById('startBtn').addEventListener('click',()=>void start());
document.getElementById('stopBtn').addEventListener('click',stop);
document.getElementById('recalBtn').addEventListener('click',recalibrate);
'''
    html=html.replace('<h2>Beacon IMU Sender</h2>','<h2>Mi movimiento · Beacon</h2><p>Envío al ensayo privado de Home. Dejá esta página visible.</p>')
    return {'phone.html':html.encode(),'phone-sender.js':script.encode()}

def build(base,expected,out):
    raw=base.read_bytes()
    if sha(raw)!=expected:raise ValueError('Selected workshop archive changed')
    with tarfile.open(fileobj=io.BytesIO(raw),mode='r:') as tar:
        files={m.name:tar.extractfile(m).read() for m in tar if m.isfile()}
    manifest=json.loads(files.pop('service.json'))
    if manifest['service']!='web' or manifest['state_schema']!='web-v1':raise ValueError('Use the configured workshop service')
    if {n:sha(v) for n,v in files.items()}!={n['path']:n['sha256'] for n in manifest['files']}:raise ValueError('Workshop inventory mismatch')
    if manifest['entry']=='pair-workshop-entry.cjs':
        files={n:v for n,v in files.items() if n!='pair-workshop-entry.cjs' and not n.startswith('pair-mode/')}
        manifest['entry']='workshop-entry.cjs'
    if manifest['entry']!='workshop-entry.cjs':raise ValueError('Reconcile the selected workshop factory')
    preserved={n:sha(v) for n,v in files.items()}
    public=['index.html','pair.css','app.mjs','voice-bank.mjs','pair-core.mjs','home-handler.mjs','NOTICE.txt']
    for n in public:files['pair-mode/'+n]=(ROOT/'web'/n).read_bytes()
    for n,v in phone_assets().items():files['pair-mode/'+n]=v
    files['pair-workshop-entry.cjs']=(ROOT/'web/workshop-entry.cjs').read_bytes()
    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    files['pair-mode/source.json']=canonical({'source_commit':head,'base_archive_sha256':expected,'state':'in-memory two-phone relay; browser audio'})
    assert all(sha(files[n])==h for n,h in preserved.items())
    manifest['entry']='pair-workshop-entry.cjs'
    manifest['files']=[{'path':n,'sha256':sha(v)} for n,v in sorted(files.items())]
    files['service.json']=canonical(manifest)
    out.mkdir(parents=True,exist_ok=True);archive=out/'pair-workshop.tar'
    if archive.exists():raise ValueError('Retain prepared package; select another output')
    with tarfile.open(archive,'w',format=tarfile.USTAR_FORMAT) as tar:
        for n,v in sorted(files.items()):
            item=tarfile.TarInfo(n);item.size=len(v);item.mode=0o644
            tar.addfile(item,io.BytesIO(v))
    if archive.stat().st_size>32*1024**2:raise ValueError('Package exceeds the existing archive limit')
    record={'schema':'pair-mode.home-package/v1','archive':str(archive),'sha256':sha(archive.read_bytes()),'bytes':archive.stat().st_size,'base_sha256':expected,'preserved_base_files':preserved,'source_commit':head,'entry':manifest['entry'],'health':manifest['health'],'files':len(files)}
    (out/'package.json').write_bytes(canonical(record))
    print(json.dumps({k:v for k,v in record.items() if k!='preserved_base_files'}))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base',type=Path,required=True);p.add_argument('--expected-base',required=True);p.add_argument('--out',type=Path,required=True)
    a=p.parse_args();build(a.base,a.expected_base,a.out)

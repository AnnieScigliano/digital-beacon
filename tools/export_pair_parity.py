"""Export synthetic reference vectors from the actual Python rehearsal profile."""
from pathlib import Path
import json
import math
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from digital_beacon.pair_rehearsal import PairRehearsalStore
from digital_beacon.audio_engine import _person_pan_gain_mod

def reference(store):
    status=store.status();snapshot=store.get_snapshot();sync=store.get_hybrid_synchrony()
    a=_person_pan_gain_mod(*store.get_hybrid_imu('A')[:2],store.get_hybrid_sway_energy('A'))
    b=_person_pan_gain_mod(*store.get_hybrid_imu('B')[:2],store.get_hybrid_sway_energy('B'))
    mod={'A':a,'B':b};master=1+min(store.get_hybrid_pair_energy()/3,1)*.25
    voices=[]
    for n in range(1,29):
        role='A' if n<=10 else 'B' if n<=20 else 'shared'
        gain=0
        if n in snapshot:
            mult=sync if role=='shared' else 1+mod[role][1]+.35*sync*mod['B' if role=='A' else 'A'][1]
            gain=min(1,snapshot[n].gain*mult)/math.sqrt(len(snapshot))*master
        voices.append({'harmonic_n':n,'frequency_hz':40.4*n,'pan':0 if role=='shared' else mod[role][0],'gain':gain})
    return {'devices':status['devices'],'relation':status['relation'],'voices':voices}

clock={'now':100.0};store=PairRehearsalStore(lambda:clock['now']);store.arm()
operations=[]
def step(t,payload=None,stop=None,panic=False):
    clock['now']=t
    operation={'t':t}
    if payload is not None:store.ingest(payload);operation['frame']=payload
    if stop:store.disconnect(*stop);operation['stop']=stop
    if panic:store.panic();operation['panic']=True
    operation['expected']=reference(store);operations.append(operation)
def frame(d,i,t):return dict(device=d,stream='phone-'+d,seq=i,yaw=50 if d=='A' else -30,pitch=10 if d=='A' else -5,roll=0,ax=0,ay=0,az=2+math.sin(t*4))
step(100)
for t,d,i in sorted([(i/10,'A',i) for i in range(20)]+[(i/8,'B',i) for i in range(16)]):step(100+t,frame(d,i,t))
step(102.5);step(103.1)
step(103.2,frame('A',30,3.2));step(103.25,stop=['A','phone-A']);step(103.3,panic=True)
target=Path(__file__).resolve().parents[1]/'tests/fixtures/pair-web-parity.json'
target.write_text(json.dumps({'source':'PairRehearsalStore + actual Python pan/gain mapping; synthetic receiver times','operations':operations},separators=(',',':'))+'\n')
print('Exported',len(operations),'independent Python reference states.')

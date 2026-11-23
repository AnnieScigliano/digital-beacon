"""Hardware-free rehearsal contracts and the real existing audio callback."""
import importlib.util
import json
import math
from pathlib import Path
import threading
import unittest
import urllib.request
import urllib.error
from unittest.mock import patch

import numpy as np
from digital_beacon import config
from digital_beacon.audio_engine import AudioEngine
from digital_beacon.pair_rehearsal import PairRehearsalStore


class Clock:
    now=100.0
    def __call__(self):return self.now

def frame(device='A',seq=0,value=0,stream=None):
    return dict(device=device,stream=stream or 'phone-'+device,seq=seq,
                yaw=0,pitch=0,roll=0,ax=0,ay=0,az=value)

def engine(store):
    out=AudioEngine.__new__(AudioEngine)
    out._store=store;out._sample_rate=44100;out._block_size=256
    out._voice_state={};out._record_sink=None
    return out

def render(e,count=1):
    for _ in range(count):
        out=np.zeros((256,2),dtype=np.float32);e._audio_callback(out,256,None,None)
    return out


class TestPairRehearsal(unittest.TestCase):
    def setUp(self):
        self.clock=Clock();self.store=PairRehearsalStore(self.clock)
        self.mode=patch.object(config,'HYBRID_PAIR_MODE_ENABLED',True);self.mode.start()
    def tearDown(self):self.mode.stop()
    def test_zero_is_valid_and_missing_does_not_refresh(self):
        self.store.ingest(frame());self.assertEqual(self.store.get_hybrid_sway_energy(),0)
        self.clock.now+=.7
        for bad in [None,float('nan'),True]:
            f=frame(seq=1);f['az']=bad
            with self.assertRaises(ValueError):self.store.ingest(f)
        self.clock.now+=.4;self.assertFalse(self.store.status()['devices']['A']['online'])
    def test_no_voices_until_armed_and_fresh(self):
        self.assertEqual(self.store.get_snapshot(),{})
        self.store.arm();self.assertEqual(self.store.get_snapshot(),{})
        self.store.ingest(frame())
        self.assertEqual(set(self.store.get_snapshot()),set(range(1,11)))
    def test_roles_are_independent_and_expire_without_new_packets(self):
        self.store.arm();self.store.ingest(frame('A'));self.clock.now+=.5
        self.store.ingest(frame('B'));self.clock.now+=.6
        self.assertEqual(set(self.store.get_snapshot()),set(range(11,21)))
        self.assertEqual(self.store.get_hybrid_imu('A'),(0,)*6)
        self.assertEqual(self.store.get_hybrid_sway_energy('A'),0)
        self.assertIsNone(self.store.relation()['value'])
    def test_receiver_time_resampling_handles_unequal_cadences(self):
        events=sorted([(i/10,'A',i) for i in range(20)]+[(i/8,'B',i) for i in range(16)])
        for t,d,seq in events:
            self.clock.now=100+t;self.store.ingest(frame(d,seq,2+math.sin(t*4)))
        r=self.store.relation();self.assertEqual(r['state'],'observed');self.assertGreater(r['value'],.98)
    def test_stillness_is_not_observed_zero_correlation(self):
        for i in range(8):
            self.clock.now=100+i*.1
            for d in ('A','B'):self.store.ingest(frame(d,i))
        self.assertEqual(self.store.relation()['state'],'flat')
        self.assertIsNone(self.store.relation()['value'])
    def test_correlated_and_opposed_magnitude_patterns(self):
        for opposed in (False,True):
            store=PairRehearsalStore(self.clock)
            for i in range(16):
                self.clock.now=100+i*.1
                store.ingest(frame('A',i,1+i%3))
                store.ingest(frame('B',i,3-i%3 if opposed else 1+i%3))
            self.assertEqual(store.relation()['state'],'observed')
            self.assertAlmostEqual(store.get_hybrid_synchrony(),0 if opposed else 1)
    def test_stopped_stream_cannot_reopen_with_an_inflight_packet(self):
        self.store.ingest(frame());self.store.disconnect('A','phone-A')
        with self.assertRaises(ValueError):self.store.ingest(frame(seq=1))
        self.store.ingest(frame(stream='new-A'));self.assertTrue(self.store.status()['devices']['A']['online'])
    def test_duplicate_sequence_and_competing_role_are_rejected(self):
        self.store.ingest(frame())
        for value in [frame(),frame(seq=3,stream='someone-else')]:
            with self.assertRaises(ValueError):self.store.ingest(value)
    def test_gap_resets_support_before_a_returning_phone(self):
        for i in range(10):
            self.clock.now=100+i*.1
            for d in ('A','B'):self.store.ingest(frame(d,i,i%3))
        self.clock.now+=1.1
        for d in ('A','B'):self.store.ingest(frame(d,20,1))
        self.assertEqual(self.store.relation()['state'],'insufficient')
    def test_actual_callback_fades_to_silence_on_drop(self):
        self.store.arm();self.store.ingest(frame(value=1));e=engine(self.store)
        warm=render(e,100);self.assertGreater(float(np.max(np.abs(warm))),.001)
        self.clock.now+=1.1
        tail=render(e,150);self.assertLess(float(np.max(np.abs(tail))),1e-7)
    def test_heart_rate_cannot_modulate_the_pair_profile(self):
        self.store.arm();self.store.ingest(frame(value=1))
        a=render(engine(self.store),50)
        self.store.update_hybrid_hr(150,400,1)
        b=render(engine(self.store),50)
        np.testing.assert_array_equal(a,b)
    def test_panic_closes_streams_and_requires_new_start(self):
        self.store.arm();self.store.ingest(frame());self.store.panic()
        self.assertEqual(self.store.get_snapshot(),{})
        with self.assertRaises(ValueError):self.store.ingest(frame(seq=1))
    def test_volume_is_bounded(self):
        for v in [-1,.31,True,float('nan')]:
            with self.assertRaises(ValueError):self.store.arm(v)


class TestRehearsalHTTP(unittest.TestCase):
    def test_real_phone_endpoint_and_silent_control_contract(self):
        path=Path(__file__).resolve().parents[1]/'scripts/pair_rehearsal.py'
        spec=importlib.util.spec_from_file_location('pair_runner',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        store=PairRehearsalStore();server=module.make_server(store,port=0)
        thread=threading.Thread(target=server.serve_forever);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        def post(route,data,headers=None):
            try:return urllib.request.urlopen(urllib.request.Request(base+route,data=json.dumps(data).encode(),headers={'Content-Type':'application/json',**(headers or {})}),timeout=2)
            except urllib.error.HTTPError as e:return e
        try:
            with urllib.request.urlopen(base+'/') as r:self.assertIn(b'handleMotion',r.read())
            with post('/imu',frame()) as r:self.assertEqual(r.status,200)
            with urllib.request.urlopen(base+'/pair/status') as r:
                value=json.load(r);self.assertTrue(value['devices']['A']['online']);self.assertFalse(value['audio_enabled'])
            with post('/pair/control',{'action':'arm'}) as r:self.assertEqual(r.status,400)
            self.assertFalse(store.status()['armed'])
            with post('/imu',frame(seq=1),{'Origin':'https://unrelated.example'}) as r:self.assertEqual(r.status,400)
            with post('/pair/disconnect',{'device':'A','stream':'phone-A'}) as r:self.assertEqual(r.status,200)
            with post('/imu',frame(seq=1)) as r:self.assertEqual(r.status,400)
        finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()

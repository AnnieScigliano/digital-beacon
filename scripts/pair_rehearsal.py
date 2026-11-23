#!/usr/bin/env python3
"""Foreground two-phone instrument; silent by default, no OSC/MIDI/SC or ECG.

Use the existing Mac Weaver Python environment (numpy and sounddevice).
Serve the phone page and /imu together through the selected local HTTPS route.
No existing process is killed, and no other audio/OSC listener is contacted.
"""
from __future__ import annotations
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from digital_beacon.pair_rehearsal import PairRehearsalStore
from hybrid_bridge import IMU_SENDER_HTML


def make_server(store, host='127.0.0.1', port=5052, audio_enabled=False):
    operator = (ROOT/'static/pair_rehearsal.html').read_bytes()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def reply(self,code,data,kind='application/json; charset=utf-8'):
            body=json.dumps(data,allow_nan=False).encode() if isinstance(data,dict) else data
            self.send_response(code);self.send_header('Content-Type',kind)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','no-referrer')
            self.end_headers();self.wfile.write(body)
        def do_GET(self):
            route=urlsplit(self.path).path
            if route=='/':self.reply(200,IMU_SENDER_HTML.encode(),'text/html; charset=utf-8')
            elif route=='/pair':self.reply(200,operator,'text/html; charset=utf-8')
            elif route=='/pair/status':self.reply(200,{**store.status(),'audio_enabled':audio_enabled})
            else:self.reply(404,{'error':'Not found'})
        def do_POST(self):
            try:
                # Browser same-origin writes; no cross-origin permissive CORS.
                origin=self.headers.get('Origin')
                if self.headers.get('Sec-Fetch-Site')=='cross-site' or (origin and urlsplit(origin).netloc!=self.headers.get('Host')):
                    raise ValueError('Use the sender and receiver on the same origin')
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=4096:raise ValueError('Expected a bounded JSON frame')
                value=json.loads(self.rfile.read(length))
                if not isinstance(value,dict):raise ValueError('Expected a JSON object')
                route=urlsplit(self.path).path
                if route=='/imu':result=store.ingest(value)
                elif route=='/pair/disconnect':
                    store.disconnect(value.get('device'),value.get('stream'));result={'ok':True}
                elif route=='/pair/control':
                    action=value.get('action')
                    if action=='arm':
                        if not audio_enabled:raise ValueError('This process is a silent rehearsal; start with --audio for physical sound')
                        store.arm(value.get('volume',0.06))
                    elif action=='pause':store.pause()
                    elif action=='panic':store.panic()
                    else:raise ValueError('Unknown control')
                    result={'ok':True}
                else:self.reply(404,{'error':'Not found'});return
                self.reply(200,result)
            except (ValueError,TypeError,KeyError) as error:self.reply(400,{'ok':False,'error':str(error)})
    server=ThreadingHTTPServer((host,port),Handler)
    server.daemon_threads=True
    return server


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host',default='127.0.0.1');p.add_argument('--port',type=int,default=5052)
    p.add_argument('--audio',action='store_true',help='Open the selected audio output; operator must still enable sound')
    p.add_argument('--device',default=None,help='Existing PortAudio device ID or name')
    a=p.parse_args()
    if not 1<=a.port<=65535:p.error('Choose an unused TCP port')
    store=PairRehearsalStore()
    # Bind first: a port conflict must never open an audio device or kill its owner.
    server=make_server(store,a.host,a.port,a.audio)
    engine=None
    try:
        if a.audio:
            from digital_beacon import config
            from digital_beacon.audio_engine import AudioEngine
            config.HYBRID_PAIR_MODE_ENABLED=True
            device=int(a.device) if a.device and a.device.isdecimal() else a.device
            engine=AudioEngine(store,device=device);engine.start()
        print(f'Pair rehearsal: http://{a.host}:{a.port}/pair',flush=True)
        print('Phone page: same origin / · ECG/EEG disabled · no OSC listeners',flush=True)
        print('Audio output opened, awaiting operator enable' if a.audio else 'SILENT rehearsal (--audio not selected)',flush=True)
        server.serve_forever()
    except KeyboardInterrupt:pass
    finally:
        store.panic()
        if engine:engine.stop()
        server.server_close()


if __name__=='__main__':main()

"""Two-phone rehearsal profile over the existing additive Pair Mode engine.

Linear acceleration is m/s^2, orientation is relative degrees. Correlation uses
a common receiver-time grid; it is not acquisition-clock synchronization.
This opt-in profile changes neither the legacy store nor the SuperCollider path.
"""
from __future__ import annotations

from bisect import bisect_left
from collections import deque
import math
import re
import time

from .state import VoiceParameterStore, _pearson_correlation


class PairRehearsalStore(VoiceParameterStore):
    WINDOW_S = 2.0
    FRESH_S = 1.0
    GRID_S = 0.1
    MAX_GAP_S = 0.35

    def __init__(self, clock=time.monotonic):
        super().__init__()
        self._clock = clock
        self._arrivals = {}
        self._streams = {}
        self._sequences = {}
        self._closed_streams = deque(maxlen=64)
        self._armed = False
        self.set_master_gain(0.06)
        self._sidechain_amount = 0.0
        self._lfo_amount = 0.0

    def ingest(self, payload):
        device = payload.get('device')
        if device not in ('A', 'B'):
            raise ValueError('Choose phone A or B')
        values = []
        for name in ('yaw', 'pitch', 'roll', 'ax', 'ay', 'az'):
            value = payload.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError('A fresh, finite reading is required for ' + name)
            values.append(float(value))
        if any(abs(v) > 200 for v in values[3:]):
            raise ValueError('Acceleration outside this rehearsal range')
        stream = payload.get('stream', 'legacy')
        if not isinstance(stream, str) or not re.fullmatch(r'[a-zA-Z0-9-]{1,64}', stream):
            raise ValueError('Invalid phone stream')
        with self._lock:
            if (device, stream) in self._closed_streams:
                raise ValueError('This stream was stopped; press Start again')
            seq = payload.get('seq', self._sequences.get(device, -1) + 1)
            if isinstance(seq, bool) or not isinstance(seq, int) or not 0 <= seq < 2**53:
                raise ValueError('Invalid sample sequence')
            previous = self._streams.get(device)
            if previous != stream:
                if previous is not None and self._fresh(device):
                    raise ValueError('This phone role is already connected; stop it first')
                self._hybrid_motion_history.pop(device, None)
            elif seq <= self._sequences.get(device, -1):
                raise ValueError('Duplicate or older sample')
            if not self._fresh(device):
                self._hybrid_motion_history.pop(device, None)
            self._streams[device] = stream
            self._sequences[device] = seq
            self._arrivals[device] = self._clock()
            super().update_hybrid_imu(*values, device=device)
        return {'ok': True, 'device': device, 'seq': seq}

    def _record_motion_sample(self, device, magnitude):
        hist = self._hybrid_motion_history.setdefault(device, deque(maxlen=64))
        hist.append((self._clock(), magnitude))
        self._prune(device)

    def _prune(self, device):
        hist = self._hybrid_motion_history.get(device)
        if hist is not None:
            cutoff = self._clock() - self.WINDOW_S
            while hist and hist[0][0] < cutoff:
                hist.popleft()

    def _fresh(self, device):
        return device in self._arrivals and 0 <= self._clock() - self._arrivals[device] <= self.FRESH_S

    def disconnect(self, device, stream):
        with self._lock:
            if device not in ('A', 'B') or self._streams.get(device) != stream:
                raise ValueError('This stream does not own the phone role')
            self._arrivals.pop(device, None)
            self._hybrid_motion_history.pop(device, None)
            self._closed_streams.append((device, stream))

    def get_hybrid_imu(self, device='A'):
        with self._lock:
            return super().get_hybrid_imu(device) if self._fresh(device) else (0.0,) * 6

    def get_hybrid_sway_energy(self, device='A'):
        with self._lock:
            self._prune(device)
            hist = self._hybrid_motion_history.get(device, ())
            if not self._fresh(device) or not hist:
                return 0.0
            return math.sqrt(sum(v*v for _, v in hist) / len(hist))

    @classmethod
    def _at(cls, hist, t):
        times = [row[0] for row in hist]
        i = bisect_left(times, t)
        if i < len(hist) and abs(hist[i][0] - t) < 1e-8:
            return hist[i][1]
        if i == 0 or i == len(hist):
            return None
        a, b = hist[i-1], hist[i]
        if b[0] - a[0] > cls.MAX_GAP_S:
            return None
        return a[1] + (b[1] - a[1]) * (t - a[0]) / (b[0] - a[0])

    def relation(self):
        with self._lock:
            for d in ('A', 'B'):
                self._prune(d)
            if not all(self._fresh(d) for d in ('A', 'B')):
                return {'state': 'waiting', 'value': None, 'samples': 0}
            a, b = (list(self._hybrid_motion_history.get(d, ())) for d in ('A', 'B'))
            if not a or not b:
                return {'state': 'insufficient', 'value': None, 'samples': 0}
            first = math.ceil((max(a[0][0], b[0][0]) - 1e-9) / self.GRID_S)
            last = math.floor((min(a[-1][0], b[-1][0]) + 1e-9) / self.GRID_S)
            pairs = [(self._at(a, i*self.GRID_S), self._at(b, i*self.GRID_S)) for i in range(first, last+1)]
            pairs = [(x,y) for x,y in pairs if x is not None and y is not None]
            if len(pairs) < 6:
                return {'state': 'insufficient', 'value': None, 'samples': len(pairs)}
            x, y = zip(*pairs)
            if any(sum((v-sum(s)/len(s))**2 for v in s) < 1e-9 for s in (x,y)):
                return {'state': 'flat', 'value': None, 'samples': len(pairs)}
            return {'state': 'observed', 'value': min(1.0, _pearson_correlation(list(x), list(y))), 'samples': len(pairs)}

    def get_hybrid_synchrony(self, device_a='A', device_b='B'):
        return self.relation()['value'] or 0.0

    def arm(self, volume=0.06):
        if isinstance(volume, bool) or not isinstance(volume, (int,float)) or not math.isfinite(volume) or not 0 <= volume <= 0.3:
            raise ValueError('Volume must be between 0 and 0.3')
        with self._lock:
            for n in range(1,29):
                index = n if n <= 10 else n-10 if n <= 20 else n-20
                gain = (0.22 if n <= 20 else 0.12) / math.sqrt(index)
                self.voice_on(n, n, self.f1*n, gain=gain)
                self.set_attack(n, 0.2)
                self.set_release(n, 0.5)
            self.set_master_gain(volume)
            self._armed = True

    def pause(self):
        # Removing voices from snapshots uses the original engine's release ramp.
        with self._lock:
            self._armed = False

    def panic(self):
        with self._lock:
            self._armed = False
            self._closed_streams.extend(self._streams.items())
            self._arrivals.clear()
            self._streams.clear()
            self._sequences.clear()
            self.set_master_gain(0)
            super().panic()

    def get_snapshot(self):
        with self._lock:
            if not self._armed:
                return {}
            fresh = {d:self._fresh(d) for d in ('A','B')}
            return {n:v for n,v in super().get_snapshot().items()
                    if (n <= 10 and fresh['A']) or (10 < n <= 20 and fresh['B'])
                    or (n > 20 and all(fresh.values()))}

    def status(self):
        with self._lock:
            return {'armed': self._armed, 'volume': self.get_master_gain(),
                    'devices': {d:{'online': self._fresh(d),
                        'age_s': max(0,self._clock()-self._arrivals[d]) if d in self._arrivals else None,
                        'sway': self.get_hybrid_sway_energy(d)} for d in ('A','B')},
                    'relation': self.relation(), 'input': 'phone linear acceleration and relative orientation; no ECG/EEG'}

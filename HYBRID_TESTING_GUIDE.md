# Guía de Prueba Completa — Hybrid Sensor Mode v1.0

## Pre-requisitos

- macOS con Bluetooth (el Mac-mini-de-AlterMundi tiene)
- Fitbit Charge 6 (o cualquier sensor BLE HR) **en modo ejercicio** para transmitir HR
- Teléfono con navegador moderno (Chrome/Safari) que soporte Web Sensors API
- digital-beacon ya clonado en `~/Documents/AlterMundi/Beacon/digital-beacon`

## Paso 1: Preparar el entorno

```bash
cd ~/Documents/AlterMundi/Beacon/digital-beacon
source venv/bin/activate
```

El venv ya tiene `bleak`, `flask`, `python-osc`, `numpy`, etc. instalados.

## Paso 2: Iniciar digital-beacon con hybrid mode

```bash
./start.sh --hybrid --file
```

Esto levanta:
- scsynth (57110)
- sclang + beacon.scd o beacon_simple.scd si no tenés el quark ATK instalado (57120)
- f1_bridge (9001)
- **hybrid_bridge.py** (5051 HTTP para IMU; manda por OSC a sclang :57120)
- Shaper audio engine (8080)

Esperá a que veas `Hybrid Sensor Bridge v1.0` en los logs.

## Paso 3: Conectar un sensor de HR real (opcional)

**Importante:** un Fitbit Charge 6 no expone el perfil BLE HR estándar a apps
de terceros (confirmado por escaneo en vivo) — `--mode ble` no lo va a
encontrar. Necesitás una banda de pecho BLE genérica (Polar H10, COOSPO
H6/H64, Magene H64, etc). Sin una de esas, saltá este paso — `hybrid_bridge.py`
ya genera HR simulado por defecto (ver logs `[HYBRID HR]`).

### 3a. En el sensor:
- Ponételo y activalo (algunas bandas necesitan humedecer los electrodos)

### 3b. En el Mac (nueva terminal):

```bash
cd ~/Documents/AlterMundi/Beacon/cymatic-control
source venv/bin/activate  # o el venv que uses
python hr_relay.py --mode ble --target digital-beacon --device "Polar"
```

Si no tenés venv en cymatic-control:

```bash
cd ~/Documents/AlterMundi/Beacon/cymatic-control
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python hr_relay.py --mode ble --target digital-beacon --device "Polar"
```

Deberías ver:
```
  [target] digital-beacon mode: OSC -> 127.0.0.1:57120
  [target] Sends /hybrid/hr [bpm, rr_ms, confidence] directly to sclang
  Scanning for BLE devices...
  Checking each device for the Heart Rate Service (0x180D)...
  Found HR sensor: Polar H10 (XX:XX:XX:XX:XX:XX)
  Streaming heart rate from Polar H10...
  ♥ beat #1  72 BPM  (RR: 833ms)
```

Este camino manda directo a sclang, en paralelo a lo que ya genera
`hybrid_bridge.py` — ambos conviven sin pisarse.

## Paso 4: Conectar el teléfono (IMU)

### 4a. En el teléfono — opción simple (curl desde computadora en la misma red):

Si tu teléfono y el Mac están en la misma WiFi, podés probar desde la terminal del Mac:

```bash
# Reemplazá 192.168.1.XXX con la IP del Mac
curl -X POST http://192.168.1.XXX:5051/imu \
  -H "Content-Type: application/json" \
  -d '{"yaw": 45, "pitch": 10, "roll": 0, "ax": 0.1, "ay": 0, "az": 9.8}'
```

### 4b. En el teléfono — opción web (HTML + JavaScript):

Crear un archivo `imu_sender.html` en el teléfono o servirlo desde el Mac:

```html
<!DOCTYPE html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Beacon IMU</title>
  <style>
    body { font-family: sans-serif; background: #111; color: #0f0; padding: 20px; }
    button { font-size: 18px; padding: 15px; margin: 10px 0; width: 100%; }
    #status { font-size: 14px; margin-top: 20px; }
    .value { font-family: monospace; font-size: 16px; }
  </style>
</head>
<body>
  <h2>🔔 Beacon IMU Sender</h2>
  <p>Server IP: <input type="text" id="ip" value="192.168.1.XXX" placeholder="Mac IP"></p>
  <button id="startBtn" onclick="start()">Start Sensors</button>
  <button id="stopBtn" onclick="stop()" disabled>Stop</button>
  <div id="status">Status: idle</div>
  <div id="data"></div>

<script>
let interval = null;
const status = document.getElementById('status');
const dataDiv = document.getElementById('data');

function start() {
  const ip = document.getElementById('ip').value;
  window.addEventListener('deviceorientation', handleOrientation);
  window.addEventListener('devicemotion', handleMotion);
  interval = setInterval(() => sendData(ip), 100); // 10 Hz
  document.getElementById('startBtn').disabled = true;
  document.getElementById('stopBtn').disabled = false;
  status.textContent = 'Status: streaming @ 10Hz';
}

function stop() {
  window.removeEventListener('deviceorientation', handleOrientation);
  window.removeEventListener('devicemotion', handleMotion);
  clearInterval(interval);
  document.getElementById('startBtn').disabled = false;
  document.getElementById('stopBtn').disabled = true;
  status.textContent = 'Status: stopped';
}

let yaw = 0, pitch = 0, roll = 0, ax = 0, ay = 0, az = 9.8;

function handleOrientation(e) {
  yaw = e.alpha || 0;
  pitch = e.beta || 0;
  roll = e.gamma || 0;
}

function handleMotion(e) {
  const a = e.acceleration || {};
  ax = a.x || 0;
  ay = a.y || 0;
  az = a.z || 9.8;
}

function sendData(ip) {
  const payload = { yaw, pitch, roll, ax, ay, az };
  fetch(`http://${ip}:5051/imu`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  }).then(r => r.json())
    .then(j => {
      dataDiv.innerHTML = `<span class="value">Y:${yaw.toFixed(1)} P:${pitch.toFixed(1)} R:${roll.toFixed(1)} | A:${ax.toFixed(2)},${ay.toFixed(2)},${az.toFixed(2)}</span>`;
    })
    .catch(e => { status.textContent = 'Error: ' + e.message; });
}
</script>
</body>
</html>
```

Servir desde el Mac:

```bash
cd ~/Documents/AlterMundi/Beacon/digital-beacon
python3 -m http.server 8082
```

Y en el teléfono abrí: `http://IP_DEL_MAC:8082/im_sender.html`

**IMPORTANTE:** En iOS, Safari requiere HTTPS para DeviceMotion. Usá ngrok o sirve desde localhost con tunnel.

## Paso 5: Verificar que todo funciona

### En el Mac — logs de hybrid_bridge:

Deberías ver algo como:
```
2026-07-06 15:30:01 | INFO    | hybrid_bridge | ♥ beat #42  72 BPM  (RR: 833ms)
2026-07-06 15:30:02 | INFO    | hybrid_bridge | ♥ beat #43  73 BPM  (RR: 821ms)
2026-07-06 15:30:02 | DEBUG   | hybrid_bridge | /hybrid/imu seq=128 yaw=15.0 pitch=-5.0 roll=0.0
```

### En el Mac — verificar estado vía API:

```bash
curl http://localhost:8080/api/hybrid/status
```

Debería devolver:
```json
{
  "bpm": 72,
  "rr_ms": 833,
  "confidence": 1.0,
  "yaw": 15.0,
  "pitch": -5.0,
  "roll": 0.0,
  "ax": 0.1,
  "ay": 0.0,
  "az": 9.8,
  "mode": 2,
  "seq": 128
}
```

### En el Mac — verificar OSC llega a SuperCollider:

En la terminal de sclang (si está visible) o revisá los logs de `start.sh`:
```
[SC] received /hybrid/hr 72.0 833.0 1.0
[SC] received /hybrid/imu 15.0 -5.0 0.0 0.1 0.0 9.8
```

## Paso 6: Probar efectos creativos

### HR → Master Gain Pulse
- Mové el Fitbit (ejercicio más intenso) para subir BPM
- Debería escucharse un pulso sutil en el volumen general
- A 72 BPM (referencia), no hay cambio. A 102 BPM (+30), +3% gain. A 42 BPM (-30), -3% gain.

### IMU → Rotación Espacial
- Girá el teléfono sobre el eje vertical (yaw)
- Debería rotar la imagen espacial en SuperCollider (azimuth)
- Incliná adelante/atrás (pitch) para modificar distancia

### Modo Híbrido
- Cuando ambos sensores están activos, el sistema automáticamente pasa a mode=2 (Hybrid)
- El audio responde a HR + IMU simultáneamente

## Paso 7: Detener todo

Presioná **Ctrl-C** en la terminal de `start.sh`. El script limpia todos los procesos:
- scsynth
- sclang
- f1_bridge
- hybrid_bridge
- Shaper

## Troubleshooting

### "No BLE device found"
- Asegurate de que el Fitbit esté en **modo ejercicio** (no solo encendido)
- El Fitbit no transmite HR por BLE en modo reposo
- Probá sin `--device` para escanear todos los dispositivos

### " bleak not found"
```bash
cd ~/Documents/AlterMundi/Beacon/digital-beacon
source venv/bin/activate
pip install bleak
```

### "No llega IMU al hybrid_bridge"
- Verificá que el teléfono y el Mac estén en la **misma red WiFi**
- Verificá la IP del Mac: `ifconfig | grep "inet "`
- En iOS, Safari requiere HTTPS para DeviceMotion — usá Chrome o un tunnel

### "No se escucha el efecto de HR"
- El efecto es **sutil** (±3% gain, ±5% shape)
- Subí `HYBRID_HR_GAIN_DEPTH` en `digital_beacon/config.py` para más intensidad
- El HR tiene que variar al menos ±10 BPM del reference (72) para ser notable

### "SuperCollider no recibe OSC"
- Verificá que sclang esté corriendo: `ps aux | grep sclang`
- Verificá puertos: `lsof -i UDP:57120`
- Revisá `beacon.scd` — los OSCdefs híbridos deben estar cargados

## Archivos clave

| Archivo | Propósito |
|---------|-----------|
| `hybrid_bridge.py` | Bridge principal: BLE HR + HTTP IMU → OSC temporal sync |
| `digital_beacon/config.py` | Constantes híbridas (puertos, depths, BPM ref) |
| `digital_beacon/osc_receiver.py` | Handlers OSC en el engine |
| `digital_beacon/state.py` | Estado híbrido thread-safe |
| `digital_beacon/audio_engine.py` | Modulación HR/IMU en audio callback |
| `beacon.scd` | OSCdefs híbridos + mapeos creativos |
| `start.sh` | Launcher con `--hybrid` flag |
| `HYBRID_IMPLEMENTATION.md` | Documentación técnica completa |

## Próximos pasos

- [ ] Calibrar IMU (zero-offset para yaw/pitch/roll)
- [ ] Ajustar profundidades de modulación a gusto
- [ ] Probar con guitarra en vivo (`--live` en lugar de `--file`)
- [ ] Experimentar con HRV (variabilidad de frecuencia cardíaca) para modulación más profunda

¡Breathe with the Pulse! 🌱

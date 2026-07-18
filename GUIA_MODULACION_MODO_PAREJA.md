# Modo Pareja: cómo el movimiento se convierte en sonido

Este documento explica en lenguaje llano qué mide exactamente cada
teléfono, en qué se transforma dentro de `digital_beacon`, y qué se
escucha como resultado — para el Modo Pareja (el MVP de abrazo/baile con
dos teléfonos), no el Modo Híbrido de un solo dispositivo más antiguo.
Cada fórmula de abajo está tomada literalmente del código real
(`digital_beacon/state.py`, `digital_beacon/audio_engine.py`,
`digital_beacon/config.py`), para que este documento se mantenga exacto
mientras se ajustan las constantes — si cambiás una constante, actualizá
también el número acá.

El Modo Pareja solo funciona cuando `config.HYBRID_PAIR_MODE_ENABLED`
(Python) y `hybridPairModeEnabled` (SuperCollider) están ambos activados.
Deliberadamente no usa ninguna entrada de ritmo cardíaco — todo lo que
sigue viene únicamente de la orientación y el acelerómetro de cada
teléfono.

## Los ingredientes crudos (por teléfono)

Cada teléfono transmite seis números a 10 Hz: `yaw, pitch, roll, ax, ay, az`.

- **yaw (orientación/guiñada)** — hacia dónde está mirando la persona,
  *puesto en cero respecto a la dirección que estaba mirando al tocar
  Start* (o Recalibrar). Por eso yaw no es un rumbo de brújula — es
  "cuánto giré desde que empecé", que es lo que realmente importa cuando
  dos personas giran una alrededor de la otra durante un baile lento.
- **pitch (inclinación)** — inclinación adelante/atrás, también puesta en
  cero al tocar Start.
- **roll (rotación lateral)** — se mide actualmente pero todavía no se usa
  en ningún mapeo.
- **ax, ay, az** — aceleración lineal en cada eje. No se usan
  directamente; se combinan en la **magnitud de aceleración** =
  `sqrt(ax² + ay² + az²)`, que es lo que realmente consume todo lo que
  sigue.

## Dos señales derivadas por persona, una compartida entre ambas

Antes de que ocurra cualquier mapeo de sonido, `state.py` mantiene una
ventana móvil de 2 segundos con la magnitud de aceleración de cada
teléfono, y de ahí calcula:

- **Energía de balanceo (sway energy)** (por persona) — el RMS (raíz de
  la media cuadrática) de la magnitud de aceleración de esa persona
  durante los últimos 2 segundos. En palabras simples: "cuánto se está
  moviendo esta persona *en este momento*", suavizado para que un
  sacudón puntual no lo dispare y la quietud lo vuelva a bajar en unos
  2 segundos.
- **Sincronía (synchrony)** (compartida, entre 0 y 1) — la correlación de
  Pearson entre los historiales de magnitud de aceleración de ambas
  personas, en esa misma ventana de 2 segundos. Esto NO es "qué tan
  cerca están" — es "¿se están moviendo con el mismo ritmo al mismo
  tiempo?", sin importar la posición. Balancearse al unísono da un valor
  alto; una persona quieta mientras la otra se mueve da un valor cercano
  a cero; moverse en ritmos opuestos/no correlacionados puede incluso dar
  negativo (se recorta a 0 para uso sonoro).
- **Energía de la pareja (pair energy)** (compartida) — simplemente el
  promedio de la energía de balanceo de ambas personas. "Qué tan viva
  está la sala en este momento", combinando ambos cuerpos.
- **Orientación relativa (relative facing)** (compartida, informativa) —
  la diferencia entre los dos yaw puestos en cero, ajustada al rango
  -180°..180°. Se registra y se muestra en el dashboard, pero todavía no
  está conectada a ningún parámetro de sonido.

## Dónde vive el sonido: tres registros dentro de 32 armónicos

El campo de 32 armónicos se divide en tres zonas. Esta es la idea central
del Modo Pareja: en vez de una mezcla única que cambia igual para todos,
cada bailarín/a tiene su propia porción de sonido que responde
principalmente a *su propio* cuerpo.

| Armónicos | Registro | Impulsado por |
|-----------|----------|----------------|
| 1–10 | Persona A | El propio yaw, pitch y balanceo de A |
| 11–20 | Persona B | El propio yaw, pitch y balanceo de B |
| 21–28 | "Florecimiento" compartido | Solo la sincronía de la pareja |
| 29–32 | Sin modificar | Manual/por defecto — ningún sensor los toca |

### Tu propio registro: paneo, ganancia, inclinación

Para cada persona, a partir de su propio yaw/pitch/balanceo:

- **Posición estéreo (paneo)**: `yaw ÷ 180°`, recortado a ±1, escalado
  por `HYBRID_PAIR_YAW_PAN_RANGE` (0.6) *y* por el control deslizante de
  sensibilidad de paneo del dashboard (0–300%, 100% por defecto). Girar
  para dejar de mirar hacia tu punto de partida hace que el sonido de tu
  registro rote audiblemente hacia ese lado — girar una vuelta completa
  lo desplaza de izquierda a fondo → centro → derecha a fondo → centro.
  Este es el control que se ajustó con el deslizante en la primera prueba
  en vivo — subilo si el paneo se siente demasiado sutil para la sala,
  bajalo si se siente desorientador.
- **Presencia (ganancia)**: `energía_de_balanceo ÷ 3.0` (el nivel de
  movimiento de referencia "a escala completa",
  `HYBRID_PAIR_ENERGY_REF`), recortado a 1.0, multiplicado por
  `HYBRID_PAIR_SWAY_GAIN_DEPTH` (0.5) → hasta **+50% más fuerte** en tu
  propio registro cuanto más te movés. Además, inclinarse
  adelante/atrás suma `pitch ÷ 90° × HYBRID_PAIR_LEAN_GAIN_DEPTH (0.2)` →
  hasta **±20%** más. Quedarse quieto y erguido deja tu registro en su
  nivel base sin refuerzo; balancearse con energía mientras te inclinás
  hacia adelante puede llevarlo hasta cerca de +70% combinado.

### Sentir a tu pareja: filtración cruzada (cross-bleed)

Un rastro del propio movimiento de ganancia de tu pareja (su balanceo +
inclinación, no el tuyo) también se filtra hacia tu registro — pero solo
en proporción a cuán sincronizados estén en ese momento:

`tu_ganancia = 1 + tu_modulación_de_ganancia + (0.35 × sincronía) × su_modulación_de_ganancia`

Con sincronía cero, escuchás solo tu propio movimiento. Con sincronía
perfecta, hasta un 35% del movimiento de ganancia de tu pareja viaja
dentro de tu propio registro — deliberadamente solo en ganancia (nunca en
paneo), para que tu registro siga sonando como *tuyo*, solo que llevando
un rastro de "puedo sentirlos" por debajo. Este es el mecanismo que hace
que caer en sincronía se sienta como algo, incluso antes de que entre la
banda compartida.

### La banda compartida: silencio hasta moverse como uno solo

Los armónicos 21–28 no tienen volumen base propio — su ganancia **es**
directamente el valor de sincronía:

- Sincronía 0.0 → silencio.
- Sincronía 1.0 → ganancia base completa.

A propósito no hay una constante de profundidad separada acá: la
sincronía ya está entre 0 y 1, así que se traduce directamente. Esta
banda es la señal de "recompensa" más legible de todo el sistema —
está en silencio cuando se mueven de forma independiente y florece a
medida que el movimiento se sincroniza, sin necesitar ningún ajuste del
dashboard para escucharlo.

### La sala respirando: energía de la pareja → pulso maestro

La energía combinada de la pareja (promedio del balanceo de ambas
personas) también hace pulsar levemente la **ganancia maestra general**:

`energía_de_pareja ÷ 3.0` (recortado a 1.0) `× HYBRID_PAIR_ENERGY_GAIN_DEPTH (0.25)`
→ hasta **+25%** de volumen general cuando ambas personas se mueven
mucho, bajando de nuevo a medida que se calman. Este es, a propósito, el
cambio más pequeño y de sensación más lenta de todo el mapeo — una
"respiración" de que la sala está viva, no una señal por persona.

## Referencia rápida: todos los números en un solo lugar

| Constante | Valor | Efecto |
|---|---|---|
| `HYBRID_PAIR_ENERGY_REF` | 3.0 m/s² | Magnitud de aceleración tratada como movimiento "a escala completa" (1.0) |
| `HYBRID_PAIR_SWAY_GAIN_DEPTH` | 0.5 | Tu propio balanceo → hasta +50% de ganancia en tu registro |
| `HYBRID_PAIR_LEAN_GAIN_DEPTH` | 0.2 | Tu propia inclinación adelante → hasta ±20% de ganancia |
| `HYBRID_PAIR_YAW_PAN_RANGE` | 0.6 | Tu propio yaw en cero → hasta ±0.6 de paneo (×0–3 con el deslizante en vivo) |
| `HYBRID_PAIR_CROSS_BLEED_DEPTH` | 0.35 | En sincronía completa, hasta 35% del movimiento de ganancia de la pareja se filtra en el tuyo |
| `HYBRID_PAIR_ENERGY_GAIN_DEPTH` | 0.25 | Energía combinada de la pareja → hasta +25% de ganancia maestra |
| (ganancia de la banda compartida) | = sincronía directamente | 0 = silencio, 1 = completo |
| Ventana de movimiento | 2.0 segundos | Ventana móvil detrás de la energía de balanceo y la sincronía |

**Importante**: cada uno de estos números de profundidad/referencia es
una primera estimación, fijada antes de cualquier prueba real de baile
lento. La primera prueba en vivo (que llevó a agregar el deslizante de
sensibilidad de paneo) ya mostró que el rango de paneo necesitaba ajuste
en vivo — es esperable que las constantes relacionadas con la ganancia
necesiten el mismo tipo de ajuste a medida que ocurran más sesiones. Si
algún número de aquí deja de coincidir con `config.py`, confiá en el
código y corregí este documento, no al revés.

## Todo lo que los dos teléfonos nunca afectan

- Ninguna entrada de ritmo cardíaco de ningún tipo — el Modo Pareja la
  pone en cero estructuralmente incluso si hay datos de ritmo cardíaco
  llegando desde otro lado.
- `roll` se mide pero todavía no está mapeado a nada.
- Los armónicos 29–32 quedan permanentemente fuera de este sistema.
- Nunca se usa la dirección absoluta de brújula — solo el propio yaw en
  cero de cada teléfono, por eso quedarse quieto después de Recalibrar
  siempre marca 0° sin importar hacia dónde estés mirando realmente en
  la sala.

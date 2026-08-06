# Handoff al dashboard: de un bot a dos

Prompt autocontenido para el agente que trabaja en el repo del dashboard web.
Copiar desde "INICIO DEL PROMPT" hasta el final.

---

## INICIO DEL PROMPT

Trabajas en el dashboard web de un bot de trading. El backend (repo aparte, no lo
tienes) acaba de cambiar de **un motor a dos**, y la base de datos de Supabase ya
está migrada. El dashboard todavía asume un solo bot por usuario y por eso ahora
muestra datos incompletos o duplicados. Tu tarea es adaptarlo.

No cambies el esquema de la base de datos: **ya está migrado y en producción**.
Tu trabajo es solo de lectura/UI.

### Contexto: qué son los dos bots

Un usuario = una cuenta de MetaTrader 5. Sobre **esa misma cuenta** corren ahora
dos procesos independientes:

| `bot_id` | Nombre | Qué hace | Cómo opera |
|---|---|---|---|
| `m15` | **Sentinel** | Estrategia principal en velas de 15 min. Abre una posición, si va en contra abre una cobertura opuesta que congela la pérdida, y promedia con recovery/rescate | Abre **sin stop loss** por diseño. Su red es la cobertura |
| `m5` | **Grinder** | Scalper en velas de 5 min. Una sola posición viva a la vez, entra y sale rápido | Abre **con stop loss** desde el primer momento |

Se distinguen en el broker por un identificador numérico (`magic`): 100100 el
M15, 100200 el M5. Cada proceso solo ve sus propias posiciones.

**Comparten el margen de la cuenta.** Hay un árbitro que da prioridad absoluta al
M15: cuando el margen aprieta, el M5 deja de abrir y luego se retira. Esto genera
eventos que el dashboard debería mostrar (ver `log_type = 'BUDGET'` más abajo).

### El cambio en la base de datos

Se añadió una columna **`bot_id TEXT NOT NULL DEFAULT 'm15'`** a cinco tablas.
Todas las filas históricas quedaron como `'m15'`, que es exactamente lo que eran.

Claves que cambiaron:

```
bot_instances : PRIMARY KEY (user_id)          ->  PRIMARY KEY (user_id, bot_id)
bot_state     : PRIMARY KEY (user_id)          ->  PRIMARY KEY (user_id, bot_id)
bot_config    : UNIQUE (user_id, symbol)       ->  UNIQUE (user_id, symbol, bot_id)
bot_positions : UNIQUE (user_id, ticket)       ->  sin cambios (+ columna bot_id)
bot_logs      : sin claves                     ->  sin cambios (+ columna bot_id)
```

**Consecuencia inmediata:** donde antes había una fila por usuario, ahora hay
dos. Cualquier consulta con `.single()`, `LIMIT 1` o que asuma unicidad por
`user_id` está devolviendo un resultado arbitrario de los dos.

---

## LA TRAMPA MÁS IMPORTANTE: `bot_state`

Lee esto dos veces. Es donde es fácil mostrar números falsos.

`bot_state` tiene ahora dos filas por usuario, pero **sus columnas no son todas
del mismo tipo de dato**:

| Columna | Ámbito | Qué hacer con las dos filas |
|---|---|---|
| `balance` | **CUENTA** | **NO sumar.** Tomar de una sola fila |
| `equity` | **CUENTA** | **NO sumar.** Tomar de una sola fila |
| `margin_used` | **CUENTA** | **NO sumar.** Tomar de una sola fila |
| `margin_free` | **CUENTA** | **NO sumar.** Tomar de una sola fila |
| `floating_pnl` | **CUENTA** | **NO sumar.** Tomar de una sola fila |
| `symbol` | cuenta | idéntico en ambas |
| `open_positions` | **POR BOT** | **SUMAR** para el total de la cuenta |
| `initial_balance` | **POR BOT** | Cada bot lo fija al arrancar. Para P&L de cuenta, usar el de `m15` |
| `updated_at` | por bot | El más reciente indica si hay algún bot vivo |

Sumar `equity` de las dos filas **duplicaría el capital del usuario en pantalla**.
Es el error más grave posible aquí.

Los dos bots escriben su fila cada 15 segundos en momentos distintos, así que los
valores de cuenta pueden diferir en unos segundos de desfase. Para las métricas
de cuenta, usa preferentemente la fila de `m15` (es el motor principal y siempre
está activo); si no existe, cae a la de `m5`.

```sql
-- Métricas de CUENTA (una sola fila, preferencia m15)
select balance, equity, margin_used, margin_free, floating_pnl, initial_balance
  from bot_state
 where user_id = :uid
 order by case when bot_id = 'm15' then 0 else 1 end
 limit 1;

-- Posiciones abiertas TOTALES (sí se suma)
select coalesce(sum(open_positions), 0) as total,
       coalesce(sum(open_positions) filter (where bot_id = 'm15'), 0) as m15,
       coalesce(sum(open_positions) filter (where bot_id = 'm5'), 0)  as m5
  from bot_state
 where user_id = :uid;
```

Métrica derivada útil: **nivel de margen** = `equity / margin_used * 100`. Cuando
baja, es el árbitro de margen el que empieza a frenar al M5. Merece la pena
mostrarlo.

---

## Tabla por tabla: qué cambiar

### `bot_instances` — el interruptor ON/OFF

Dos filas por usuario, **cada una con su propio interruptor**.

```
user_id, bot_id, label, account_login, is_active, bot_status, last_heartbeat, created_at, updated_at
```

- `is_active` (BOOLEAN) lo togglea **el usuario** desde el dashboard:
  - `true` = el bot abre y gestiona
  - `false` = *close-only*: no abre nada nuevo, pero **sigue gestionando y
    cerrando** lo que ya tiene. No es una pausa dura
- `bot_status` (TEXT) lo escribe **el bot**: `RUNNING` | `CLOSE_ONLY` | `FLAT` |
  `STOPPED` | `ERROR`
- `last_heartbeat` (TIMESTAMPTZ) lo escribe el bot cada 15 s. Si tiene más de
  ~60 s, el proceso está caído aunque `bot_status` diga `RUNNING`

**Cambios de UI:**
- Dos interruptores separados, claramente etiquetados (Sentinel M15 / Grinder M5)
- Dos indicadores de estado y dos de "online/offline" por heartbeat
- El `UPDATE` del toggle **debe filtrar por `bot_id`**, o apagarás los dos:
  ```sql
  update bot_instances set is_active = :v, updated_at = now()
   where user_id = :uid and bot_id = :bot;   -- <- el bot_id es obligatorio
  ```
- Es normal y esperado que el M5 esté apagado y el M15 encendido. No lo trates
  como un estado de error

### `bot_positions` — posiciones vivas e históricas

```
id, user_id, bot_id, ticket, symbol, position_type, op_type, comment, lots,
open_price, open_time, sl, tp, current_price, profit, swap, status,
close_price, close_time, updated_at
```

`UNIQUE (user_id, ticket)` no cambió: los tickets de MT5 son únicos por cuenta,
así que los dos bots nunca colisionan. `bot_id` sirve para filtrar y agrupar.

`op_type` clasifica la posición y **te dice el rol de cada leg**:

| `op_type` | `bot_id` | Qué es |
|---|---|---|
| `ENTRADA` | m15 | Posición inicial del ciclo |
| `PROTECCION` | m15 | **Cobertura.** Abre en sentido opuesto y congela la pérdida |
| `RECOVERY` | m15 | Promedio tras la cobertura |
| `RESCATE` | m15 | Leg de rescate en drawdown profundo. El comment indica nivel (L1/L2/L3) |
| `GRINDER` | m5 | Scalp del M5 |
| `OPERACION` | — | Sin clasificar (histórico antiguo) |

**Cambios de UI:**
- Columna o badge de bot en la tabla de posiciones
- Filtro por bot: Todas / Sentinel M15 / Grinder M5
- Si agrupas por ciclo o cesta, agrupa **solo dentro de `bot_id='m15'`**: los
  scalps del M5 no forman parte de ninguna cesta
- Diferencia visual para `PROTECCION`: es el leg que sostiene el riesgo del
  ciclo. Que se vea de un vistazo si existe o no
- El M5 siempre tiene `sl != 0`; el M15 normalmente tiene `sl = 0`. **No lo
  marques como anomalía en el M15**: es su diseño, su red es la cobertura, no un
  stop

Nota sobre datos antiguos: hay filas con `op_type`, `lots` y `comment` en NULL de
antes de que existiera este seguimiento. Trátalas como histórico incompleto, no
como error. Algunas llevan el sufijo `[cuenta previa al reset]` en el comment.

### `bot_logs` — stream de eventos

```
id, user_id, bot_id, symbol, log_type, message, price, lots, balance, ticket, created_at
```

Índice disponible: `(user_id, bot_id, created_at DESC)`. Úsalo.

`log_type` completo, con lo que significa cada uno:

| `log_type` | Bot | Significado | Severidad sugerida |
|---|---|---|---|
| `SYSTEM` | ambos | Arranque, parada, cambios de estado | info |
| `ERROR` | ambos | Fallo de orden, rechazo del broker, hedge fallido | **error** |
| `ENTRADA` | m15 | Apertura de la posición inicial | éxito |
| `PROTECCION` | m15 | Apertura de la cobertura | aviso |
| `RECOVERY` | m15 | Apertura de recovery | info |
| `RESCATE` | m15 | Apertura de leg de rescate | **aviso fuerte** |
| `HEALER` | m15 | Cierre parcial de un leg perdedor | aviso |
| `UNWIND` | m15 | Cierre de una ganadora para asegurar beneficio | info |
| `CIERRE` | m15 | Cierre de un leg dentro del cierre de ciclo | info |
| `EXITO` | m15 | **Cierre de ciclo completo con su P&L** | éxito |
| `GRINDER` | m5 | Apertura/cierre de scalp | info |
| `VCB` | ambos | Circuit breaker de volatilidad: vela anómala, se bloquean aperturas | aviso |
| `BUDGET` | **m5** | **NUEVO.** El árbitro de margen bloqueó una apertura del M5, o lo obligó a cerrar y retirarse | aviso |
| `SHADOW` | ambos | Modo simulación: decidió pero no envió orden | info, atenuado |

`BUDGET` es nuevo y aún no aparece en datos históricos. Añádelo al mapa de
colores/iconos igualmente.

**Cambios de UI:**
- Filtro por bot en el stream
- Badge de bot en cada línea si muestras el stream unificado
- `ERROR` y `RESCATE` deberían destacar; son los que anticipan problemas

### `bot_config` — parámetros de estrategia

Dos filas por usuario+símbolo, una por bot. **Los conjuntos de parámetros son casi
disjuntos**, así que un editor genérico que muestre todas las columnas de la fila
va a enseñar decenas de campos inútiles.

- **35 parámetros con prefijo `m5_`**: los usa **solo** la fila `bot_id='m5'`.
  La fila `m15` los tiene pero los ignora
- **~55 parámetros sin prefijo**: los usa **solo** la fila `m15` (`base_lots`,
  `hedge_dist`, `use_healer`, `max_net_lots`, ...). La fila `m5` los ignora
- **4 compartidos**, que los dos motores interpretan: `equity_weight`,
  `margin_cap_pct`, `ml_no_add`, `ml_flatten`

Regla simple para el editor:

```
si bot_id = 'm5'  -> mostrar columnas que empiecen por 'm5_' + las 4 compartidas
si bot_id = 'm15' -> mostrar columnas que NO empiecen por 'm5_'
```

Excluir siempre de la UI: `id`, `user_id`, `bot_id`, `updated_at`.

Ten en cuenta que `bot_config` también tiene columnas de control (`is_active`,
`status`, `symbol`) que no son parámetros de estrategia.

---

## Qué construir

Prioriza en este orden:

**1. No mentir con los números.** Antes que nada, arregla toda consulta que
asuma una fila por usuario. Especialmente cualquier sitio donde se sume o se
promedie `bot_state`. Un equity duplicado es peor que una UI fea.

**2. Selector de bot.** Un control global: **Cuenta (ambos) / Sentinel M15 /
Grinder M5**. La vista "Cuenta" muestra los agregados correctos según la tabla de
ámbitos de arriba; las otras dos filtran por `bot_id`.

**3. Dos tarjetas de estado**, una por bot: nombre, interruptor `is_active`,
`bot_status`, indicador de heartbeat, número de posiciones abiertas y P&L
flotante de sus propias posiciones (`sum(profit + swap)` sobre `bot_positions`
con `status='OPEN'` y ese `bot_id` — este sí se calcula por bot y sí se puede
sumar).

**4. Nivel de margen visible.** `equity / margin_used * 100`. Es la métrica que
gobierna cuándo el M5 se frena y se retira. Con un indicador de color basta.

**5. Filtros de bot** en la tabla de posiciones y en el stream de logs.

**6. Editor de parámetros** consciente del bot, según la regla de prefijos.

## Verificación antes de dar por terminado

- [ ] Ningún `.single()` ni `LIMIT 1` sobre `bot_state`, `bot_instances` o
      `bot_config` sin filtrar por `bot_id`
- [ ] El equity mostrado coincide con el de MetaTrader 5, **no es el doble**
- [ ] Apagar el M5 desde la UI **no** apaga el M15, y viceversa
- [ ] Con solo el M15 corriendo (caso habitual hoy), el dashboard se ve completo
      y no muestra huecos ni errores por la ausencia de datos del M5
- [ ] Las posiciones del M5 aparecen con su badge y se pueden filtrar
- [ ] `BUDGET` tiene color/icono asignado aunque todavía no haya datos

## Datos para probar

Hoy existe un usuario con las dos filas creadas. El M15 lleva histórico real
(223 posiciones cerradas, ~630 logs); el M5 puede tener poco o ningún dato según
cuándo lo mires. **Prueba explícitamente el caso "un bot sin datos"**: es el
estado normal durante las próximas semanas y no debe romper ninguna vista.

## FIN DEL PROMPT

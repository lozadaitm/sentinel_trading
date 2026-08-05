# Plan: coexistencia M15 (Sentinel) + M5 (Grinder) en una sola cuenta

> **ESTADO: código implementado.** Todo el código de las fases 0-7 está en el repo
> y verificado por `tests/test_m5_and_budget.py` (45 comprobaciones, todas en verde).
>
> **Pendiente de acción manual** (escrituras a producción, no las hace el agente):
>
> | Acción | Dónde |
> |---|---|
> | Correr la auditoría de margen en la máquina con MT5 | `python -m scripts.audit_margin` |
> | Correr la auditoría de señal M5 | `python -m scripts.audit_m5_signals` |
> | Aplicar la migración de `bot_id` a Supabase | `migrations/002_bot_id.sql` |
> | Crear las filas `bot_id='m5'` (bloque SEED) | `migrations/002_bot_id.sql` |
> | Actualizar el dashboard para filtrar por `bot_id` | fuera de este repo |
> | Apagar el grinder del M15 (**requiere tu confirmación**) | `migrations/003_disable_m15_grinder.sql` |
> | Rollout shadow → live | Fase 8 |


**Objetivo.** Separar el grinder M5 del Sentinel M15 en dos procesos independientes que
comparten una única cuenta MT5 por usuario, con un gobierno de margen explícito que
garantice que el M15 nunca se queda sin margen para su Hedge Lock.

**Estado de partida.** El grinder vive dentro de `bot/strategy.py` (`_run_grinder`,
`_grinder_trailing`), solo se activa con `core_count >= 4`, y sus deals ganadores
alimentan el presupuesto de amputación del Healer (`_check_healing` filtra por
`d.magic == self.b.magic`).

**Decisión de arquitectura.** El M5 se basa en `Grinder_Anterior.mq5`
(`M5_Gold_SmartCut_v13_02`), no en el grinder actual del bot. El grinder actual es una
versión destripada cuyo propósito real es generar combustible para el Healer, no
producir alpha.

---

## Principios de diseño

1. **Asimetría de criticidad.** El M15 no tiene SL por diseño
   (`docs/memory/bot-design-constraints.md`); su Hedge Lock es el único mecanismo que
   congela la pérdida. El M5 tiene SL y sus scalps son desechables. Ante escasez de
   margen, **el M5 cede siempre**.
2. **La coordinación vive en el broker, no en la DB.** Ambos procesos leen
   `mt5.positions_get()` y discriminan por magic. Latencia cero, sin race de red, y
   sigue funcionando si un proceso muere. Supabase queda solo para observabilidad.
3. **Órdenes protectoras exentas.** El Hedge Lock nunca se bloquea por presupuesto.
   Cualquier cap que pueda impedirlo es un bug, no una feature.
4. **Cada fase es commiteable y reversible por sí sola** (ver
   `docs/memory/workflow-implement-then-commit.md`).

---

## Fase 0 — Medición

No se toca código de trading. Produce los números que calibran las fases 3 y 5.

### 0.1 `scripts/audit_margin.py`

Volcado read-only de:

| Dato | Fuente | Para qué |
|---|---|---|
| `margin_hedged`, `margin_hedged_use_leg` | `mt5.symbol_info(SYMBOL)` | Si es 0, el par Op1+Hedge no cuesta margen extra y el presupuesto es holgado. Si cobra completo, el lock es lo más caro del sistema. |
| `leverage`, `margin_so_call`, `margin_so_so` | `mt5.account_info()` | Calibrar la escalera de degradación contra el stop-out real. |
| `order_calc_margin` para 0.01 / 0.50 / 1.00 / 3.00 lotes | MT5 | Coste unitario real. |
| `max_net_lots`, `max_entry_lots`, `max_recovery_lots`, `max_rescue_legs` | fila real de `bot_config` | `DEFAULTS` en `strategy.py` es solo fallback; el valor efectivo puede diferir. |

**Peor caso bruto del M15.** Con `max_net_lots = 1.0` activo, `_net_cap_ok`
(`strategy.py:169-182`) estrangula la escalera: el bruto realista pico es **~3-4 lotes**
con neto 1.0. La escalera de ~10 lotes (Op1 1.0 + Hedge 1.0 + Op3 2.0 + 3 rescates ×2.0)
solo se materializa si `max_net_lots = 0` (tope desactivado). Verificar cuál es el caso.

**Criterio de salida:** si el margen del peor caso bruto del M15 supera el equity de la
cuenta, el M15 no puede completar su propia escalera y el presupuesto del M5 es
irrelevante — hay que bajar los caps del M15 antes de seguir.

### 0.2 `scripts/audit_m5_signals.py`

Replay sobre M5 histórico contando señales de reversión bajo tres fórmulas de filtro de
ancho de banda:

- **(a) Anterior tal cual:** `bbWidth < 400` puntos fijos.
- **(b) % de precio:** `bbWidth < price × pct`.
- **(c) Bandas ATR:** sustituir `CalculateDynamicTolerance` por `tolerance = ATR_M5 × k × adxFactor`.

Contexto del problema. En Anterior, la rama de reversión exige `adx < 30`, luego
`adxFactor = (50-adx)/50 > 0.4`, luego `tolerance = price × 0.002 × adxFactor` y
`bbWidth = 2 × tolerance`. A precio 2200 el filtro de 400 pts solo dejaba pasar una
ventana de `adx ≈ 27-30`; a precio 4000 esa ventana se cierra por completo
(`bbWidth > 640 pts` siempre). **El modo Scalper hoy no dispara nunca; solo corre el
Surfer.**

**Entregable:** tabla de frecuencia de señal por mes bajo cada fórmula, para elegir
formulación y calibrar el umbral en Fase 5.

---

## Fase 1 — Fix del Hedge sin margen

Bug latente, independiente del resto. Se despliega ya.

`bot/strategy.py:936-943` actual:

```python
if loss_pts >= active_hedge_dist:
    self.b.modify_sl(p, 0)          # limpia el SL...
    if p.type == mt5.POSITION_TYPE_BUY:
        if self.b.check_free_margin(p.volume, mt5.ORDER_TYPE_SELL):
            self.b.market_order(...)  # ...y si el margen falla, no pasa nada
```

Si el margen no alcanza: borró el SL y no abrió el hedge. Posición desnuda, sin SL, sin
cobertura, sin log. Hoy es improbable; con un M5 compitiendo por margen deja de serlo.

**Cambio:**

1. Abrir el hedge **primero**.
2. Limpiar el SL solo si el hedge confirmó (`retcode == TRADE_RETCODE_DONE`).
3. Si el hedge falla: `logger.write("ERROR", ...)` con el motivo (margen / retcode) y
   dejar el SL intacto. El bucle reintenta en el siguiente tick.

**Verificación:** `SHADOW_MODE=true` + forzar `check_free_margin` a `False` en una
sesión de prueba; comprobar que aparece el ERROR y que el SL sobrevive.

---

## Fase 2 — Primitivas de margen en `broker.py`

Sin cambio de comportamiento. Commit aislado.

```python
def positions_all(self)                    # todas las del símbolo, sin filtrar magic
def positions_of_magic(self, magic)        # las de un magic concreto
def margin_level(self)                     # account_info().margin_level
def stop_out_levels(self)                  # (margin_so_call, margin_so_so)
def hedged_margin_rate(self)               # symbol_info().margin_hedged / _use_leg
def order_margin(self, order_type, lots)   # order_calc_margin al precio actual
def magic_margin(self, magic)              # suma de order_margin sobre las posiciones de ese magic
def own_margin(self)                       # magic_margin(self.magic)
```

`positions()` (filtrado por magic propio) se mantiene intacto: es la base de toda la
lógica existente.

---

## Fase 3 — `bot/budget.py`: MarginGovernor

Colaborador independiente, no clase base. Ambos motores lo instancian.

```python
KIND_PROTECTIVE = "protective"   # Hedge Lock: exento de todo cap
KIND_ADDITIVE   = "additive"     # entry / recovery / rescue / scalp

POSTURE_NORMAL  = "NORMAL"       # opera normal
POSTURE_NO_ADD  = "NO_ADD"       # no abre aditivas; gestiona y cierra
POSTURE_FLATTEN = "FLATTEN"      # cierra lo propio y se queda fuera

class MarginGovernor:
    def __init__(self, broker, param_getter, role, own_magic, peer_magics): ...
    def posture(self) -> str: ...
    def peer_reserve(self) -> float: ...
    def can_open(self, lots, order_type, kind) -> bool: ...
```

### 3.1 Reserva de protección (el mecanismo central)

Antes de abrir cualquier aditiva, el motor **junior** (M5) descuenta el margen que el
M15 necesitará para su próxima orden protectora:

```
m15_pos   = broker.positions_of_magic(MAGIC_M15)
unhedged  = abs(neto de m15_pos)                       # lo que aún no está cubierto
reserva   = broker.order_margin(lado_opuesto, unhedged) * peer_reserve_mult
disponible = broker.margin_free() - reserva
```

`peer_reserve_mult` por defecto **1.5** (colchón para slippage y para el siguiente leg).
El M15 (senior) usa `peer_reserve() == 0`: no reserva para nadie.

### 3.2 Techo propio

```
own = broker.own_margin()
req = broker.order_margin(order_type, lots)
if own + req > equity * (margin_cap_pct / 100): return False
```

### 3.3 Escalera de degradación

| Margin level | M5 | M15 |
|---|---|---|
| ≥ 400% | NORMAL | NORMAL |
| < 400% | NO_ADD | NORMAL |
| < 250% | FLATTEN | NORMAL |
| < 200% | FLATTEN | NO_ADD (sin recovery/rescate) |
| cualquiera | — | **Hedge Lock siempre permitido** |

Umbrales por parámetro; calibrar contra `margin_so_call` / `margin_so_so` reales de la
Fase 0. La forma de la escalera es lo que importa, no los números iniciales.

### 3.4 Cableado en el M15

Sustituir `self.b.check_free_margin(...)` por `self.gov.can_open(..., KIND_ADDITIVE)` en:

- `strategy.py:904` / `:908` — entry Op1
- `strategy.py:978` — recovery Op3
- `strategy.py:708` / `:720` — rescate Op4
- `strategy.py:528` — grinder (mientras siga vivo)

Y por `KIND_PROTECTIVE` en `strategy.py:939` / `:942` — Hedge Lock.

`peer_magics=[]` en este punto: sin M5 todavía, el único cambio observable es la
escalera. Nuevo `log_type` **`BUDGET`** para cada bloqueo (añadir a `LOG_COLORS` en
`tui.py` y a la taxonomía de `docs/memory/logging-taxonomy-and-ticket.md`).

---

## Fase 4 — DB multi-bot (`bot_id`)

Bloqueo real hoy: `bot_instances.user_id` y `bot_state.user_id` son **PRIMARY KEY**, y
`bot_config` es `UNIQUE (user_id, symbol)`. Dos procesos con el mismo `USER_ID` y el
mismo `SYMBOL` se pisan el heartbeat, el estado y la config.

| Tabla | Clave actual | Clave nueva | Nota |
|---|---|---|---|
| `bot_instances` | PK `user_id` | PK `(user_id, bot_id)` | interruptor `is_active` por bot |
| `bot_config` | UNIQUE `(user_id, symbol)` | UNIQUE `(user_id, symbol, bot_id)` | params M5 ≠ M15 |
| `bot_state` | PK `user_id` | PK `(user_id, bot_id)` | snapshot por bot |
| `bot_positions` | UNIQUE `(user_id, ticket)` | igual + columna `bot_id` | tickets ya son únicos; `bot_id` para filtrar en el dashboard |
| `bot_logs` | insert-only | + columna `bot_id` | filtrado en el dashboard |

**Migración** (`migrations/002_bot_id.sql`):

1. `ALTER TABLE ... ADD COLUMN bot_id TEXT NOT NULL DEFAULT 'm15'` en las cinco tablas.
2. Backfill implícito por el `DEFAULT` (todas las filas existentes quedan `'m15'`).
3. Drop + recreate de PK/UNIQUE con la clave compuesta.
4. Índices: `(user_id, bot_id)` en `bot_logs` y `bot_positions`.
5. Revisar políticas RLS que referencien las claves modificadas.

**Código:**

- `config.py`: `BOT_ID = os.environ.get("BOT_ID", "m15")`, `MAGIC_M15`, `MAGIC_M5`,
  y `PEER_MAGICS` derivado (los magics que no son el propio).
- `db.py`: añadir `.eq("bot_id", config.BOT_ID)` en `load_config`, `get_instance`,
  `report`, `get_state_initial_balance`, `get_open_tickets`; `bot_id` en las filas de
  `report_state`, `upsert_positions`, `log_sink`; cambiar `on_conflict` a
  `"user_id,bot_id"` (bot_state) y `"user_id,ticket"` se mantiene (bot_positions).

**Coordinación:** el dashboard web consume estas tablas
(`docs/dashboard-handoff.md`). Requiere addendum antes de desplegar la migración.

---

## Fase 5 — Motor M5 (`bot/strategy_m5.py` + `bot/main_m5.py`)

Port de `Grinder_Anterior.mq5` reutilizando `Broker`, `Logger`, `Database`, `config`.

### 5.1 Se conserva de Anterior

| Elemento | Detalle |
|---|---|
| Indexación `[1]` | decisiones sobre vela **cerrada**; sin repintado |
| SL por ATR | `slDist = ATR_M5 × sl_atr`, piso `hard_sl_points` |
| Gran Hermano M15 | EMA(50) M15, 3 barras monótonas → veta operar contra la tendencia mayor |
| Lote dinámico | `(balance/1000) × lots_per_1000`, cap `max_lot_cap` |
| `min_margin_level` | gate de nivel de margen (clave para la convivencia) |
| Filtro horario | `start_hour` / `end_hour` |
| Time-stop | 45 min si pierde |
| Trailing 2 velocidades | **con** `trail_step` (histéresis) y guarda `SYMBOL_TRADE_STOPS_LEVEL` |
| `lastOpWasWin` | memoria de racha para el Surfer |
| Una posición a la vez | `CountMyPositions() > 0 → return` |

### 5.2 Se corrige

| Problema en Anterior | Fix |
|---|---|
| `InpMaxBBWidth = 400` puntos fijos → reversión muerta a precio actual | Formulación elegida en Fase 0 (bandas ATR, preferente) |
| Sin filtro de spread | `m5_max_spread` |
| Sin circuit breaker de volatilidad | Portar VCB de `strategy.py:143-158` |
| `ORDER_FILLING_FOK` | `Broker.market_order` ya usa IOC |
| Sin gobierno de margen | `MarginGovernor(role="m5")`, todas las aperturas `KIND_ADDITIVE` |

### 5.3 Entrypoint

`bot/main_m5.py` reutiliza `setup()` / `trading_loop()` de `main.py` parametrizando la
clase de motor. `run_instance.ps1` no necesita cambios: ya carga cualquier `.env`.

### 5.4 HUD

`M5Engine` publica `self.hud` con la misma forma que `SentinelEngine._build_hud` (gates
"actual vs requerido") para que la TUI lo consuma sin cambios estructurales.

---

## Fase 6 — Apagar el grinder del M15

**Runtime, no código.** El parámetro ya existe: `use_grinder` (`strategy.py:501`).

1. `bot_config` fila `bot_id='m15'` → `use_grinder = false`.
2. **Consecuencia esperada:** el presupuesto del Healer baja, porque los verdes del
   grinder eran su combustible principal (`_check_healing` filtra por magic propio).
   Dado `docs/memory/healer-unwind-hedge-cascade.md` (Healer + Unwind desarmando el
   hedge = los dos blowups), esto es **de-risking deliberado**, no un efecto lateral.
3. **Monitorizar 2 semanas:** amputaciones/semana (`log_type='HEALER'`), P&L por ciclo,
   profundidad máxima de cesta.
4. **Limpieza de código** solo tras confirmar estabilidad: borrar `_run_grinder`,
   `_grinder_trailing`, `_is_grinder` y las exclusiones en `_net_exposure`,
   `_apply_healing`, `_check_rescue`, `core_count`. Elimina de paso la fragilidad de la
   identidad por comment con fallback a lote 0.05.

**No** conectar los deals del M5 al Healer del M15. Ese acoplamiento es justo lo que
este plan elimina.

---

## Fase 7 — Operación

- **Un solo `.env` por usuario.** El M5 no lleva fichero propio: se declara con
  `BOTS=m15,m5` en el `.env` del usuario, y el launcher inyecta `BOT_ID` y
  `MAGIC_NUMBER` desde `MAGIC_M15`/`MAGIC_M5`. Duplicar el `.env` significaba
  duplicar credenciales de Supabase y MT5, con el riesgo de que se
  desincronicen.
- `launch_all.ps1` ya levanta un proceso por `.env` — funciona sin cambios, pero el
  `.env` del M5 debe indicar `-Module bot.main_m5`. Alternativa: variable `MODULE` en el
  `.env` leída por `run_instance.ps1`.
- TUI: `bot/tui.py` parametrizado por módulo de motor (`--bot m5`), o `tui_m5.py`.
- Añadir `BUDGET` a `LOG_COLORS` en `tui.py`.

---

## Fase 8 — Rollout

| Paso | Criterio de avance |
|---|---|
| M5 en `SHADOW_MODE=true`, 1 semana | Revisar `bot_logs` (`bot_id='m5'`): frecuencia de señal razonable, cero bloqueos `BUDGET` inesperados |
| M5 live con `m5_max_lot_cap` al mínimo del broker | 2 semanas sin que el M15 registre un solo bloqueo de Hedge Lock |
| Subida gradual de `m5_max_lot_cap` | Margin level nunca por debajo de 400% por causa del M5 |

**Criterios de aborto (revertir a M5 apagado):**
- Cualquier ERROR de Hedge Lock por falta de margen.
- Margin level por debajo del umbral `FLATTEN` más de una vez por semana.
- Stop-out o margin call.

---

## Parámetros nuevos en `bot_config`

### Fila `bot_id='m15'`

| Param | Default | Nota |
|---|---|---|
| `equity_weight` | 0.80 | sizing sobre `equity × weight` |
| `margin_cap_pct` | 60 | techo de margen propio |
| `ml_stop_additive` | 200 | sin recovery/rescate bajo este margin level |

### Fila `bot_id='m5'`

| Grupo | Params |
|---|---|
| Gestión | `m5_lots_per_1000`=0.02, `m5_max_lot_cap`=0.50, `m5_min_margin_level`=150 |
| Gran Hermano | `m5_use_bigbro`=true, `m5_bigbro_period`=50 |
| Riesgo | `m5_sl_atr`=2.0, `m5_max_trade_min`=45, `m5_hard_sl_points`=1000 |
| Scalper | `m5_ma_period`=50, `m5_adx_period`=14, `m5_rsi_period`=14, `m5_base_tolerance`=0.20, `m5_rsi_os`=30, `m5_rsi_ob`=70, `m5_max_bb_width_atr`=**Fase 0** |
| Surfer | `m5_trend_adx`=30, `m5_surfer_rsi_max`=85, `m5_force_rsi_buy`=55, `m5_force_rsi_sell`=45 |
| Tiempo | `m5_use_time_filter`=true, `m5_start_hour`=1, `m5_end_hour`=23 |
| Trailing | `m5_trail_start`=100, `m5_trail_dist`=50, `m5_trail_step`=10, `m5_turbo_trigger`=300, `m5_turbo_dist`=150 |
| Nuevos | `m5_max_spread`=350, `m5_use_vcb`=true, `equity_weight`=0.20, `margin_cap_pct`=10, `ml_no_open`=400, `ml_flatten`=250, `peer_reserve_mult`=1.5 |

---

## Riesgos

| Riesgo | Mitigación |
|---|---|
| Migración `bot_id` rompe el dashboard web | Addendum de handoff + desplegar migración y dashboard coordinados |
| El M5 consume margen que el M15 necesita | Reserva de protección (3.1) + escalera (3.3) + `max_lot_cap` mínimo en rollout |
| Apagar el grinder degrada el Healer más de lo previsto | Fase 6 es un flag de runtime: revertir es un UPDATE, no un deploy |
| `margin_hedged` cobra completo → el lock del M15 es carísimo | Se detecta en Fase 0, antes de escribir código |
| Reversión M5 sigue sin disparar tras el fix de banda | Fase 0.2 lo cuantifica antes de portar |
| Dos procesos escriben `bot_positions` del mismo ticket | Imposible: cada uno solo ve sus magics vía `positions()` |

---

## Orden de ejecución

```
Fase 0  Medición                    (bloquea calibración de 3 y 5)
Fase 1  Fix Hedge sin margen        (independiente — desplegar ya)
Fase 2  Primitivas broker           (sin cambio de comportamiento)
Fase 3  MarginGovernor + M15
Fase 4  DB bot_id                   (coordinar con dashboard)
Fase 5  Motor M5
Fase 6  use_grinder=false           (flag de runtime)
Fase 7  Operación / TUI / launcher
Fase 8  Rollout shadow → live
```

Fases 1 y 2 son paralelizables con la 0. La 4 puede adelantarse a la 3 si el dashboard
necesita lead time.

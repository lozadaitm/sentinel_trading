# Handoff — Dashboard Web de Sentinel

> **Cómo usar este archivo.** Es un **prompt autocontenido** para una segunda instancia de Claude
> Code que va a construir, desde cero y en un **repositorio separado**, el dashboard web del bot
> "Sentinel / HyperGrinder v20". Copia el bloque "PROMPT" completo (o pásale este archivo) a esa
> instancia. No necesita acceso a esta conversación ni al repo del bot: todo el contexto y el
> contrato de datos están aquí. Las rutas `bot/...` que se citan son del repo del bot (referencia).

---

## PROMPT

### CONTEXTO — Vas a construir el Dashboard Web de "Sentinel"

Eres una instancia de Claude Code encargada de **crear desde cero** el dashboard web de **Sentinel**
(bot de trading "HyperGrinder v20"). Trabajas en un **repositorio NUEVO y separado**; el bot ya
existe en otro repo y **no lo modificas** — solo consumes su base de datos Supabase. Este documento
te da todo el contexto y el contrato de datos. Idioma del proyecto: **español** (UI, commits,
comentarios). Commits estilo Conventional Commits con scope (`feat(dashboard): ...`).

**Qué es Sentinel.** Un bot Python (port de un EA MQL5) que opera `XAUUSD+` en MetaTrader 5 (broker
Vantage) con una estrategia martingala/grid con cobertura ("Hedge Lock"), "Healer" (cierres parciales)
y grinder (scalping). Corre en un VPS Windows. **Modelo multi-instancia: 1 proceso = 1 usuario =
1 cuenta Vantage = 1 terminal MT5.** Cada usuario controla su bot desde Supabase.

**Regla de oro de seguridad.** El bot usa la **service-role key** (bypassa RLS). El dashboard
**NUNCA** debe exponer la service-role key en el navegador. El cliente usa el **JWT del usuario**
(rol `authenticated`) y la RLS lo aísla a sus propias filas. La vista admin usa service-role
**solo del lado servidor** (Server Components / Route Handlers), nunca en el bundle del cliente.

### DECISIONES YA TOMADAS (no re-preguntes)

- **Stack:** Next.js (App Router) + TypeScript + Tailwind CSS + `@supabase/ssr` + `@supabase/supabase-js`.
  Auth SSR con cookies. Supabase Realtime para vivo. Deploy objetivo: Vercel.
- **Dos audiencias:**
  1. **Cliente final (self-service):** login con email/password (Supabase Auth). Ve y controla
     **solo su instancia** (RLS `auth.uid() = user_id`).
  2. **Admin (operador):** vista agregada de **todas** las instancias. Implementar con service-role
     del lado servidor, gateada por un allowlist de emails admin (env var, p.ej. `ADMIN_EMAILS`).
     Nunca query admin desde el cliente.
- **Repositorio separado**, monorepo NO.

### CONTRATO DE DATOS (Supabase) — esto es lo que consumes

Cuatro tablas en el schema `public`. Todas con FK a `auth.users(id)` y RLS por usuario.

**`bot_instances`** — interruptor maestro, 1 fila por usuario:
- `user_id` UUID (PK), `label` TEXT, `account_login` BIGINT.
- `is_active` BOOLEAN — **lo togglea el USUARIO** (el dashboard hace `UPDATE`): `true`=operando,
  `false`=close-only (no abre, sigue gestionando/cerrando; NO es pausa dura).
- `bot_status` TEXT — lo escribe el bot: `RUNNING | CLOSE_ONLY | FLAT | ERROR | STOPPED`.
- `last_heartbeat` TIMESTAMPTZ — lo escribe el bot cada ~15 s. **Online/offline** = `now - last_heartbeat < ~45 s`.
- `force_close` BOOLEAN — **cierre forzado** (migración 004): lo setea el USUARIO (el dashboard hace
  `UPDATE { is_active: false, force_close: true }` sin filtro de `bot_id` → ambos motores); el bot
  lo ejecuta en su refresh de control (~3 s), cierra todas sus posiciones asumiendo el flotante y lo
  resetea a `false`. NO es un stop automático: siempre lo dispara el usuario.
- `created_at`, `updated_at`.
- RLS: `FOR ALL` al dueño → el dashboard del usuario puede leer y **actualizar `is_active`/`force_close`**.

**`account_settings`** — preferencias a nivel CUENTA, 1 fila por usuario, SIN `bot_id` (migración 004):
- `user_id` UUID (PK), `initial_deposit` DOUBLE (**sin uso, siempre NULL**: se decidió
  auto-detectar la base con `bot_state.initial_balance`, capturado de MT5 en la primera corrida),
  `profit_target_pct` DOUBLE (% objetivo sobre la base, medido en EQUITY; NULL = desactivado),
  `target_reached_at` TIMESTAMPTZ, `updated_at`.
- `target_reached_at` lo estampa el BOT al alcanzar el objetivo (claim atómico entre m15/m5 → un
  solo email; ver `bot/main.py::_check_profit_target` y `bot/notify.py`); en ese momento apaga
  ambos motores (`is_active=false` = close-only). El dashboard lo LIMPIA al guardar un objetivo
  nuevo (re-armar). RLS: `FOR ALL` al dueño.

**`bot_config`** — ~60 parámetros de estrategia por `(user_id, symbol)`, `UNIQUE(user_id, symbol)`:
- Control: `id`, `user_id`, `symbol`, `is_active` (incluye/excluye símbolo), `status`, `updated_at`.
- ~60 columnas numéricas/booleanas (riesgo, hedge, healer, grinder, trailing, rescate, vol-breaker…),
  todas con DEFAULT. El bot las relee en caliente en ≤3 s. El dashboard debe permitir **verlas y
  editarlas** (formulario) — un `UPDATE` a esta fila reconfigura el bot sin reiniciarlo.
- RLS: `FOR ALL` al dueño (lee y edita sus filas).

**`bot_logs`** — feed de eventos e **historial** (única fuente histórica hoy):
- `id`, `user_id`, `symbol`, `log_type` TEXT, `message` TEXT, `price`, `lots`, `balance` (todos
  DOUBLE), `ticket` BIGINT, y **la columna de tiempo** (ver CAVEAT abajo).
- RLS: `FOR SELECT` al dueño (el INSERT lo hace el bot con service-role).
- Índice por `(user_id, symbol, <tiempo> DESC)`.
- **Taxonomía de `log_type`** (para reconstruir trades / métricas):
  - Aperturas (traen `ticket`): `ENTRADA` (OP1 SMC), `PROTECCION` (Hedge Lock), `RECOVERY` (OP3),
    `RESCATE` (OP4 Sentinel), `GRINDER` (scalp), `OPERACION` (fallback).
  - Cierres: `CIERRE` (ticket + PnL por leg), `HEALER` (amputación parcial), `UNWIND` (banking),
    `GRINDER` (time-stop); resumen de ciclo `EXITO` (sin ticket, agregado).
  - Sistema/otros: `SYSTEM`, `VCB`, `SHADOW`, `ERROR`.
  - El PnL de un cierre viene **dentro del texto de `message`** (p.ej. `f1=-17.52`), no en columna
    numérica → parsear es frágil. Para % win/loss y ganancias, **prefiere** el delta de `balance`
    entre eventos o `bot_state` antes que parsear strings.

**`bot_state`** — snapshot en vivo, 1 fila por usuario:
- `balance`, `equity`, `margin_used`, `margin_free`, `floating_pnl` DOUBLE, `open_positions` INT,
  `initial_balance` DOUBLE, `last_flow_ticket` BIGINT, `symbol`, `updated_at`. RLS `FOR SELECT` al dueño.
- `initial_balance` se captura de MT5 en la primera corrida y se **auto-ajusta por
  depósitos/retiros** (deals BALANCE/CREDIT; migración 005): la ganancia mostrada es solo la del
  trading. `last_flow_ticket` es el ancla interna de esa reconciliación (no mostrar en UI).
- Fuente de: saldo actual, equity, **balance en uso (margen)**, contador rápido de operaciones
  abiertas, ganancias totales (`equity - initial_balance`).

**`bot_positions`** — tabla principal de posiciones (histórico + vivo), ya desplegada:
- `id` BIGSERIAL PK, `user_id`, `ticket` BIGINT, `symbol`, `position_type` (`BUY`/`SELL`),
  `op_type` (misma taxonomía de apertura que `bot_logs`: `ENTRADA | PROTECCION | RECOVERY | RESCATE
  | GRINDER | OPERACION`), `comment` (texto original de MT5), `lots`, `open_price`, `open_time`,
  `sl`, `tp`, `current_price`, `profit`, `swap`, `status` (`OPEN | CLOSED`), `close_price`,
  `close_time`, `updated_at`. `UNIQUE(user_id, ticket)`.
- El bot upsertea cada posición **abierta** en cada heartbeat (precio/P&L flotante frescos). Al
  cerrarse (el ticket desaparece de MT5) la marca `CLOSED` con el cierre reconstruido del historial
  de deals de esa posición: `close_price`/`close_time` del deal final y `profit`/`swap` **totales**
  (incluye cierres parciales previos del Healer/Unwind, no solo el último tramo).
- RLS: `FOR SELECT` al dueño (el upsert lo hace el bot con service-role).
- **Esta es ahora la fuente preferida** para operaciones abiertas en detalle, histórico de
  posiciones y % win/loss — ver mapeo de métricas actualizado abajo. `bot_logs` sigue siendo el
  feed de eventos (útil para ver HEALER/UNWIND/VCB/SYSTEM/ERROR con contexto), no lo elimines.

**CAVEAT crítico — schema drift de la columna de tiempo.** El SQL declara `created_at`, pero la
tabla `bot_logs` **viva en producción** usa `ts`. **Antes de ordenar/filtrar por tiempo, verifica
contra la BD real** cuál existe (introspección o probar `ts` y caer a `created_at`). No asumas.

### FUNCIONALIDADES A CONSTRUIR

**Cliente final:**
1. **Auth**: login/logout con Supabase Auth (email+password), sesión por cookies (`@supabase/ssr`).
2. **Panel de estado (vivo)**: badge `is_active` (ON/operando · OFF/close-only), `bot_status`,
   **online/offline** por `last_heartbeat`, `label`, `account_login`.
3. **Toggle ON/OFF**: botón que hace `UPDATE bot_instances SET is_active`. Deja claro en UI que OFF
   = close-only (no mata posiciones).
4. **Métricas (tarjetas)**: saldo inicial, saldo actual, **balance en uso**, equity, ganancias
   totales $, % win/loss, operaciones totales, **operaciones abiertas ahora (live)**. Ver mapeo de
   fuentes abajo.
5. **Editor de configuración**: formulario sobre `bot_config` (~60 params, agrupados por sección
   como en el schema). Guardar = `UPDATE`. Validar tipos.
6. **Feed de eventos (vivo)**: tabla/stream de `bot_logs` con color por `log_type`, filtros y
   paginación. Suscripción **Realtime** a nuevos INSERT.
7. **Curva de balance**: serie temporal de `bot_logs.balance` (o `bot_state`).
8. **Panel de posiciones abiertas (vivo)**: tabla de `bot_positions WHERE status='OPEN'`, una fila
   por ticket (tipo, lotes, precio apertura, precio actual, P&L flotante, SL/TP, tiempo abierta).
   Suscripción Realtime (INSERT/UPDATE) para que se actualice sin polling.
9. **Histórico de posiciones**: tabla de `bot_positions WHERE status='CLOSED'`, paginada,
   ordenable por `close_time` DESC, filtrable por `op_type`/`position_type`. Reemplaza la necesidad
   de reconstruir trades parseando `bot_logs.message`.

**Admin (server-side, service-role, allowlist):**
8. Grilla de **todas** las instancias: usuario/email, `bot_status`, online/offline, `is_active`,
   equity/PnL, nº posiciones. Ordenable. Drill-down a la vista de un usuario.

**Mapeo métrica → fuente** (respétalo):
- Saldo inicial → `bot_state.initial_balance` (fallback: `balance` más antiguo en `bot_logs`).
- Saldo actual → `bot_state.balance`; Equity → `bot_state.equity`.
- Balance en uso → `bot_state.margin_used` (**solo aquí**).
- Operaciones abiertas live (contador rápido) → `bot_state.open_positions`; **detalle por posición**
  → `bot_positions WHERE status='OPEN'`.
- Ganancias totales $ → `bot_state.equity - bot_state.initial_balance` (fuente primaria; opcional
  cruzarla con `SUM(profit+swap)` de `bot_positions CLOSED` + `bot_state.floating_pnl`).
- Operaciones totales → `COUNT(*)` en `bot_positions` (antes se contaban aperturas en `bot_logs`;
  `bot_positions` es ahora la fuente preferida, 1 fila por ticket real).
- % win/loss → entre `bot_positions WHERE status='CLOSED'`, ganadoras = `profit + swap > 0` vs
  perdedoras = `profit + swap <= 0`. Columna numérica directa — **ya no hace falta** parsear
  `bot_logs.message` para esto.

### TÉCNICA / BUENAS PRÁCTICAS

- Cliente Supabase del navegador: **solo** `NEXT_PUBLIC_SUPABASE_URL` + `NEXT_PUBLIC_SUPABASE_ANON_KEY`.
- Service-role key: solo en env server (`SUPABASE_SERVICE_ROLE_KEY`), usada en Server Components /
  Route Handlers para la vista admin. Jamás en `NEXT_PUBLIC_*`.
- Realtime: habilitar replicación en `bot_instances`, `bot_state`, `bot_logs`, `bot_positions` (te
  lo confirma el operador). Suscribir cambios para vivo sin polling.
- Manejo de "sin fila": un usuario nuevo puede no tener aún `bot_state`/`bot_logs`; muestra estados
  vacíos, no errores.
- Formatea dinero y % con locale es. Zona horaria: muestra en local del navegador; los timestamps
  vienen en UTC.

### QUÉ **NO** HACER

- No modificar el bot ni su lógica de trading. No proponer stops/emergency-close en la UI (el bot
  por diseño **no** los tiene; ver restricciones abajo).
- No exponer service-role al cliente. No romper la semántica de `is_active` (OFF ≠ matar posiciones).
- No inventar tablas de trades/equity que el bot no escribe; usa el contrato de arriba.

### RESTRICCIONES DE DISEÑO DEL BOT (contexto, para no diseñar UI que las contradiga)

El bot **no** tiene emergency-stop ni SL catastrófico; OP2 (Hedge Lock) **congela** la pérdida; el
Healer hace cierres parciales; `is_active=false` es **close-only**. La UI debe reflejar esta
semántica, no ofrecer acciones destructivas que el bot no soporta.

### NOTA — `bot_state` y `bot_positions` ya están desplegadas

Ambas tablas existen en la BD de producción (no son placeholders): `bot_state` desde 2026-07-15,
`bot_positions` desde 2026-07-16. Ambas se alimentan del mismo heartbeat del bot (~15 s). Puedes
consumirlas directamente sin gate ni fallback — solo confirma con el operador que Realtime está
habilitado en ellas si lo necesitas para vivo.

### PRIMEROS PASOS SUGERIDOS

1. `create-next-app` (TS, App Router, Tailwind). Añade `@supabase/ssr`, `@supabase/supabase-js`.
2. Configura clientes Supabase (browser + server) y middleware de sesión (`@supabase/ssr`).
3. Auth (login/logout) + guard de rutas. Layout con nav.
4. Introspección de `bot_logs` para resolver `ts` vs `created_at`.
5. Panel de estado + toggle `is_active` (Realtime).
6. Métricas (las derivables ya; live si `bot_state` existe).
7. Editor `bot_config`. Feed `bot_logs`. Curva de balance.
8. Vista admin server-side con allowlist.

### QUÉ TE DA EL OPERADOR (pídelo si falta)

- `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`.
- Lista de emails admin (`ADMIN_EMAILS`).
- Confirmación de si `bot_state` ya está desplegada y de si Realtime está habilitado en las tablas.
- El `symbol` en uso (hoy `XAUUSD+`).

---

## Referencias en el repo del bot (para el operador / trazabilidad)

- `supabase_schema.sql` — esquema canónico de las 5 tablas (`bot_instances`, `bot_config`,
  `bot_logs`, `bot_state`, `bot_positions`) + RLS.
- `bot/db.py` — capa Supabase (queries; `report()`/`report_state()`/`upsert_positions()`; logging
  por lotes).
- `bot/broker.py` (`history_deals_for_position`) — reconstruye el cierre exacto de una posición.
- `bot/strategy.py` (`_build_hud`) — de dónde salen los valores del snapshot `bot_state`
  (`balance`, `equity`, `positions`, etc.).
- `bot/main.py` — heartbeat/control loop; upserts de `bot_state` y `bot_positions` en cada ciclo.
- `docs/memory/supabase-multi-instance-model.md` — modelo multi-instancia y semántica de `is_active`.
- `docs/memory/logging-taxonomy-and-ticket.md` — taxonomía de `log_type` y caveat `ts`/`created_at`.
- `docs/memory/bot-design-constraints.md` — restricciones de diseño (no stops duros, hedge congela).
- `README.md` — alta de usuario/instancia y control en caliente.

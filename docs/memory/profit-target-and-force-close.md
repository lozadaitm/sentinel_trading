---
name: profit-target-and-force-close
description: Objetivo de ganancia por cuenta (email + close-only) y cierre forzado por el usuario; tabla account_settings
metadata:
  type: project
---

Feature de 2026-08-12 (migración `migrations/004_profit_target.sql`, pendiente de aplicar
en Supabase): el usuario configura en el dashboard un **% de ganancia objetivo** y puede
**forzar el cierre** de todas sus posiciones asumiendo el flotante.

**Objetivo de ganancia (a nivel CUENTA, no por motor):**
- Vive en la tabla nueva **`account_settings`** (1 fila por usuario, sin `bot_id`):
  `profit_target_pct` (NULL = off) y `target_reached_at`. La columna `initial_deposit`
  quedó **sin uso** (siempre NULL): el usuario decidió que la base sea siempre el
  `bot_state.initial_balance` auto-capturado de MT5 en la primera corrida (mecanismo que
  ya existía), sin pedir el fondeo en el dashboard.
- La ganancia se mide en **EQUITY** (incluye flotante): `(equity - base) / base * 100`.
- **La base se auto-ajusta por depósitos/retiros** (migración `005_capital_flows.sql`):
  cada heartbeat, `_reconcile_capital_flows` busca deals `DEAL_TYPE_BALANCE/CREDIT` con
  ticket > `bot_state.last_flow_ticket` (`broker.capital_flows`, ventana 45 días) y los
  suma a `initial_balance` (depósito sube la base, retiro la baja) → la ganancia medida
  es solo la del trading. El ancla es el **ticket** (no la fecha) para esquivar la TZ del
  servidor MT5; se persiste en el mismo `report_state`.
- Chequeo en `bot/main.py::_check_profit_target` (cada heartbeat, ~15 s). Al alcanzarlo:
  1. **Claim atómico** de `target_reached_at` (`db.claim_profit_target`: UPDATE ... WHERE
     target_reached_at IS NULL) — solo un motor gana aunque m15 y m5 crucen a la vez → un
     solo email.
  2. `db.deactivate_all_instances()`: `is_active=false` para TODAS las instancias del
     usuario = close-only (semántica de [[supabase-multi-instance-model]]; NO cierra nada
     a la fuerza).
  3. Email vía `bot/notify.py` (**Resend**, API HTTP con urllib; env `RESEND_API_KEY` /
     `RESEND_FROM`, ver instances/example.env; destinatario = email de auth.users; sin
     API key queda inerte y solo loguea). El from default `onboarding@resend.dev` solo
     entrega al dueño de la cuenta Resend: para usuarios reales hay que verificar un
     dominio en resend.com y setear `RESEND_FROM`.
  4. Log `log_type=TARGET` (nuevo en la taxonomía de [[logging-taxonomy-and-ticket]]).
- El dashboard **re-arma** limpiando `target_reached_at` al guardar un objetivo nuevo
  (upsert desde `profit-target-panel.tsx` en el repo webapp).
- **El P&L funciona por CICLOS de meta** (migración `009_pnl_rebase.sql`): al reactivar
  un motor (o re-armar la meta) tras un objetivo alcanzado, el dashboard estampa
  `account_settings.baseline_reset_at`; cada bot lo aplica UNA vez en su heartbeat
  (`_apply_baseline_reset`): base nueva = **equity presente** (post-retiro si lo hubo),
  ancla de flujos adelantada, sello guardado en `bot_state.baseline_applied_at`
  (idempotente entre motores/restarts). Sin esto, re-armar el mismo % re-dispararía la
  meta al instante. Consecuencia: "Ganancias totales" del dashboard = ganancia del
  ciclo actual, no histórica.

**Cierre forzado (decisión del usuario tras alcanzar el objetivo, o en cualquier momento):**
- Columna nueva `bot_instances.force_close`. El dashboard setea
  `{is_active: false, force_close: true}` en las filas del usuario (ambos motores) tras
  mostrar el flotante que asumiría; cada bot lo ve en su refresh de control (~3 s),
  llama `engine.close_all(reason)` (m15: wrapper de `_close_all`; m5: método nuevo) y
  consume el comando (`db.clear_force_close`: force_close=false + is_active=false).
- **NO viola [[bot-design-constraints]]**: no es un emergency-stop automático por
  drawdown; siempre lo dispara el usuario a mano y la UI le muestra el flotante
  (positivo o negativo) que va a asumir antes de confirmar.

**Why:** el usuario quiere retirarse con la ganancia asegurada: notificación al llegar al
objetivo y elegir entre esperar la gestión normal (close-only) o cortar ya asumiendo el
flotante.

**How to apply:** cualquier lógica nueva que "apague" al bot debe pasar por
`is_active=false` (close-only), nunca pausar `on_tick`; un cierre total inmediato solo
puede originarse en `force_close` (usuario). El contrato completo está en
`docs/dashboard-handoff.md` y en el HANDOFF.md del repo webapp.

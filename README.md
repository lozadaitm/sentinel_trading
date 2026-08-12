# Sentinel — Bot de Trading (Hyper Grinder v20)

Port en Python del Expert Advisor MQL5 original. Opera vía **MetaTrader 5** sobre
`XAUUSD+` y se controla en caliente desde **Supabase** (Postgres cloud). El usuario
final lo maneja desde el **dashboard web** (repo separado `sentinel_trading-webapp`,
Next.js en Vercel: https://sentinel-trading-webapp.vercel.app/).

**Dos motores sobre la misma cuenta**, coordinados por magic number:
- **m15 — Sentinel** (`bot/strategy.py`): motor principal. Entradas SMC, cobertura
  (Hedge Lock), Healer, recovery/rescate. Sin stop loss por diseño.
- **m5 — Grinder** (`bot/strategy_m5.py`): scalper junior. Una posición viva, siempre
  con SL; cede margen al M15 (gobierno de margen en `bot/budget.py`).

**Multi-instancia:** un proceso = un usuario = una cuenta Vantage = un terminal MT5.
Cada usuario corre su(s) motor(es) en el VPS con un `.env` por instancia.

**Control:** cada usuario enciende/apaga su bot con `bot_instances.is_active`:
- `true`  → **operando** (abre nuevas posiciones y gestiona todo).
- `false` → **close-only**: NO abre nada nuevo pero sigue gestionando y cerrando lo
  abierto hasta quedar plano (no abandona posiciones). No es una pausa/kill duro.

## Requisitos

- **Windows** (la librería `MetaTrader5` solo funciona en Windows).
- **Python 3.10+**.
- **Terminal de MetaTrader 5** por cuenta (los clones portables los crea el provisioner;
  ver [Alta automática](#a-alta-automática-recomendada-signup-por-invitación)).
- **Proyecto Supabase** con el esquema cargado.

## Instalación

```powershell
# 1. (Opcional pero recomendado) crear y activar entorno virtual
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Instalar dependencias
pip install -r requirements.txt
```

## Setup inicial del proyecto (una sola vez)

1. Crear un proyecto en [Supabase](https://supabase.com).
2. En **SQL Editor**, correr [`supabase_schema.sql`](supabase_schema.sql) (esquema
   canónico completo). Para una base que ya existía, aplicar en orden las migraciones
   pendientes de [`migrations/`](migrations/) — cada archivo indica en su cabecera si
   ya fue aplicada:

   | Migración | Qué añade |
   |-----------|-----------|
   | `002_bot_id.sql` | multi-bot (`bot_id` en las 5 tablas, PKs compuestas) |
   | `003_disable_m15_grinder.sql` | desactiva el grinder embebido del M15 |
   | `004_profit_target.sql` | `account_settings` (objetivo de ganancia) + `bot_instances.force_close` |
   | `005_capital_flows.sql` | `bot_state.last_flow_ticket` (base ajustada por depósitos/retiros) |
   | `006_candles.sql` | `bot_candles` (velas OHLC para la gráfica del dashboard) |
   | `007_withdrawals.sql` | `billing_settings` + `withdrawals` (comisión USDT de retiros) |
   | `008_signup_provisioning.sql` | `invites` + `provision_requests` (signup privado) |

3. Anotar del dashboard (**Project Settings → API**):
   - **Project URL** → `SUPABASE_URL`.
   - **service_role key** (secreta) → `SUPABASE_SERVICE_ROLE_KEY`. El bot la usa para
     bypassar RLS; **nunca** se expone al frontend.

## Alta de un nuevo usuario / instancia

### A. Alta automática (recomendada): signup por invitación

1. Un admin del dashboard genera un **código de invitación** en `/dashboard/admin`
   (sección *Invitaciones de registro*) y envía el link
   `https://<dashboard>/signup?invite=CODIGO`.
2. El invitado se registra con todos los datos: nombre de cuenta, email+password del
   panel, y su MT5 (login, servidor, **password de trading**, símbolo). El webapp crea
   el usuario en Auth, siembra `bot_instances` (m15+m5, apagados) y `bot_config`
   (defaults), y encola una fila en `provision_requests`.
3. En el VPS, el **provisioner** ([`scripts/provision.py`](scripts/provision.py))
   procesa la cola: clona la instalación base de MT5 a `C:\MT5_instances\mt5_<login>`
   (modo portable), escribe `instances/<user_id>.env` completo y marca la solicitud
   como READY **borrando la password MT5 de la DB**.

```powershell
python -m scripts.provision            # procesa las PENDING y termina
python -m scripts.provision --watch    # queda vigilando (poll cada 60 s)
python -m scripts.provision --start    # además lanza los motores (modo consola)
```

Config del provisioner por entorno: `MT5_BASE_DIR` (instalación a clonar; default
`C:\Program Files\MetaTrader 5`) y `MT5_CLONES_DIR` (default `C:\MT5_instances`).

**En este VPS ya corre solo**: la tarea programada **`SentinelProvisioner`**
(Task Scheduler, como SYSTEM) ejecuta el modo one-shot **cada 5 minutos** y escribe su
salida en `logs\provision.log`. Comandos utilitarios:

```powershell
Get-ScheduledTaskInfo -TaskName "SentinelProvisioner"      # última corrida y resultado (0 = OK)
Start-ScheduledTask   -TaskName "SentinelProvisioner"      # forzar una corrida ya
Disable-ScheduledTask -TaskName "SentinelProvisioner"      # pausar la provisión automática
Enable-ScheduledTask  -TaskName "SentinelProvisioner"      # reanudarla
Unregister-ScheduledTask -TaskName "SentinelProvisioner"   # eliminarla
Get-Content logs\provision.log -Tail 20                    # ver el log del provisioner
```

> La tarea NO lanza los motores: provisiona la instancia y el arranque sigue siendo
> `scripts\start_all.ps1` (preflight + tests + auditorías). La instancia nace con
> `is_active=false`, así que nada opera hasta encender el toggle en el dashboard.

### B. Alta manual (fallback)

<details>
<summary>Pasos manuales (lo que el signup automatiza)</summary>

1. **Usuario en Supabase Auth**: Dashboard → Authentication → Users → Add user.
   Copiar su **User UID** (= `user_id` en todas las tablas y en el `.env`).
2. **Filas en la base** (SQL Editor), una por motor:

```sql
insert into public.bot_instances (user_id, bot_id, label, account_login, is_active)
values ('<USER_UUID>', 'm15', 'Cuenta Vantage', 12345678, false),
       ('<USER_UUID>', 'm5',  'Cuenta Vantage', 12345678, false)
on conflict (user_id, bot_id) do nothing;

insert into public.bot_config (user_id, symbol, bot_id, is_active)
values ('<USER_UUID>', 'XAUUSD+', 'm15', true),
       ('<USER_UUID>', 'XAUUSD+', 'm5',  true)
on conflict (user_id, symbol, bot_id) do nothing;
```

3. **`.env` de la instancia**: copiar `instances\example.env` a
   `instances\<USER_UUID>.env` y rellenarlo (los `.env` de `instances/` no se
   commitean, salvo las plantillas).
</details>

### El `.env` de una instancia

```ini
# Supabase (igual para todas las instancias del proyecto)
SUPABASE_URL=https://<tu-proyecto>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service_role_key>

# Identidad de ESTE usuario
USER_ID=<USER_UUID>
SYMBOL=XAUUSD+

# Motores a levantar sobre esta cuenta: m15 | m15,m5
BOTS=m15,m5
MAGIC_M15=100100
MAGIC_M5=100200

# Terminal MT5 de la cuenta de este usuario (clon propio; ver abajo)
MT5_PATH=C:\MT5_instances\mt5_12345678\terminal64.exe
MT5_LOGIN=12345678
MT5_SERVER=VantageInternational-Live
MT5_PASSWORD=<password_de_trading>
MT5_PORTABLE=true

# Opcional
# SHADOW_MODE=true          # decide y loguea, pero NO envía órdenes
# RESEND_API_KEY=re_xxx     # notificaciones por correo (bot/notify.py)
# RESEND_FROM=Sentinel <alertas@tu-dominio.com>
# DASHBOARD_URL=https://sentinel-trading-webapp.vercel.app/
```

- **Un fichero por usuario, no por bot.** Sobre la misma cuenta corren el Sentinel M15
  y el Grinder M5: comparten credenciales y solo difieren en `BOT_ID` y `MAGIC_NUMBER`,
  que **inyecta el launcher** a partir de `BOTS` y de `MAGIC_M15`/`MAGIC_M5`.
- `MAGIC_M15` y `MAGIC_M5` deben ser **distintos**: son lo que permite a cada motor ver
  solo sus posiciones y a la vez leer la cesta del vecino para la reserva de margen
  (`bot/budget.py`). El preflight aborta si coinciden.
- `MT5_PORTABLE=true` para los clones creados por el provisioner (los datos viven en la
  carpeta del clon, no en AppData).
- Si dejas `MT5_*` vacíos, el bot se conecta al terminal que ya esté abierto y logueado.

> El `.env` de la **raíz** del repo actúa como capa de respaldo: `bot/config.py` lo
> carga con `setdefault()`, así que el de la instancia siempre gana. Sirve para claves
> comunes (`SUPABASE_*`, `RESEND_*`). No pongas ahí `BOT_ID` ni `MAGIC_NUMBER`.

## Arranque completo (recomendado)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_all.ps1
```

Encadena en el orden correcto todo lo necesario y **aborta antes de operar** si algo
no cuadra:

| Fase | Qué hace | Si falla |
|------|----------|----------|
| 1. Preflight | python, dependencias, `.env` completos, y que `BOT_ID` y `MAGIC_NUMBER` se correspondan | **aborta** |
| 2. Tests | regresión local (`tests/`), sin tocar MT5 ni Supabase | **aborta** |
| 3. Auditorías | `audit_margin` (siempre) + `audit_m5_signals` (si la última tiene más de 30 días) | aborta solo si MT5 no responde |
| 4. Lanzamiento | una ventana de PowerShell por bot, con la TUI. M15 primero | — |

| Flag | Para qué |
|------|----------|
| `-M15Only` | levantar solo el Sentinel |
| `-SkipAudits` | reinicio rápido el mismo día |
| `-ForceAudits` | repetir `audit_m5_signals` aunque sea reciente |
| `-DryRun` | preflight + tests + auditorías, sin lanzar los bots |
| `-NoUI` | modo consola en vez de TUI |
| `-SkipTests` | saltar la regresión (no recomendado tras un `git pull`) |

Las auditorías son read-only y se archivan con fecha en `logs/audits/`. Si
`audit_margin` dice que el peor caso de la escalera **NO CABE** en el equity, pide
confirmación antes de seguir.

## Arranque de una instancia suelta

```powershell
# Consola (m15; para el m5 añade -BotId m5)
powershell -ExecutionPolicy Bypass -File scripts\run_instance.ps1 instances\<USER_UUID>.env

# TUI en vivo (gráfica M15, HUD de entrada, log)
powershell -ExecutionPolicy Bypass -File scripts\run_instance.ps1 instances\<USER_UUID>.env -Module bot.tui
powershell -ExecutionPolicy Bypass -File scripts\run_instance.ps1 instances\<USER_UUID>.env -BotId m5 -Module bot.tui_m5

# Levantar todas las instancias de instances\*.env
powershell -ExecutionPolicy Bypass -File scripts\launch_all.ps1
```

Teclas de la TUI: `p` parámetros · `↑`/`↓` `PgUp`/`PgDn` desplazar log · `Fin` log en
vivo · `q` salir (shutdown ordenado, `bot_status=STOPPED`).

## Integración con el dashboard

El bot **lee** el control del usuario y **publica** todo lo que el dashboard muestra
(heartbeat cada ~15 s; control refresco cada ~3 s):

| El bot LEE | Para qué |
|------------|----------|
| `bot_instances.is_active` | operar vs close-only |
| `bot_instances.force_close` | **cierre forzado**: el usuario asume el flotante; el bot cierra todo (verificando retcode, con reintento hasta quedar plano) y consume el comando |
| `bot_config.*` | ~60 parámetros de estrategia, editables en caliente |
| `account_settings.profit_target_pct` | **objetivo de ganancia** de la cuenta (en equity vs saldo inicial). Al alcanzarlo: claim atómico (un solo email aunque corran m15+m5), ambos motores a close-only, correo vía Resend y log `TARGET` |
| `withdrawals.status='PENDING'` | **gate de comisión**: con una comisión de retiro sin pagar, el bot revierte `is_active=true` a close-only |

| El bot ESCRIBE | Qué es |
|----------------|--------|
| `bot_instances.bot_status/last_heartbeat` | estado (`RUNNING/CLOSE_ONLY/FLAT/ERROR/STOPPED`) y online/offline |
| `bot_state` | snapshot vivo: balance, equity, márgenes, flotante, `initial_balance` (capturado de MT5 en la primera corrida y **auto-ajustado por depósitos/retiros** — deals BALANCE/CREDIT, ancla `last_flow_ticket`) |
| `bot_positions` | posiciones abiertas (refresco por heartbeat) e histórico con P&L total al cerrarse |
| `bot_candles` | velas M15 para la gráfica (solo las publica el proceso m15: backfill ~400 + las 2 últimas por heartbeat; poda >30 días) |
| `bot_logs` | feed de eventos (taxonomía `ENTRADA/PROTECCION/RECOVERY/RESCATE/GRINDER/CIERRE/HEALER/UNWIND/EXITO/TARGET/SYSTEM/VCB/ERROR...`) |

Correos (objetivo alcanzado): `bot/notify.py` vía **Resend** (`RESEND_API_KEY`,
`RESEND_FROM` con dominio verificado); sin key queda inerte y solo loguea.

## Modo sombra (pruebas sin órdenes reales)

`SHADOW_MODE=true` en el `.env` de la instancia. El bot evalúa la estrategia, conecta a
Supabase y registra decisiones, pero **no** envía órdenes al broker. Recomendado para
validar una instancia nueva antes de operar en real.

## Detener

`Ctrl + C` en la ventana de la instancia: shutdown ordenado (flush de logs pendientes,
`bot_status=STOPPED`, `mt5.shutdown()`).

## Memoria del proyecto

Las decisiones de diseño y su porqué viven en [`docs/memory/`](docs/memory/MEMORY.md)
(versionada; una línea por memoria en el índice). El contrato de datos completo para el
dashboard está en [`docs/dashboard-handoff.md`](docs/dashboard-handoff.md).

## Estructura

```
bot/
  main.py           Entrypoint m15: setup() / trading_loop() / shutdown(). Heartbeat,
                    objetivo de ganancia, cierre forzado, gate de comisión, flujos, velas
  main_m5.py        Entrypoint m5 (reusa el mismo loop con M5Engine)
  strategy.py       Motor Sentinel M15 (SMC + Hedge Lock + Healer + rescate)
  strategy_m5.py    Motor Grinder M5 (scalper con SL, régimen de rango v2)
  budget.py         Gobierno de margen entre motores (reserva de protección)
  ledger.py         CycleLedger: identidad por rol de cada posición del ciclo (JSON local)
  news.py           NewsGuard: bloqueo por noticias rojas USD (exportador MQL5→JSON)
  notify.py         Correos vía Resend (objetivo de ganancia)
  broker.py         Capa de órdenes sobre MetaTrader5 (+ capital_flows, historial deals)
  db.py             Acceso a Supabase: config, instancia, state, posiciones, velas,
                    account_settings, withdrawals, logs por lotes
  tui.py / tui_m5.py  Interfaces de terminal en vivo
  config.py         Identidad + conexión + attach MT5, todo por entorno/.env
  indicators.py     Indicadores técnicos
  logger.py         Logging multi-sink: consola/CSV + Supabase + buffer de la TUI
supabase_schema.sql   Esquema canónico completo + RLS
migrations/           Migraciones incrementales (aplicar en orden; ver cabeceras)
scripts/
  start_all.ps1       Arranque completo: preflight + tests + auditorías + lanzamiento
  run_instance.ps1    Arranca UN motor de UNA instancia desde su .env
  launch_all.ps1      Levanta todas las instancias de instances/*.env
  provision.py        Provisioner: provision_requests → clon MT5 portable + .env
  audit_margin.py     ¿Cabe el peor caso de la escalera en el equity de hoy?
  audit_m5_signals.py Calibración de señales del M5 sobre histórico
  CalendarExporter.mq5  Exportador del calendario económico para NewsGuard
instances/            Un .env por usuario (no versionados; example.env es la plantilla)
docs/                 dashboard-handoff.md + memory/ (memoria versionada del proyecto)
requirements.txt      Dependencias Python
```

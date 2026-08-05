# Sentinel — Bot de Trading (Hyper Grinder v20)

Port en Python del Expert Advisor MQL5 original. Opera vía **MetaTrader 5** sobre
`XAUUSD` y se controla en caliente desde **Supabase** (Postgres cloud).

**Multi-instancia:** un proceso = un usuario = una cuenta Vantage = un terminal MT5.
Cada usuario corre su propia instancia del bot en el VPS. La identidad
(`user_id`, `symbol`, credenciales MT5) se pasa por un archivo `.env` por instancia.

**Control:** cada usuario enciende/apaga su bot con `bot_instances.is_active`:
- `true`  → **operando** (abre nuevas posiciones y gestiona todo).
- `false` → **close-only**: NO abre nada nuevo pero sigue gestionando y cerrando lo
  abierto hasta quedar plano (no abandona posiciones). No es un pausa/kill duro.

## Requisitos

- **Windows** (la librería `MetaTrader5` solo funciona en Windows).
- **Python 3.10+**.
- **Terminal de MetaTrader 5** de la cuenta Vantage (una instalación por cuenta si son varias — ver [Multi-instancia](#multi-instancia-varias-cuentas)).
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
2. En **SQL Editor**, correr [`supabase_schema.sql`](supabase_schema.sql). Crea las tablas
   `bot_instances`, `bot_config` y `bot_logs`, con FKs a `auth.users` y RLS.
3. Anotar del dashboard (**Project Settings → API**):
   - **Project URL** → `SUPABASE_URL`.
   - **service_role key** (secreta) → `SUPABASE_SERVICE_ROLE_KEY`. El bot la usa para
     bypassar RLS; **nunca** se expone al frontend.

## Alta de un nuevo usuario / instancia

Repetir estos pasos por cada usuario que vaya a correr el bot.

### 1. Crear el usuario en Supabase Auth

Dashboard → **Authentication → Users → Add user** (email + password). Copiar su **User UID**
(un UUID). Ese UUID es el `user_id` en todas las tablas y en el `.env`.

> El `user_id` **debe** ser el `auth.users.id` real: la RLS del frontend se basa en
> `auth.uid() = user_id`.

### 2. Crear sus filas en la base de datos

En el **SQL Editor**, reemplazando `<USER_UUID>` y `<SYMBOL>`:

```sql
-- Interruptor maestro del usuario (arranca apagado / close-only).
insert into public.bot_instances (user_id, label, account_login, is_active)
values ('<USER_UUID>', 'Cuenta Vantage', 12345678, false)
on conflict (user_id) do nothing;

-- Config de estrategia para su símbolo (arranca incluida).
insert into public.bot_config (user_id, symbol, is_active, status)
values ('<USER_UUID>', '<SYMBOL>', true, 'INACTIVE')
on conflict (user_id, symbol) do nothing;
```

Los ~60 parámetros de estrategia usan sus defaults; se ajustan editando esa fila de
`bot_config` (o desde el frontend).

### 3. Crear el `.env` de la instancia

Copiar la plantilla a `instances/<USER_UUID>.env`. Los `.env` de `instances/` **no** se
commitean (`.gitignore`), salvo `example.env`.

```powershell
Copy-Item instances\example.env instances\<USER_UUID>.env
notepad instances\<USER_UUID>.env
```

```ini
# Supabase (igual para todas las instancias del proyecto)
SUPABASE_URL=https://<tu-proyecto>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service_role_key>

# Identidad de ESTE usuario
USER_ID=<USER_UUID>
SYMBOL=XAUUSD+

# Motores a levantar sobre esta cuenta: m15 | m15,m5
BOTS=m15
MAGIC_M15=100100
MAGIC_M5=100200

# Terminal MT5 de la cuenta Vantage de este usuario
MT5_PATH=C:\Program Files\MetaTrader 5\terminal64.exe
MT5_LOGIN=12345678
MT5_SERVER=VantageInternational-Live
MT5_PASSWORD=<password_de_la_cuenta>
```

- **Un fichero por usuario, no por bot.** Sobre la misma cuenta pueden correr el
  Sentinel M15 y el Grinder M5: comparten credenciales y solo difieren en `BOT_ID`
  y `MAGIC_NUMBER`, que **inyecta el launcher** a partir de `BOTS` y de
  `MAGIC_M15`/`MAGIC_M5`. No dupliques el `.env`.
- `MAGIC_M15` y `MAGIC_M5` deben ser **distintos**: son lo que permite a cada motor
  ver solo sus posiciones y a la vez leer la cesta del vecino para calcular la
  reserva de margen (`bot/budget.py`). El preflight aborta si coinciden.
- Antes de añadir `m5` a `BOTS`: aplicar `migrations/002_bot_id.sql`, crear su fila
  de `bot_config`/`bot_instances` y actualizar el dashboard para filtrar por `bot_id`.
- Si dejas `MT5_*` vacíos, el bot se conecta al terminal MT5 que ya esté **abierto y logueado**.
- Si los rellenas, el bot **abre y loguea** ese terminal por sí mismo.

> El `.env` de la **raíz** del repo actúa como capa de respaldo: `bot/config.py`
> lo carga con `setdefault()`, así que el de la instancia siempre gana. Sirve para
> claves comunes a todos los usuarios (p. ej. `SUPABASE_*`). No pongas ahí `BOT_ID`
> ni `MAGIC_NUMBER`.

## Arranque completo (recomendado)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_all.ps1
```

Encadena en el orden correcto todo lo necesario y **aborta antes de operar** si algo
no cuadra, para que no se pueda arrancar en un estado incoherente:

| Fase | Qué hace | Si falla |
|------|----------|----------|
| 1. Preflight | python, dependencias, `.env` completos, y que `BOT_ID` y `MAGIC_NUMBER` se correspondan | **aborta** |
| 2. Tests | regresión local (`tests/`), sin tocar MT5 ni Supabase | **aborta** |
| 3. Auditorías | `audit_margin` (siempre) + `audit_m5_signals` (si la última tiene más de 30 días) | aborta solo si MT5 no responde |
| 4. Lanzamiento | una ventana de PowerShell por bot, con la TUI. M15 primero | — |

El preflight caza el fallo de configuración más caro: copiar el `.env` del M15 y
cambiar solo `BOT_ID` dejando el mismo `MAGIC_NUMBER`. Los dos motores compartirían
magic y cada uno vería las posiciones del otro como propias.

Las auditorías son read-only y se archivan con fecha en `logs/audits/`, así queda
histórico de cómo estaba la cuenta en cada arranque. Si `audit_margin` dice que el
peor caso de la escalera **NO CABE** en el equity, pide confirmación antes de seguir.

| Flag | Para qué |
|------|----------|
| `-M15Only` | levantar solo el Sentinel (mientras el M5 esté en validación) |
| `-SkipAudits` | reinicio rápido el mismo día |
| `-ForceAudits` | repetir `audit_m5_signals` aunque sea reciente |
| `-DryRun` | preflight + tests + auditorías, sin lanzar los bots |
| `-NoUI` | modo consola en vez de TUI |
| `-SkipTests` | saltar la regresión (no recomendado tras un `git pull`) |

## Arranque de una instancia suelta

Desde la raíz del proyecto. El launcher carga el `.env` indicado y arranca el proceso.

### Modo consola

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_instance.ps1 instances\<USER_UUID>.env
```

Salida esperada:

```
HYPER GRINDER v20.0 (Python) INICIADO. user=<USER_UUID> symbol=XAUUSD+
```

### Modo TUI (interfaz en vivo)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_instance.ps1 instances\<USER_UUID>.env -Module bot.tui
```

Muestra en tiempo real: gráfica de velas M15, **HUD ENTRADA OP1** (*actual vs requerido*)
y el **LOG** de eventos.

| Tecla | Acción |
|-------|--------|
| `p` | Mostrar/ocultar panel de **parámetros** (`bot_config`) |
| `↑`/`↓` `PgUp`/`PgDn` | Desplazar el log |
| `Fin` | Volver al log en vivo |
| `q` | Salir (shutdown ordenado; marca `bot_status=STOPPED`) |

### Levantar TODAS las instancias a la vez

```powershell
powershell -ExecutionPolicy Bypass -File scripts\launch_all.ps1
```

Abre una ventana por cada `instances\*.env` (ignora `example.env`).

## Control en caliente

El bot relee `bot_instances` y `bot_config` cada pocos segundos (no reinicia el proceso):

- **`bot_instances.is_active`** (interruptor del usuario):
  `true` = operando · `false` = close-only (gestiona/cierra, no abre).
- **`bot_config.is_active`** — incluye/excluye ese símbolo.

El bot **reporta su estado** de vuelta en `bot_instances`: `bot_status`
(`RUNNING` / `CLOSE_ONLY` / `FLAT` / `ERROR` / `STOPPED`) y `last_heartbeat` (para
saber si está online). Los logs se persisten en `bot_logs` con el `user_id` de la instancia.

## Modo sombra (pruebas sin órdenes reales)

Poner `SHADOW_MODE = True` en `bot/config.py` antes de lanzar. El bot evalúa la estrategia,
conecta a Supabase y registra decisiones, pero **no** envía órdenes al broker. Recomendado
para validar una instancia nueva antes de operar en real.

## Detener

`Ctrl + C` en la ventana de la instancia: hace shutdown ordenado (flush de logs pendientes,
`bot_status=STOPPED`, `mt5.shutdown()`).

## Multi-instancia (varias cuentas)

En este VPS puede haber una o varias instancias, cada una con su `.env`. Aviso importante:
**dos procesos no deben atacar el mismo `terminal64.exe`**. Para varias cuentas Vantage,
instala MetaTrader 5 en **carpetas portables separadas** (una por cuenta) y apunta cada
`MT5_PATH` a su terminal. Hoy cada usuario opera **un símbolo**; el esquema ya soporta
varios símbolos por usuario para el futuro.

## Estructura

```
bot/
  main.py        Entrypoint: setup() / trading_loop() (gate is_active + heartbeat) / shutdown()
  tui.py         Interfaz de terminal en vivo (gráfica + HUD + log + params)
  config.py      Identidad + conexión Supabase + attach MT5, todo por entorno/.env
  broker.py      Capa de órdenes sobre MetaTrader5
  strategy.py    Motor de estrategia (SentinelEngine) + flag close_only + snapshot HUD
  indicators.py  Indicadores técnicos
  db.py          Acceso a Supabase (supabase-py): config, instancia, logs por lotes
  logger.py      Logging multi-sink: consola/CSV + Supabase (bot_logs) + buffer de la TUI
supabase_schema.sql   Esquema Supabase (bot_instances, bot_config, bot_logs) + RLS
scripts/
  run_instance.ps1    Arranca una instancia desde su .env (-Module bot.main | bot.tui)
  launch_all.ps1      Levanta todas las instancias de instances/*.env
instances/            Un .env por instancia (no versionados; example.env es la plantilla)
requirements.txt      Dependencias Python
```

# Sentinel — Bot de Trading (Hyper Grinder v20)

Port en Python del Expert Advisor MQL5 original. Opera vía **MetaTrader 5** sobre
`XAUUSD` y se controla en caliente desde una tabla de **PostgreSQL** (`bot_config`):
mientras esa fila tenga `status = 'ACTIVE'`, el motor corre; con cualquier otro
estado, el bot queda en espera sin detenerse.

## Requisitos

- **Windows** (la librería `MetaTrader5` solo funciona en Windows).
- **Python 3.10+**.
- **Terminal de MetaTrader 5** instalado y con la cuenta de trading logueada.
- **PostgreSQL** accesible con el esquema cargado.

## Instalación

```powershell
# 1. (Opcional pero recomendado) crear y activar entorno virtual
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Instalar dependencias
pip install -r requirements.txt
```

## Configuración

### 1. Variables de entorno (credenciales de DB)

Copiar la plantilla y rellenar con valores reales. El archivo `.env` **no** se
commitea (está en `.gitignore`).

```powershell
Copy-Item .env.example .env
notepad .env
```

```ini
DB_HOST=localhost
DB_NAME=sentinel_local
DB_USER=postgres
DB_PASS=tu_password_aqui
DB_PORT=5432
```

### 2. Esquema de base de datos

Cargar `schema.sql` en la base configurada (crea, entre otras, la tabla
`bot_config` que controla al bot):

```powershell
psql -h localhost -U postgres -d sentinel_local -f schema.sql
```

### 3. Parámetros de mercado

Las constantes de infraestructura están en `bot/config.py`. Ajustar si tu broker
usa otro sufijo de símbolo:

- `SYMBOL` — símbolo a operar (por defecto `XAUUSD+`; algunos brokers usan `XAUUSD.v`, etc.).
- `MAGIC_NUMBER` — identificador de las órdenes del bot.
- `USER_ID` — UUID que identifica la fila de `bot_config` a cargar.
- `SHADOW_MODE` — `True` loguea decisiones **sin enviar órdenes reales** (recomendado para probar).

> Los ~60 parámetros de estrategia **no** viven en el código: se leen en vivo
> desde la tabla `bot_config`.

## Lanzamiento

Con MetaTrader 5 abierto y logueado, y la fila de `bot_config` en `status = 'ACTIVE'`:

```powershell
python -m bot.main
```

Salida esperada al iniciar:

```
HYPER GRINDER v20.0 (Python) INICIADO.
```

### Modo sombra (pruebas sin órdenes reales)

Poner `SHADOW_MODE = True` en `bot/config.py` antes de lanzar. El bot evalúa la
estrategia y registra las decisiones, pero **no** envía órdenes al broker:

```
[MODO SOMBRA] No se enviaran ordenes reales; solo se loguean decisiones.
```

## Control en caliente

El bot consulta `bot_config` en cada iteración del bucle:

- `status = 'ACTIVE'` → el motor ejecuta `on_tick()`.
- cualquier otro valor → el bot espera (`[AUDITORIA] Esperando ACTIVE...`) sin cerrarse.

Esto permite pausar/reanudar el bot cambiando un valor en la base de datos, sin
reiniciar el proceso.

## Detener

`Ctrl + C` en la terminal:

```
[SISTEMA] Detencion manual solicitada.
```

Cierra la conexión a la DB y hace `shutdown()` de MetaTrader 5 de forma ordenada.

## Estructura

```
bot/
  main.py        Entrypoint y bucle de polling (estilo OnTick)
  config.py      Constantes de conexión, símbolo, timeframes
  broker.py      Capa de órdenes sobre MetaTrader5
  strategy.py    Motor de estrategia (SentinelEngine)
  indicators.py  Indicadores técnicos
  db.py          Acceso a PostgreSQL (carga de bot_config)
  logger.py      Logging a consola y CSV
schema.sql       Esquema de la base de datos
requirements.txt Dependencias Python
```

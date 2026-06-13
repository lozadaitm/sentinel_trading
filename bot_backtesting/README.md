# bot_backtesting

Backtester del bot Sentinel. **No reimplementa la estrategia**: reutiliza el
`SentinelEngine` real de `bot/` e inyecta un broker y un feed de datos simulados.
Lo que se prueba aquí es exactamente el mismo código que opera en vivo.

## Arquitectura

```
Market        feed historico M15/M5/H4 (sin lookahead: decide al cierre de barra)
SimBroker     broker simulado: posiciones, balance/equity/margen, SL auto-close,
              cierres parciales, deals para el Healer, stop-out de margen
BacktestEngine  = SentinelEngine real, solo sobrescribe los 2 seams de datos
                (_fetch_tick, _rates). Toda la logica de trading es heredada.
run_backtest  itera barras, corre engine.on_tick() y registra equity/trades
metrics       net profit, drawdown, win rate, profit factor, etc.
```

## Requisitos

```
pip install MetaTrader5 pandas numpy
```

`MetaTrader5` se importa solo por sus constantes (ORDER_TYPE/POSITION_TYPE/...);
**no requiere terminal abierto** para el backtest. Sí lo requiere `fetch_data`.

## Flujo de uso

### 1. Conseguir datos

**Opción A — datos reales desde MT5** (recomendado):
```
python -m bot_backtesting.fetch_data --start 2024-01-01 --end 2024-12-31 \
    --out-dir bot_backtesting/data
```
Genera `XAUUSD+_M15.csv`, `XAUUSD+_M5.csv`, `XAUUSD+_H4.csv`.

**Opción B — datos sintéticos** (solo smoke-test, precio inventado):
```
python -m bot_backtesting.gen_sample_data --days 200 --out-dir bot_backtesting/data
```

### 2. Correr el backtest
```
python -m bot_backtesting.run_backtest --data-dir bot_backtesting/data \
    --balance 5000 --out-dir bot_backtesting/results [--start 2024-03-01] [--end 2024-09-01]
```
Imprime el resumen y vuelca `results/trades.csv` y `results/equity.csv`.

## Formato CSV de datos

Columnas requeridas: `time` (epoch segundos, hora del servidor), `open`, `high`,
`low`, `close`. (El export de MT5 ya las trae.)

## Modelo de simulación (supuestos)

- **Paso = cierre de barra M15.** La decisión usa la barra M15 recién cerrada;
  el motor solo ve barras con `open_time < cierre actual` (sin lookahead).
- **SL intrabar:** se cierra si el rango `[low, high]` de la barra toca el SL.
- **Precio:** entradas a `ask` (BUY) / `bid` (SELL); cierres al lado contrario.
  Spread fijo configurable en `config_bt.SYMBOL_SPECS`.
- **PnL del deal = bruto** (lo que ve el Healer en vivo). La comisión se descuenta
  del balance aparte (apertura + cierre).
- **Stop-out del broker:** si el nivel de margen baja de `stop_out_level` (50% por
  defecto) el broker liquida todo y marca la cuenta reventada. Es mecánica de la
  cuenta, no de la estrategia.

## Specs y parámetros

- Specs del instrumento (point, tick_value, contract_size, leverage, spread,
  comisión, stop-out): `config_bt.SYMBOL_SPECS` — **ajústalos a tu broker real**.
- Parámetros de estrategia: por defecto `DEFAULTS` de `bot.strategy`. Para barrer
  configuraciones, llama `run_backtest(..., params=make_params(grinder_lots=0.03, ...))`.

## Limitaciones conocidas (v1)

- Spread y slippage fijos; sin modelado de swaps ni de gaps de fin de semana.
- Granularidad de relleno = barra M15 (no tick a tick); el SL se aproxima con el
  rango de la barra (sin secuencia intrabar exacta).
- La paridad de indicadores depende de `bot/indicators.py` (aproximaciones pandas
  de iATR/iRSI/iADX/iFractals de MT5), igual que el bot en vivo.

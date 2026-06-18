# bot_backtesting

Backtester del bot Sentinel. **No reimplementa la estrategia**: reutiliza el
`SentinelEngine` real de `bot/` e inyecta un broker y un feed de datos simulados.
Lo que se prueba aquí es exactamente el mismo código que opera en vivo.

## Arquitectura

```
fetch_data    descarga M15/M5/H4 reales de MT5 a memoria (fetch_frames); el
              run_backtest la llama solo, no hace falta generar CSVs antes
Market        feed historico M15/M5/H4 (sin lookahead: decide al cierre de barra)
SimBroker     broker simulado: posiciones, balance/equity/margen, SL auto-close,
              cierres parciales, deals para el Healer, stop-out de margen
BacktestEngine  = SentinelEngine real, solo sobrescribe los 2 seams de datos
                (_fetch_tick, _rates). Toda la logica de trading es heredada.
run_backtest  descarga datos, itera barras, corre engine.on_tick() y registra
              equity/trades
metrics       net profit, drawdown, win rate, profit factor, etc.
```

## Requisitos

```
pip install MetaTrader5 pandas numpy
```

`MetaTrader5` se importa por sus constantes (ORDER_TYPE/POSITION_TYPE/...) y, en
el nuevo flujo, **también para descargar las velas**: el terminal debe estar
abierto y logueado al correr `run_backtest`.

## Uso

Una sola orden. El backtester descarga las velas de MT5 por sí mismo para el
periodo pedido (más un margen de calentamiento para los indicadores):

```
python -m bot_backtesting.run_backtest --balance 5000 \
    --start-date 2026-06-12 --end-date 2026-06-16 [--output bot_backtesting/results]
```

Argumentos:
- `--balance` (requerido): balance inicial de la cuenta.
- `--start-date` / `--end-date` (requeridos): periodo `YYYY-MM-DD` (UTC). `end-date`
  es **inclusivo** (cubre todo ese día).
- `--output` (opcional): directorio para `trades.csv` y `equity.csv`
  (default `bot_backtesting/results`).
- `--symbol` (opcional): default `config.SYMBOL`.
- `--warmup-days` (opcional): días descargados antes de `start-date` para calentar
  indicadores (default 45; la estructura H4 necesita ~25 días de mercado).

Imprime el resumen y vuelca `output/trades.csv` y `output/equity.csv`.

### Export a CSV (opcional)

`fetch_data` y `gen_sample_data` siguen existiendo para cachear/inspeccionar datos,
pero ya **no son un paso previo obligatorio** del backtest:
```
python -m bot_backtesting.fetch_data --start 2024-01-01 --end 2024-12-31 \
    --out-dir bot_backtesting/data
```

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

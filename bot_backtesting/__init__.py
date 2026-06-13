"""Backtesting del bot Sentinel.

NO reimplementa la estrategia: reutiliza el SentinelEngine real de `bot/` y le
inyecta un broker simulado (SimBroker) y un feed de datos historicos (Market).
Asi el backtest ejercita EXACTAMENTE el mismo codigo que corre en vivo.
"""

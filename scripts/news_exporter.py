"""Exportador de calendario economico para el NewsGuard (bot/news.py), en Python.

Alternativa headless al EA MQL5 (scripts/CalendarExporter.mq5): en el VPS, colgar
un EA en un grafico de cada clon portable es fragil (el auto-arranque headless no
adjunta el EA de forma fiable y la carpeta MQL5 del clon no comparte la Common que
leen los bots). Este proceso hace lo mismo SIN depender de un grafico:

  1. Descarga el calendario de la semana actual y la siguiente de Forex Factory
     (JSON publico; fechas ISO con offset -> UTC exacto).
  2. Convierte cada evento al MISMO reloj que usa el bot: el epoch del SERVIDOR
     (tick.time). El offset servidor<->UTC se deriva en vivo atacando (solo
     lectura) un terminal MT5 ya corriendo -- el mismo que levantan los bots --,
     asi que no hay zonas horarias hardcodeadas y sigue el DST solo.
  3. Escribe el JSON en la carpeta COMUN de MetaQuotes
     (%APPDATA%\\MetaQuotes\\Terminal\\Common\\Files\\news_calendar.json), la
     UNICA ruta que leen todas las instancias Python de la maquina.

El esquema de salida es identico al del EA MQL5, asi que bot/news.py y la
regresion no cambian: {"generated_at", "server_time", "events":[{time, currency,
importance, name}]} con time en epoch del servidor.

Uso:
    python -m scripts.news_exporter            # bucle: refresca cada 15 min
    python -m scripts.news_exporter --once     # una pasada y sale (para scheduler)

Config por entorno:
    NEWS_CALENDAR_PATH   destino del JSON (default = carpeta Common de MetaQuotes)
    NEWS_REFRESH_MINUTES minutos entre refrescos (default 15)
    NEWS_MT5_PATH        terminal a atacar para el offset (default: autodescubre
                         de instances/*.env + el install base)
    NEWS_SYMBOL          simbolo para leer el tick (default XAUUSD+)
    NEWS_SERVER_TZ       zona IANA de respaldo si no hay terminal vivo
                         (default Europe/Bucharest = GMT+2/+3 como Vantage)
"""

import argparse
import glob
import json
import os
import sys
import time
import urllib.request
import zoneinfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTANCES_DIR = os.path.join(ROOT, "instances")

# Forex Factory solo publica la semana en curso en este endpoint (Sun-Sat, rola
# el fin de semana). Basta para el guard: news.py solo bloquea dentro de
# [T-pre, T+post] (pre max 2h), asi que un evento siempre es visible horas antes
# de entrar en su ventana mientras la semana en curso este cubierta.
FF_URLS = (
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
)
# Cloudflare responde 403/1010 sin un User-Agent de navegador (ver commit e94f563).
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_IMPORTANCE = {"high": "high", "medium": "moderate", "low": "low"}  # 'holiday'/'' se omiten


def _default_calendar_path():
    return os.environ.get(
        "NEWS_CALENDAR_PATH",
        os.path.join(os.environ.get("APPDATA", ""), "MetaQuotes", "Terminal",
                     "Common", "Files", "news_calendar.json"))


def _log(msg):
    print(f"[news_exporter {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ======================================================================
# 1) Descarga del calendario Forex Factory
# ======================================================================
def _fetch_ff():
    """Devuelve la lista cruda de eventos (this + next week), deduplicada."""
    seen, events, errors = set(), [], []
    for url in FF_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001 -- una URL caida no tumba el resto
            errors.append(f"{url}: {exc}")
            continue
        for e in data:
            key = (e.get("title"), e.get("country"), e.get("date"))
            if key in seen:
                continue
            seen.add(key)
            events.append(e)
    if not events and errors:
        raise RuntimeError("; ".join(errors))
    return events


def _parse_utc(date_str):
    """'2026-08-09T19:50:00-04:00' -> epoch UTC (int). None si no parsea."""
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(date_str).timestamp())
    except (ValueError, TypeError):
        return None


# ======================================================================
# 2) Offset servidor<->UTC (el bot compara contra tick.time del servidor)
# ======================================================================
def _terminal_paths():
    """Rutas de terminal a probar para leer el tick: override + instancias + base."""
    paths = []
    override = os.environ.get("NEWS_MT5_PATH", "")
    if override:
        paths.append(override)
    for env in sorted(glob.glob(os.path.join(INSTANCES_DIR, "*.env"))):
        name = os.path.basename(env)
        if name.startswith(("_", "example", "template")):
            continue
        try:
            with open(env, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("MT5_PATH="):
                        p = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if p and p not in paths:
                            paths.append(p)
        except OSError:
            continue
    base = r"C:\Program Files\MetaTrader 5\terminal64.exe"
    if base not in paths:
        paths.append(base)
    return paths


def _offset_from_mt5():
    """Offset (segundos) = tick.time(servidor) - now(UTC), atacando un terminal
    YA corriendo en solo lectura. None si ningun terminal responde."""
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return None
    symbol = os.environ.get("NEWS_SYMBOL", "XAUUSD+")
    for path in _terminal_paths():
        portable = os.sep + "MT5_instances" + os.sep in path
        kw = {"path": path, "timeout": 15000}
        if portable:
            kw["portable"] = True
        if not mt5.initialize(**kw):
            continue
        try:
            for s in (symbol, "XAUUSD", "EURUSD", "BTCUSD"):
                if mt5.symbol_select(s, True):
                    t = mt5.symbol_info_tick(s)
                    if t and t.time:
                        off = t.time - time.time()
                        return round(off / 60.0) * 60  # a minuto entero, sin jitter
        finally:
            mt5.shutdown()
    return None


def _offset_fallback():
    """Respaldo sin terminal vivo: offset de la zona del servidor (sigue el DST)."""
    tzname = os.environ.get("NEWS_SERVER_TZ", "Europe/Bucharest")
    try:
        from datetime import datetime
        tz = zoneinfo.ZoneInfo(tzname)
        return int(datetime.now(tz).utcoffset().total_seconds())
    except Exception:  # noqa: BLE001
        return 3 * 3600  # ultimo recurso: GMT+3 (verano Vantage)


# ======================================================================
# 3) Construccion y escritura del JSON (esquema del EA MQL5)
# ======================================================================
def build_payload(offset):
    events = []
    for e in _fetch_ff():
        imp = _IMPORTANCE.get(str(e.get("impact", "")).lower())
        if imp is None:                       # 'Holiday' / sin impacto
            continue
        utc = _parse_utc(e.get("date", ""))
        if utc is None:
            continue
        events.append({
            "time": utc + offset,             # epoch del SERVIDOR (como tick.time)
            "currency": str(e.get("country", "")).upper(),
            "importance": imp,
            "name": str(e.get("title", ""))[:80],
        })
    events.sort(key=lambda ev: ev["time"])
    server_now = int(time.time() + offset)
    return {"generated_at": server_now, "server_time": server_now, "events": events}


def write_atomic(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)


def run_once(path, cached_offset):
    offset = _offset_from_mt5()
    src = "MT5"
    if offset is None:
        offset = cached_offset if cached_offset is not None else _offset_fallback()
        src = "cache/tz (sin terminal vivo)"
    payload = build_payload(offset)
    write_atomic(path, payload)
    highs = sum(1 for e in payload["events"] if e["importance"] == "high")
    _log(f"{len(payload['events'])} eventos ({highs} HIGH) -> {path} "
         f"| offset {offset:+d}s via {src}")
    return offset


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="una pasada y salir")
    args = ap.parse_args()

    path = _default_calendar_path()
    refresh = int(os.environ.get("NEWS_REFRESH_MINUTES", "15")) * 60
    cached = None
    while True:
        try:
            cached = run_once(path, cached)
        except Exception as exc:  # noqa: BLE001 -- el exportador nunca debe morir
            _log(f"ERROR en la pasada: {exc}")
        if args.once:
            return
        time.sleep(refresh)


if __name__ == "__main__":
    main()

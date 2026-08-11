"""NewsGuard: bloqueo duro por noticias de alto impacto (Fase 1).

El paquete MetaTrader5 de Python NO expone el calendario economico (eso es
API de MQL5), asi que el dato entra por archivo: un exportador MQL5
(scripts/CalendarExporter.mq5) corre en el terminal y vuelca los eventos de
las proximas horas a un JSON en la carpeta COMUN de MetaQuotes. Este modulo
lo lee y responde una sola pregunta: ¿estamos dentro de la ventana de una
noticia roja relevante?

Fase 1 = solo el bloqueo innegociable: eventos de importancia HIGH (rojas)
de las divisas configuradas (default USD, que es lo que mueve al oro). La
escala continua 0-1 (NRI) es Fase 2 y se construira solo si los datos de la
observacion la justifican.

Ventanas: cada evento proyecta [T-pre, T+post]. El guard esta activo si el
instante actual cae dentro de CUALQUIER ventana; los clusters de noticias
seguidas quedan cubiertos por la union natural de ventanas solapadas (el
bloqueo corre hasta T+post del ULTIMO evento del cluster).

Fail-safe PERMISIVO CON ALERTA (decision de diseño): si el archivo falta,
esta ilegible o el exportador dejo de refrescar, el guard responde inactivo
y loguea ERROR una sola vez (dedupe). Bloquear por defecto dejaria el bot
mudo para siempre ante un archivo roto. Se sigue usando el ultimo snapshot
conocido mientras exista: los eventos futuros ya descargados siguen siendo
validos aunque el exportador este caido.

Tiempos: los timestamps del JSON vienen de TimeTradeServer() del terminal,
la MISMA base temporal que usa el bot (tick.time). Sin zonas horarias.
"""

import json
import os


class NewsGuard:
    def __init__(self, path, logger):
        self.path = path
        self.log = logger
        self._events = []           # [{time, currency, importance, name}] orden cronologico
        self._generated_at = 0      # epoch (server time) del ultimo export
        self._mtime = 0.0           # mtime del archivo ya cargado
        self._next_load_check = 0   # throttle de os.path.getmtime (1 vez/min)
        self._last_active = None    # dedupe del log de transicion de ventana
        self._alerted_missing = False
        self._alerted_stale = False

    # ==============================================================
    # Carga del snapshot (throttled; jamas tumba el tick)
    # ==============================================================
    def _load(self, now):
        if now < self._next_load_check:
            return
        self._next_load_check = now + 60
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            if not self._alerted_missing:
                self.log.write(
                    "ERROR",
                    f"NewsGuard: calendario no disponible en {self.path}. "
                    f"¿Exportador MQL5 corriendo? Guard PERMISIVO hasta que aparezca.")
                self._alerted_missing = True
            return
        if self._alerted_missing:
            self.log.write("SYSTEM", "NewsGuard: calendario de noticias restablecido.")
            self._alerted_missing = False
        if mtime == self._mtime:
            return
        try:
            with open(self.path, encoding="utf-8-sig", errors="replace") as f:
                data = json.load(f)
            events = []
            for e in data.get("events", []):
                events.append({
                    "time": int(e["time"]),
                    "currency": str(e.get("currency", "")).upper(),
                    "importance": str(e.get("importance", "")).lower(),
                    "name": str(e.get("name", ""))[:80],
                })
            events.sort(key=lambda ev: ev["time"])
            self._events = events
            self._generated_at = int(data.get("generated_at", 0)) or int(mtime)
            self._mtime = mtime
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if not self._alerted_missing:
                self.log.write(
                    "ERROR",
                    f"NewsGuard: calendario ilegible ({exc}). Guard PERMISIVO; "
                    f"se conserva el snapshot anterior si existia.")
                self._alerted_missing = True

    def _check_staleness(self, now):
        """Alerta (con dedupe) si el exportador lleva horas sin refrescar."""
        stale = self._generated_at > 0 and (now - self._generated_at) > 6 * 3600
        if stale and not self._alerted_stale:
            self.log.write(
                "ERROR",
                "NewsGuard: el calendario lleva >6h sin refrescarse "
                "(¿exportador MQL5 caido?). Se sigue usando el ultimo snapshot.")
            self._alerted_stale = True
        elif not stale:
            self._alerted_stale = False

    # ==============================================================
    # Consulta
    # ==============================================================
    @staticmethod
    def _parse_currencies(currencies):
        return {c.strip().upper() for c in str(currencies).split(",") if c.strip()}

    def in_window(self, now, pre, post, currencies):
        """(activo, motivo) SIN log de transiciones (consulta silenciosa).

        Activo si `now` cae en [T-pre, T+post] de algun evento HIGH de las
        divisas dadas. El motivo reporta el evento mas proximo por delante
        (o el mas reciente si todos quedaron atras), para que el HUD y los
        logs digan QUE noticia esta mandando.
        """
        self._load(now)
        wanted = self._parse_currencies(currencies)
        best = None  # evento cuya ventana contiene `now`, priorizando el futuro
        for e in self._events:
            if e["importance"] != "high" or e["currency"] not in wanted:
                continue
            if e["time"] - pre <= now <= e["time"] + post:
                if best is None:
                    best = e
                elif best["time"] < now <= e["time"]:
                    best = e  # prefiere el que aun no ocurrio (el que viene)
        if best is None:
            return False, None
        delta = best["time"] - now
        if delta >= 0:
            reason = f"{best['name']} ({best['currency']}) en {delta // 60} min"
        else:
            reason = f"{best['name']} ({best['currency']}) hace {(-delta) // 60} min"
        return True, reason

    def check(self, now, pre, post, currencies):
        """Consulta principal del tick: como in_window + staleness + log de
        transiciones (ACTIVA/liberada, una vez por cambio de estado)."""
        active, reason = self.in_window(now, pre, post, currencies)
        self._check_staleness(now)
        if active != self._last_active:
            if active:
                self.log.write("NOTICIAS", f"Ventana de noticia ACTIVA: {reason}.")
            elif self._last_active is not None:
                self.log.write("NOTICIAS", "Ventana de noticia liberada.")
            self._last_active = active
        return active, reason

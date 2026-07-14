"""Capa de datos sobre Supabase (REST/PostgREST via supabase-py).

El bot usa la service-role key (bypassa RLS) y filtra SIEMPRE por USER_ID.
Multi-instancia: cada proceso lleva su USER_ID/SYMBOL en el entorno (.env).

Diseño clave:
  - Resiliencia: cachea la ultima config buena; ante un hipo de red reusa la
    cache en vez de tumbar el motor (load_config/get_instance nunca lanzan).
  - Logging no bloqueante: log_sink() encola cada write() y un hilo de fondo
    hace INSERT por lotes en bot_logs. Nunca bloquea el hilo del motor (un
    INSERT REST son 50-200 ms). Si Supabase falla, el CSV local (logger.py)
    conserva el registro.
"""

import datetime
import queue
import threading

from supabase import create_client

from . import config


def _utcnow_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class Database:
    def __init__(self):
        self.client = None
        self._cfg_cache = None       # ultima config buena (dict)
        self._inst_cache = None      # ultima fila de instancia buena (dict)
        # Buffer de logs + hilo flusher
        self._log_q = queue.Queue(maxsize=10000)
        self._stop = threading.Event()
        self._flusher = None
        self._batch_size = 50
        self._flush_interval = 1.5   # segundos

    # ==============================================================
    # Conexion
    # ==============================================================
    def connect(self):
        if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
            raise RuntimeError(
                "Faltan SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY en el entorno/.env"
            )
        self.client = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_ROLE_KEY)
        self._stop.clear()
        self._flusher = threading.Thread(target=self._flush_loop, name="log-flusher", daemon=True)
        self._flusher.start()

    # ==============================================================
    # Config de estrategia (bot_config)
    # ==============================================================
    def load_config(self):
        """Devuelve (config_dict, status). Ante fallo de red reusa la cache."""
        try:
            res = (
                self.client.table("bot_config")
                .select("*")
                .eq("user_id", config.USER_ID)
                .eq("symbol", config.SYMBOL)
                .eq("is_active", True)
                .limit(1)
                .execute()
            )
            rows = res.data or []
            if rows:
                self._cfg_cache = dict(rows[0])
                return self._cfg_cache, rows[0].get("status", "INACTIVE")
            # No hay fila activa: no pisar la cache; reportar INACTIVE.
            return (self._cfg_cache or {}), "INACTIVE"
        except Exception:  # noqa: BLE001  (hipo de red no debe tumbar el motor)
            if self._cfg_cache is not None:
                return self._cfg_cache, self._cfg_cache.get("status", "INACTIVE")
            return {}, "INACTIVE"

    # ==============================================================
    # Instancia / interruptor maestro (bot_instances)
    # ==============================================================
    def get_instance(self):
        """Devuelve la fila de bot_instances del usuario (dict) o {} si no existe.

        is_active True  = operar (abrir + gestionar).
        is_active False = close-only (no abrir; seguir gestionando/cerrando).
        Ante fallo de red reusa la ultima fila buena para no hacer churn.
        """
        try:
            res = (
                self.client.table("bot_instances")
                .select("*")
                .eq("user_id", config.USER_ID)
                .limit(1)
                .execute()
            )
            rows = res.data or []
            if rows:
                self._inst_cache = dict(rows[0])
                return self._inst_cache
            return {}
        except Exception:  # noqa: BLE001
            return self._inst_cache or {}

    def report(self, bot_status):
        """Escribe bot_status + heartbeat en bot_instances (best-effort)."""
        if self.client is None:
            return
        now_iso = _utcnow_iso()
        try:
            self.client.table("bot_instances").update(
                {"bot_status": bot_status, "last_heartbeat": now_iso, "updated_at": now_iso}
            ).eq("user_id", config.USER_ID).execute()
        except Exception:  # noqa: BLE001
            pass

    # ==============================================================
    # Logging (sink no bloqueante + flusher por lotes)
    # ==============================================================
    def log_sink(self, log_type, message, ts, price=0.0, lots=0.0, balance=0.0, ticket=0):
        """Sink del Logger. Encola la fila; NO bloquea el hilo del motor.

        Firma identica al sink previo (db.insert_log) para no tocar logger.py.
        Si la cola esta llena, descarta (el CSV local conserva el registro).
        """
        row = {
            "user_id": config.USER_ID,
            "symbol": config.SYMBOL,
            "log_type": log_type,
            "message": message,
            "price": float(price),
            "lots": float(lots),
            "balance": float(balance),
            "ticket": int(ticket or 0),
        }
        try:
            self._log_q.put_nowait(row)
        except queue.Full:
            pass

    def _drain(self):
        batch = []
        while len(batch) < self._batch_size:
            try:
                batch.append(self._log_q.get_nowait())
            except queue.Empty:
                break
        return batch

    def insert_logs(self, rows):
        """INSERT por lotes en bot_logs. Lanza si falla (lo captura el flusher)."""
        if not rows or self.client is None:
            return
        self.client.table("bot_logs").insert(rows).execute()

    def _flush_loop(self):
        while not self._stop.is_set():
            batch = self._drain()
            if batch:
                try:
                    self.insert_logs(batch)
                except Exception:  # noqa: BLE001  (best-effort; no reintentar en bucle)
                    pass
            # Si vaciamos un lote completo, puede haber mas pendiente: no dormir.
            if len(batch) < self._batch_size:
                self._stop.wait(self._flush_interval)
        # Flush final al cerrar.
        batch = self._drain()
        if batch:
            try:
                self.insert_logs(batch)
            except Exception:  # noqa: BLE001
                pass

    # ==============================================================
    # Cierre
    # ==============================================================
    def close(self):
        self._stop.set()
        if self._flusher is not None:
            self._flusher.join(timeout=5)
            self._flusher = None
        self.client = None

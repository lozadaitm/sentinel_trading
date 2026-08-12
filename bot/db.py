"""Capa de datos sobre Supabase (REST/PostgREST via supabase-py).

El bot usa la service-role key (bypassa RLS) y filtra SIEMPRE por USER_ID.
Multi-instancia: cada proceso lleva su USER_ID/SYMBOL en el entorno (.env).

Multi-bot: un mismo usuario puede correr varios motores contra la MISMA cuenta
(BOT_ID 'm15' = Sentinel, 'm5' = Grinder). Todas las consultas filtran ademas
por BOT_ID para que los procesos no se pisen la config, el heartbeat ni el
snapshot. Ver migrations/002_bot_id.sql.

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
                .eq("bot_id", config.BOT_ID)
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
                .eq("bot_id", config.BOT_ID)
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

    def clear_force_close(self):
        """Consuma el comando de cierre forzado de ESTA instancia.

        Ademas de resetear el flag deja is_active=false: tras un cierre forzado
        el bot no debe reabrir aunque el usuario no haya alcanzado a togglear.
        Best-effort, pero devuelve False si no se pudo escribir (el caller
        decide si reintenta en el proximo refresh).
        """
        if self.client is None:
            return False
        try:
            self.client.table("bot_instances").update(
                {"force_close": False, "is_active": False, "updated_at": _utcnow_iso()}
            ).eq("user_id", config.USER_ID).eq("bot_id", config.BOT_ID).execute()
            # Mantener la cache coherente para no re-disparar con una fila vieja.
            if self._inst_cache is not None:
                self._inst_cache["force_close"] = False
                self._inst_cache["is_active"] = False
            return True
        except Exception:  # noqa: BLE001
            return False

    # ==============================================================
    # Preferencias a nivel cuenta (account_settings)
    # ==============================================================
    def get_account_settings(self):
        """Fila de account_settings del usuario (dict) o {} si no existe/falla.

        Es a nivel CUENTA (sin bot_id): el objetivo de ganancia se mide contra
        el equity, que ambos motores comparten.
        """
        if self.client is None:
            return {}
        try:
            res = (
                self.client.table("account_settings")
                .select("*")
                .eq("user_id", config.USER_ID)
                .limit(1)
                .execute()
            )
            rows = res.data or []
            return dict(rows[0]) if rows else {}
        except Exception:  # noqa: BLE001
            return {}

    def claim_profit_target(self):
        """Reclama el evento 'objetivo alcanzado' de forma atomica.

        UPDATE ... WHERE target_reached_at IS NULL: solo un motor (m15 o m5)
        gana la fila, asi el email sale UNA vez aunque ambos detecten el
        cruce en el mismo heartbeat. Devuelve True si este proceso gano.
        """
        if self.client is None:
            return False
        try:
            res = (
                self.client.table("account_settings")
                .update({"target_reached_at": _utcnow_iso(), "updated_at": _utcnow_iso()})
                .eq("user_id", config.USER_ID)
                .is_("target_reached_at", "null")
                .execute()
            )
            return bool(res.data)
        except Exception:  # noqa: BLE001
            return False

    def deactivate_all_instances(self):
        """Apaga TODAS las instancias del usuario (is_active=false = close-only).

        Se usa al alcanzar el objetivo de ganancia: el evento es de cuenta,
        no de motor, asi que apaga m15 y m5 a la vez. Best-effort.
        """
        if self.client is None:
            return
        try:
            self.client.table("bot_instances").update(
                {"is_active": False, "updated_at": _utcnow_iso()}
            ).eq("user_id", config.USER_ID).execute()
            if self._inst_cache is not None:
                self._inst_cache["is_active"] = False
        except Exception:  # noqa: BLE001
            pass

    def has_pending_commission(self):
        """True si el usuario debe una comision de retiro (withdrawals PENDING).

        Con deuda pendiente la instancia no puede operar: el guard del loop
        revierte is_active=true a close-only. Ante fallo de red devuelve False
        (un hipo de Supabase no debe frenar el trading).
        """
        if self.client is None:
            return False
        try:
            res = (
                self.client.table("withdrawals")
                .select("id")
                .eq("user_id", config.USER_ID)
                .eq("status", "PENDING")
                .limit(1)
                .execute()
            )
            return bool(res.data)
        except Exception:  # noqa: BLE001
            return False

    def get_user_email(self):
        """Email del usuario (auth.users) via Admin API (service-role). None si falla.

        Se usa para identificar en la UI a que usuario corresponde esta instancia.
        El email casi no cambia: conviene leerlo una vez al arrancar y cachearlo.
        """
        if self.client is None:
            return None
        try:
            resp = self.client.auth.admin.get_user_by_id(config.USER_ID)
            user = getattr(resp, "user", resp)  # UserResponse.user o el user directo
            return getattr(user, "email", None)
        except Exception:  # noqa: BLE001  (no bloquear el arranque por esto)
            return None

    def report(self, bot_status):
        """Escribe bot_status + heartbeat en bot_instances (best-effort)."""
        if self.client is None:
            return
        now_iso = _utcnow_iso()
        try:
            self.client.table("bot_instances").update(
                {"bot_status": bot_status, "last_heartbeat": now_iso, "updated_at": now_iso}
            ).eq("user_id", config.USER_ID).eq("bot_id", config.BOT_ID).execute()
        except Exception:  # noqa: BLE001
            pass

    # ==============================================================
    # Snapshot en vivo para el dashboard (bot_state)
    # ==============================================================
    def get_state_flow_info(self):
        """(initial_balance, last_flow_ticket, baseline_applied_at) de bot_state.

        (None, None, None) si no hay fila aun. Se lee una vez al arrancar para
        no resetear la base, re-contar flujos ni re-aplicar un reinicio de P&L
        en cada restart del proceso (ver bot/main.py::setup,
        _reconcile_capital_flows y _apply_baseline_reset).
        """
        if self.client is None:
            return None, None, None
        try:
            res = (
                self.client.table("bot_state")
                .select("initial_balance,last_flow_ticket,baseline_applied_at")
                .eq("user_id", config.USER_ID)
                .eq("bot_id", config.BOT_ID)
                .limit(1)
                .execute()
            )
            rows = res.data or []
            if rows:
                ib = rows[0].get("initial_balance")
                tk = rows[0].get("last_flow_ticket")
                return (
                    float(ib) if ib is not None else None,
                    int(tk) if tk is not None else None,
                    rows[0].get("baseline_applied_at"),
                )
        except Exception:  # noqa: BLE001
            pass
        return None, None, None

    def report_state(self, *, symbol, balance, equity, margin_used, margin_free,
                      floating_pnl, open_positions, initial_balance,
                      last_flow_ticket=None, baseline_applied_at=None):
        """Upsert del snapshot en vivo (bot_state) para el dashboard. Best-effort."""
        if self.client is None:
            return
        row = {
            "user_id": config.USER_ID,
            "bot_id": config.BOT_ID,
            "symbol": symbol,
            "balance": float(balance),
            "equity": float(equity),
            "margin_used": float(margin_used),
            "margin_free": float(margin_free),
            "floating_pnl": float(floating_pnl),
            "open_positions": int(open_positions),
            "initial_balance": float(initial_balance),
            "last_flow_ticket": int(last_flow_ticket or 0),
            "baseline_applied_at": baseline_applied_at,
            "updated_at": _utcnow_iso(),
        }
        try:
            self.client.table("bot_state").upsert(row, on_conflict="user_id,bot_id").execute()
        except Exception:  # noqa: BLE001
            pass

    # ==============================================================
    # Posiciones (bot_positions): tabla principal de posiciones para el
    # dashboard. Vivas se refrescan en cada heartbeat; al cerrarse quedan
    # como historico (status=CLOSED).
    # ==============================================================
    def get_open_tickets(self):
        """Tickets marcados status='OPEN' en bot_positions (para reconciliar
        al arrancar, ver bot/main.py::setup). Set vacio ante fallo/sin datos.
        """
        if self.client is None:
            return set()
        try:
            res = (
                self.client.table("bot_positions")
                .select("ticket")
                .eq("user_id", config.USER_ID)
                .eq("bot_id", config.BOT_ID)
                .eq("status", "OPEN")
                .execute()
            )
            return {row["ticket"] for row in (res.data or [])}
        except Exception:  # noqa: BLE001
            return set()

    def upsert_positions(self, rows):
        """Upsert en bot_positions (posiciones abiertas y/o cierres). Best-effort.

        Cada row debe traer al menos user_id/ticket; columnas ausentes NO se
        tocan en un UPDATE por conflicto (permite cerrar una posicion mandando
        solo status/close_price/... sin repetir los datos de apertura).
        """
        if self.client is None or not rows:
            return
        now_iso = _utcnow_iso()
        for row in rows:
            row.setdefault("user_id", config.USER_ID)
            row.setdefault("bot_id", config.BOT_ID)
            row["updated_at"] = now_iso
        try:
            self.client.table("bot_positions").upsert(rows, on_conflict="user_id,ticket").execute()
        except Exception:  # noqa: BLE001
            pass

    # ==============================================================
    # Velas OHLC (bot_candles): datos de precio para la grafica del
    # dashboard. Solo las publica el proceso m15 (ver bot/main.py).
    # ==============================================================
    def upsert_candles(self, rows):
        """Upsert de velas en bot_candles. Best-effort.

        Cada row trae symbol/timeframe/ts/open/high/low/close; user_id y
        updated_at se completan aqui. La vela en formacion se re-upsertea en
        cada heartbeat (mismo ts -> se actualiza OHLC).
        """
        if self.client is None or not rows:
            return
        now_iso = _utcnow_iso()
        for row in rows:
            row.setdefault("user_id", config.USER_ID)
            row["updated_at"] = now_iso
        try:
            self.client.table("bot_candles").upsert(
                rows, on_conflict="user_id,symbol,timeframe,ts"
            ).execute()
        except Exception:  # noqa: BLE001
            pass

    def prune_candles(self, days=30):
        """Borra velas mas viejas que `days` (retencion). Best-effort.

        El pequeño desfase entre la hora del servidor MT5 (dominio de ts) y
        UTC es irrelevante a escala de 30 dias.
        """
        if self.client is None:
            return
        cutoff = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=days)
        ).isoformat()
        try:
            self.client.table("bot_candles").delete().eq(
                "user_id", config.USER_ID
            ).lt("ts", cutoff).execute()
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
            "bot_id": config.BOT_ID,
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

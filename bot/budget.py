"""Gobierno de margen para bots que conviven en una misma cuenta MT5.

Problema: un usuario corre el Sentinel M15 y el Grinder M5 contra la misma
cuenta. Los magics separan la LOGICA (cada motor solo ve sus posiciones) pero
NO el MARGEN: ambos comparten equity, margen libre y nivel de stop-out. Sin
gobierno, el M5 puede consumir el margen justo en el tick en que el M15
necesita abrir su Hedge Lock.

Principio rector: **asimetria de criticidad**. El M15 no lleva SL catastrofico
por diseño (ver docs/memory/bot-design-constraints); su Hedge Lock es la unica
red que congela la perdida. El M5 lleva SL y sus scalps son desechables. Ante
escasez de margen, el M5 cede SIEMPRE.

De ahi las dos reglas duras:
  1. Una orden PROTECTORA (el hedge) nunca se bloquea por presupuesto.
  2. El bot junior reserva de antemano el margen que el senior necesitara para
     su proxima protectora, y solo compite por lo que sobra.

La coordinacion NO pasa por la DB: ambos procesos leen las posiciones del otro
directamente de MT5 (Broker.positions_of_magic). Latencia cero, sin race de
red, y sigue funcionando si uno de los procesos muere.
"""

import MetaTrader5 as mt5

# Naturaleza de la orden que se quiere abrir.
KIND_PROTECTIVE = "protective"   # Hedge Lock: reduce riesgo. Exento de todo tope.
KIND_ADDITIVE = "additive"       # entry / recovery / rescate / scalp: añade riesgo.

# Postura segun el nivel de margen de la CUENTA (compartido entre bots).
POSTURE_NORMAL = "NORMAL"        # opera con normalidad
POSTURE_NO_ADD = "NO_ADD"        # no abre aditivas; sigue gestionando y cerrando
POSTURE_FLATTEN = "FLATTEN"      # ademas, deberia cerrar lo propio y salir

# Rol dentro de la cuenta.
ROLE_SENIOR = "senior"           # M15: manda, no reserva para nadie
ROLE_JUNIOR = "junior"           # M5: cede, reserva para el senior

DEFAULTS = {
    "equity_weight": 1.0,        # fraccion del equity que este bot considera suya
    "margin_cap_pct": 100.0,     # techo de margen propio, en % del equity (0 = sin tope)
    "peer_reserve_mult": 1.5,    # colchon sobre la protectora estimada del vecino
    "ml_no_add": 0.0,            # margin level bajo el cual no se abren aditivas (0 = off)
    "ml_flatten": 0.0,           # margin level bajo el cual hay que salir (0 = off)
    "margin_safety_mult": 1.1,   # margen requerido x este factor (igual que CheckFreeMargin)
}


class MarginGovernor:
    """Decide si este bot puede abrir, dado lo que el resto de la cuenta necesita.

    `cfg_provider` es un callable sin argumentos que devuelve el dict de config
    vigente (p.ej. `lambda: engine.cfg`). Se resuelve contra DEFAULTS propios
    para no acoplar este modulo a los DEFAULTS de ningun motor.
    """

    def __init__(self, broker, cfg_provider, role=ROLE_SENIOR, peer_magics=(), logger=None):
        self.b = broker
        self._cfg = cfg_provider
        self.role = role
        self.peer_magics = tuple(peer_magics)
        self.log = logger
        self.last_block = None    # motivo del ultimo bloqueo (lo lee el HUD)
        self._last_logged = None  # dedupe: solo se loguea el cambio de motivo
        self._quiet = False       # modo sonda: evalua sin loguear ni mover el dedupe

    # --------------------------------------------------------------
    def _p(self, key):
        return (self._cfg() or {}).get(key, DEFAULTS[key])

    def _block(self, reason):
        """Registra el motivo del bloqueo y devuelve False.

        Dedupe por motivo: on_tick corre cada segundo y un bloqueo sostenido
        inundaria bot_logs. Solo se emite cuando el motivo CAMBIA.
        """
        self.last_block = reason
        if self._quiet:
            return False
        if self.log is not None and reason != self._last_logged:
            self.log.write("BUDGET", f"Apertura bloqueada: {reason}",
                           balance=self.b.account_balance())
            self._last_logged = reason
        return False

    def _allow(self):
        self.last_block = None
        if not self._quiet:
            self._last_logged = None  # el proximo bloqueo vuelve a ser noticia
        return True

    # --------------------------------------------------------------
    # Lectura de estado
    # --------------------------------------------------------------
    def sizing_equity(self):
        """Equity nominal asignado a este bot = equity real x su peso.

        Los motores dimensionan el lote contra esto en vez de contra el equity
        completo, para que dos bots no escalen ambos sobre el mismo capital.
        """
        return self.b.account_equity() * float(self._p("equity_weight"))

    def posture(self):
        """Postura actual segun el margin level de la cuenta."""
        ml = self.b.margin_level()
        flatten = float(self._p("ml_flatten"))
        no_add = float(self._p("ml_no_add"))
        if flatten > 0 and ml < flatten:
            return POSTURE_FLATTEN
        if no_add > 0 and ml < no_add:
            return POSTURE_NO_ADD
        return POSTURE_NORMAL

    def should_flatten(self):
        """True si este bot deberia cerrar lo suyo y quedarse fuera."""
        return self.posture() == POSTURE_FLATTEN

    def peer_reserve(self):
        """Margen a dejar libre para la proxima orden PROTECTORA de los vecinos.

        Estimacion: el hedge del vecino cubrira su exposicion neta descubierta,
        al lado contrario y por el mismo volumen. Se multiplica por
        `peer_reserve_mult` como colchon para slippage y para el siguiente leg.

        El senior no reserva para nadie: es el que tiene prioridad.
        """
        if self.role == ROLE_SENIOR or not self.peer_magics:
            return 0.0
        mult = float(self._p("peer_reserve_mult"))
        total = 0.0
        for magic in self.peer_magics:
            net = 0.0
            for p in self.b.positions_of_magic(magic):
                net += p.volume if p.type == mt5.POSITION_TYPE_BUY else -p.volume
            if net == 0:
                continue  # ya esta cubierto: no hay protectora pendiente
            hedge_type = mt5.ORDER_TYPE_SELL if net > 0 else mt5.ORDER_TYPE_BUY
            total += self.b.order_margin(hedge_type, abs(net)) * mult
        return total

    # --------------------------------------------------------------
    # Decision
    # --------------------------------------------------------------
    def probe(self, lots, order_type, kind=KIND_ADDITIVE):
        """Igual que can_open pero SIN loguear: para el HUD y los paneles.

        El HUD se recalcula en cada tick aunque no se este intentando abrir
        nada; usar can_open ahi inundaria bot_logs de eventos BUDGET falsos.
        Deja `last_block` actualizado para poder mostrar el motivo.
        """
        self._quiet = True
        try:
            return self.can_open(lots, order_type, kind)
        finally:
            self._quiet = False

    def can_open(self, lots, order_type, kind=KIND_ADDITIVE):
        """True si este bot puede abrir `lots` sin comprometer a la cuenta.

        Las PROTECTORAS pasan siempre: no se pre-filtran por margen ni por
        ningun tope. Si el broker no puede, que lo rechace el broker y quede el
        retcode real en el log (ver SentinelEngine._open_hedge).
        """
        if kind == KIND_PROTECTIVE:
            return True

        posture = self.posture()
        if posture != POSTURE_NORMAL:
            ml = self.b.margin_level()
            return self._block(f"postura {posture} (margin level {ml:.0f}%)")

        required = self.b.order_margin(order_type, lots)
        if required <= 0:
            return self._block("MT5 no devolvio el margen requerido")
        required *= float(self._p("margin_safety_mult"))

        # 1. Margen libre real, descontando lo reservado para las protectoras
        #    pendientes de los vecinos.
        reserve = self.peer_reserve()
        available = self.b.margin_free() - reserve
        if required > available:
            return self._block(
                f"margen insuficiente (req {required:.2f}, libre {self.b.margin_free():.2f}, "
                f"reservado para el vecino {reserve:.2f})")

        # 2. Techo de margen propio: acota cuanto de la cuenta puede acaparar
        #    este bot aunque haya margen libre de sobra.
        cap_pct = float(self._p("margin_cap_pct"))
        if cap_pct > 0:
            cap = self.b.account_equity() * cap_pct / 100.0
            own = self.b.own_margin()
            if own + required > cap:
                return self._block(
                    f"techo propio superado (usado {own:.2f} + req {required:.2f} > "
                    f"cap {cap:.2f} = {cap_pct:.0f}% del equity)")

        return self._allow()

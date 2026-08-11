"""CycleLedger: identidad de cada operacion del ciclo por ROL, no por conteo.

Motivo (auditoria 6-11 ago, ver docs/memory/unwind-orphans-hedge-count-state):
la maquina de estados por `len(positions)` pierde la identidad en cuanto un
subsistema rompe el par OP1<->OP2. Un Hedge Lock cuya OP1 murio quedaba con
count==1 y era re-etiquetado como "OP1", recibiendo un hedge encima
(hedge-sobre-hedge) y ciclos enteros de Recovery/OP4 sobre una base falsa.

El ledger mantiene, por ticket vivo:
  - role:   ENTRY | HEDGE | RECOVERY | RESCUE | ORPHAN
  - covers: para un HEDGE, el ticket del leg que congela (su par)

Fuentes de identidad, por prioridad:
  1. Registro explicito al abrir (register(), llamado por strategy).
  2. Persistencia JSON por instancia: sobrevive restarts. Sin ella, un hedge
     huerfano de un ciclo muerto se emparejaria por comment con la ENTRY nueva
     del ciclo siguiente (lados opuestos), que es exactamente el bug historico.
  3. Rehidratacion por comment de MT5 (el broker los preserva): fallback para
     posiciones que el ledger nunca vio (crash entre apertura y save).

Reglas de reconciliacion (sync, cada tick):
  - Un HEDGE cuyo leg cubierto murio se degrada a ORPHAN (queda registrado:
    nunca vuelve a ser emparejado por inferencia).
  - Si no hay ENTRY viva y existe un huerfano SIN cobertura, el mas antiguo se
    promueve a ENTRY: es un leg direccional desnudo y estructuralmente es una
    OP1 (el hedge monitor lo congelara si cae). Los huerfanos CUBIERTOS son
    pares congelados y no se promueven.

El modulo no importa MetaTrader5 a proposito: solo compara atributos de las
posiciones entre si (ticket, type, time, comment), asi los tests corren sin
terminal. type: 0 == BUY, 1 == SELL (valores de POSITION_TYPE_*).
"""

import json
import os

ROLE_ENTRY = "ENTRY"
ROLE_HEDGE = "HEDGE"
ROLE_RECOVERY = "RECOVERY"
ROLE_RESCUE = "RESCUE"
ROLE_ORPHAN = "ORPHAN"

# Prefijos de comment -> rol (mismos textos que usa strategy al abrir).
_COMMENT_ROLES = (
    ("SMC", ROLE_ENTRY),
    ("Hedge", ROLE_HEDGE),
    ("Recovery", ROLE_RECOVERY),
    ("Sentinel Op 4", ROLE_RESCUE),
)


def role_from_comment(comment):
    """Rol declarado por el comment de apertura; ORPHAN si no se reconoce
    (trades manuales con nuestro magic, comments truncados por el broker)."""
    c = comment or ""
    for prefix, role in _COMMENT_ROLES:
        if c.startswith(prefix):
            return role
    return ROLE_ORPHAN


class CycleLedger:
    def __init__(self, path=None, logger=None):
        self.path = path
        self.log = logger
        self.rec = {}  # ticket(int) -> {"role": str, "covers": int|None}
        self._load()

    # --------------------------------------------------------------
    # Persistencia (JSON atomico; corrupto o ausente => ledger vacio,
    # sync() rehidrata por comment en el primer tick)
    # --------------------------------------------------------------
    def _load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.rec = {int(t): {"role": r["role"], "covers": r.get("covers")}
                        for t, r in data.get("positions", {}).items()}
        except (OSError, ValueError, KeyError, TypeError) as e:
            self.rec = {}
            self._log_write("LEDGER", f"Ledger ilegible ({e}); se rehidrata por comments de MT5.")

    def _save(self):
        if not self.path:
            return
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"positions": {str(t): r for t, r in self.rec.items()}}, f, indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            # Best-effort: sin disco el bot sigue; el riesgo es solo un restart.
            self._log_write("LEDGER", f"No se pudo persistir el ledger: {e}")

    def _log_write(self, log_type, message, ticket=0):
        if self.log is not None:
            self.log.write(log_type, message, ticket=ticket)

    # --------------------------------------------------------------
    # Registro explicito (en la apertura, con el ticket del broker)
    # --------------------------------------------------------------
    def register(self, ticket, role, covers=None):
        if not ticket:
            return  # SHADOW_MODE o rechazo: no hay ticket real que registrar
        self.rec[int(ticket)] = {"role": role, "covers": int(covers) if covers else None}
        self._save()

    def role_of(self, ticket):
        r = self.rec.get(ticket)
        return r["role"] if r else None

    # --------------------------------------------------------------
    # Reconciliacion por tick
    # --------------------------------------------------------------
    def sync(self, positions):
        """Reconcilia el ledger con las posiciones vivas del magic.

        Olvida tickets muertos, adopta desconocidos por comment, degrada
        hedges viudos a ORPHAN y promueve un huerfano solitario a ENTRY.
        """
        live = {p.ticket: p for p in positions}
        changed = False

        # 1. Olvidar tickets que ya no existen en MT5.
        for t in [t for t in self.rec if t not in live]:
            del self.rec[t]
            changed = True

        # 2. Adoptar desconocidos: primero los no-hedge (para que los hedges
        #    desconocidos tengan candidatos de par), en orden de apertura.
        unknown = sorted((p for p in positions if p.ticket not in self.rec),
                         key=lambda p: (p.time, p.ticket))
        for p in (q for q in unknown if role_from_comment(q.comment) != ROLE_HEDGE):
            role = role_from_comment(p.comment)
            if role == ROLE_ENTRY and self._live_entry_ticket(live) is not None:
                # Dos ENTRY vivas es una anomalia (no deberia poder abrirse la
                # segunda); se trata como aditiva para no romper el par real.
                role = ROLE_RECOVERY
                self._log_write("LEDGER", "ENTRY duplicada adoptada como RECOVERY.", ticket=p.ticket)
            self.rec[p.ticket] = {"role": role, "covers": None}
            changed = True
        for p in (q for q in unknown if role_from_comment(q.comment) == ROLE_HEDGE):
            target = self._pair_candidate(p, live)
            self.rec[p.ticket] = {
                "role": ROLE_HEDGE if target else ROLE_ORPHAN,
                "covers": target,
            }
            if target is None:
                self._log_write("LEDGER", "Hedge sin leg que cubrir adoptado como ORPHAN.",
                                ticket=p.ticket)
            changed = True

        # 3. Hedges viudos: su leg cubierto murio -> ORPHAN permanente.
        for t, r in self.rec.items():
            if r["role"] == ROLE_HEDGE and r["covers"] not in live:
                r["role"] = ROLE_ORPHAN
                r["covers"] = None
                changed = True
                self._log_write("LEDGER",
                                "Hedge quedo HUERFANO (su leg cubierto ya no existe). "
                                "No se re-etiqueta como OP1: queda ORPHAN.", ticket=t)

        # 4. Promocion: cuando SOLO quedan huerfanos (ningun leg de ciclo:
        #    ENTRY/RECOVERY/RESCUE), el huerfano DESCUBIERTO mas antiguo es
        #    estructuralmente una OP1 y pasa a serlo (el hedge monitor lo
        #    cubrira si cae). Mientras el ciclo tenga otros legs vivos, el
        #    huerfano NO se promueve: queda congelado/vigilado por el watchdog
        #    y no cambia la direccion ni la profundidad del ciclo en curso.
        cycle_roles = (ROLE_ENTRY, ROLE_RECOVERY, ROLE_RESCUE)
        no_cycle_alive = not any(r["role"] in cycle_roles
                                 for t, r in self.rec.items() if t in live)
        if no_cycle_alive:
            candidates = [live[t] for t, r in self.rec.items()
                          if r["role"] == ROLE_ORPHAN and t not in self._covered_tickets(live)]
            if candidates:
                oldest = min(candidates, key=lambda p: (p.time, p.ticket))
                self.rec[oldest.ticket]["role"] = ROLE_ENTRY
                changed = True
                self._log_write("LEDGER",
                                f"Huerfano descubierto promovido a ENTRY "
                                f"({'BUY' if oldest.type == 0 else 'SELL'} {oldest.volume:.2f}).",
                                ticket=oldest.ticket)

        if changed:
            self._save()
        return {t: r["role"] for t, r in self.rec.items()}

    def _live_entry_ticket(self, live):
        for t, r in self.rec.items():
            if r["role"] == ROLE_ENTRY and t in live:
                return t
        return None

    def _covered_tickets(self, live):
        """Tickets vivos que tienen un hedge vivo apuntandoles."""
        return {r["covers"] for t, r in self.rec.items()
                if r["role"] == ROLE_HEDGE and t in live and r["covers"] in live}

    def _pair_candidate(self, hedge_pos, live):
        """Leg vivo que un hedge desconocido deberia estar cubriendo: lado
        opuesto y sin cobertura previa; ENTRY primero, luego el huerfano mas
        antiguo. None si no hay candidato (=> ORPHAN)."""
        covered = self._covered_tickets(live)
        entry_t = self._live_entry_ticket(live)
        if (entry_t is not None and entry_t not in covered
                and live[entry_t].type != hedge_pos.type):
            return entry_t
        orphans = [live[t] for t, r in self.rec.items()
                   if r["role"] == ROLE_ORPHAN and t in live
                   and t not in covered and live[t].type != hedge_pos.type]
        if orphans:
            return min(orphans, key=lambda p: (p.time, p.ticket)).ticket
        return None

    # --------------------------------------------------------------
    # Vistas para el dispatch de strategy (todas sobre posiciones vivas)
    # --------------------------------------------------------------
    def entry_pos(self, positions):
        for p in positions:
            if self.role_of(p.ticket) == ROLE_ENTRY:
                return p
        return None

    def hedge_of(self, ticket, positions):
        for p in positions:
            r = self.rec.get(p.ticket)
            if r and r["role"] == ROLE_HEDGE and r["covers"] == ticket:
                return p
        return None

    def pairs(self, positions):
        """Lista de (leg_cubierto, hedge) con AMBOS miembros vivos."""
        live = {p.ticket: p for p in positions}
        out = []
        for t, r in self.rec.items():
            if r["role"] == ROLE_HEDGE and t in live and r["covers"] in live:
                out.append((live[r["covers"]], live[t]))
        return out

    def pair_members(self, positions):
        """Tickets que pertenecen a un par intacto (leg cubierto o su hedge).
        Son intocables para Unwind y Healer mientras el ciclo siga en rojo:
        el par se congela y solo se desarma completo."""
        members = set()
        for leg, hedge in self.pairs(positions):
            members.add(leg.ticket)
            members.add(hedge.ticket)
        return members

    def cycle_legs(self, positions):
        """Posiciones del CICLO activo: excluye huerfanos y sus coberturas.
        Es el conteo que alimenta la maquina de estados; los huerfanos no deben
        fabricar profundidad (Recovery/OP4) sobre una base que no es del ciclo."""
        live = {p.ticket: p for p in positions}
        orphan_cover = {t for t, r in self.rec.items()
                        if r["role"] == ROLE_HEDGE and t in live
                        and self.rec.get(r["covers"], {}).get("role") == ROLE_ORPHAN}
        return [p for p in positions
                if self.role_of(p.ticket) != ROLE_ORPHAN
                and p.ticket not in orphan_cover]

    def naked_orphans(self, positions):
        """Huerfanos vivos SIN hedge que los cubra (objetivo del watchdog)."""
        covered = self._covered_tickets({p.ticket: p for p in positions})
        return [p for p in positions
                if self.role_of(p.ticket) == ROLE_ORPHAN and p.ticket not in covered]

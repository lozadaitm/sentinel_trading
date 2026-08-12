"""Notificaciones por correo via Resend (API HTTP). Best-effort: nunca tumba el motor.

Config por entorno/.env (ver instances/example.env):
    RESEND_API_KEY  -> key del proyecto en resend.com (obligatoria para enviar).
    RESEND_FROM     -> remitente. Default: "Sentinel <onboarding@resend.dev>",
                       que Resend solo entrega al email dueño de la cuenta;
                       para enviar a cualquier usuario hay que verificar un
                       dominio en Resend y usar un from de ese dominio.

Sin RESEND_API_KEY el modulo queda inerte (solo loguea): el evento de objetivo
alcanzado sigue quedando en bot_logs y en account_settings aunque el correo no
salga.

El envio se hace en un hilo daemon: una llamada HTTP lenta (segundos) no debe
congelar el bucle de trading, que corre a 1 s por tick. Se usa urllib (stdlib)
para no añadir dependencias.
"""

import json
import threading
import urllib.error
import urllib.request

from . import config

API_URL = "https://api.resend.com/emails"


def is_configured():
    return bool(config.RESEND_API_KEY)


def _send(to_addr, subject, body, logger=None):
    payload = json.dumps({
        "from": config.RESEND_FROM,
        "to": [to_addr],
        "subject": subject,
        "text": body,
    }).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {config.RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
        if logger:
            logger.write("SYSTEM", f"Email enviado a {to_addr}: {subject}")
    except urllib.error.HTTPError as e:  # respuesta de la API con detalle util
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001
            detail = ""
        if logger:
            logger.write("ERROR", f"Resend rechazo el email a {to_addr} ({e.code}): {detail}")
    except Exception as e:  # noqa: BLE001  (best-effort; el evento ya quedo en DB)
        if logger:
            logger.write("ERROR", f"Fallo el envio de email a {to_addr}: {e}")


def send_async(to_addr, subject, body, logger=None):
    """Envia el correo en un hilo daemon. No-op (con log) si falta config o destinatario."""
    if not to_addr:
        if logger:
            logger.write("ERROR", "Email de objetivo NO enviado: sin destinatario (auth.users).")
        return
    if not is_configured():
        if logger:
            logger.write("SYSTEM", f"RESEND_API_KEY sin configurar: se omite el email a {to_addr} ({subject}).")
        return
    threading.Thread(
        target=_send, args=(to_addr, subject, body, logger),
        name="mail-sender", daemon=True,
    ).start()


def profit_target_body(*, profit_pct, target_pct, equity, base, symbol):
    return (
        f"Tu cuenta alcanzo el objetivo de ganancia configurado.\n"
        f"\n"
        f"  Ganancia actual : +{profit_pct:.2f}%  (objetivo: {target_pct:g}%)\n"
        f"  Equity          : {equity:,.2f}\n"
        f"  Saldo inicial   : {base:,.2f}\n"
        f"  Simbolo         : {symbol}\n"
        f"\n"
        f"Los bots pasaron a modo close-only: no abriran operaciones nuevas y\n"
        f"seguiran gestionando/cerrando las abiertas hasta quedar planos.\n"
        f"\n"
        f"En el dashboard puedes elegir entre esperar a que el bot termine de\n"
        f"gestionar las operaciones abiertas, o forzar el cierre inmediato\n"
        f"asumiendo el flotante actual.\n"
    )

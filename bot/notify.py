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


def _send(to_addr, subject, body, logger=None, html=None):
    data = {
        "from": config.RESEND_FROM,
        "to": [to_addr],
        "subject": subject,
        "text": body,
    }
    if html:
        data["html"] = html
    payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {config.RESEND_API_KEY}",
            "Content-Type": "application/json",
            # Cloudflare (delante de api.resend.com) devuelve 403 error 1010
            # al User-Agent por defecto de urllib ("Python-urllib/3.x").
            "User-Agent": "sentinel-bot/1.0 (+notify)",
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


def send_async(to_addr, subject, body, logger=None, html=None):
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
        target=_send, args=(to_addr, subject, body, logger, html),
        name="mail-sender", daemon=True,
    ).start()


def profit_target_body(*, profit_pct, target_pct, equity, base, symbol):
    """Version texto plano (fallback de clientes sin HTML)."""
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
        f"asumiendo el flotante actual: {config.DASHBOARD_URL}\n"
    )


# Paleta del dashboard (tema oscuro de globals.css del webapp).
_BG = "#0b0c14"        # fondo
_CARD = "#141824"      # superficie de tarjeta
_LINE = "#212734"      # bordes
_FG = "#eef1f6"        # texto principal
_MUTED = "#98a0b0"     # texto secundario
_FAINT = "#616a7b"     # texto terciario
_BRAND = "#7c83ff"     # acento de marca
_POS = "#2fd58f"       # verde P&L
_NEG = "#ff6b6b"       # rojo P&L


def profit_target_html(*, profit_pct, target_pct, equity, base, symbol):
    """Version HTML con la estetica del dashboard (tablas + estilos inline,
    lo unico que renderizan bien Gmail/Outlook) y CTA al dashboard web."""
    gain = equity - base
    gain_color = _POS if gain >= 0 else _NEG
    stats = [
        ("Equity", f"${equity:,.2f}"),
        ("Saldo inicial", f"${base:,.2f}"),
        ("Ganancia", f"{'+' if gain >= 0 else ''}${gain:,.2f}"),
        ("Símbolo", symbol),
    ]
    stat_rows = "".join(
        f"""<tr>
              <td style="padding:9px 0;border-top:1px solid {_LINE};color:{_MUTED};font-size:13px;">{label}</td>
              <td align="right" style="padding:9px 0;border-top:1px solid {_LINE};color:{_FG};font-size:13px;font-weight:600;font-variant-numeric:tabular-nums;">{value}</td>
            </tr>"""
        for label, value in stats
    )
    return f"""<!DOCTYPE html>
<html lang="es">
<body style="margin:0;padding:0;background:{_BG};">
  <!-- preheader oculto -->
  <div style="display:none;max-height:0;overflow:hidden;">
    Tu cuenta alcanzó +{profit_pct:.2f}% (objetivo {target_pct:g}%). Los bots están en solo-cierre.
  </div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_BG};padding:32px 12px;">
    <tr><td align="center">
      <table role="presentation" width="520" cellpadding="0" cellspacing="0" style="max-width:520px;width:100%;font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">

        <!-- marca -->
        <tr><td style="padding:0 4px 16px;">
          <span style="color:{_FG};font-size:17px;font-weight:700;letter-spacing:-0.02em;">Sentinel</span>
          <span style="color:{_BRAND};font-size:17px;font-weight:700;">.</span>
        </td></tr>

        <!-- tarjeta principal -->
        <tr><td style="background:{_CARD};border:1px solid {_LINE};border-radius:14px;padding:28px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
            <tr><td style="color:{_POS};font-size:12px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;">
              &#127919; Objetivo de ganancia alcanzado
            </td></tr>
            <tr><td style="padding-top:14px;color:{_FG};font-size:40px;font-weight:700;letter-spacing:-0.02em;font-variant-numeric:tabular-nums;">
              <span style="color:{_POS};">+{profit_pct:.2f}%</span>
            </td></tr>
            <tr><td style="padding-top:4px;color:{_FAINT};font-size:13px;">
              Objetivo configurado: {target_pct:g}%
            </td></tr>

            <tr><td style="padding-top:22px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{stat_rows}</table>
            </td></tr>

            <tr><td style="padding-top:22px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                     style="background:{_BG};border:1px solid {_LINE};border-radius:10px;">
                <tr><td style="padding:14px 16px;color:{_MUTED};font-size:13px;line-height:1.55;">
                  Ambos motores pasaron a <span style="color:{_FG};font-weight:600;">solo-cierre</span>:
                  no abrirán operaciones nuevas y seguirán gestionando las abiertas hasta quedar planos.
                  En el dashboard puedes <span style="color:{_FG};font-weight:600;">esperar</span> a que el
                  bot termine, o <span style="color:{_FG};font-weight:600;">forzar el cierre</span> asumiendo
                  el flotante actual (te lo muestra antes de confirmar).
                </td></tr>
              </table>
            </td></tr>

            <!-- CTA -->
            <tr><td align="center" style="padding-top:24px;">
              <a href="{config.DASHBOARD_URL}"
                 style="display:inline-block;background:{_BRAND};color:{_BG};text-decoration:none;
                        font-size:14px;font-weight:600;padding:12px 28px;border-radius:10px;">
                Abrir el dashboard
              </a>
            </td></tr>
            <tr><td align="center" style="padding-top:10px;color:{_FAINT};font-size:11px;">
              Inicia sesión con tu cuenta para decidir cómo terminar.
            </td></tr>
          </table>
        </td></tr>

        <!-- pie -->
        <tr><td align="center" style="padding:18px 8px 0;color:{_FAINT};font-size:11px;line-height:1.6;">
          Sentinel · HyperGrinder v20 — notificación automática de tu instancia.<br>
          Si no esperabas este correo, revisa tu configuración en el dashboard.
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""

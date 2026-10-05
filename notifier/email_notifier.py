"""Envío de alertas por SMTP (Gmail con contraseña de aplicación)."""

from __future__ import annotations

import logging
import smtplib
import ssl
from abc import ABC, abstractmethod
from email.message import EmailMessage

import pandas as pd

from detector.models import Direction, Setup

log = logging.getLogger(__name__)


def _fmt_time(ts) -> str:
    t = pd.Timestamp(ts)
    return f"{t.tz_convert('UTC'):%Y-%m-%d %H:%M} UTC ({t.tz_convert('America/New_York'):%H:%M} NY)"


def format_alert(setup: Setup, pip_size: float = 0.0001) -> tuple[str, str]:
    """Asunto y cuerpo del email de una alerta."""
    short = setup.direction is Direction.SHORT
    side = "SHORT (venta)" if short else "LONG (compra)"
    entry = setup.fvg_mid
    risk_pips = abs(setup.sweep_extreme - entry) / pip_size
    choch_word = "por debajo del swing low" if short else "por encima del swing high"

    subject = (
        f"[{setup.symbol}] {setup.direction.value.upper()} | sweep {setup.swept_level_name} "
        f"{setup.swept_level:.5f} -> CHoCH -> FVG {setup.fvg_low:.5f}-{setup.fvg_high:.5f}"
    )
    body = "\n".join(
        [
            f"Setup SMC en {setup.symbol} (15m)",
            "",
            f"Dirección:        {side}",
            f"Nivel barrido:    {setup.swept_level_name} {setup.swept_level:.5f}",
            f"  vela del sweep: {_fmt_time(setup.sweep_time)}",
            f"CHoCH:            cierre {choch_word} {setup.choch_level:.5f}",
            f"  swing:          {_fmt_time(setup.choch_swing_time)}",
            f"  vela del CHoCH: {_fmt_time(setup.choch_time)}",
            f"Entrada (FVG):    {setup.fvg_low:.5f} - {setup.fvg_high:.5f} (medio {entry:.5f})",
            f"Stop loss:        {setup.sweep_extreme:.5f} (extremo del sweep)",
            f"Riesgo desde el medio del FVG: {risk_pips:.1f} pips",
            "",
            f"Secuencia completada al cierre de {_fmt_time(setup.ready_time)}",
            f"ID: {setup.id}",
            "",
            "Alerta automática, no es una recomendación de inversión.",
        ]
    )
    return subject, body


class Notifier(ABC):
    @abstractmethod
    def send(self, subject: str, body: str) -> None:
        """Debe lanzar excepción si el envío falla (el setup se reintentará)."""


class LogNotifier(Notifier):
    """Modo dry_run: escribe la alerta en el log."""

    def send(self, subject: str, body: str) -> None:
        log.warning("ALERTA (dry run): %s\n%s", subject, body)


class EmailNotifier(Notifier):
    def __init__(self, user: str, password: str, to: list[str], host: str = "smtp.gmail.com", port: int = 465):
        if not user or not password or not to:
            raise ValueError("Faltan SMTP_USER, SMTP_APP_PASSWORD o ALERT_TO en .env")
        self.user = user
        self.password = password.replace(" ", "")  # Google muestra la clave en grupos de 4
        self.to = to
        self.host = host
        self.port = port

    def send(self, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.user
        msg["To"] = ", ".join(self.to)
        msg.set_content(body)
        context = ssl.create_default_context()
        if self.port == 465:
            with smtplib.SMTP_SSL(self.host, self.port, context=context, timeout=30) as smtp:
                smtp.login(self.user, self.password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
                smtp.starttls(context=context)
                smtp.login(self.user, self.password)
                smtp.send_message(msg)
        log.info("Email enviado a %s: %s", self.to, subject)

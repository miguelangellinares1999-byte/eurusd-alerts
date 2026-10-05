"""Alertas en vivo de la estrategia Daily Cycle (detector/daily_cycle.py) para un símbolo.

Corre en el mismo ciclo que las alertas PDH/PDL de EURUSD pero con su propia fuente, su
propio estado y su propio notificador (dry_run = solo log). En cada pasada descarga las
últimas velas M15 cerradas, recalcula las señales y avisa de las nuevas.

Estado (JSON): hora de la última señal avisada y un diario de todas las señales detectadas,
para poder revisar después qué habría operado.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import pandas as pd

from detector.daily_cycle import DailyCycleConfig, DailyCycleSignal, detect_signals
from notifier import Notifier

log = logging.getLogger("eurusd_alerts.daily_cycle")

JOURNAL_MAX = 500


@dataclass
class DailyCycleAlerts:
    enabled: bool = False
    dry_run: bool = True
    symbol: str = "GBPUSD"
    mt5_symbol: str = "GBPUSD"
    yahoo_ticker: str = "GBPUSD=X"
    lookback_days: int = 40
    tp_r: float = 2.0
    state_path: Path = Path("state/daily_cycle_gbpusd.json")
    detector: DailyCycleConfig = field(default_factory=DailyCycleConfig)


class SignalStore:
    def __init__(self, path: Path):
        self.path = path
        self.last_alerted: pd.Timestamp | None = None
        self.journal: list[dict] = []

    def load(self) -> "SignalStore":
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.last_alerted = pd.Timestamp(raw["last_alerted"]) if raw.get("last_alerted") else None
                self.journal = raw.get("journal", [])
            except (json.JSONDecodeError, KeyError, ValueError):
                log.error("Estado de Daily Cycle corrupto en %s; se empieza de cero", self.path)
        return self

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_alerted": None if self.last_alerted is None else self.last_alerted.isoformat(),
            "journal": self.journal[-JOURNAL_MAX:],
        }
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".dc-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def take_profit(sig: DailyCycleSignal, r: float) -> float:
    risk = abs(sig.entry - sig.stop_loss)
    return sig.entry + r * risk if sig.direction.value == "long" else sig.entry - r * risk


def format_signal(symbol: str, sig: DailyCycleSignal, tp_r: float, pip_size: float, dry_run: bool) -> tuple[str, str]:
    tp = take_profit(sig, tp_r)
    risk = abs(sig.entry - sig.stop_loss) / pip_size
    ny = sig.time.tz_convert("America/New_York")
    side = "LONG (compra)" if sig.direction.value == "long" else "SHORT (venta)"
    subject = (
        f"{'[DRY RUN] ' if dry_run else ''}[{symbol}] {sig.direction.value.upper()} Daily Cycle {sig.variation} | "
        f"límite {sig.entry:.5f} SL {sig.stop_loss:.5f} TP {tp:.5f}"
    )
    body = "\n".join([
        f"Daily Cycle en {symbol} (M15, sesgo 4H)",
        "",
        f"Dirección:      {side}",
        f"Variación:      {sig.variation}",
        f"Rango de Asia:  {sig.asia_low:.5f} - {sig.asia_high:.5f}",
        f"TR 4H:          {sig.tr_low:.5f} - {sig.tr_high:.5f}",
        f"Entrada límite: {sig.entry:.5f} (50% del FVG del CHoCH)",
        f"Stop loss:      {sig.stop_loss:.5f} (extremo del sweep, {risk:.1f} pips)",
        f"Take profit:    {tp:.5f} ({tp_r:g}R)",
        f"Señal al cierre de {sig.time:%Y-%m-%d %H:%M} UTC ({ny:%H:%M} NY)",
        "La orden se cancela si toca el TP antes de entrar o al cierre del día (17:00 NY).",
        "",
        "Alerta automática, no es una recomendación de inversión.",
    ])
    return subject, body


def run_daily_cycle(
    dc: DailyCycleAlerts,
    source,
    store: SignalStore,
    notifier: Notifier,
    max_age_minutes: int,
    now: pd.Timestamp | None = None,
) -> int:
    """Una pasada: avisa de las señales nuevas. Devuelve cuántas se han enviado."""
    now = pd.Timestamp.now(tz="UTC") if now is None else now
    candles = source.get_closed_candles(dc.detector.ltf_minutes, dc.lookback_days, now)
    if candles.empty:
        return 0
    signals = [s for s in detect_signals(candles, dc.detector) if s.time <= now]
    new = [s for s in signals if store.last_alerted is None or s.time > store.last_alerted]
    sent = 0
    for sig in new:
        store.last_alerted = sig.time
        entry = {k: (v.isoformat() if isinstance(v, pd.Timestamp) else getattr(v, "value", v)) for k, v in sig.__dict__.items()}
        entry["take_profit"] = round(take_profit(sig, dc.tp_r), 5)
        if now - sig.time > timedelta(minutes=max_age_minutes):
            entry["status"] = "antigua, no avisada"
            store.journal.append(entry)
            continue
        subject, body = format_signal(dc.symbol, sig, dc.tp_r, dc.detector.pip_size, dc.dry_run)
        try:
            notifier.send(subject, body)
        except Exception:
            log.exception("Fallo avisando la señal %s de %s", sig.time, dc.symbol)
            store.last_alerted = sig.time - pd.Timedelta(seconds=1)  # se reintenta en la siguiente pasada
            break
        entry["status"] = "avisada (dry run)" if dc.dry_run else "avisada"
        store.journal.append(entry)
        sent += 1
    store.save()
    return sent

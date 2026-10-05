"""Backtest: recorre un histórico vela a vela con el mismo motor que el modo en vivo."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import pandas as pd

from detector import DetectorConfig, SetupState, SMCEngine
from detector.models import Setup

ALERT_COLUMNS = [
    "alert_time_utc",
    "direction",
    "swept_level",
    "swept_price",
    "sweep_time_utc",
    "choch_level",
    "choch_time_utc",
    "fvg_low",
    "fvg_high",
    "entry_mid",
    "stop_loss",
    "risk_pips",
    "setup_id",
]


@dataclass
class BacktestResult:
    alerts: pd.DataFrame
    setups: list[Setup] = field(default_factory=list)

    @property
    def invalidation_reasons(self) -> Counter:
        return Counter(s.invalidated_reason for s in self.setups if s.state is SetupState.INVALIDATED)

    def summary(self) -> str:
        total = len(self.setups)
        lines = [
            f"Setups (sweeps válidos): {total}",
            f"Alertas: {len(self.alerts)}",
        ]
        if len(self.alerts):
            counts = self.alerts["direction"].value_counts()
            lines.append("  " + ", ".join(f"{k}: {v}" for k, v in counts.items()))
        for reason, n in self.invalidation_reasons.most_common():
            lines.append(f"Descartados por {reason}: {n}")
        return "\n".join(lines)


def _row(s: Setup, pip_size: float) -> dict:
    return {
        "alert_time_utc": s.ready_time,
        "direction": s.direction.value,
        "swept_level": s.swept_level_name,
        "swept_price": round(s.swept_level, 5),
        "sweep_time_utc": s.sweep_time,
        "choch_level": round(s.choch_level, 5),
        "choch_time_utc": s.choch_time,
        "fvg_low": round(s.fvg_low, 5),
        "fvg_high": round(s.fvg_high, 5),
        "entry_mid": round(s.fvg_mid, 5),
        "stop_loss": round(s.sweep_extreme, 5),
        "risk_pips": round(abs(s.sweep_extreme - s.fvg_mid) / pip_size, 1),
        "setup_id": s.id,
    }


def run_backtest(candles: pd.DataFrame, cfg: DetectorConfig, symbol: str = "EURUSD") -> BacktestResult:
    engine = SMCEngine(cfg, symbol)
    df = engine.prepare(candles)
    live: dict[str, Setup] = {}
    archive: list[Setup] = []
    rows = []
    current_day = None
    for i in range(len(df)):
        day = df["trading_day"].iat[i]
        if day != current_day:
            # Los setups finalizados de días anteriores ya no influyen: se archivan
            # para que el bucle no crezca con todo el histórico.
            done = (SetupState.ALERTED, SetupState.INVALIDATED)
            for k in [k for k, s in live.items() if s.state in done]:
                archive.append(live.pop(k))
            current_day = day
        for s in engine.process_bar(df, i, live):
            s.alerted_at = s.ready_time
            s.transition(SetupState.ALERTED, s.ready_time, "backtest")
            rows.append(_row(s, cfg.pip_size))
    archive.extend(live.values())
    alerts = pd.DataFrame(rows, columns=ALERT_COLUMNS)
    return BacktestResult(alerts=alerts, setups=sorted(archive, key=lambda s: s.sweep_time))

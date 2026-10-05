"""Comparativa PDH/PDL frente a Chartz con las mismas reglas de ejecución (velas M1)."""

from __future__ import annotations

import pandas as pd

from backtest.runner import run_backtest
from backtest.trades import Simulator, Trade, summarize
from detector import DetectorConfig
from detector.chartz import ChartzConfig, detect_signals, resample

TARGETS = (1, 2, 3, "ERL")


def _signals(m1: pd.DataFrame, det: DetectorConfig, chartz: ChartzConfig, start, end) -> dict[str, list[tuple]]:
    alerts = run_backtest(resample(m1, det.timeframe_minutes), det).alerts
    alerts = alerts[(alerts["alert_time_utc"] >= start) & (alerts["alert_time_utc"] < end)]
    ltf = m1 if chartz.ltf_minutes == 1 else resample(m1, chartz.ltf_minutes)
    signals = [s for s in detect_signals(ltf, chartz) if start <= s.time < end]
    return {
        "PDH/PDL": [(r.alert_time_utc, r.direction, r.entry_mid, r.stop_loss, None) for r in alerts.itertuples()],
        "Chartz": [(s.time, s.direction.value, s.entry, s.stop_loss, s.target_erl) for s in signals],
    }


def _trades(sim: Simulator, signals: list[tuple], target) -> list[Trade]:
    out = []
    for ts, direction, entry, stop, erl in signals:
        risk = abs(entry - stop)
        sign = 1 if direction == "long" else -1
        if target == "ERL":
            if erl is None or (erl - entry) * sign < risk:
                continue  # sin objetivo ERL o a menos de 1R
            tp = erl
        else:
            tp = entry + sign * target * risk
        out.append(sim.run(ts, direction, entry, stop, tp))
    return out


def compare(
    m1: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    det: DetectorConfig,
    chartz: ChartzConfig,
    spread_pips: float = 0.0,
) -> tuple[pd.DataFrame, dict[str, list[Trade]]]:
    """Tabla resumen por estrategia y objetivo, y las operaciones a 2R."""
    sim = Simulator(m1, spread=spread_pips * det.pip_size)
    rows, at_2r = [], {}
    for name, signals in _signals(m1, det, chartz, start, end).items():
        for target in TARGETS:
            if target == "ERL" and name == "PDH/PDL":
                continue
            trades = _trades(sim, signals, target)
            row = {"strategy": name, "tp": "ERL 1H" if target == "ERL" else f"{target}R"} | summarize(trades)
            for year in sorted({t.signal_time.year for t in trades}):
                y = summarize([t for t in trades if t.signal_time.year == year])
                row[f"{year}"] = f"{y['trades']} ops {y['winrate']:.0f}% {y['total_r']:+.1f}R"
            rows.append(row)
            if target == 2:
                at_2r[name] = trades
    return pd.DataFrame(rows), at_2r

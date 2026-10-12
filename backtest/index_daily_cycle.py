"""Daily Cycle (detector/daily_cycle.py, misma configuración que GBPUSD en vivo) en S&P 500 y Nasdaq 100,
y su resultado en pruebas de fondeo (backtest/prop_challenge.py).

Datos: CFD SPX500 y NAS100 del terminal MT5, velas M5 reales desde mediados de 2020 (antes MT5 solo
guarda una vela por día). El servidor va a hora NY + 7 (también con horario de verano).
Señales en M15 con sesgo del TR de 4H, CHoCH de una sola vela con FVG, entrada límite al 50% del
FVG, SL en el extremo del barrido, TP a 2R (como config.yaml). Se prueba la killzone de Londres
(02:00-05:00 NY, la de GBPUSD) y la de Nueva York (08:00-11:00 NY, la apertura del contado).
Ejecución con backtest/trades.py sobre M5: la orden se cancela a las 17:00 NY, peor caso si SL y TP
caen en la misma vela, spread mediano del CFD. Sin comisiones ni swaps.
"""

from __future__ import annotations

import argparse
from itertools import product

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

from backtest.prop_challenge import Rules, Sizing, simulate
from backtest.trades import Simulator, summarize
from detector.daily_cycle import DailyCycleConfig, detect_signals

START = pd.Timestamp("2020-07-01", tz="UTC")  # primeras semanas con M5 reales + margen para el TR de 4H
SYMBOLS = {"SP500": "SPX500", "NAS100": "NAS100"}
MIN_RISK = {"SP500": 2.0, "NAS100": 8.0}  # puntos: evita stops dentro del ruido
KILLZONES = {"Londres 02-05": [(2, 5)], "NY 08-11": [(8, 11)]}
TP_R = 2.0


def load_m5(symbol: str) -> tuple[pd.DataFrame, float]:
    mt5.symbol_select(symbol, True)
    r = pd.DataFrame(mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 2_000_000))
    server = pd.to_datetime(r["time"], unit="s")
    ny = (server - pd.Timedelta(hours=7)).dt.tz_localize("America/New_York", ambiguous="NaT", nonexistent="NaT")
    df = r.assign(time=ny.dt.tz_convert("UTC")).dropna(subset=["time"]).set_index("time")
    df = df[~df.index.duplicated()].sort_index()
    spread = float(df["spread"].median() * mt5.symbol_info(symbol).point)
    return df.loc[df.index >= START, ["open", "high", "low", "close"]].iloc[:-1], spread


def daily_cycle_trades(m5: pd.DataFrame, name: str, windows, spread: float) -> pd.DataFrame:
    cfg = DailyCycleConfig(ltf_minutes=15, htf_minutes=240, single_candle_inf=True, entry="fvg_mid",
                           windows=windows, min_risk_pips=MIN_RISK[name], pip_size=1.0)
    sim = Simulator(m5, spread=spread)
    rows = []
    for s in detect_signals(m5, cfg):
        sign = 1 if s.direction.value == "long" else -1
        tp = s.entry + sign * TP_R * abs(s.entry - s.stop_loss)
        tr = sim.run(s.time, s.direction.value, s.entry, s.stop_loss, tp)
        rows.append({"indice": name, "signal_time": s.time, "variacion": s.variation, "lado": s.direction.value,
                     "riesgo_pts": abs(s.entry - s.stop_loss), "resultado": tr.result, "r": tr.r, "tp_r": TP_R,
                     "fill_time": tr.fill_time, "exit_time": tr.exit_time, "_trade": tr})
    return pd.DataFrame(rows)


def trade_table(t: pd.DataFrame) -> pd.DataFrame:
    rows = {}
    for key, g in [(("Total", "todos"), t)] + list(t.groupby(["indice", t.signal_time.dt.year])):
        s = summarize(list(g._trade))
        done = g[g.resultado.isin(["win", "loss"])]
        rows[key] = {"señales": s["signals"], "ops": s["trades"], "acierto %": round(s["winrate"], 1),
                     "total R": round(s["total_r"], 1), "R/op": round(s["r_per_trade"], 3),
                     "máx DD R": round(s["max_dd_r"], 1), "racha pérd.": s["max_loss_streak"],
                     "riesgo mediano pts": round(done.riesgo_pts.median(), 1) if len(done) else None}
    return pd.DataFrame(rows).T


def prop_grid(trades: pd.DataFrame) -> pd.DataFrame:
    t = trades[trades.resultado.isin(["win", "loss"])]
    sizings = [Sizing(r) for r in (0.005, 0.01, 0.02)] + [
        Sizing(0.01, cushion=6, daily_guard=0.8),
        Sizing(0.02, cushion=6, daily_guard=0.8, to_target=True),
    ]
    rows = []
    for daily, trailing, sz in product((0.03, 0.05), (False, True), sizings):
        rules = Rules(daily=daily, trailing=trailing)
        rows.append({"diario": f"{100 * daily:g}%", "DD": "trailing" if trailing else "estático",
                     "riesgo": sz.label(), **simulate(t, rules, sz)})
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="backtest/index_daily_cycle_trades.csv")
    args = ap.parse_args()
    pd.set_option("display.width", 220)
    if not mt5.initialize():
        raise SystemExit(f"No se pudo conectar con MT5: {mt5.last_error()}")
    data = {}
    for name, sym in SYMBOLS.items():
        data[name] = load_m5(sym)
        m5, spread = data[name]
        print(f"{name} ({sym}): {len(m5)} velas M5 {m5.index.min():%Y-%m-%d} -> {m5.index.max():%Y-%m-%d}, spread {spread:.2f} pts")

    out = []
    for kz, windows in KILLZONES.items():
        t = pd.concat([daily_cycle_trades(m5, name, windows, spread) for name, (m5, spread) in data.items()],
                      ignore_index=True)
        t["killzone"] = kz
        out.append(t)
        print(f"\n=== Daily Cycle — killzone {kz} (TP {TP_R:g}R) ===")
        print(trade_table(t).to_string())
        print(f"\n--- Pruebas de fondeo: objetivo +10%, DD máx 10% — Daily Cycle {kz} (SP500 + NAS100) ---")
        print(prop_grid(t).to_string(index=False))
    pd.concat(out).drop(columns="_trade").to_csv(args.csv, index=False)


if __name__ == "__main__":
    main()

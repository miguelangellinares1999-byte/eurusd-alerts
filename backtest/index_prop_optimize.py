"""Riesgo por estrategia para pasar pruebas de fondeo más rápido: ORB 30 + RSI(2) diario en índices.

Parte de las operaciones de backtest/index_prop_strategies.py (backtest/index_prop_trades.csv) y
les añade deslizamiento en la entrada stop del ORB (SLIP_PTS por operación; x1 = realista, x2 = duro).
Busca la combinación de riesgo ORB / RSI2 que minimiza los días esperados hasta tener la cuenta
(días medios por intento / % de aprobadas, encadenando intentos) con reglas +10% / DD 10% / diario 5%,
y comprueba las mejores con diario 3%, DD trailing y el doble de deslizamiento. El RSI(2) se valida
aparte con todo el histórico D1 (2014-2026), porque en 2020-26 solo tiene ~120 operaciones.
"""

from __future__ import annotations

from itertools import product

import MetaTrader5 as mt5
import pandas as pd

from backtest.index_daily_cycle import SYMBOLS
from backtest.index_prop_strategies import load_d1, rsi2_daily
from backtest.prop_challenge import Rules, Sizing, simulate

SLIP_PTS = {"SP500": 0.5, "NAS100": 2.0}
SPREAD = {"SP500": 0.6, "NAS100": 1.9}  # spread mediano de los CFD en MT5
ORB = "ORB 30"
RSI = "RSI2 diario"
COLS = ["aprobadas %", "DD máx %", "diario %", "días mediana (aprob.)", "días p75 (aprob.)",
        "intentos esperados", "días esperados (reintentos)"]


def load_trades(slip: float = 1.0, path: str = "backtest/index_prop_trades.csv") -> pd.DataFrame:
    t = pd.read_csv(path)
    for c in ("fill_time", "exit_time"):
        t[c] = pd.to_datetime(t[c], utc=True, format="mixed")
    orb = t.estrategia.str.startswith("ORB") | (t.estrategia == "Gap & Go")
    t.loc[orb, "r"] -= slip * t.loc[orb, "indice"].map(SLIP_PTS) / t.loc[orb, "riesgo_pts"]
    return t


def subset(t: pd.DataFrame, universe: str) -> pd.DataFrame:
    rsi = t.estrategia == RSI
    orb = t.estrategia == ORB
    if universe == "ORB NAS100":
        orb &= t.indice == "NAS100"
    return t[rsi | orb]


def sizings(orb_risk: float, rsi_risk: float, protected: bool) -> Sizing:
    pairs = ((ORB, orb_risk), (RSI, rsi_risk))
    if protected:
        return Sizing(orb_risk, daily_guard=0.8, to_target=True, by_strategy=pairs)
    return Sizing(orb_risk, by_strategy=pairs)


def row(t, universe, orb_risk, rsi_risk, protected, rules) -> dict:
    sz = sizings(orb_risk, rsi_risk, protected)
    res = simulate(subset(t, universe), rules, sz)
    return {"ORB en": universe, "riesgo ORB %": 100 * orb_risk, "riesgo RSI2 %": 100 * rsi_risk,
            "protección": "guarda diaria + justo al objetivo" if protected else "-",
            **{k: res[k] for k in COLS}}


def main() -> None:
    pd.set_option("display.width", 240)
    t1 = load_trades(1.0)
    base = Rules(daily=0.05)
    grid = [row(t1, u, o, r, p, base) for u, o, r, p in
            product(("SP500 + NAS100", "ORB NAS100"), (0.005, 0.0075, 0.01, 0.015, 0.02), (0.0, 0.01, 0.02, 0.03), (False, True))]
    g = pd.DataFrame(grid).sort_values("días esperados (reintentos)")
    g.to_csv("backtest/index_prop_optimize.csv", index=False)
    print("=== Rejilla (deslizamiento realista, +10% / DD 10% estático / diario 5%), ordenada por días esperados ===")
    print(g.to_string(index=False))

    print("\n=== Las 6 mejores con reglas y costes más duros ===")
    best = g.head(6)
    t2 = load_trades(2.0)
    stress = {"diario 3%": (t1, Rules(daily=0.03)), "DD trailing": (t1, Rules(daily=0.05, trailing=True)),
              "deslizamiento x2": (t2, base)}
    out = []
    for b in best.itertuples():
        args = (b._1, b._2 / 100, b._3 / 100, b.protección != "-")
        for name, (tt, rules) in stress.items():
            out.append({"escenario": name, **row(tt, args[0], args[1], args[2], args[3], rules)})
    print(pd.DataFrame(out).to_string(index=False))

    print("\n=== RSI(2) diario solo, histórico D1 2014-2026 (validación del riesgo alto) ===")
    if not mt5.initialize():
        raise SystemExit(f"No se pudo conectar con MT5: {mt5.last_error()}")
    long_rsi = pd.DataFrame([x for name, sym in SYMBOLS.items() for x in rsi2_daily(load_d1(sym), name, SPREAD[name])])
    long_rsi["estrategia"] = RSI
    rows = []
    for risk, protected in product((0.01, 0.02, 0.03, 0.04), (False, True)):
        sz = Sizing(risk, daily_guard=0.8, to_target=True) if protected else Sizing(risk)
        rows.append({"riesgo": sz.label(), **{k: v for k, v in simulate(long_rsi, base, sz).items() if k in COLS}})
    print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()

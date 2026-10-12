"""Compara las operaciones del bot MT5 (mt5/IndexPropBot.mq5) con las del backtest en Python.

Pasos: ejecutar el bot en el Probador de Estrategias de MT5 con InpVerifyMode = true (riesgo fijo,
sin reglas de fondeo) y luego `python -m backtest.verify_mt5`. El bot escribe sus operaciones en
Common\\Files\\IndexPropBot_trades.csv (horas de servidor = NY + 7). Se emparejan por estrategia,
índice y día de entrada en Nueva York, y se compara la dirección, el resultado y la R.

El backtest descuenta el spread mediano; el probador de MT5 ejecuta con el bid/ask de cada tick,
así que las R no coinciden al decimal, pero el signo y el tamaño deben parecerse.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

COMMON = Path.home() / "AppData/Roaming/MetaQuotes/Terminal/Common/Files/IndexPropBot_trades.csv"
SYMBOL_MAP = {"SPX500": "SP500", "NAS100": "NAS100"}
NY = "America/New_York"


def load_mt5(path: Path, server_minus_ny: int = 7) -> pd.DataFrame:
    t = pd.read_csv(path, sep=";")
    for col in ("entrada", "salida"):
        server = pd.to_datetime(t[col], format="%Y.%m.%d %H:%M:%S")
        t[col] = (server - pd.Timedelta(hours=server_minus_ny)).dt.tz_localize(NY, ambiguous="NaT", nonexistent="NaT")
    t["indice"] = t.simbolo.map(SYMBOL_MAP)
    t["dia"] = t.entrada.dt.date
    return t


def load_python(path: str = "backtest/index_prop_trades.csv") -> pd.DataFrame:
    t = pd.read_csv(path)
    t = t[((t.estrategia == "ORB 30") & (t.indice == "NAS100")) | (t.estrategia == "RSI2 diario")].copy()
    t["lado"] = pd.to_numeric(t.lado)  # en el CSV la columna mezcla números y textos (Daily Cycle)
    t["entrada"] = pd.to_datetime(t.fill_time, utc=True, format="mixed").dt.tz_convert(NY)
    # el RSI2 del backtest entra al cierre de las 17:00 NY; el bot, a las 16:50 del mismo día
    t["dia"] = (t.entrada - pd.Timedelta(minutes=30)).dt.date
    return t


def compare(mt: pd.DataFrame, py: pd.DataFrame) -> None:
    lo, hi = mt.entrada.min(), mt.entrada.max()
    py = py[(py.entrada >= lo - pd.Timedelta(days=1)) & (py.entrada <= hi + pd.Timedelta(days=1))]
    keys = ["estrategia", "indice", "dia"]
    m = py.merge(mt, on=keys, how="outer", suffixes=("_py", "_mt5"), indicator=True)
    print(f"Periodo del probador: {lo:%Y-%m-%d} -> {hi:%Y-%m-%d}\n")
    rows = {}
    for (strat, ix), g in m.groupby(["estrategia", "indice"]):
        both = g[g._merge == "both"]
        rows[(strat, ix)] = {
            "ops Python": int((g._merge != "right_only").sum()),
            "ops MT5": int((g._merge != "left_only").sum()),
            "coinciden": len(both),
            "% coinciden": round(100 * len(both) / max(len(g), 1), 1),
            "misma dirección %": round(100 * (both.lado_py == both.lado_mt5).mean(), 1) if len(both) else None,
            "mismo resultado %": round(100 * ((both.r > 0) == (both.R > 0)).mean(), 1) if len(both) else None,
            "R/op Python": round(g.r.mean(), 3),
            "R/op MT5": round(g.R.mean(), 3),
            "total R Python": round(g.r.sum(), 1),
            "total R MT5": round(g.R.sum(), 1),
            "correlación R": round(np.corrcoef(both.r, both.R)[0, 1], 2) if len(both) > 2 else None,
        }
    pd.set_option("display.width", 220)
    print(pd.DataFrame(rows).T.to_string())
    diff = m[m._merge == "both"].assign(dif=lambda d: (d.r - d.R).abs()).nlargest(10, "dif")
    print("\nMayores diferencias de R (revisar en el gráfico):")
    print(diff[keys + ["lado_py", "lado_mt5", "r", "R"]].to_string(index=False))
    solo = m[m._merge != "both"].sort_values("dia")
    print(f"\nOperaciones que solo están en un lado: {len(solo)} (primeras 15)")
    print(solo[keys + ["_merge"]].head(15).to_string(index=False))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", type=Path, default=COMMON)
    args = ap.parse_args()
    if not args.csv.exists():
        raise SystemExit(f"No existe {args.csv}: ejecuta antes el bot en el Probador de Estrategias.")
    compare(load_mt5(args.csv), load_python())


if __name__ == "__main__":
    main()

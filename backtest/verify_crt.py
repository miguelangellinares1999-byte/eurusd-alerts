"""Compara las operaciones del bot MT5 (mt5/CRTPropBot.mq5) con las del backtest backtest/crt_funded.py.

Pasos: ejecutar el bot en el Probador de Estrategias con InpVerifyMode = true (mt5/verificar_crt_en_mt5.bat)
y luego `python -m backtest.verify_crt`. El bot escribe sus operaciones en
Common\\Files\\CRTPropBot_trades.csv en hora de servidor, la misma que las velas de MT5 que usa el backtest.
Se empareja cada operación del bot con la del backtest de la misma estrategia que entró a menos de
--tol minutos. El backtest descuenta un coste fijo en R y el probador ejecuta con bid/ask, así que las R
no coinciden al decimal.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from backtest import crt_funded as cf

COMMON = Path.home() / "AppData/Roaming/MetaQuotes/Terminal/Common/Files/CRTPropBot_trades.csv"
# estrategia del bot -> (símbolo, sesión, sesgo, entrada, tp, be), como en mt5/CRTPropBot.mq5
PORTFOLIO = {
    "CRT NAS100": ("NAS100", "all", "d1", "fvg", "1r", False),
    "CRT SPX500": ("SPX500", "london", "ema", "mss", "2r", True),
    "CRT US30": ("US30", "london", "d1", "mss", "2r", False),
    "CRT GBPUSD": ("GBPUSDp", "both", "ema", "fvg", "2r", True),
    "CRT XAUUSD": ("XAUUSDp", "ny", "prem", "mss", "crt", False),
}


def python_trades(start: str, end: str) -> pd.DataFrame:
    out = []
    for name, (sym, session, bias, entry, tp, be) in PORTFOLIO.items():
        m15 = cf.load_m15(sym, cf.START.get(sym, "2016-01-01"), end)
        sets = cf.filt(cf.crt_setups(m15, cf.COST[sym]), session, bias)
        tr = cf.simulate(m15, sets, entry, tp, be, cf.COST[sym], 1 if session != "all" else cf.WINDOW)
        out.append(tr[tr.entry_time >= start].assign(estrategia=name))
    t = pd.concat(out, ignore_index=True)
    return t.assign(entry_time=t.entry_time.astype("datetime64[ns]"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(COMMON))
    ap.add_argument("--tol", type=int, default=30, help="minutos de diferencia máxima en la entrada")
    a = ap.parse_args()
    mt = pd.read_csv(a.csv, sep=";")
    mt["entry_time"] = pd.to_datetime(mt.entrada, format="%Y.%m.%d %H:%M:%S").astype("datetime64[ns]")
    lo, hi = mt.entry_time.min(), mt.entry_time.max()
    py = python_trades(f"{lo:%Y-%m-%d}", f"{hi + pd.Timedelta(days=1):%Y-%m-%d}")
    py = py[py.entry_time <= hi + pd.Timedelta(hours=1)]
    print(f"Periodo del probador: {lo:%Y-%m-%d} -> {hi:%Y-%m-%d}\n")

    rows = {}
    for name in PORTFOLIO:
        p = py[py.estrategia == name].sort_values("entry_time")
        m = mt[mt.estrategia == name].sort_values("entry_time")
        j = pd.merge_asof(m, p[["entry_time", "dir", "r"]], on="entry_time", direction="nearest",
                          tolerance=pd.Timedelta(minutes=a.tol))
        both = j[j.r.notna()]
        rows[name] = {
            "ops Python": len(p), "ops MT5": len(m), "coinciden": len(both),
            "% de Python": round(100 * len(both) / max(len(p), 1), 1),
            "misma dirección %": round(100 * (both.lado == both.dir).mean(), 1) if len(both) else None,
            "mismo resultado %": round(100 * ((both.r > 0) == (both.R > 0)).mean(), 1) if len(both) else None,
            "R/op Python": round(p.r.mean(), 3) if len(p) else None,
            "R/op MT5": round(m.R.mean(), 3) if len(m) else None,
            "R total Python": round(p.r.sum(), 1), "R total MT5": round(m.R.sum(), 1),
        }
    print(pd.DataFrame(rows).T.to_string())
    print(f"\nBeneficio del bot en el probador: {mt.beneficio.sum():.2f} USD en {len(mt)} operaciones")


if __name__ == "__main__":
    main()

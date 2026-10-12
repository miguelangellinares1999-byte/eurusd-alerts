"""Backtest de reversión a la media en diario para divisas: RSI(2) a favor de la tendencia (velas de Yahoo).

Reglas (decididas antes de mirar resultados, sin optimizar por par):
- Tendencia: cierre por encima de la SMA200 -> solo compras; por debajo -> solo ventas.
- Entrada: al cierre del día en que el RSI(2) baja de 10 (compra) o sube de 90 (venta).
- Salida: al cierre del primer día que cierra por encima de la SMA5 (compra) / por debajo (venta).
- Stop: 2.5 x ATR(14) desde la entrada, comprobado contra el máximo/mínimo del día (si salta
  el mismo día de una señal de salida se asume el stop: peor caso).
- Stop temporal: se cierra al cierre del 10º día si no ha salido antes.
Costes: SPREAD[par] pips por operación (ida y vuelta). Sin swaps.
Se separa in-sample (hasta 2015) y out-of-sample (2016 en adelante) para ver si el acierto aguanta.
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from backtest.market import load_daily

PAIRS = {"EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "AUDJPY": "AUDJPY=X", "USDCAD": "CAD=X", "USDJPY": "JPY=X"}
PIP = {"EURUSD": 0.0001, "GBPUSD": 0.0001, "AUDJPY": 0.01, "USDCAD": 0.0001, "USDJPY": 0.01}
SPREAD = {"EURUSD": 1.0, "GBPUSD": 1.5, "AUDJPY": 2.0, "USDCAD": 1.5, "USDJPY": 1.0}
SPLIT = "2016-01-01"


def rsi(close: pd.Series, n: int) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """Yahoo tiene velas FX con máximos/mínimos erróneos: se recortan a un rango razonable."""
    df = df[["open", "high", "low", "close"]].dropna()
    df = df[df["close"] > 0]
    rng = (df["high"] - df["low"]).rolling(20, min_periods=5).median().shift()
    lim = 4 * rng.fillna(df["close"] * 0.02)
    df["high"] = np.minimum(df["high"].clip(lower=df["close"]), df["close"] + lim)
    df["low"] = np.maximum(df["low"].clip(upper=df["close"]), df["close"] - lim)
    return df


def backtest(df: pd.DataFrame, pair: str, rsi_lo=10, rsi_hi=90, stop_atr=2.5, max_days=10,
             trend=200, exit_ma=5) -> pd.DataFrame:
    c, h, l = df["close"], df["high"], df["low"]
    sma = c.rolling(trend).mean()
    ma_exit = c.rolling(exit_ma).mean()
    r = rsi(c, 2)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    pip, cost = PIP[pair], SPREAD[pair]
    trades, i, n = [], trend, len(df)
    while i < n - 1:
        side = 1 if c.iloc[i] > sma.iloc[i] and r.iloc[i] < rsi_lo else \
            -1 if c.iloc[i] < sma.iloc[i] and r.iloc[i] > rsi_hi else 0
        if not side:
            i += 1
            continue
        entry, risk = c.iloc[i], stop_atr * atr.iloc[i]
        stop = entry - side * risk
        exit_px, reason, j = None, "tiempo", i
        for j in range(i + 1, min(i + max_days, n - 1) + 1):
            if (side == 1 and l.iloc[j] <= stop) or (side == -1 and h.iloc[j] >= stop):
                exit_px, reason = stop, "stop"
                break
            if (side == 1 and c.iloc[j] > ma_exit.iloc[j]) or (side == -1 and c.iloc[j] < ma_exit.iloc[j]):
                exit_px, reason = c.iloc[j], "objetivo"
                break
        if exit_px is None:
            exit_px = c.iloc[j]
        pips = side * (exit_px - entry) / pip - cost
        trades.append({"par": pair, "entrada": df.index[i], "salida": df.index[j], "lado": side,
                       "pips": pips, "R": pips * pip / risk, "motivo": reason, "dias": j - i})
        i = j + 1
    return pd.DataFrame(trades)


def stats(t: pd.DataFrame) -> dict:
    if t.empty:
        return {"ops": 0}
    win, loss = t[t.pips > 0], t[t.pips <= 0]
    eq = t["R"].cumsum()
    return {
        "ops": len(t),
        "acierto %": round(100 * len(win) / len(t), 1),
        "gan. media R": round(win.R.mean(), 2) if len(win) else 0,
        "pérd. media R": round(loss.R.mean(), 2) if len(loss) else 0,
        "esperanza R": round(t.R.mean(), 3),
        "profit factor": round(win.pips.sum() / -loss.pips.sum(), 2) if len(loss) else np.inf,
        "total R": round(t.R.sum(), 1),
        "máx DD R": round((eq - eq.cummax()).min(), 1),
        "ops/año": round(len(t) / max((t.salida.max() - t.entrada.min()).days / 365.25, 1), 1),
    }


def report(all_t: pd.DataFrame) -> pd.DataFrame:
    rows = {}
    for (pair, period), g in all_t.groupby(["par", "periodo"]):
        rows[(pair, period)] = stats(g)
    for period, g in all_t.groupby("periodo"):
        rows[("CARTERA", period)] = stats(g.sort_values("salida"))
    return pd.DataFrame(rows).T


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2004-01-01")
    ap.add_argument("--grid", action="store_true", help="sensibilidad del acierto a umbral RSI y stop")
    args = ap.parse_args()
    data = {p: clean(d) for p, d in
            ((p, load_daily([t], args.start).get(t)) for p, t in PAIRS.items()) if d is not None}
    for p, d in data.items():
        print(f"{p}: {len(d)} velas {d.index.min().date()} -> {d.index.max().date()}")

    t = pd.concat([backtest(d, p) for p, d in data.items()], ignore_index=True)
    t["periodo"] = np.where(t.entrada < SPLIT, "IS <2016", "OOS 2016+")
    pd.set_option("display.width", 200)
    print("\n=== RSI(2) a favor de tendencia — reglas base ===")
    print(report(t).to_string())
    print("\nSalidas por motivo (%):")
    print((t.motivo.value_counts(normalize=True) * 100).round(1).to_string())
    print("\nAcierto OOS por año (cartera):")
    oos = t[t.periodo == "OOS 2016+"]
    print(oos.groupby(oos.salida.dt.year).apply(
        lambda g: pd.Series({"ops": len(g), "acierto %": round(100 * (g.pips > 0).mean(), 1),
                             "total R": round(g.R.sum(), 1)})).to_string())
    t.to_csv("backtest/fx_meanrev_trades.csv", index=False)

    if args.grid:
        print("\n=== Sensibilidad (todos los pares, todo el periodo) ===")
        rows = []
        for lo in (5, 10, 20):
            for stop in (1.5, 2.5, 4.0, 99):
                g = pd.concat([backtest(d, p, rsi_lo=lo, rsi_hi=100 - lo, stop_atr=stop) for p, d in data.items()])
                rows.append({"RSI <": lo, "stop ATR": "sin stop" if stop == 99 else stop, **stats(g)})
        print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()

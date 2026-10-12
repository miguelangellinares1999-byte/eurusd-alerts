"""Backtest de seguimiento de tendencia en H4 (MT5) con filtro de sesión: pocas ganadoras grandes.

Mismos datos, pares, costes y división in-sample/out-of-sample que backtest/fx_session_4h.py
(H1 reales desde 2020, H4 construidas desde las H1, IS 2020-2022, OOS 2023+).

Estrategias (reglas fijadas antes de ver resultados, iguales para todos los pares):
- D "Donchian 55/20": compra al cierre por encima del máximo de las 55 H4 anteriores si el cierre
  está sobre la EMA200 (ventas simétricas). Stop inicial 2 ATR(14). Salida al cierre por debajo del
  mínimo de las 20 H4 anteriores (o del stop, lo que esté más cerca).
- E "Cruce EMA 20/50": compra cuando la EMA20 cruza sobre la EMA50 con el cierre sobre la EMA200.
  Stop inicial 2 ATR. Salida con el cruce contrario.
- F "Retroceso a EMA50, TP 3R": tendencia alcista (EMA50 > EMA200 y cierre > EMA200); la vela toca
  la EMA50 y cierra por encima -> compra. Stop 1.5 ATR, objetivo fijo 3R, máximo 60 H4 (10 días).
Filtro de sesión sobre la hora de entrada (cierre de la H4, hora servidor = NY + 7):
"Todas" o "Londres+NY" (08, 12 y 16 servidor = 01, 05 y 09 NY).
Si el stop y el objetivo caen en la misma H4, cuenta el stop (peor caso). Coste: spread de la vela de
entrada. Sin swaps ni deslizamiento.
"""

from __future__ import annotations

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

from backtest.fx_session_4h import PAIRS, SPLIT, atr, load_h1, stats, to_h4, trade

SESSIONS = {"Todas": (0, 4, 8, 12, 16, 20), "Londres+NY": (8, 12, 16)}


def indicators(h4: pd.DataFrame) -> pd.DataFrame:
    c = h4.close
    out = pd.DataFrame({
        "ema20": c.ewm(span=20, adjust=False).mean(),
        "ema50": c.ewm(span=50, adjust=False).mean(),
        "ema200": c.ewm(span=200, adjust=False).mean(),
        "hi55": h4.high.rolling(55).max().shift(),
        "lo55": h4.low.rolling(55).min().shift(),
        "hi20": h4.high.rolling(20).max().shift(),
        "lo20": h4.low.rolling(20).min().shift(),
        "atr": atr(h4),
    })
    out["hour"] = (h4.index + pd.Timedelta(hours=4)).hour
    return out


def signal(kind: str, h4: pd.DataFrame, ind: pd.DataFrame, i: int) -> int:
    c, x, p = h4.close.iloc[i], ind.iloc[i], ind.iloc[i - 1]
    if kind == "D":
        return 1 if c > x.hi55 and c > x.ema200 else -1 if c < x.lo55 and c < x.ema200 else 0
    if kind == "E":
        up = p.ema20 <= p.ema50 and x.ema20 > x.ema50
        dn = p.ema20 >= p.ema50 and x.ema20 < x.ema50
        return 1 if up and c > x.ema200 else -1 if dn and c < x.ema200 else 0
    lo, hi = h4.low.iloc[i], h4.high.iloc[i]
    if x.ema50 > x.ema200 and c > x.ema200 and lo <= x.ema50 < c:
        return 1
    if x.ema50 < x.ema200 and c < x.ema200 and hi >= x.ema50 > c:
        return -1
    return 0


def run(kind: str, h1: pd.DataFrame, pair: str, session: str) -> list[dict]:
    h4 = to_h4(h1)
    ind = indicators(h4)
    pip = h1.attrs["pip"]
    h, l, c, sp = (h4[x].to_numpy() for x in ("high", "low", "close", "spread"))
    allowed = np.isin(ind.hour, SESSIONS[session])
    out, i, n = [], 200, len(h4)
    while i < n - 1:
        side = signal(kind, h4, ind, i) if allowed[i] else 0
        if not side:
            i += 1
            continue
        entry = c[i]
        risk = (1.5 if kind == "F" else 2.0) * ind.atr.iloc[i]
        stop = entry - side * risk
        target = entry + side * 3 * risk if kind == "F" else None
        px, why, j = None, "tiempo", i
        last = min(i + 60, n - 1) if kind == "F" else n - 1
        for j in range(i + 1, last + 1):
            if (side == 1 and l[j] <= stop) or (side == -1 and h[j] >= stop):
                px, why = stop, "stop"
                break
            if target is not None and ((side == 1 and h[j] >= target) or (side == -1 and l[j] <= target)):
                px, why = target, "objetivo"
                break
            x = ind.iloc[j]
            if kind == "D" and ((side == 1 and c[j] < x.lo20) or (side == -1 and c[j] > x.hi20)):
                px, why = c[j], "salida"
                break
            if kind == "E" and side * (x.ema20 - x.ema50) < 0:
                px, why = c[j], "salida"
                break
        if px is None:
            px = c[j]
        out.append(trade(pair, h4, i, j, side, entry, px, risk, sp[i] / 10, pip, why))
        i = j + 1
    return out


def main() -> None:
    pd.set_option("display.width", 220)
    if not mt5.initialize():
        raise SystemExit(f"No se pudo conectar con MT5: {mt5.last_error()}")
    data = {p: load_h1(s) for p, s in PAIRS.items()}
    names = {"D": "D Donchian 55/20", "E": "E Cruce EMA 20/50", "F": "F Retroceso EMA50 3R"}
    summary, by_pair, all_t = {}, {}, []
    for kind, name in names.items():
        for session in SESSIONS:
            t = pd.DataFrame([x for p, d in data.items() for x in run(kind, d, p, session)])
            t["periodo"] = np.where(t.entrada < SPLIT, "IS 2020-22", "OOS 2023+")
            t["estrategia"] = f"{name} - {session}"
            all_t.append(t)
            for per, g in t.groupby("periodo"):
                summary[(f"{name} - {session}", per)] = stats(g)
            if session == "Todas":
                for (pair, per), g in t.groupby(["par", "periodo"]):
                    by_pair[(name, pair, per)] = stats(g)
    print("=== Resumen (cartera de 5 pares) ===")
    print(pd.DataFrame(summary).T.to_string())
    print("\n=== Por par (sin filtro de sesión) ===")
    print(pd.DataFrame(by_pair).T.to_string())
    pd.concat(all_t).to_csv("backtest/fx_trend_4h_trades.csv", index=False)


if __name__ == "__main__":
    main()

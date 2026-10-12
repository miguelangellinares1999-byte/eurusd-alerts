"""Backtest EURUSD 4H: BOS a favor de tendencia + entrada límite en el 50% del último rango.

Reglas (largos; cortos simétricos):
- Swings H4 por fractal (2 velas a cada lado), confirmados 2 velas después del pivote.
- Tendencia: los dos últimos swings confirmados forman HH y HL. Con --ema se exige además
  cierre por encima de la EMA200 H4.
- BOS: primera vela H4 que cierra por encima del último swing high confirmado.
- Rango: desde el mínimo previo al BOS (mínimo entre el swing roto y la vela del BOS) hasta
  el máximo del impulso, que se actualiza con cada vela H4 cerrada mientras la orden no se llena.
- Entrada: límite en el 50% del rango. SL: mínimo del rango. Válida 30 velas H4; se cancela si
  aparece otro BOS (en cualquier dirección).
- Salidas: 2R, 3R fijos, o estructural (trailing al último swing low confirmado tras la
  entrada; sale cuando una vela H4 cierra por debajo). El SL inicial siempre por toque.
- Simulación sobre velas H1 (bid) con spread: lo peor si SL y TP caen en la misma vela, y
  en la vela del llenado solo cuenta el SL.
"""

from __future__ import annotations

import argparse

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

PIVOT = 2
ORDER_BARS = 30


def load(tf, start: str) -> pd.DataFrame:
    mt5.initialize()
    r = pd.DataFrame(mt5.copy_rates_from_pos("EURUSD", tf, 0, 100_000))
    r["time"] = pd.to_datetime(r["time"], unit="s")
    r = r.set_index("time")[["open", "high", "low", "close"]]
    return r[r.index >= start]


def setups(h4: pd.DataFrame, use_ema: bool) -> list[dict]:
    hi, lo, cl = (h4[c].to_numpy() for c in ("high", "low", "close"))
    ema = h4["close"].ewm(span=200, adjust=False).mean().to_numpy()
    n = len(h4)
    sh, sl = [], []  # (idx, precio) confirmados
    broken_h, broken_l = set(), set()
    out = []
    for i in range(n):
        p = i - PIVOT
        if p >= PIVOT:
            w = slice(p - PIVOT, p + PIVOT + 1)
            if hi[p] == hi[w].max():
                sh.append((p, hi[p]))
            if lo[p] == lo[w].min():
                sl.append((p, lo[p]))
        if len(sh) < 2 or len(sl) < 2:
            continue
        up = sh[-1][1] > sh[-2][1] and sl[-1][1] > sl[-2][1]
        dn = sh[-1][1] < sh[-2][1] and sl[-1][1] < sl[-2][1]
        (ih, ph), (il, pl) = sh[-1], sl[-1]
        if cl[i] > ph and ih not in broken_h:
            broken_h.add(ih)
            if up and (not use_ema or cl[i] > ema[i]):
                out.append(dict(i=i, dir=1, base=lo[ih:i + 1].min()))
        if cl[i] < pl and il not in broken_l:
            broken_l.add(il)
            if dn and (not use_ema or cl[i] < ema[i]):
                out.append(dict(i=i, dir=-1, base=hi[il:i + 1].max()))
    return out


def simulate(h4: pd.DataFrame, h1: pd.DataFrame, sets: list[dict], spread: float) -> pd.DataFrame:
    hi4, lo4, cl4 = (h4[c].to_numpy() for c in ("high", "low", "close"))
    t4 = h4.index
    h1_hi, h1_lo = h1["high"].to_numpy(), h1["low"].to_numpy()
    h1_pos = np.searchsorted(h1.index.values, t4.values)  # primera H1 de cada H4
    h1_end = np.append(h1_pos[1:], len(h1))
    next_bos = [s["i"] for s in sets[1:]] + [len(h4)]
    rows = []
    for s, nb in zip(sets, next_bos):
        d, base, i = s["dir"], s["base"], s["i"]
        ext = hi4[i] if d == 1 else lo4[i]
        fill = None
        for j in range(i + 1, min(i + 1 + ORDER_BARS, nb + 1, len(h4))):
            entry = base + 0.5 * (ext - base)
            for k in range(h1_pos[j], h1_end[j]):
                # largos: el ask toca la entrada; cortos: el bid toca la entrada
                if (d == 1 and h1_lo[k] + spread <= entry) or (d == -1 and h1_hi[k] >= entry):
                    fill = (j, k, entry)
                    break
            if fill:
                break
            ext = max(ext, hi4[j]) if d == 1 else min(ext, lo4[j])
        if not fill:
            rows.append(dict(time=t4[i], dir=d, filled=False))
            continue
        j0, k0, entry = fill
        stop = base
        risk = abs(entry - stop)
        row = dict(time=t4[i], dir=d, filled=True, entry=entry, risk_pips=risk * 1e4)

        def bid_hits_sl(k):  # SL de largos con el bid; de cortos con el ask
            return h1_lo[k] <= stop if d == 1 else h1_hi[k] + spread >= stop

        for rr in (2, 3):
            tp = entry + d * rr * risk
            res = 0.0  # abierta al final de los datos
            for k in range(k0, len(h1)):
                if bid_hits_sl(k):
                    res = -1.0
                    break
                if k > k0 and ((d == 1 and h1_hi[k] >= tp) or (d == -1 and h1_lo[k] + spread <= tp)):
                    res = float(rr)
                    break
            row[f"r{rr}"] = res

        # estructural: trailing al último swing confirmado tras la entrada, salida por cierre H4
        trail = stop
        res = None
        for j in range(j0, len(h4)):
            for k in range(k0 if j == j0 else h1_pos[j], h1_end[j]):
                if bid_hits_sl(k):
                    res = -1.0
                    break
            if res is not None:
                break
            if j > j0 and ((d == 1 and cl4[j] < trail) or (d == -1 and cl4[j] + spread > trail)):
                px = cl4[j] if d == 1 else cl4[j] + spread
                res = d * (px - entry) / risk
                break
            p = j - PIVOT
            if p > j0:  # swing nacido tras la entrada
                w = slice(p - PIVOT, p + PIVOT + 1)
                if d == 1 and lo4[p] == lo4[w].min() and lo4[p] > trail:
                    trail = lo4[p]
                if d == -1 and hi4[p] == hi4[w].max() and hi4[p] < trail:
                    trail = hi4[p]
        row["rs"] = res if res is not None else 0.0
        rows.append(row)
    return pd.DataFrame(rows)


def summary(df: pd.DataFrame, col: str) -> dict:
    t = df[df.filled][col]
    wins, losses = t[t > 0], t[t < 0]
    eq = t.cumsum()
    return {
        "operaciones": len(t),
        "% acierto": round(100 * len(wins) / len(t), 1),
        "R medio": round(t.mean(), 3),
        "R total": round(t.sum(), 1),
        "profit factor": round(wins.sum() / -losses.sum(), 2),
        "max DD (R)": round((eq.cummax() - eq).max(), 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--spread-pips", type=float, default=1.0)
    ap.add_argument("--ema", action="store_true")
    a = ap.parse_args()
    h4, h1 = load(mt5.TIMEFRAME_H4, a.start), load(mt5.TIMEFRAME_H1, a.start)
    sets = setups(h4, a.ema)
    df = simulate(h4, h1, sets, a.spread_pips * 1e-4)
    print(f"H4 {h4.index[0]:%Y-%m-%d} -> {h4.index[-1]:%Y-%m-%d} | setups {len(df)} | "
          f"llenadas {df.filled.sum()} ({100 * df.filled.mean():.0f}%) | "
          f"riesgo medio {df.risk_pips.mean():.1f} pips")
    res = pd.DataFrame({name: summary(df, c) for name, c in
                        (("Estructural", "rs"), ("2R", "r2"), ("3R", "r3"))}).T
    print(res.to_string())
    f = df[df.filled]
    print("\nPor año (% acierto / R total):")
    print(f.groupby(f.time.dt.year).apply(lambda g: pd.Series({
        c: f"{100 * (g[c] > 0).mean():.0f}% / {g[c].sum():+.1f}R" for c in ("rs", "r2", "r3")})).to_string())


if __name__ == "__main__":
    main()

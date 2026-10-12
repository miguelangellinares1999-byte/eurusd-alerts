"""Backtest CRT (Candle Range Theory): bias en H4, entrada en M15 con FVG.

Reglas (cortos; largos simétricos):
- Bias H4: la vela A marca el rango. La vela B (siguiente) barre el máximo de A y cierra
  de vuelta dentro del rango de A (cierre < high A y > low A) -> bias bajista.
  Objetivo CRT: el mínimo de A.
- Entrada M15: tras el cierre de B, durante las siguientes WINDOW velas H4, se espera un FVG
  bajista en M15 (low[k-2] > high[k]). Orden límite en el borde inferior del gap (high[k]).
  Se exige que la entrada quede por debajo del high de B y por encima del objetivo.
- Filtro de sesión (--session): solo setups cuya vela de entrada C (la H4 siguiente a B) es la
  de Londres (01:00-05:00 NY) o la de Nueva York (09:00-13:00 NY); la orden debe llenarse
  dentro de esa vela. El servidor MT5 va a hora NY + 7 (también con horario de verano).
- SL: high de B (el barrido) + 1 pip. TP: mínimo de A (CRT) o 2R fijo.
- Se cancela la orden si antes del llenado el precio toca el objetivo o supera el SL, o si
  termina la ventana. Un trade a la vez. Cierre a mercado tras MAX_HOLD velas M15.
- Simulación en M15 (bid de MT5): SL antes que TP si caen en la misma vela; en la vela del
  llenado solo cuenta el SL. El spread se descuenta como coste en R.
"""

from __future__ import annotations

import argparse

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

PIP = 0.0001
WINDOW = 2      # velas H4 tras B para formar y llenar el FVG
MAX_HOLD = 96   # velas M15 (24h)
BUFFER = 1 * PIP
SERVER_MINUS_NY = 7  # horas
SESSIONS = {"london": (1,), "ny": (9,), "both": (1, 9)}  # hora NY de inicio de la vela C


def load_m15(symbol: str, start: str, end: str) -> pd.DataFrame:
    mt5.initialize()
    r = pd.DataFrame(mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, 100_000))
    r["time"] = pd.to_datetime(r["time"], unit="s")
    r = r.set_index("time")[["open", "high", "low", "close"]]
    return r[(r.index >= start) & (r.index < end)]


def to_h4(m15: pd.DataFrame) -> pd.DataFrame:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    return m15.resample("4h").agg(agg).dropna()


def crt_setups(h4: pd.DataFrame) -> list[dict]:
    hi, lo, cl = (h4[c].to_numpy() for c in ("high", "low", "close"))
    out = []
    for b in range(1, len(h4) - 1):
        a = b - 1
        inside = lo[a] < cl[b] < hi[a]
        if hi[b] > hi[a] and lo[b] >= lo[a] and inside:
            out.append(dict(t=h4.index[b + 1], dir=-1, sl=hi[b] + BUFFER, tgt=lo[a]))
        elif lo[b] < lo[a] and hi[b] <= hi[a] and inside:
            out.append(dict(t=h4.index[b + 1], dir=1, sl=lo[b] - BUFFER, tgt=hi[a]))
    return out


def in_session(sets: list[dict], session: str) -> list[dict]:
    hours = {(h + SERVER_MINUS_NY) % 24 for h in SESSIONS[session]}
    return [s for s in sets if s["t"].hour in hours]


def simulate(m15: pd.DataFrame, sets: list[dict], tp_mode: str, spread: float,
             window: int = WINDOW) -> pd.DataFrame:
    t = m15.index
    hi, lo, cl = (m15[c].to_numpy() for c in ("high", "low", "close"))
    rows, busy_until = [], t[0]
    for s in sets:
        if s["t"] < busy_until:
            continue
        d, sl, tgt = s["dir"], s["sl"], s["tgt"]
        k0 = t.searchsorted(s["t"])
        k_end = t.searchsorted(s["t"] + pd.Timedelta(hours=4 * window))
        entry = fill = None
        for k in range(k0, min(k_end, len(t))):
            if entry is not None and ((d == -1 and hi[k] >= entry) or (d == 1 and lo[k] <= entry)):
                fill = k
                break
            # invalidación: toca el SL o el objetivo antes del llenado
            if (d == -1 and (hi[k] >= sl or lo[k] <= tgt)) or (d == 1 and (lo[k] <= sl or hi[k] >= tgt)):
                break
            # FVG formado al cierre de la vela k (velas k-2, k-1, k), con k-2 dentro de la ventana
            if entry is None and k - 2 >= k0:
                if d == -1 and lo[k - 2] > hi[k] and tgt < hi[k] < sl:
                    entry = hi[k]
                elif d == 1 and hi[k - 2] < lo[k] and sl < lo[k] < tgt:
                    entry = lo[k]
        if fill is None:
            continue
        risk = abs(entry - sl)
        tp = tgt if tp_mode == "crt" else entry + d * 2 * risk
        r, exit_k, how = None, None, "time"
        for k in range(fill, min(fill + MAX_HOLD, len(t))):
            if (d == -1 and hi[k] >= sl) or (d == 1 and lo[k] <= sl):
                r, exit_k, how = -1.0, k, "sl"
                break
            if k > fill and ((d == -1 and lo[k] <= tp) or (d == 1 and hi[k] >= tp)):
                r, exit_k, how = abs(tp - entry) / risk, k, "tp"
                break
        if r is None:
            exit_k = min(fill + MAX_HOLD, len(t)) - 1
            r = d * (cl[exit_k] - entry) / risk
        r -= spread / risk
        busy_until = t[exit_k]
        rows.append(dict(entry_time=t[fill], dir="long" if d == 1 else "short", entry=entry, sl=sl,
                         tp=tp, risk_pips=risk / PIP, exit=how, r=r))
    return pd.DataFrame(rows)


def summary(tr: pd.DataFrame) -> pd.DataFrame:
    def stats(g: pd.DataFrame) -> pd.Series:
        eq = g["r"].cumsum()
        wins, losses = g.loc[g["r"] > 0, "r"], g.loc[g["r"] <= 0, "r"]
        return pd.Series({
            "trades": len(g),
            "ganadas": len(wins),
            "perdidas": len(losses),
            "winrate %": round(100 * len(wins) / len(g), 1),
            "R total": round(g["r"].sum(), 2),
            "R medio": round(g["r"].mean(), 3),
            "profit factor": round(wins.sum() / -losses.sum(), 2) if len(losses) else np.inf,
            "max DD (R)": round((eq.cummax().clip(lower=0) - eq).max(), 2),
            "riesgo medio (pips)": round(g["risk_pips"].mean(), 1),
        })
    by_year = tr.groupby(tr["entry_time"].dt.year).apply(stats)
    by_year.loc["Total"] = stats(tr)
    return by_year


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="EURUSD")
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="2026-01-01")
    ap.add_argument("--spread", type=float, default=1.0, help="coste en pips por trade")
    ap.add_argument("--session", choices=["all", *SESSIONS], default="all",
                    help="all = sin filtro; london/ny/both = solo la vela de esa sesión")
    ap.add_argument("--csv", help="guardar trades en este CSV")
    a = ap.parse_args()
    m15 = load_m15(a.symbol, a.start, a.end)
    h4 = to_h4(m15)
    sets = crt_setups(h4)
    window = WINDOW
    if a.session != "all":
        sets, window = in_session(sets, a.session), 1
    print(f"{a.symbol} M15: {m15.index[0]} -> {m15.index[-1]} | sesión: {a.session} | setups CRT H4: {len(sets)}\n")
    for mode, name in (("crt", "TP = extremo opuesto de la vela A (CRT)"), ("2r", "TP = 2R fijo")):
        tr = simulate(m15, sets, mode, a.spread * PIP, window)
        print(f"== {name} ==")
        print(summary(tr).to_string(), "\n")
        print(tr.groupby([tr["entry_time"].dt.year, "dir"])["r"].agg(["count", "sum"]).round(2).to_string(), "\n")
        if a.csv:
            tr.to_csv(a.csv.replace(".csv", f"_{mode}.csv"), index=False)


if __name__ == "__main__":
    main()

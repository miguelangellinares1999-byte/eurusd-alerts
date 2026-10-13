"""CRT H4 -> entrada M15 orientado a retiros en cuenta fondeada.

Generaliza backtest/crt_4h_15m.py a varios símbolos y variantes, elige configuraciones solo con
el periodo de muestra (IS) y las valida fuera de muestra (OOS).

Setup H4 (cortos; largos simétricos): la vela B barre el máximo de A y cierra dentro del rango de
A. SL = máximo de B + colchón. Objetivo CRT = mínimo de A. Las velas H4 empiezan a las 17/21/1/5/9/13
NY (servidor MT5 = NY + 7), así que la vela C de Londres es la de la 1:00 y la de NY la de las 9:00.

Entradas M15 (dentro de las WINDOW velas H4 tras B):
- market: a mercado en la apertura de C.
- fvg: límite en el borde del primer FVG M15 a favor.
- mss: cierre M15 por debajo del mínimo de las 4 velas anteriores (ruptura de estructura); entra
  al cierre.
- wick50: límite en el 50% de la mecha del barrido (entre el máximo de A y el de B).
Una orden pendiente se cancela si antes toca el SL o el objetivo CRT.

Objetivos: crt (extremo opuesto de A), half (50% del camino a ese extremo), 1r, 2r. `be`: SL a
entrada al llegar a +1R. Simulación conservadora: SL antes que TP en la misma vela; en la vela del
llenado de un límite solo cuenta el SL. Coste (spread + comisión) restado en R.

Filtros de sesgo: d1 (dirección de la vela diaria anterior), ema (cierre H4 de B frente a EMA50
H4), prem (cortos solo si C abre por encima de la apertura del día; largos por debajo).

Cuenta fondeada (`funded`): riesgo fijo sobre el saldo inicial, pérdida diaria 5% y DD estático
10%. Cada 14 días naturales, si el beneficio >= MIN_PAYOUT se retira y el saldo vuelve al
inicial. Si se rompe una regla la cuenta se pierde y se empieza otra.
"""

from __future__ import annotations

import argparse
import itertools

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

from backtest.crt_4h_15m import to_h4

SERVER_MINUS_NY = 7
SESSIONS = {"all": None, "london": (1,), "ny": (9,), "both": (1, 9)}
WINDOW = 2
MAX_HOLD = 96
MSS_LOOKBACK = 4
IS_END = "2023-01-01"
PERIOD_DAYS = 14
MIN_PAYOUT = 0.005
# coste por operación (spread + comisión) en precio, aproximado para cuentas de fondeo
COST = {"EURUSDp": 0.00010, "GBPUSDp": 0.00015, "USDJPYp": 0.012, "XAUUSDp": 0.30,
        "NAS100": 2.0, "SPX500": 0.6, "US30": 3.0}
START = {"XAUUSDp": "2020-01-01", "US30": "2020-09-01"}

ENTRIES = ("market", "fvg", "mss", "wick50")
TPS = ("crt", "half", "1r", "2r")
BIASES = ("none", "d1", "ema", "prem")


def load_m15(symbol: str, start: str, end: str) -> pd.DataFrame:
    mt5.initialize()
    mt5.symbol_select(symbol, True)
    r = pd.DataFrame(mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, 200_000))
    r["time"] = pd.to_datetime(r["time"], unit="s")
    r = r.set_index("time")[["open", "high", "low", "close"]]
    return r[(r.index >= start) & (r.index < end)]


def crt_setups(m15: pd.DataFrame, buffer: float) -> pd.DataFrame:
    h4 = to_h4(m15)
    hi, lo, cl = (h4[c].to_numpy() for c in ("high", "low", "close"))
    ema = h4["close"].ewm(span=50, adjust=False).mean().to_numpy()
    d1 = m15.resample("1D").agg({"open": "first", "close": "last"}).dropna()
    rows = []
    for b in range(1, len(h4) - 1):
        a = b - 1
        if not lo[a] < cl[b] < hi[a]:
            continue
        if hi[b] > hi[a] and lo[b] >= lo[a]:
            d, sl, tgt, sweep_from = -1, hi[b] + buffer, lo[a], hi[a]
        elif lo[b] < lo[a] and hi[b] <= hi[a]:
            d, sl, tgt, sweep_from = 1, lo[b] - buffer, hi[a], lo[a]
        else:
            continue
        t = h4.index[b + 1]
        day = t.normalize()
        prev = d1[d1.index < day]
        today = d1[d1.index == day]
        rows.append(dict(
            t=t, dir=d, sl=sl, tgt=tgt, wick50=(sweep_from + sl - d * buffer) / 2,
            d1=int(np.sign(prev.close.iloc[-1] - prev.open.iloc[-1])) if len(prev) else 0,
            ema=1 if cl[b] > ema[b] else -1,
            prem=(-1 if h4.open.iloc[b + 1] > today.open.iloc[0] else 1) if len(today) else 0,
        ))
    return pd.DataFrame(rows)


def filt(sets: pd.DataFrame, session: str, bias: str) -> pd.DataFrame:
    s = sets
    if SESSIONS[session]:
        hours = {(h + SERVER_MINUS_NY) % 24 for h in SESSIONS[session]}
        s = s[s.t.dt.hour.isin(hours)]
    if bias != "none":
        s = s[s[bias] == s.dir]
    return s


def simulate(m15: pd.DataFrame, sets: pd.DataFrame, entry_mode: str, tp_mode: str, be: bool,
             cost: float, window: int) -> pd.DataFrame:
    t = m15.index
    op, hi, lo, cl = (m15[c].to_numpy() for c in ("open", "high", "low", "close"))
    rows, busy = [], t[0]
    for s in sets.itertuples():
        if s.t < busy:
            continue
        d, sl, tgt = s.dir, s.sl, s.tgt
        k0 = t.searchsorted(s.t)
        k_end = min(t.searchsorted(s.t + pd.Timedelta(hours=4 * window)), len(t))
        if k0 >= len(t):
            break
        entry = fill = None
        first_tp = 1  # en un límite, la vela del llenado solo cuenta el SL
        if entry_mode == "market":
            entry, fill, first_tp = op[k0], k0, 0
            if not (d == -1 and tgt < entry < sl or d == 1 and sl < entry < tgt):
                continue
        else:
            if entry_mode == "wick50":
                entry = s.wick50
            for k in range(k0, k_end):
                if entry is not None and entry_mode != "mss" and (d == -1 and hi[k] >= entry or d == 1 and lo[k] <= entry):
                    fill = k
                    break
                if d == -1 and (hi[k] >= sl or lo[k] <= tgt) or d == 1 and (lo[k] <= sl or hi[k] >= tgt):
                    break
                if entry_mode == "fvg" and entry is None and k - 2 >= k0:
                    if d == -1 and lo[k - 2] > hi[k] and tgt < hi[k] < sl:
                        entry = hi[k]
                    elif d == 1 and hi[k - 2] < lo[k] and sl < lo[k] < tgt:
                        entry = lo[k]
                elif entry_mode == "mss" and k >= MSS_LOOKBACK:
                    ref = lo[k - MSS_LOOKBACK:k].min() if d == -1 else hi[k - MSS_LOOKBACK:k].max()
                    if (d == -1 and cl[k] < ref or d == 1 and cl[k] > ref) and (tgt < cl[k] < sl or sl < cl[k] < tgt):
                        entry, fill = cl[k], k + 1  # al cierre: gestión desde la vela siguiente
                        first_tp = 0
                        break
        if fill is None or fill >= len(t):
            continue
        risk = abs(entry - sl)
        if risk < 3 * cost:
            continue
        tp = {"crt": tgt, "half": entry + (tgt - entry) / 2, "1r": entry + d * risk,
              "2r": entry + d * 2 * risk}[tp_mode]
        stop, r, exit_k, how = sl, None, None, "time"
        last = min(fill + MAX_HOLD, len(t))
        for k in range(fill, last):
            if d == -1 and hi[k] >= stop or d == 1 and lo[k] <= stop:
                r, exit_k, how = d * (stop - entry) / risk, k, "sl" if stop == sl else "be"
                break
            if k - fill >= first_tp and (d == -1 and lo[k] <= tp or d == 1 and hi[k] >= tp):
                r, exit_k, how = abs(tp - entry) / risk, k, "tp"
                break
            if be and (d == -1 and lo[k] <= entry - risk or d == 1 and hi[k] >= entry + risk):
                stop = entry
        if r is None:
            exit_k = last - 1
            r = d * (cl[exit_k] - entry) / risk
        r -= cost / risk
        busy = t[exit_k]
        rows.append(dict(entry_time=t[fill], exit_time=t[exit_k] + pd.Timedelta(minutes=15),
                         dir=d, risk=risk, exit=how, r=r))
    return pd.DataFrame(rows)


def stats(tr: pd.DataFrame, years: float) -> dict:
    if tr.empty:
        return dict(trades=0)
    wins, losses = tr.r[tr.r > 0], tr.r[tr.r <= 0]
    eq = tr.r.cumsum()
    return {
        "trades/año": round(len(tr) / years, 1),
        "WR %": round(100 * len(wins) / len(tr), 1),
        "R medio": round(tr.r.mean(), 3),
        "PF": round(wins.sum() / -losses.sum(), 2) if losses.sum() < 0 else np.inf,
        "R/año": round(tr.r.sum() / years, 1),
        "DD máx R": round((eq.cummax().clip(lower=0) - eq).max(), 1),
        "racha perd.": int(max((len(list(g)) for k, g in itertools.groupby(tr.r <= 0) if k), default=0)),
    }


def funded(tr: pd.DataFrame, risk: float, daily: float = 0.05, max_dd: float = 0.10) -> dict:
    """Cuenta fondeada con retiros cada PERIOD_DAYS días; saldo relativo al inicial."""
    if tr.empty:
        return {}
    tr = tr.sort_values("exit_time")
    t0 = tr.exit_time.iloc[0].normalize()
    bal, day, day_start, period = 0.0, None, 0.0, 0
    payouts, period_pnl, breaches, pnl_by_period = [], 0.0, 0, {}
    for row in tr.itertuples():
        p = (row.exit_time - t0).days // PERIOD_DAYS
        while period < p:  # cierre de periodo: retiro
            pnl_by_period[period] = period_pnl
            if bal >= MIN_PAYOUT:
                payouts.append(bal)
                bal = day_start = 0.0
            period, period_pnl = period + 1, 0.0
        d = row.exit_time.normalize()
        if d != day:
            day, day_start = d, bal
        pnl = row.r * risk
        bal += pnl
        period_pnl += pnl
        if bal <= -max_dd or day_start - bal >= daily:
            breaches += 1
            bal = day_start = 0.0
    n_periods = period + 1
    years = n_periods * PERIOD_DAYS / 365.25
    per = pd.Series([pnl_by_period.get(i, 0.0) for i in range(n_periods)])
    return {
        "periodos +%": round(100 * (per > 0).mean(), 1),
        "retiros %": round(100 * len(payouts) / n_periods, 1),
        "retiro medio %": round(100 * np.mean(payouts), 2) if payouts else 0.0,
        "retirado %/año": round(100 * sum(payouts) / years, 1),
        "cuentas perdidas/año": round(breaches / years, 2),
    }


def grid(symbols: list[str], end: str) -> pd.DataFrame:
    out = []
    for sym in symbols:
        m15 = load_m15(sym, START.get(sym, "2016-01-01"), end)
        cost = COST[sym]
        sets = crt_setups(m15, cost)
        print(f"{sym}: {m15.index[0].date()} -> {m15.index[-1].date()}, setups {len(sets)}", flush=True)
        for session, bias, entry, tp, be in itertools.product(SESSIONS, BIASES, ENTRIES, TPS, (False, True)):
            if be and tp == "1r":
                continue
            s = filt(sets, session, bias)
            tr = simulate(m15, s, entry, tp, be, cost, 1 if session != "all" else WINDOW)
            if tr.empty:
                continue
            for part, g in (("IS", tr[tr.entry_time < IS_END]), ("OOS", tr[tr.entry_time >= IS_END])):
                if g.empty:
                    continue
                span = max((g.entry_time.iloc[-1] - g.entry_time.iloc[0]).days / 365.25, 0.5)
                out.append(dict(simbolo=sym, sesion=session, sesgo=bias, entrada=entry, tp=tp, be=be,
                                parte=part, **stats(g, span), **funded(g, 0.005)))
    return pd.DataFrame(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="EURUSDp,GBPUSDp,USDJPYp,XAUUSDp,NAS100,SPX500,US30")
    ap.add_argument("--end", default="2026-10-01")
    ap.add_argument("--csv", default="backtest/crt_funded_grid.csv")
    a = ap.parse_args()
    res = grid(a.symbols.split(","), a.end)
    res.to_csv(a.csv, index=False)
    print(f"guardado {a.csv}: {len(res)} filas")


if __name__ == "__main__":
    main()

"""Backtest de reversión a la media en ETFs de índices (RSI(2) de Connors), velas diarias de Yahoo.

Reglas (solo largos):
- Filtro: cierre por encima de la media de 200 sesiones.
- Entrada: RSI(2) < RSI_IN al cierre. Se compra a ese cierre (o a la apertura siguiente con
  --next-open).
- Salida: primer cierre por encima de la media de 5 sesiones, o tras MAX_DAYS sesiones.
  Opcional (--stop-atr N): stop a N ATR(14) bajo la entrada, por toque con el mínimo del día.
- Costes de CFD en MT5: COST_RT ida y vuelta (spread + comisión) y swap del SWAP_YEAR anual
  sobre el valor de la posición por cada noche natural (los fines de semana cuentan).
- Cartera: capital repartido a partes iguales entre los ETFs; cada uno usa su parte cuando
  tiene señal (sin apalancamiento). Con --dynamic, cada día todo el capital se reparte entre
  las posiciones abiertas (1 posición = 100%, 2 = 50% cada una...). Rentabilidades de precio,
  sin dividendos.
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from backtest.market import load_daily

TICKERS = ["SPY", "QQQ", "IWM", "DIA"]
RSI_IN = 10
MAX_DAYS = 10
COST_RT = 0.0005
SWAP_YEAR = 0.0743


def rsi(close: pd.Series, n: int = 2) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def indicators(df: pd.DataFrame) -> pd.DataFrame:
    """MM200, MM5, RSI(2), ATR(14) y si la vela da señal de entrada (también lo usa etf_alerts.py)."""
    h, l, c = df["high"], df["low"], df["close"]
    out = pd.DataFrame({
        "close": c,
        "sma200": c.rolling(200).mean(),
        "sma5": c.rolling(5).mean(),
        "rsi2": rsi(c),
        "atr": pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1).rolling(14).mean(),
    })
    out["entry_signal"] = (c > out["sma200"]) & (out["rsi2"] < RSI_IN)
    return out


def trades_for(df: pd.DataFrame, start, end, next_open: bool, stop_atr: float | None,
               swap_year: float = SWAP_YEAR) -> pd.DataFrame:
    o, h, l, c = (df[k] for k in ("open", "high", "low", "close"))
    ind = indicators(df)
    sma5, atr, signal = ind["sma5"], ind["atr"], ind["entry_signal"]
    idx, rows, i = df.index, [], 0
    while i < len(df) - 1:
        t = idx[i]
        if not (start <= t < end and signal.iloc[i]):
            i += 1
            continue
        e_i = i + 1 if next_open else i
        entry = o.iloc[e_i] if next_open else c.iloc[i]
        stop = entry - stop_atr * atr.iloc[i] if stop_atr else None
        j, exit_px, how = e_i, None, "tiempo"
        while j < len(df) - 1:
            j += 1
            if stop is not None and l.iloc[j] <= stop:
                exit_px, how = min(o.iloc[j], stop), "stop"
                break
            if c.iloc[j] > sma5.iloc[j]:
                exit_px, how = c.iloc[j], "rebote"
                break
            if j - i >= MAX_DAYS:
                exit_px = c.iloc[j]
                break
        if exit_px is None:  # sigue abierta al final de los datos
            break
        nights = (idx[j] - idx[e_i]).days
        ret = exit_px / entry - 1 - COST_RT - swap_year / 365 * nights
        rows.append(dict(entrada=idx[e_i], salida=idx[j], dias=j - e_i, ret=ret, salida_por=how,
                         entry_px=entry, exit_px=exit_px))
        i = j + 1  # señal nueva solo tras cerrar
    return pd.DataFrame(rows)


def daily_returns(df: pd.DataFrame, tr: pd.DataFrame, start, end) -> pd.Series:
    """Rendimiento diario del ETF mientras hay posición (0 fuera), con los costes en el día de salida."""
    c = df["close"]
    out = pd.Series(0.0, index=c.index)
    for t in tr.itertuples():
        path = c.loc[t.entrada:t.salida].copy()
        path.iloc[-1] = t.exit_px
        daily = pd.concat([pd.Series([t.entry_px]), path.reset_index(drop=True)]).pct_change().iloc[1:].to_numpy().copy()
        daily[-1] = (1 + daily[-1]) * (1 + t.ret) / (t.exit_px / t.entry_px) - 1  # costes en el día de salida
        out.loc[path.index] = daily
    return out[(out.index >= start) & (out.index < end)]


def trade_stats(tr: pd.DataFrame) -> dict:
    w, ls = tr.loc[tr.ret > 0, "ret"], tr.loc[tr.ret <= 0, "ret"]
    return {
        "trades": len(tr),
        "acierto %": round(100 * len(w) / len(tr), 1) if len(tr) else np.nan,
        "gana media %": round(100 * w.mean(), 2) if len(w) else np.nan,
        "pierde media %": round(100 * ls.mean(), 2) if len(ls) else np.nan,
        "esperanza %": round(100 * tr.ret.mean(), 3) if len(tr) else np.nan,
        "profit factor": round(w.sum() / -ls.sum(), 2) if len(ls) else np.inf,
        "peor trade %": round(100 * tr.ret.min(), 1) if len(tr) else np.nan,
        "días medios": round(tr.dias.mean(), 1) if len(tr) else np.nan,
    }


def curve_stats(r: pd.Series) -> dict:
    eq = (1 + r).cumprod()
    years = (r.index[-1] - r.index[0]).days / 365.25
    vol = r.std() * np.sqrt(252)
    return {
        "rentab. anual %": round(100 * (eq.iloc[-1] ** (1 / years) - 1), 1),
        "total %": round(100 * (eq.iloc[-1] - 1), 1),
        "max DD %": round(100 * (eq / eq.cummax() - 1).min(), 1),
        "Sharpe": round(r.mean() * 252 / vol, 2) if vol else np.nan,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default="2026-10-08")
    ap.add_argument("--next-open", action="store_true", help="entrar en la apertura siguiente")
    ap.add_argument("--stop-atr", type=float, default=None)
    ap.add_argument("--tickers", nargs="+", default=TICKERS)
    ap.add_argument("--swap", type=float, default=SWAP_YEAR, help="swap anual (0 = ETF real sin CFD)")
    ap.add_argument("--dynamic", action="store_true", help="todo el capital entre las posiciones abiertas")
    a = ap.parse_args()
    s, e = pd.Timestamp(a.start), pd.Timestamp(a.end)
    tickers = a.tickers
    data = load_daily(tickers, "2014-01-01")
    all_tr, rets, bh, inpos = [], {}, {}, {}
    print(f"RSI(2)<{RSI_IN} sobre MM200, salida > MM5 o {MAX_DAYS} sesiones"
          f"{', entrada apertura siguiente' if a.next_open else ', entrada al cierre'}"
          f"{f', stop {a.stop_atr} ATR' if a.stop_atr else ', sin stop'}, swap {a.swap:.2%} | {' '.join(tickers)} | {s.date()} -> {e.date()}\n")
    per_ticker = {}
    for t in tickers:
        df = data[t]
        tr = trades_for(df, s, e, a.next_open, a.stop_atr, a.swap)
        tr["etf"] = t
        all_tr.append(tr)
        rets[t] = daily_returns(df, tr, s, e)
        held = pd.Series(False, index=df.index)
        for x in tr.itertuples():  # días con exposición (desde el primero con rendimiento)
            held.loc[x.entrada:x.salida] = True
            if not a.next_open:
                held.loc[x.entrada] = False
        inpos[t] = held[(held.index >= s) & (held.index < e)]
        c = df["close"]
        bh[t] = c[(c.index >= s) & (c.index < e)].pct_change().fillna(0)
        per_ticker[t] = trade_stats(tr) | curve_stats(rets[t]) | {
            "% tiempo invertido": round(100 * (rets[t] != 0).mean(), 0),
            "comprar y mantener anual %": curve_stats(bh[t])["rentab. anual %"],
            "comprar y mantener DD %": curve_stats(bh[t])["max DD %"],
        }
    tr = pd.concat(all_tr, ignore_index=True)
    print("== Por ETF ==")
    print(pd.DataFrame(per_ticker).T.to_string(), "\n")

    if a.dynamic:
        n = pd.DataFrame(inpos).fillna(False).sum(axis=1).clip(lower=1)
        port = pd.DataFrame(rets).fillna(0).sum(axis=1) / n
    else:
        port = pd.DataFrame(rets).fillna(0).mean(axis=1)
    bh_port = pd.DataFrame(bh).fillna(0).mean(axis=1)
    rows = {}
    for y in sorted(tr.entrada.dt.year.unique()):
        ty = tr[tr.entrada.dt.year == y]
        py, by = port[port.index.year == y], bh_port[bh_port.index.year == y]
        st = trade_stats(ty)
        rows[y] = {k: st[k] for k in ("trades", "acierto %", "gana media %", "pierde media %", "esperanza %", "peor trade %")} | {
            "cartera %": round(100 * ((1 + py).prod() - 1), 1),
            "max DD %": round(100 * ((1 + py).cumprod() / (1 + py).cumprod().cummax() - 1).min(), 1),
            "comprar y mantener %": round(100 * ((1 + by).prod() - 1), 1),
        }
    st, cs, cb = trade_stats(tr), curve_stats(port), curve_stats(bh_port)
    rows["Total"] = {k: st[k] for k in ("trades", "acierto %", "gana media %", "pierde media %", "esperanza %", "peor trade %")} | {
        "cartera %": f"{cs['rentab. anual %']}/año", "max DD %": cs["max DD %"], "comprar y mantener %": f"{cb['rentab. anual %']}/año"}
    print(f"== Cartera ({'todo el capital entre las posiciones abiertas' if a.dynamic else f'1/{len(tickers)} del capital por ETF'}) por año ==")
    print(pd.DataFrame(rows).T.to_string(), "\n")
    print(f"Cartera: Sharpe {cs['Sharpe']}, max DD {cs['max DD %']}%, total {cs['total %']}% | "
          f"comprar y mantener: Sharpe {cb['Sharpe']}, max DD {cb['max DD %']}%, total {cb['total %']}%")
    print(f"Tiempo invertido medio: {100 * (pd.DataFrame(rets) != 0).mean().mean():.0f}% | salidas: {tr.salida_por.value_counts().to_dict()}")
    print("Peores trades:\n" + tr.nsmallest(5, "ret").assign(ret=lambda x: (100 * x.ret).round(1)).to_string(index=False))


if __name__ == "__main__":
    main()

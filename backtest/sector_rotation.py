"""Rotación sectorial + barrida semanal tras impulso con FVG, en acciones del S&P 500 (solo largos).

Sectores líderes (se recalculan cada semana con datos cerrados, así se sigue la rotación):
- Fuerza relativa de cada ETF sectorial SPDR frente a SPY: media del exceso de rentabilidad
  a 13 y 26 semanas. Se cogen los `top` mejores que además estén sobre su media de 40 semanas.
- Filtro de mercado opcional: SPY sobre su media de 40 semanas (mercado alcista).

Modelo de entrada semanal:
1. Impulso: dos velas semanales alcistas seguidas (cierre > apertura) que dejan un FVG alcista,
   es decir, el mínimo de la segunda queda por encima del máximo de la vela previa al impulso.
2. Retroceso: en las `window` semanas siguientes, una semana perfora el mínimo de la semana
   anterior (toma el último low semanal) y cierra por encima de él (reintegra al rango).
   El setup se anula si alguna semana cierra por debajo del FVG (máximo de la vela previa).
3. Entrada al cierre de la semana de la barrida, SL en su mínimo. TP: máximo del rango desde el
   impulso ("rango") o un múltiplo fijo del riesgo ("2R", "3R").

Cartera: cada operación arriesga `risk` del capital realizado, con un máximo de `max_pos`
posiciones y como mucho `cap` del capital en nocional por posición (sin apalancamiento).
Universo: S&P 500, Nasdaq 100 o ambos. Ojo: son los componentes ACTUALES (sesgo de supervivencia).
"""

from __future__ import annotations

import argparse
import itertools
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.market import load_daily, sp500_members
from backtest.weekly import WeeklyTrade, r_stats, simulate, weekly_bars

SECTOR_ETF = {
    "Information Technology": "XLK",
    "Financials": "XLF",
    "Energy": "XLE",
    "Health Care": "XLV",
    "Industrials": "XLI",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Utilities": "XLU",
    "Materials": "XLB",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
}
START = "1989-01-01"
NDX_FILE = Path(__file__).with_name("ndx100.csv")
# Valores del Nasdaq 100 que no están en el S&P 500, con su sector GICS.
NDX_EXTRA_SECTORS = {
    "ALAB": "Information Technology", "ARM": "Information Technology", "ASML": "Information Technology",
    "CRWV": "Information Technology", "MSTR": "Information Technology", "NBIS": "Information Technology",
    "SHOP": "Information Technology", "ALNY": "Health Care", "CCEP": "Consumer Staples",
    "MELI": "Consumer Discretionary", "PDD": "Consumer Discretionary", "FER": "Industrials",
    "RKLB": "Industrials", "SPCX": "Industrials", "TRI": "Industrials",
}
PERIODS = ((2000, 2012), (2013, 2026))


def universe(name: str) -> dict[str, str]:
    """Ticker -> sector del universo: "sp500", "ndx" (Nasdaq 100) o "ambos"."""
    m = sp500_members()
    sp = dict(zip(m["ticker"], m["sector"]))
    ndx = {t: sp.get(t) or NDX_EXTRA_SECTORS.get(t) for t in pd.read_csv(NDX_FILE)["ticker"]}
    ndx = {t: s for t, s in ndx.items() if s}
    return {"sp500": sp, "ndx": ndx, "ambos": {**sp, **ndx}}[name]


def weekly_close(daily: pd.DataFrame) -> pd.Series:
    return daily["close"].resample("W-FRI").last().dropna()


def sector_strength(etfs: dict[str, pd.DataFrame], spy: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Tablas semanales (viernes x sector): fuerza relativa frente a SPY, si está sobre su MM40 y
    su puesto entre los sectores en tendencia (NaN si no lo está)."""
    closes = pd.DataFrame({s: weekly_close(etfs[t]) for s, t in SECTOR_ETF.items() if t in etfs})
    spy_w = weekly_close(spy).reindex(closes.index)
    rs = sum(closes.pct_change(n).sub(spy_w.pct_change(n), axis=0) for n in (13, 26)) / 2
    trend = closes > closes.rolling(40).mean()
    return rs, trend, rs.where(trend).rank(axis=1, ascending=False)


def leading_sectors(etfs: dict[str, pd.DataFrame], spy: pd.DataFrame, top: int = 3) -> pd.DataFrame:
    """Tabla semanal (viernes x sector) con True en los sectores líderes de esa semana."""
    return sector_strength(etfs, spy)[2] <= top


def market_bull(spy: pd.DataFrame) -> pd.Series:
    w = weekly_close(spy)
    return w > w.rolling(40).mean()


def setups(daily: pd.DataFrame, window: int = 8, tp: str = "rango") -> pd.DataFrame:
    """Señales del modelo impulso + FVG + barrida del último low semanal."""
    w = weekly_bars(daily)
    o, h, l, c = (w[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    rows = []
    i = 2
    while i < len(w):
        # i-1 e i: dos semanas alcistas; FVG entre el máximo de i-2 y el mínimo de i.
        if not (c[i - 1] > o[i - 1] and c[i] > o[i] and l[i] > h[i - 2]):
            i += 1
            continue
        fvg_low, top = h[i - 2], h[i]
        j = i + 1
        while j < len(w) and j <= i + window:
            if c[j] < fvg_low:
                break
            if l[j] < l[j - 1] < c[j]:
                target = top if tp == "rango" else c[j] + float(tp[:-1]) * (c[j] - l[j])
                if target > c[j] > l[j]:
                    rows.append((w.index[j], w["last_day"].iloc[j], c[j], l[j], target))
                break
            top = max(top, h[j])
            j += 1
        i = max(i + 1, j)
    return pd.DataFrame(rows, columns=["week", "last_day", "entry", "stop_loss", "take_profit"]).set_index("week")


def parse_exit(mode: str) -> tuple[bool, list[float | None]]:
    """"3R+5R" -> (con stop, [3, 5]); "3R+trail" -> mitad a 3R y mitad con stop dinámico;
    "sinstop:3R+5R" -> sin stop loss. Cada tramo es una parte igual de la posición."""
    stop = not mode.startswith("sinstop:")
    legs = [None if p == "trail" else float(p[:-1]) for p in mode.split(":")[-1].split("+")]
    return stop, legs


def simulate_exits(ticker: str, daily: pd.DataFrame, signals: pd.DataFrame, mode: str,
                   cost: float = 0.0, trail_weeks: int = 2) -> list[WeeklyTrade]:
    """Como `weekly.simulate` pero con salidas por tramos.

    - Tramos con objetivo en R; al tocar el primero el stop del resto sube a break-even.
    - Tramo "trail": stop dinámico en el mínimo de las `trail_weeks` últimas semanas cerradas
      (solo sube). Si todos los tramos son "trail" se activa desde la entrada.
    - "sinstop:": no hay stop loss; solo se sale en los objetivos (puede quedar abierta).
    El precio de salida es la media de los tramos y la fecha la del último en cerrar.
    """
    use_stop, legs = parse_exit(mode)
    dates = daily.index
    o, h, l, c = (daily[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    w = weekly_bars(daily)
    wk_last, wk_low = w["last_day"].to_numpy(), w["low"].to_numpy(float)
    trail = any(t is None for t in legs)
    trades: list[WeeklyTrade] = []
    busy_until = pd.Timestamp.min
    for sig in signals.itertuples():
        day = sig.last_day
        if day <= busy_until or sig.entry <= sig.stop_loss:
            continue
        entry, sl = sig.entry, sig.stop_loss
        risk = entry - sl
        tps = [None if t is None else entry + t * risk for t in legs]
        exits: list[float | None] = [None] * len(legs)
        last_i, hit_target = len(dates) - 1, False
        trail_stop = sl
        for i in range(int(dates.searchsorted(day, side="right")), len(dates)):
            if trail:
                k = int(np.searchsorted(wk_last, dates[i].to_datetime64(), side="left"))
                if k >= trail_weeks:
                    trail_stop = max(trail_stop, float(wk_low[k - trail_weeks:k].min()))
            for n, tp in enumerate(tps):
                if exits[n] is not None:
                    continue
                level = trail_stop if tp is None else sl
                if hit_target:
                    level = max(level, entry)
                if use_stop and o[i] <= level:
                    exits[n] = o[i]
                elif tp is not None and o[i] >= tp:
                    exits[n] = o[i]
                elif use_stop and l[i] <= level:
                    exits[n] = level
                elif tp is not None and h[i] >= tp:
                    exits[n] = tp
            hit_target = hit_target or any(
                x is not None and tp is not None and x >= tp for x, tp in zip(exits, tps))
            if all(x is not None for x in exits):
                last_i = i
                break
        done = all(x is not None for x in exits)
        px = float(np.mean([c[-1] if x is None else x for x in exits]))
        result = ("win" if px > entry else "loss") if done else "open"
        trades.append(WeeklyTrade(ticker, day, entry, sl, tps[0] or 0.0, result,
                                  dates[last_i] if done else None, px, cost))
        busy_until = dates[last_i] if done else pd.Timestamp.max
    return trades


def run_trades(
    daily: dict[str, pd.DataFrame],
    sectors: dict[str, str],
    lead: pd.DataFrame | None,
    bull: pd.Series | None,
    window: int = 8,
    tp: str = "rango",
    cost: float = 0.001,
    min_risk: float = 0.01,
) -> list[WeeklyTrade]:
    trades: list[WeeklyTrade] = []
    for ticker, df in daily.items():
        sig = setups(df, window, "rango" if tp == "rango" else "1R")
        if sig.empty:
            continue
        sig = sig[sig["entry"] - sig["stop_loss"] >= min_risk * sig["entry"]]
        if lead is not None:
            sec = sectors.get(ticker)
            if sec not in lead.columns:
                continue
            sig = sig[lead[sec].reindex(sig.index).fillna(False).astype(bool).to_numpy()]
        if bull is not None:
            sig = sig[bull.reindex(sig.index).fillna(False).astype(bool).to_numpy()]
        trades.extend(simulate(ticker, df, sig, cost) if tp == "rango" else simulate_exits(ticker, df, sig, tp, cost))
    return trades


def portfolio(trades: list[WeeklyTrade], risk: float = 0.01, max_pos: int = 10, cap: float = 0.2) -> pd.DataFrame:
    """Simula la cartera por orden de entrada; devuelve una fila por operación tomada con su PnL."""
    events = sorted(trades, key=lambda t: (t.signal_week, t.ticker))
    equity, open_pos, rows = 1.0, [], []
    for t in events:
        # Cierra lo que ya terminó antes de esta entrada.
        for p in sorted([p for p in open_pos if p[0].exit_date is not None and p[0].exit_date < t.signal_week],
                        key=lambda p: p[0].exit_date):
            equity += p[1]
            open_pos.remove(p)
        if len(open_pos) >= max_pos:
            continue
        size = min(risk * equity / (t.risk / t.entry), cap * equity)  # nocional
        pnl = size * t.ret
        open_pos.append((t, pnl))
        rows.append({"ticker": t.ticker, "entry_date": t.signal_week, "exit_date": t.exit_date,
                     "result": t.result, "r": t.r, "ret_pct": 100 * t.ret, "size": size, "pnl": pnl})
    df = pd.DataFrame(rows)
    return df


def yearly(book: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Rentabilidad por año (sobre el capital realizado a inicio de año) y total compuesto."""
    done = book.dropna(subset=["exit_date"]).sort_values("exit_date")
    eq = 1 + done["pnl"].cumsum()
    eq.index = pd.DatetimeIndex(done["exit_date"])
    by_year = eq.groupby(eq.index.year).last()
    start = by_year.shift(1).fillna(1.0)
    years = pd.DataFrame({
        "trades": done.groupby(done["exit_date"].dt.year).size(),
        "winrate": 100 * done.groupby(done["exit_date"].dt.year)["result"].apply(lambda s: (s == "win").mean()),
        "total_r": done.groupby(done["exit_date"].dt.year)["r"].sum(),
        "ret_pct": 100 * (by_year / start - 1),
    })
    curve = pd.concat([pd.Series([1.0]), eq.reset_index(drop=True)])
    dd = 100 * float((1 - curve / curve.cummax()).max())
    span = (done["exit_date"].iloc[-1] - done["entry_date"].iloc[0]).days / 365.25
    total = {
        "trades": len(done),
        "winrate": 100 * float((done["result"] == "win").mean()),
        "avg_r": float(done["r"].mean()),
        "total_ret_pct": 100 * (float(eq.iloc[-1]) - 1),
        "cagr_pct": 100 * (float(eq.iloc[-1]) ** (1 / span) - 1) if eq.iloc[-1] > 0 else -100.0,
        "max_dd_pct": dd,
        "abiertas": int(book["exit_date"].isna().sum()),
        "semanas_media": float((done["exit_date"] - done["entry_date"]).dt.days.mean() / 7),
        "anios_pos": int((years["ret_pct"] > 0).sum()),
        "anios": len(years),
    }
    return years, total


def spy_yearly(spy: pd.DataFrame, years: pd.Index) -> pd.Series:
    c = spy["close"].groupby(spy.index.year).last()
    return (100 * c.pct_change()).reindex(years)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="ambos", choices=("sp500", "ndx", "ambos"))
    ap.add_argument("--start-year", type=int, default=2000)
    ap.add_argument("--risk", type=float, default=0.01)
    ap.add_argument("--max-pos", type=int, default=10)
    ap.add_argument("--tops", default="3,4")
    ap.add_argument("--windows", default="10")
    ap.add_argument("--tps", default="2R,3R,4R,5R")
    ap.add_argument("--filters", default="sector+spy,ninguno")
    ap.add_argument("--csv", default="backtest/sector_rotation_results.csv")
    a = ap.parse_args()

    sectors = universe(a.universe)
    stocks = load_daily(list(sectors), START)
    ref = load_daily(["SPY", *SECTOR_ETF.values()], "1985-01-01")
    spy = ref.pop("SPY")
    print(f"{a.universe}: {len(stocks)} acciones, ETFs: {', '.join(sorted(ref))}")

    since = pd.Timestamp(f"{a.start_year}-01-01")
    stocks = {t: d[d.index >= since - pd.Timedelta(days=60)] for t, d in stocks.items()}
    bull = market_bull(spy)
    tops = [int(x) for x in a.tops.split(",")]
    out, best = [], None
    for top, window, tp, filt in itertools.product(tops, [int(x) for x in a.windows.split(",")],
                                                   a.tps.split(","), a.filters.split(",")):
        if filt == "ninguno" and top != tops[0]:
            continue
        lead = leading_sectors(ref, spy, top) if filt != "ninguno" else None
        tr = [t for t in run_trades(stocks, sectors, lead, bull if filt == "sector+spy" else None, window, tp)
              if t.signal_week >= since]
        if len(tr) < 20:
            continue
        book = portfolio(tr, a.risk, a.max_pos)
        years, total = yearly(book)
        row = dict(universo=a.universe, filtro=filt, top=top if filt != "ninguno" else None, ventana=window,
                   tp=tp, **total, r_total_todas=r_stats(tr)["total_r"])
        for y0, y1 in PERIODS:  # rentabilidad compuesta de cada subperiodo, para ver si el TP aguanta
            part = years.loc[(years.index >= y0) & (years.index <= y1), "ret_pct"]
            row[f"ret_{y0}_{y1}"] = 100 * (float((1 + part / 100).prod()) - 1)
            row[f"avg_r_{y0}_{y1}"] = float(book.loc[book["entry_date"].dt.year.between(y0, y1), "r"].mean())
        out.append(row)
        print({k: round(v, 2) if isinstance(v, float) else v for k, v in row.items()}, flush=True)
        if filt != "ninguno" and (best is None or total["cagr_pct"] / max(total["max_dd_pct"], 1)
                                  > best[0]["cagr_pct"] / max(best[0]["max_dd_pct"], 1)):
            best = (total, years, row, book)

    res = pd.DataFrame(out).sort_values("cagr_pct", ascending=False)
    res.to_csv(a.csv, index=False)
    print(f"\nguardado {a.csv}: {len(res)} configs")
    total, years, row, book = best
    years["spy_pct"] = spy_yearly(spy, years.index)
    print(f"\nMejor config (CAGR/DD): {row}")
    print(years.round(1).to_string())
    book.to_csv(a.csv.replace("_results", "_trades"), index=False)


if __name__ == "__main__":
    main()

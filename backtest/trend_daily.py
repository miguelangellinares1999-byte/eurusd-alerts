"""Backtest de trend following diario multi-activo (velas diarias de Yahoo).

Universo: divisas (spot), índices, bonos, metales, energía y agrícolas (futuros continuos).
Señales, calculadas al cierre del día t y aplicadas al rendimiento del día t+1:
- EMA 50/200: largo si EMA50 > EMA200, corto si no (siempre en mercado).
- Donchian 55/20: entra largo al cerrar por encima del máximo de 55 días y sale al cerrar por
  debajo del mínimo de 20 (cortos simétricos); fuera de mercado entre medias.
- Momentum 12m: signo del rendimiento de los últimos 252 días.
- Combinado: media de las tres señales (posición entre -1 y +1).
Tamaño: cada activo apunta a un 10% de volatilidad anual (vol EWMA de 60 días del día anterior),
con el peso nominal limitado a 4x. La cartera es la media de los activos disponibles multiplicada
por LEVERAGE (fijo) para una volatilidad total de un 10-16% según la estrategia.
Costes: COST[clase] por unidad de cambio en el peso nominal. No incluye swaps de divisas ni
el ajuste de los vencimientos de los futuros continuos (Yahoo no los ajusta).
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from backtest.market import load_daily

UNIVERSE = {
    "Divisas": ["EURUSD=X", "GBPUSD=X", "JPY=X", "AUDUSD=X", "CAD=X", "CHF=X", "NZDUSD=X",
                "EURJPY=X", "EURGBP=X", "AUDJPY=X"],
    "Índices": ["ES=F", "NQ=F", "YM=F", "^GDAXI", "^FTSE", "^N225"],
    "Bonos": ["ZN=F", "ZB=F", "ZF=F"],
    "Metales": ["GC=F", "SI=F", "HG=F", "PL=F"],
    "Energía": ["BZ=F", "NG=F", "HO=F"],
    "Agrícolas": ["ZC=F", "ZW=F", "ZS=F", "SB=F", "KC=F", "CT=F", "LE=F"],
}
COST = {"Divisas": 0.0001, "Índices": 0.0002, "Bonos": 0.0001, "Metales": 0.0003,
        "Energía": 0.0005, "Agrícolas": 0.0005}
ASSET_VOL = 0.10
MAX_WEIGHT = 4.0
LEVERAGE = 5.0
MAX_DAILY_RET = 0.25  # filtra saltos de datos (vencimientos, errores) de más del 25%


def signals(close: pd.Series) -> dict[str, pd.Series]:
    ema = (close.ewm(span=50, adjust=False).mean() > close.ewm(span=200, adjust=False).mean()) * 2 - 1
    hi, lo = close.rolling(55).max().shift(), close.rolling(55).min().shift()
    ex_hi, ex_lo = close.rolling(20).max().shift(), close.rolling(20).min().shift()
    pos, don = 0, []
    for c, h, l, eh, el in zip(close, hi, lo, ex_hi, ex_lo):
        if pos == 1 and c < el or pos == -1 and c > eh:
            pos = 0
        if pos == 0:
            pos = 1 if c > h else -1 if c < l else 0
        don.append(pos)
    don = pd.Series(don, index=close.index, dtype=float)
    mom = np.sign(close / close.shift(252) - 1)
    ema = ema.where(close.expanding().count() >= 200).astype(float)
    sig = {"EMA 50/200": ema, "Donchian 55/20": don, "Momentum 12m": mom}
    sig["Combinado"] = pd.concat(sig.values(), axis=1).mean(axis=1, skipna=False)
    return sig


def backtest(data: dict[str, pd.DataFrame]) -> tuple[dict[str, pd.Series], dict[str, pd.DataFrame]]:
    """Rendimiento diario de la cartera por estrategia y contribución por activo."""
    by_strategy: dict[str, dict[str, pd.Series]] = {}
    for cls, tickers in UNIVERSE.items():
        for t in tickers:
            if t not in data:
                continue
            close = data[t]["close"]
            close = close[close > 0]
            ret = close.pct_change().clip(-MAX_DAILY_RET, MAX_DAILY_RET)
            vol = ret.ewm(span=60, min_periods=60).std() * np.sqrt(252)
            weight_unit = (ASSET_VOL / vol).clip(upper=MAX_WEIGHT)
            for name, s in signals(close).items():
                w = (s * weight_unit).shift(1)  # posición decidida al cierre anterior
                pnl = w * ret - COST[cls] * w.diff().abs()
                by_strategy.setdefault(name, {})[f"{cls}|{t}"] = pnl
    port, contrib = {}, {}
    for name, cols in by_strategy.items():
        df = pd.DataFrame(cols).sort_index()
        df = df[df.index.dayofweek < 5]
        n = df.notna().sum(axis=1).replace(0, np.nan)
        port[name] = (df.sum(axis=1, min_count=1) / n * LEVERAGE).fillna(0)
        contrib[name] = df.div(n, axis=0) * LEVERAGE
    return port, contrib


def stats(r: pd.Series) -> dict:
    eq = (1 + r).cumprod()
    dd = (eq / eq.cummax() - 1).min()
    vol = r.std() * np.sqrt(252)
    return {
        "rentab. %": round(100 * (eq.iloc[-1] - 1), 1),
        "vol %": round(100 * vol, 1),
        "Sharpe": round(r.mean() * 252 / vol, 2) if vol else np.nan,
        "max DD %": round(100 * dd, 1),
    }


def yearly(r: pd.Series) -> pd.DataFrame:
    out = pd.DataFrame({y: stats(g) for y, g in r.groupby(r.index.year)}).T
    years = (r.index[-1] - r.index[0]).days / 365.25
    total = stats(r)
    total["rentab. %"] = round(100 * ((1 + r).prod() ** (1 / years) - 1), 1)  # anualizada
    out.loc["Total (anual.)"] = total
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2005-01-01")
    ap.add_argument("--end", default="2026-01-01")
    a = ap.parse_args()
    tickers = [t for ts in UNIVERSE.values() for t in ts] + ["^GSPC"]
    data = load_daily(tickers, "2000-01-01")
    missing = sorted(set(tickers) - set(data))
    print(f"Activos con datos: {len(data) - 1} / {len(tickers) - 1}" + (f" (faltan {missing})" if missing else ""))
    port, contrib = backtest({t: d for t, d in data.items() if t != "^GSPC"})
    s, e = pd.Timestamp(a.start), pd.Timestamp(a.end)
    spx = data["^GSPC"]["close"].pct_change()
    spx = spx[(spx.index >= s) & (spx.index < e)].fillna(0)

    summary = {}
    for name, r in port.items():
        r = r[(r.index >= s) & (r.index < e)]
        y = yearly(r)
        print(f"\n== {name} ==\n{y.to_string()}")
        summary[name] = y["rentab. %"]
    summary["S&P 500 (comprar y mantener)"] = yearly(spx)["rentab. %"]
    print("\n== Rentabilidad % por año ==\n" + pd.DataFrame(summary).to_string())

    c = contrib["Combinado"]
    c = c[(c.index >= s) & (c.index < e)]
    by_cls = c.T.groupby(lambda k: k.split("|")[0]).sum().T
    print("\n== Combinado: contribución % por clase de activo ==")
    print((100 * by_cls.groupby(by_cls.index.year).sum()).round(1).to_string())


if __name__ == "__main__":
    main()

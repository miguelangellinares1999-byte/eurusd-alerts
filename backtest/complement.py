"""¿Qué estrategias complementan a IndexPropBot para retirar (casi) todos los meses en una cuenta fondeada?

Bot actual (perfil de fondeo, operaciones del backtest en Python backtest/index_prop_trades.csv):
ORB 30 en NAS100 (1.5% del saldo inicial) y RSI(2) diario en SP500 + NAS100 (3%).

Complementos en velas diarias de MT5 (reglas fijadas antes de ver resultados, iguales en todos los
símbolos; cartera: índices, oro, plata, petróleo, BTC y divisas mayores, lo que permite The5%ers):
- "EMA 20/50 D1": cruce de la EMA20 sobre la EMA50 con el cierre sobre la EMA200 -> compra al cierre
  (ventas simétricas). Stop inicial 2 ATR(14), salida con el cruce contrario.
- "EMA50 retroceso D1": tendencia (EMA50 > EMA200 y cierre > EMA200), la vela toca la EMA50 y cierra
  por encima -> compra (simétrico en ventas). Stop 1.5 ATR, TP 3R, máximo 20 días.
- "Weinstein": velas semanales (cierre del viernes) y SMA de 30 semanas. Etapa 2: cierre > SMA30,
  SMA30 subiendo (mayor que hace 4 semanas) y cierre por encima del máximo de las 10 semanas
  anteriores (ruptura de la base) -> compra al cierre del viernes. Stop en el mínimo de esas 10
  semanas, salida al cierre semanal por debajo de la SMA30. Etapa 4 simétrica (ventas).
Los stops se comprueban contra el máximo/mínimo diario; si salta el mismo día que la salida, cuenta
el stop. Coste: spread mediano D1 del símbolo + swap de MT5 por noche (en puntos o en % anual del precio,
x3 el día de triple swap). Datos de BlackBull (los de The5%ers pueden diferir algo).

Cuenta fondeada (The5%ers High Stakes): DD máximo 10% estático, pérdida diaria 5% (sobre resultados
cerrados), sin objetivo. A final de cada mes se retira todo lo que esté por encima del saldo inicial
(reparto 80%). Si se rompe una regla se pierde la cuenta y se empieza otra igual.
Simplificaciones: la pérdida flotante de las operaciones largas (Weinstein, EMA) no se mira, solo el
cierre; el resultado se apunta el día de salida.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

CACHE = Path("backtest/cache")
SYMBOLS = ["SPX500", "NAS100", "US30", "GER40", "UK100", "XAUUSDp", "XAGUSDp", "WTI", "BTCUSD",
           "EURUSDp", "GBPUSDp", "USDJPYp", "AUDUSDp"]
START = pd.Timestamp("2020-07-01")
SPLIT = 0.8  # reparto de beneficios


def load_d1(symbol: str) -> tuple[pd.DataFrame, float, dict]:
    """D1 de MT5 (cacheado en backtest/cache/mt5_d1_<símbolo>.csv) + spread mediano y swaps."""
    path = CACHE / f"mt5_d1_{symbol}.csv"
    meta = CACHE / f"mt5_d1_{symbol}.meta.csv"
    if not path.exists():
        import MetaTrader5 as mt5
        mt5.initialize()
        mt5.symbol_select(symbol, True)
        r = pd.DataFrame(mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, 5000))
        r["time"] = pd.to_datetime(r["time"], unit="s")
        r.iloc[:-1].to_csv(path, index=False)  # sin la vela en formación
        i = mt5.symbol_info(symbol)
        pd.DataFrame([{"point": i.point, "swap_mode": i.swap_mode, "swap_long": i.swap_long,
                       "swap_short": i.swap_short, "swap_3day": i.swap_rollover3days}]).to_csv(meta, index=False)
    df = pd.read_csv(path, parse_dates=["time"]).set_index("time")
    m = pd.read_csv(meta).iloc[0].to_dict()
    return df, float(df.spread.median() * m["point"]), m


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df.close.shift()
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def swap_points(meta: dict, side: int, t_in: pd.Timestamp, t_out: pd.Timestamp, price: float) -> float:
    """Swap en unidades de precio (negativo = coste). Modo 1: puntos por noche; modo 5: % anual del
    precio (/360 por noche). Otros modos no se cuentan."""
    mode = int(meta["swap_mode"])
    if mode not in (1, 5):
        return 0.0
    days = pd.date_range(t_in, t_out - pd.Timedelta(days=1), freq="D")
    days = days[days.weekday < 5]
    nights = len(days) + 2 * int((days.weekday == int(meta["swap_3day"])).sum())
    rate = meta["swap_long"] if side == 1 else meta["swap_short"]
    return nights * (rate * meta["point"] if mode == 1 else price * rate / 100 / 360)


def run_trades(df, symbol, strat, signals, spread, meta, max_days=None, tp_r=None, exit_fn=None):
    """Recorre las velas diarias. `signals[k]` = (lado, stop) para entrar al cierre de k."""
    h, l, c = df.high.to_numpy(), df.low.to_numpy(), df.close.to_numpy()
    t = df.index
    out, k = [], 0
    n = len(df)
    while k < n:
        sig = signals.get(k)
        if sig is None:
            k += 1
            continue
        side, stop = sig
        entry = c[k]
        risk = side * (entry - stop)
        if risk <= 2 * spread:
            k += 1
            continue
        target = entry + side * tp_r * risk if tp_r else None
        j, px = k + 1, None
        while j < n:
            if (side == 1 and l[j] <= stop) or (side == -1 and h[j] >= stop):
                px = stop
                break
            if target is not None and ((side == 1 and h[j] >= target) or (side == -1 and l[j] <= target)):
                px = target
                break
            if (exit_fn and exit_fn(j, side)) or (max_days and j - k >= max_days):
                px = c[j]
                break
            j += 1
        if px is None:
            break  # sigue abierta al final de los datos
        pnl = side * (px - entry) - spread + swap_points(meta, side, t[k], t[j], entry)
        out.append({"simbolo": symbol, "estrategia": strat, "entrada": t[k], "salida": t[j], "lado": side,
                    "R": pnl / risk})
        k = j  # una posición por símbolo y estrategia
    return out


def ema_cross(df, symbol, spread, meta):
    e20, e50, e200, a = ema(df.close, 20), ema(df.close, 50), ema(df.close, 200), atr(df)
    up = (e20 > e50) & (e20.shift() <= e50.shift())
    dn = (e20 < e50) & (e20.shift() >= e50.shift())
    sig = {}
    for k in range(200, len(df)):
        if up.iloc[k] and df.close.iloc[k] > e200.iloc[k]:
            sig[k] = (1, df.close.iloc[k] - 2 * a.iloc[k])
        elif dn.iloc[k] and df.close.iloc[k] < e200.iloc[k]:
            sig[k] = (-1, df.close.iloc[k] + 2 * a.iloc[k])
    above = (e20 > e50).to_numpy()
    return run_trades(df, symbol, "EMA 20/50 D1", sig, spread, meta,
                      exit_fn=lambda j, s: above[j] != (s == 1))


def ema_pullback(df, symbol, spread, meta):
    e50, e200, a = ema(df.close, 50), ema(df.close, 200), atr(df)
    c, h, l = df.close, df.high, df.low
    sig = {}
    for k in range(200, len(df)):
        if e50.iloc[k] > e200.iloc[k] and c.iloc[k] > e200.iloc[k] and l.iloc[k] <= e50.iloc[k] < c.iloc[k]:
            sig[k] = (1, c.iloc[k] - 1.5 * a.iloc[k])
        elif e50.iloc[k] < e200.iloc[k] and c.iloc[k] < e200.iloc[k] and h.iloc[k] >= e50.iloc[k] > c.iloc[k]:
            sig[k] = (-1, c.iloc[k] + 1.5 * a.iloc[k])
    return run_trades(df, symbol, "EMA50 retroceso D1", sig, spread, meta, max_days=20, tp_r=3.0)


def weinstein(df, symbol, spread, meta):
    """Señales con velas semanales; entrada y stops sobre las diarias (último día de cada semana)."""
    wk = df.resample("W-FRI").agg({"high": "max", "low": "min", "close": "last"}).dropna()
    last_day = df.index.to_series().resample("W-FRI").last().dropna()
    sma = wk.close.rolling(30).mean()
    rising, falling = sma > sma.shift(4), sma < sma.shift(4)
    hi10, lo10 = wk.high.rolling(10).max().shift(), wk.low.rolling(10).min().shift()
    pos = {d: i for i, d in enumerate(df.index)}
    sig, below, above = {}, set(), set()
    for w in range(len(wk)):
        d = last_day.get(wk.index[w])
        if d is None or pd.isna(sma.iloc[w]):
            continue
        k, cl = pos[d], wk.close.iloc[w]
        if cl < sma.iloc[w]:
            below.add(k)
        if cl > sma.iloc[w]:
            above.add(k)
        if cl > sma.iloc[w] and rising.iloc[w] and cl > hi10.iloc[w]:
            sig[k] = (1, lo10.iloc[w])
        elif cl < sma.iloc[w] and falling.iloc[w] and cl < lo10.iloc[w]:
            sig[k] = (-1, hi10.iloc[w])
    return run_trades(df, symbol, "Weinstein", sig, spread, meta,
                      exit_fn=lambda j, s: j in (below if s == 1 else above))


def bot_trades() -> pd.DataFrame:
    t = pd.read_csv("backtest/index_prop_trades.csv")
    keep = ((t.indice == "NAS100") & (t.estrategia == "ORB 30")) | (t.estrategia == "RSI2 diario")
    t = t[keep]
    ny = pd.to_datetime(t.exit_time, utc=True).dt.tz_convert("America/New_York").dt.tz_localize(None)
    return pd.DataFrame({"simbolo": t.indice, "estrategia": t.estrategia, "salida": ny, "R": t.r})


def funded(trades: pd.DataFrame, risk: dict, size: float) -> dict:
    """Cuenta fondeada: DD 10% estático, diario 5%, retiro mensual de lo que pase del inicial."""
    t = trades[trades.salida >= START].sort_values("salida")
    pnl = t.R * t.estrategia.map(risk) / 100 * size
    day = (t.salida + pd.Timedelta(hours=7)).dt.normalize()  # día de trading de 17:00 a 17:00 NY
    bal, pays, lost, day_start, cur = size, {}, 0, size, None
    month = None
    for x, d in zip(pnl, day):
        if month is not None and d.to_period("M") != month:
            pays[month] = max(bal - size, 0) * SPLIT
            bal = min(bal, size)
        month = d.to_period("M")
        if d != cur:
            cur, day_start = d, bal
        bal += x
        if bal <= size * 0.9 or day_start - bal >= size * 0.05:
            lost += 1
            bal, day_start = size, size
    pays[month] = max(bal - size, 0) * SPLIT
    months = pd.period_range(START.to_period("M"), month, freq="M")
    p = pd.Series(pays).reindex(months, fill_value=0.0)
    m = pnl.groupby(day.dt.to_period("M")).sum().reindex(months, fill_value=0.0)
    eq = pnl.cumsum()
    return {"meses": len(p), "meses con retiro": int((p > 0).sum()),
            "% meses +": round(100 * (m > 0).mean()), "retiro medio/mes": round(p.mean()),
            "mediana retiro": round(p[p > 0].median()) if (p > 0).any() else 0, "mayor retiro": round(p.max()),
            "peor mes": round(m.min()), "DD máx": round((eq.cummax().clip(lower=0) - eq).max()),
            "cuentas perdidas": lost, "total retirado": round(p.sum())}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=float, default=5000)
    args = ap.parse_args()
    rows = []
    for s in SYMBOLS:
        df, spread, meta = load_d1(s)
        for fn in (ema_cross, ema_pullback, weinstein):
            rows += fn(df, s, spread, meta)
    comp = pd.DataFrame(rows)
    comp.to_csv("backtest/complement_trades.csv", index=False)
    comp = comp[comp.salida >= START]
    pd.set_option("display.width", 220)
    print("Complementos desde 2020-07 (R por operación, con spread y swap):")
    print(comp.groupby("estrategia").R.agg(ops="size", R_media="mean", R_total="sum",
                                           acierto=lambda r: round(100 * (r > 0).mean())).round(2))
    print(comp.groupby(["estrategia", "simbolo"]).R.sum().unstack(0).round(1))

    bot = bot_trades()
    allt = pd.concat([bot, comp[["simbolo", "estrategia", "salida", "R"]]], ignore_index=True)
    monthly = allt[allt.salida >= START].groupby([allt.salida.dt.to_period("M"), "estrategia"]).R.sum().unstack(fill_value=0)
    print("\nCorrelación mensual (R):")
    print(monthly.corr().round(2))

    bot_risk = {"ORB 30": 1.5, "RSI2 diario": 3.0}
    scenarios = []
    for scale in (1.0, 0.5, 1 / 3):
        base = {k: v * scale for k, v in bot_risk.items()}
        scenarios.append((f"bot x{scale:.2f}", base))
        for comp_risk in (0.25, 0.5):
            for extra in (["EMA 20/50 D1"], ["EMA50 retroceso D1"], ["Weinstein"],
                          ["EMA 20/50 D1", "Weinstein"], ["EMA 20/50 D1", "EMA50 retroceso D1", "Weinstein"]):
                r = dict(base, **{e: comp_risk for e in extra})
                scenarios.append((f"bot x{scale:.2f} + {' + '.join(extra)} {comp_risk}%", r))
    res = []
    for name, risk in scenarios:
        t = allt[allt.estrategia.isin(risk)]
        res.append({"escenario": name, **funded(t, risk, args.size)})
    res = pd.DataFrame(res)
    res.to_csv("backtest/complement_results.csv", index=False)
    print(f"\nCuenta fondeada de {args.size:,.0f} $ (retiro mensual, reparto 80%), desde {START:%Y-%m}:")
    print(res.to_string(index=False))


if __name__ == "__main__":
    main()

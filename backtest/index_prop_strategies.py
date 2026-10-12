"""Estrategias para pruebas de fondeo en S&P 500 y Nasdaq 100 frente al Daily Cycle.

Datos: CFD SPX500 / NAS100 de MT5 (servidor = NY + 7). M5 reales desde julio 2020; D1 desde 2014.
Reglas fijadas antes de ver resultados (iguales en los dos índices):
- ORB 15 / ORB 30: rango de los primeros 15 / 30 minutos del contado (09:30 NY). Orden stop en
  el máximo y el mínimo hasta las 12:00 NY; la primera que salta (si saltan las dos en la misma
  vela M5 se descarta el día). SL en el otro extremo del rango, TP 2R, cierre a las 16:00 NY.
- Gap & Go: hueco de apertura > 0.3% frente al cierre de las 16:00 del día anterior y la primera
  media hora cierra a favor del hueco -> entrada a las 10:00 NY a favor del hueco. SL en el
  extremo contrario de la primera media hora, TP 2R, cierre a las 16:00 NY.
- RSI(2) diario solo compras: cierre D1 > SMA200 y RSI(2) < 10 -> compra al cierre; salida al
  primer cierre por encima de la SMA5, SL 2.5 ATR(14), máximo 10 días. El stop se mira contra el
  mínimo diario (si coincide con el día de salida, cuenta el stop).
Simulación intradía en M5: si el SL y el TP caen en la misma vela cuenta el SL; en la vela de
entrada solo cuenta el SL. Coste: spread mediano del CFD por operación. Sin comisiones ni swaps.
"""

from __future__ import annotations

from itertools import product

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

from backtest.index_daily_cycle import SYMBOLS, START, load_m5
from backtest.prop_challenge import Rules, Sizing, simulate

NY = "America/New_York"
SPLIT = pd.Timestamp("2024-01-01", tz="UTC")
TP_R = 2.0


def rth_days(m5: pd.DataFrame):
    """Por cada sesión de contado: arrays de la sesión 09:30-16:00 NY (y su índice UTC)."""
    ny = m5.index.tz_convert(NY)
    minutes = ny.hour * 60 + ny.minute
    rth = m5[(minutes >= 570) & (minutes < 960)]
    rth_ny = rth.index.tz_convert(NY)
    for day, g in rth.groupby(rth_ny.date):
        if len(g) < 70 or g.index[0].tz_convert(NY).strftime("%H:%M") != "09:30":
            continue  # sesiones cortas o con huecos
        yield day, g


def walk(h, l, c, k0, side, entry, stop, target, k_end, partial_first=True):
    """Desde la vela k0 hasta k_end. Si la entrada fue dentro de k0 (orden stop), en k0 solo cuenta el SL."""
    for k in range(k0, k_end + 1):
        if (side == 1 and l[k] <= stop) or (side == -1 and h[k] >= stop):
            return stop, k
        if (k > k0 or not partial_first) and ((side == 1 and h[k] >= target) or (side == -1 and l[k] <= target)):
            return target, k
    return c[k_end], k_end


def make_trade(name, strat, g, k_in, k_out, side, entry, px, risk, spread, tp_r=TP_R, in_close=False):
    bar = pd.Timedelta(minutes=5)
    r = (side * (px - entry) - spread) / risk
    return {"indice": name, "estrategia": strat, "fill_time": g.index[k_in] + (bar if in_close else pd.Timedelta(0)),
            "exit_time": g.index[k_out] + bar, "lado": side, "r": r, "tp_r": tp_r,
            "resultado": "win" if r > 0 else "loss", "riesgo_pts": risk}


def orb(m5: pd.DataFrame, name: str, spread: float, minutes: int) -> list[dict]:
    out, n_or = [], minutes // 5
    for day, g in rth_days(m5):
        h, l, c = (g[x].to_numpy() for x in ("high", "low", "close"))
        ny = g.index.tz_convert(NY)
        hi, lo = h[:n_or].max(), l[:n_or].min()
        risk = hi - lo
        if risk <= 4 * spread:
            continue
        cutoff = n_or + int(((ny.hour >= 12)[n_or:]).argmax()) if (ny.hour >= 12).any() else len(g) - 1
        for k in range(n_or, cutoff):
            up, dn = h[k] > hi, l[k] < lo
            if up and dn:
                break
            if not (up or dn):
                continue
            side, entry = (1, hi) if up else (-1, lo)
            stop, target = entry - side * risk, entry + side * TP_R * risk
            px, kx = walk(h, l, c, k, side, entry, stop, target, len(g) - 1)
            out.append(make_trade(name, f"ORB {minutes}", g, k, kx, side, entry, px, risk, spread))
            break
    return out


def gap_and_go(m5: pd.DataFrame, name: str, spread: float, min_gap: float = 0.003) -> list[dict]:
    out, prev_close = [], None
    for day, g in rth_days(m5):
        h, l, c, o = (g[x].to_numpy() for x in ("high", "low", "close", "open"))
        if prev_close is not None:
            gap = o[0] / prev_close - 1
            side = 1 if gap > min_gap and c[5] > o[0] else -1 if gap < -min_gap and c[5] < o[0] else 0
            if side:
                entry = c[5]
                stop = l[:6].min() if side == 1 else h[:6].max()
                risk = abs(entry - stop)
                if risk > 4 * spread:
                    target = entry + side * TP_R * risk
                    px, kx = walk(h, l, c, 6, side, entry, stop, target, len(g) - 1, partial_first=False)
                    out.append(make_trade(name, "Gap & Go", g, 5, kx, side, entry, px, risk, spread, in_close=True))
        prev_close = c[-1]
    return out


def load_d1(symbol: str) -> pd.DataFrame:
    r = pd.DataFrame(mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, 10_000))
    server = pd.to_datetime(r["time"], unit="s")
    # la vela D1 del día X de servidor abre a las 17:00 NY del día anterior y cierra 24 h después
    opens = (server - pd.Timedelta(hours=7)).dt.tz_localize(NY, ambiguous="NaT", nonexistent="NaT").dt.tz_convert("UTC")
    df = r.assign(time=opens).dropna(subset=["time"]).set_index("time")[["open", "high", "low", "close"]]
    return df.iloc[:-1]


def rsi(close: pd.Series, n: int = 2) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def rsi2_daily(d1: pd.DataFrame, name: str, spread: float) -> list[dict]:
    c, h, l = d1.close, d1.high.to_numpy(), d1.low.to_numpy()
    sma200, sma5, r2 = c.rolling(200).mean(), c.rolling(5).mean(), rsi(c)
    tr = pd.concat([d1.high - d1.low, (d1.high - c.shift()).abs(), (d1.low - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    cl, close_t = c.to_numpy(), d1.index + pd.Timedelta(hours=24)
    out, i, n = [], 200, len(d1)
    while i < n - 1:
        if not (cl[i] > sma200.iloc[i] and r2.iloc[i] < 10):
            i += 1
            continue
        entry, risk = cl[i], 2.5 * atr.iloc[i]
        stop, px, j = entry - risk, None, i
        for j in range(i + 1, min(i + 10, n - 1) + 1):
            if l[j] <= stop:
                px = stop
                break
            if cl[j] > sma5.iloc[j]:
                px = cl[j]
                break
        px = cl[j] if px is None else px
        r = (px - entry - spread) / risk
        out.append({"indice": name, "estrategia": "RSI2 diario", "fill_time": close_t[i], "exit_time": close_t[j],
                    "lado": 1, "r": r, "tp_r": 1.0, "resultado": "win" if r > 0 else "loss", "riesgo_pts": risk})
        i = j + 1
    return out


def trade_stats(t: pd.DataFrame) -> dict:
    if t.empty:
        return {"ops": 0}
    win, loss = t[t.r > 0], t[t.r <= 0]
    eq = t.sort_values("exit_time").r.cumsum()
    years = max((t.exit_time.max() - t.fill_time.min()).days / 365.25, 0.5)
    streak = longest = 0
    for r in t.sort_values("exit_time").r:
        streak = streak + 1 if r <= 0 else 0
        longest = max(longest, streak)
    return {"ops": len(t), "ops/año": round(len(t) / years), "acierto %": round(100 * len(win) / len(t), 1),
            "R/op": round(t.r.mean(), 3), "total R": round(t.r.sum(), 1),
            "PF": round(win.r.sum() / -loss.r.sum(), 2) if len(loss) else np.inf,
            "máx DD R": round((eq - eq.cummax()).min(), 1), "racha pérd.": longest}


def load_daily_cycle(path="backtest/index_daily_cycle_trades.csv") -> pd.DataFrame:
    t = pd.read_csv(path, parse_dates=["fill_time", "exit_time", "signal_time"])
    t = t[(t.killzone == "Londres 02-05") & t.resultado.isin(["win", "loss"])].copy()
    for col in ("fill_time", "exit_time"):
        t[col] = pd.to_datetime(t[col], utc=True)
    t["estrategia"] = "Daily Cycle Londres"
    return t


def main() -> None:
    pd.set_option("display.width", 240)
    if not mt5.initialize():
        raise SystemExit(f"No se pudo conectar con MT5: {mt5.last_error()}")
    rows, long_rsi = [], []
    for name, sym in SYMBOLS.items():
        m5, spread = load_m5(sym)
        rows += orb(m5, name, spread, 15) + orb(m5, name, spread, 30) + gap_and_go(m5, name, spread)
        d1 = load_d1(sym)
        rsi_all = rsi2_daily(d1, name, spread)
        long_rsi += rsi_all
        rows += [x for x in rsi_all if x["fill_time"] >= START]
    t = pd.concat([pd.DataFrame(rows), load_daily_cycle()], ignore_index=True)
    t.to_csv("backtest/index_prop_trades.csv", index=False)

    print("=== Operaciones (jul 2020 - oct 2026; SP500 + NAS100) ===")
    stats = {}
    for strat, g in t.groupby("estrategia"):
        stats[(strat, "total")] = trade_stats(g)
        stats[(strat, "2020-23")] = trade_stats(g[g.fill_time < SPLIT])
        stats[(strat, "2024-26")] = trade_stats(g[g.fill_time >= SPLIT])
        for ix, gi in g.groupby("indice"):
            stats[(strat, ix)] = trade_stats(gi)
    print(pd.DataFrame(stats).T.to_string())
    lr = pd.DataFrame(long_rsi)
    print("\nRSI2 diario con todo el histórico D1 (2014-2026):")
    print(pd.DataFrame({ix: trade_stats(g) for ix, g in lr.groupby("indice")}).T.to_string())

    combos = {s: [s] for s in t.estrategia.unique()}
    combos["ORB 15 + RSI2 diario"] = ["ORB 15", "RSI2 diario"]
    combos["ORB 30 + RSI2 diario"] = ["ORB 30", "RSI2 diario"]
    sizings = [Sizing(0.005), Sizing(0.01), Sizing(0.02),
               Sizing(0.01, cushion=8, daily_guard=0.8, to_target=True),
               Sizing(0.02, cushion=6, daily_guard=0.8, to_target=True)]
    rules_set = {"5% diario, DD estático": Rules(daily=0.05), "3% diario, DD estático": Rules(daily=0.03),
                 "5% diario, DD trailing": Rules(daily=0.05, trailing=True)}
    out = []
    for (cname, strats), (rname, rules), sz in product(combos.items(), rules_set.items(), sizings):
        out.append({"estrategia": cname, "reglas": rname, "riesgo": sz.label(),
                    **simulate(t[t.estrategia.isin(strats)], rules, sz)})
    res = pd.DataFrame(out)
    res.to_csv("backtest/index_prop_results.csv", index=False)
    print("\n=== Pruebas de fondeo sin límite de tiempo: +10%, DD 10% ===")
    for rname in rules_set:
        print(f"\n--- {rname} ---")
        print(res[res.reglas == rname].drop(columns="reglas").to_string(index=False))


if __name__ == "__main__":
    main()

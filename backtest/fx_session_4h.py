"""Backtest intradía (H1/H4 de MT5) con filtro de sesión en EURUSD, GBPUSD, AUDJPY, USDCAD y USDJPY.

Datos: velas H1 bid del terminal MT5 (servidor = hora NY + 7, también con horario de verano), así
que el día de servidor empieza a las 17:00 NY. Las H4 se construyen desde las H1 (00, 04, 08... servidor).
Horas de servidor -> NY: 08 = 01:00 (apertura Londres), 16 = 09:00 (apertura NY), 20 = 13:00.

Estrategias (reglas fijadas antes de ver resultados, iguales para todos los pares):
- A "Ruptura Asia": rango asiático = velas H1 de 00 a 07 servidor. De 08 a 19 se compra/vende la
  primera ruptura del máximo/mínimo (orden stop en el borde). SL en la mitad del rango, TP a 1R.
  Cierre a las 23 servidor. Se descartan rangos < 0.3 o > 1.5 ATR diario.
- B "Barrida Asia (Judas)": de 08 a 15 servidor, si una H1 supera el máximo asiático y cierra de
  vuelta dentro del rango -> venta al cierre (simétrico en mínimos). SL en el extremo de la barrida
  + 1 pip, TP en la mitad del rango. Cierre a las 23 servidor.
- C "RSI(2) H4 + tendencia": cierre > EMA200 H4 y RSI(2) < 10 -> compra al cierre de la H4 (ventas
  simétricas con > 90). Salida al cierre H4 que cruza la SMA5, stop 2.5 ATR(14) H4, máximo 30 H4.
  El filtro de sesión decide en qué cierres H4 se puede entrar: Asia (20, 00, 04), Londres (08, 12),
  NY (16) o todas.
Simulación en H1: si el stop y el objetivo caen en la misma vela, cuenta el stop (peor caso).
Coste: el spread de la vela de entrada (columna spread de MT5). Sin swaps ni deslizamiento.
MT5 solo tiene H1 reales desde 2020: in-sample 2020-2022, out-of-sample 2023 en adelante.
"""

from __future__ import annotations

import argparse

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

PAIRS = {"EURUSD": "EURUSD", "GBPUSD": "GBPUSD", "AUDJPY": "AUDJPYp", "USDCAD": "USDCAD", "USDJPY": "USDJPY"}
START = pd.Timestamp("2020-01-01")  # antes, MT5 solo guarda una vela "H1" por día
SPLIT = pd.Timestamp("2023-01-01")
SESSIONS = {"Asia": (20, 0, 4), "Londres": (8, 12), "NY": (16,), "Todas": (0, 4, 8, 12, 16, 20)}


def load_h1(symbol: str) -> pd.DataFrame:
    mt5.symbol_select(symbol, True)
    r = pd.DataFrame(mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 200_000))
    r["time"] = pd.to_datetime(r["time"], unit="s")
    info = mt5.symbol_info(symbol)
    df = r.set_index("time")[["open", "high", "low", "close", "spread"]]
    df.attrs["pip"] = 10 * info.point
    return df[df.index >= START].iloc[:-1]  # la última vela está en curso


def to_h4(h1: pd.DataFrame) -> pd.DataFrame:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "spread": "last"}
    return h1.resample("4h").agg(agg).dropna()


def rsi(close: pd.Series, n: int) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    c = df["close"].shift()
    tr = pd.concat([df.high - df.low, (df.high - c).abs(), (df.low - c).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def run_trade(h, l, c, k0, side, entry, stop, target, k_end):
    """Recorre las H1 desde k0 hasta k_end (incluida). Devuelve (precio de salida, índice, motivo)."""
    for k in range(k0, k_end + 1):
        if (side == 1 and l[k] <= stop) or (side == -1 and h[k] >= stop):
            return stop, k, "stop"
        if target is not None and ((side == 1 and h[k] >= target) or (side == -1 and l[k] <= target)):
            return target, k, "objetivo"
    return c[k_end], k_end, "tiempo"


def asia_days(h1: pd.DataFrame):
    """Por cada día de servidor: (rango asiático hi/lo, índices H1 de 08-19/08-15 y de cierre 23)."""
    hours = h1.index.hour.to_numpy()
    daily_atr = atr(h1.resample("1D").agg({"high": "max", "low": "min", "close": "last"}).dropna(), 14).shift()
    pos = pd.Series(np.arange(len(h1)), index=h1.index)
    for day, idx in pos.groupby(h1.index.normalize()):
        idx = idx.to_numpy()
        hrs = hours[idx]
        asia = idx[hrs < 8]
        if len(asia) < 7 or hrs.max() < 23 or day not in daily_atr.index or np.isnan(daily_atr[day]):
            continue
        yield day, asia, idx, hrs, daily_atr[day]


def strat_breakout(h1: pd.DataFrame, pair: str) -> list[dict]:
    h, l, c, sp = (h1[x].to_numpy() for x in ("high", "low", "close", "spread"))
    pip = h1.attrs["pip"]
    out = []
    for day, asia, idx, hrs, datr in asia_days(h1):
        hi, lo = h[asia].max(), l[asia].min()
        if not 0.3 * datr <= hi - lo <= 1.5 * datr:
            continue
        mid, end = (hi + lo) / 2, idx[hrs == 23][0]
        for k in idx[(hrs >= 8) & (hrs < 20)]:
            up, dn = h[k] >= hi, l[k] <= lo
            if not (up or dn):
                continue
            if up and dn:  # no se sabe cuál fue primero: se descarta el día
                break
            side, entry = (1, hi) if up else (-1, lo)
            risk = abs(entry - mid)
            target = entry + side * risk
            # en la vela de entrada solo cuenta el stop (no se sabe si el objetivo llegó después)
            if (side == 1 and l[k] <= mid) or (side == -1 and h[k] >= mid):
                px, kx, why = mid, k, "stop"
            else:
                px, kx, why = run_trade(h, l, c, k + 1, side, entry, mid, target, end)
            out.append(trade(pair, h1, k, kx, side, entry, px, risk, sp[k] / 10, pip, why))
            break
    return out


def strat_sweep(h1: pd.DataFrame, pair: str) -> list[dict]:
    h, l, c, sp = (h1[x].to_numpy() for x in ("high", "low", "close", "spread"))
    pip = h1.attrs["pip"]
    out = []
    for day, asia, idx, hrs, datr in asia_days(h1):
        hi, lo = h[asia].max(), l[asia].min()
        if not 0.3 * datr <= hi - lo <= 1.5 * datr:
            continue
        mid, end = (hi + lo) / 2, idx[hrs == 23][0]
        ext_hi, ext_lo = hi, lo
        for k in idx[(hrs >= 8) & (hrs < 16)]:
            ext_hi, ext_lo = max(ext_hi, h[k]), min(ext_lo, l[k])
            side = -1 if h[k] > hi and lo < c[k] < hi else 1 if l[k] < lo and lo < c[k] < hi else 0
            if not side:
                continue
            entry = c[k]
            stop = ext_hi + pip if side == -1 else ext_lo - pip
            if side * (mid - entry) <= 0:  # ya está pasada la mitad del rango
                break
            px, kx, why = run_trade(h, l, c, k + 1, side, entry, stop, mid, end)
            out.append(trade(pair, h1, k, kx, side, entry, px, abs(entry - stop), sp[k] / 10, pip, why))
            break
    return out


def strat_rsi_h4(h1: pd.DataFrame, pair: str, session: str) -> list[dict]:
    h4 = to_h4(h1)
    pip = h1.attrs["pip"]
    c4 = h4.close
    ema, sma5, r, a = c4.ewm(span=200, adjust=False).mean(), c4.rolling(5).mean(), rsi(c4, 2), atr(h4)
    # hora de entrada = cierre de la H4 = apertura de la siguiente
    entry_hour = (h4.index + pd.Timedelta(hours=4)).hour
    allowed = np.isin(entry_hour, SESSIONS[session])
    h, l, c, sp = (h4[x].to_numpy() for x in ("high", "low", "close", "spread"))
    out, i, n = [], 200, len(h4)
    while i < n - 1:
        side = 0
        if allowed[i]:
            side = 1 if c[i] > ema.iloc[i] and r.iloc[i] < 10 else -1 if c[i] < ema.iloc[i] and r.iloc[i] > 90 else 0
        if not side:
            i += 1
            continue
        entry, risk = c[i], 2.5 * a.iloc[i]
        stop = entry - side * risk
        px, why, j = None, "tiempo", i
        for j in range(i + 1, min(i + 30, n - 1) + 1):
            if (side == 1 and l[j] <= stop) or (side == -1 and h[j] >= stop):
                px, why = stop, "stop"
                break
            if side * (c[j] - sma5.iloc[j]) > 0:
                px, why = c[j], "objetivo"
                break
        if px is None:
            px = c[j]
        out.append(trade(pair, h4, i, j, side, entry, px, risk, sp[i] / 10, pip, why))
        i = j + 1
    return out


def trade(pair, df, k_in, k_out, side, entry, px, risk, spread_pips, pip, why) -> dict:
    pips = side * (px - entry) / pip - spread_pips
    return {"par": pair, "entrada": df.index[k_in], "salida": df.index[k_out], "lado": side,
            "pips": pips, "R": pips * pip / risk, "motivo": why}


def stats(t: pd.DataFrame) -> dict:
    if t.empty:
        return {"ops": 0}
    win, loss = t[t.pips > 0], t[t.pips <= 0]
    eq = t.sort_values("salida")["R"].cumsum()
    years = max((t.salida.max() - t.entrada.min()).days / 365.25, 1)
    return {
        "ops": len(t),
        "acierto %": round(100 * len(win) / len(t), 1),
        "gan. media R": round(win.R.mean(), 2) if len(win) else 0,
        "pérd. media R": round(loss.R.mean(), 2) if len(loss) else 0,
        "esperanza R": round(t.R.mean(), 3),
        "profit factor": round(win.R.sum() / -loss.R.sum(), 2) if len(loss) else np.inf,
        "total R": round(t.R.sum(), 1),
        "máx DD R": round((eq - eq.cummax()).min(), 1),
        "ops/año": round(len(t) / years, 1),
    }


def table(t: pd.DataFrame, by_pair: bool = True) -> pd.DataFrame:
    rows = {}
    if by_pair:
        for (pair, per), g in t.groupby(["par", "periodo"]):
            rows[(pair, per)] = stats(g)
    for per, g in t.groupby("periodo"):
        rows[("CARTERA", per)] = stats(g)
    return pd.DataFrame(rows).T


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="backtest/fx_session_4h_trades.csv")
    args = ap.parse_args()
    pd.set_option("display.width", 220)
    if not mt5.initialize():
        raise SystemExit(f"No se pudo conectar con MT5: {mt5.last_error()}")
    data = {p: load_h1(s) for p, s in PAIRS.items()}
    for p, d in data.items():
        print(f"{p}: {len(d)} velas H1 {d.index.min().date()} -> {d.index.max().date()}, "
              f"spread mediano {d.spread.median() / 10:.1f} pips")

    runs = {
        "A Ruptura Asia (Londres+NY)": lambda d, p: strat_breakout(d, p),
        "B Barrida Asia en Londres": lambda d, p: strat_sweep(d, p),
        **{f"C RSI2 H4 - {s}": (lambda s: lambda d, p: strat_rsi_h4(d, p, s))(s) for s in SESSIONS},
    }
    all_t = []
    summary = {}
    for name, fn in runs.items():
        t = pd.DataFrame([x for p, d in data.items() for x in fn(d, p)])
        t["periodo"] = np.where(t.entrada < SPLIT, "IS 2020-22", "OOS 2023+")
        t["estrategia"] = name
        all_t.append(t)
        for per, g in t.groupby("periodo"):
            summary[(name, per)] = stats(g)
        if not name.startswith("C") or name.endswith("Todas"):
            print(f"\n=== {name} ===")
            print(table(t).to_string())
    print("\n=== Resumen (cartera de 5 pares) ===")
    print(pd.DataFrame(summary).T.to_string())
    pd.concat(all_t).to_csv(args.csv, index=False)


if __name__ == "__main__":
    main()

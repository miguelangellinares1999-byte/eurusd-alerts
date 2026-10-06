"""Barridas semanales en S&P 500 y Nasdaq 100: salidas, riesgo por operación y rotación entre índices.

Las señales salen de `backtest.confluence` (barrida de PWL, rango o PML en el índice, con los dos
índices alineados y la amplitud en sobreventa). Aquí se estudia qué hacer con ellas:

- Salidas: TP en la estructura barrida, TP en múltiplos de R, mantener N semanas o trailing semanal
  (salir cuando una semana cierra por debajo del mínimo de la anterior). El SL en el mínimo de la
  barrida está siempre activo; si un día abre más allá, se sale a la apertura.
- Riesgo: curva de capital arriesgando una fracción fija del capital por operación (con interés
  compuesto) y Monte Carlo remuestreando las operaciones para ver rachas y caídas posibles.
- Rotación: cuando los dos índices dan señal, cuál comprar.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from backtest.weekly import WeeklyTrade


@dataclass(frozen=True)
class Exit:
    name: str
    tp: str | float | None = "structure"  # "structure", múltiplo de R o None (sin TP)
    max_weeks: float | None = None  # salida por tiempo, al cierre
    trail: bool = False  # trailing semanal: cierre semanal bajo el mínimo de la semana anterior


EXITS = [
    Exit("TP en la estructura barrida"),
    Exit("TP 1R", 1.0),
    Exit("TP 2R", 2.0),
    Exit("TP 3R", 3.0),
    Exit("TP 5R", 5.0),
    Exit("Mantener 2 semanas", None, 2),
    Exit("Mantener 4 semanas", None, 4),
    Exit("Mantener 8 semanas", None, 8),
    Exit("Mantener 13 semanas", None, 13),
    Exit("Mantener 26 semanas", None, 26),
    Exit("Trailing semanal", None, None, True),
    Exit("Estructura + trailing", "structure", None, True),
]


def manage(ticker: str, daily: pd.DataFrame, signals: pd.DataFrame, rule: Exit, cost: float = 0.0) -> list[WeeklyTrade]:
    """Simula las señales (last_day, entry, stop_loss, take_profit) con la regla de salida dada.

    Una posición a la vez por índice. Resultado "win" si sale por encima de la entrada.
    """
    dates = daily.index
    o, h, l, c = (daily[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    week_id = dates.to_period("W-FRI")
    last_of_week = np.r_[week_id[1:] != week_id[:-1], True]
    trades: list[WeeklyTrade] = []
    busy_until = pd.Timestamp.min
    for sig in signals.itertuples():
        day = sig.last_day
        if day <= busy_until:
            continue
        entry, sl = sig.entry, sig.stop_loss
        risk = entry - sl
        if risk <= 0:
            continue
        if rule.tp == "structure":
            tp = sig.take_profit
        elif rule.tp is None:
            tp = np.inf
        else:
            tp = entry + float(rule.tp) * risk
        deadline = day + pd.Timedelta(weeks=rule.max_weeks) if rule.max_weeks else None
        start = int(dates.searchsorted(day, side="right"))
        prev_week_low = sig.stop_loss  # mínimo de la semana de la señal
        week_low = np.inf
        exit_i, exit_px, done = len(dates) - 1, c[-1], False
        for i in range(start, len(dates)):
            if o[i] <= sl:
                exit_i, exit_px, done = i, o[i], True
            elif o[i] >= tp:
                exit_i, exit_px, done = i, o[i], True
            elif l[i] <= sl:
                exit_i, exit_px, done = i, sl, True
            elif h[i] >= tp:
                exit_i, exit_px, done = i, tp, True
            elif deadline is not None and dates[i] >= deadline:
                exit_i, exit_px, done = i, c[i], True
            if done:
                break
            week_low = min(week_low, l[i])
            if last_of_week[i]:
                if rule.trail and c[i] < prev_week_low:
                    exit_i, exit_px, done = i, c[i], True
                    break
                prev_week_low, week_low = week_low, np.inf
        result = ("win" if exit_px > entry else "loss") if done else "open"
        exit_date = dates[exit_i] if done else None
        trades.append(WeeklyTrade(ticker, day, entry, sl, float(tp) if np.isfinite(tp) else float("nan"),
                                  result, exit_date, float(exit_px), cost))
        busy_until = dates[exit_i] if done else pd.Timestamp.max
    return trades


def mfe_r(daily: pd.DataFrame, signals: pd.DataFrame, weeks: int = 26) -> pd.Series:
    """Máximo recorrido a favor (en R) antes de tocar el SL, dentro de `weeks` semanas."""
    out = {}
    for sig in signals.itertuples():
        after = daily[(daily.index > sig.last_day) & (daily.index <= sig.last_day + pd.Timedelta(weeks=weeks))]
        risk = sig.entry - sig.stop_loss
        if after.empty or risk <= 0:
            continue
        hit = np.flatnonzero(after["low"].to_numpy() <= sig.stop_loss)
        upto = after.iloc[: hit[0] + 1] if len(hit) else after
        out[sig.last_day] = (upto["high"].max() - sig.entry) / risk
    return pd.Series(out, dtype=float)


# ------------------------------------------------------------------- capital
def equity_curve(trades: list[WeeklyTrade], risk: float, start: float = 1.0) -> pd.Series:
    """Capital tras cada cierre arriesgando `risk` del capital (en el momento de entrar) por operación."""
    events = []
    for k, t in enumerate(trades):
        if t.exit_date is None:
            continue
        events.append((t.signal_week, 0, k))
        events.append((t.exit_date, 1, k))
    events.sort()
    equity, stake, points = start, {}, {}
    for when, kind, k in events:
        if kind == 0:
            stake[k] = equity * risk
        else:
            equity += stake.pop(k) * trades[k].r
            points[when] = equity
    return pd.Series(points, dtype=float).sort_index()


def curve_stats(curve: pd.Series, start: pd.Timestamp, end: pd.Timestamp, base: float = 1.0) -> dict:
    years = max((end - start).days / 365.25, 1e-9)
    full = pd.concat([pd.Series([base], index=[start]), curve])
    final = float(full.iloc[-1])
    dd = float((1 - full / full.cummax()).max())
    return {"cagr_pct": 100 * (max(final, 1e-9) ** (1 / years) - 1), "max_dd_pct": 100 * dd, "final": final}


def kelly(rs: np.ndarray) -> float:
    """Fracción que maximiza el crecimiento esperado log(1 + f·R) con la distribución observada."""
    grid = np.linspace(0.001, 0.5, 500)
    growth = [np.mean(np.log(np.maximum(1 + f * rs, 1e-9))) for f in grid]
    return float(grid[int(np.argmax(growth))])


def monte_carlo(rs: np.ndarray, trades_per_year: float, years: float, risks: list[float], sims: int = 5000, seed: int = 7) -> pd.DataFrame:
    """Remuestrea las R observadas (con reposición) y simula `years` años para cada riesgo."""
    rng = np.random.default_rng(seed)
    n = max(1, int(round(trades_per_year * years)))
    draws = rng.choice(rs, size=(sims, n), replace=True)
    rows = []
    for f in risks:
        eq = np.cumprod(1 + f * draws, axis=1)
        eq = np.maximum(eq, 0)
        peak = np.maximum.accumulate(np.concatenate([np.ones((sims, 1)), eq], axis=1), axis=1)[:, 1:]
        dd = (1 - eq / peak).max(axis=1)
        cagr = np.maximum(eq[:, -1], 1e-9) ** (1 / years) - 1
        rows.append({
            "riesgo_pct": 100 * f,
            "cagr_mediana_pct": 100 * float(np.median(cagr)),
            "cagr_p10_pct": 100 * float(np.percentile(cagr, 10)),
            "dd_mediana_pct": 100 * float(np.median(dd)),
            "dd_p95_pct": 100 * float(np.percentile(dd, 95)),
            "prob_dd_30_pct": 100 * float((dd > 0.30).mean()),
            "prob_perder_pct": 100 * float((eq[:, -1] < 1).mean()),
        })
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ rotación
def rotate(
    trades_by_index: dict[str, list[WeeklyTrade]],
    score: pd.DataFrame | None,
    prefer: str = "high",
    window_days: int = 7,
) -> list[WeeklyTrade]:
    """Si los dos índices dan señal a la vez (±`window_days`), se queda con uno según `score`.

    `score` tiene una columna por índice indexada por semana (viernes): se elige el de mayor
    valor ("high") o menor ("low"). Las señales en solitario se operan siempre.
    """
    names = list(trades_by_index)
    a, b = (sorted(trades_by_index[n], key=lambda t: t.signal_week) for n in names)
    used_b, out = set(), []
    for ta in a:
        pair = next((tb for tb in b if id(tb) not in used_b and abs((tb.signal_week - ta.signal_week).days) <= window_days), None)
        if pair is None:
            out.append(ta)
            continue
        used_b.add(id(pair))
        week = ta.signal_week.to_period("W-FRI").end_time.normalize()
        sa = score[names[0]].asof(week) if score is not None else np.nan
        sb = score[names[1]].asof(week) if score is not None else np.nan
        pick_a = (sa >= sb) if prefer == "high" else (sa <= sb)
        out.append(ta if (pick_a or np.isnan(sb)) and not np.isnan(sa) else pair)
    out.extend(tb for tb in b if id(tb) not in used_b)
    return sorted(out, key=lambda t: t.signal_week)

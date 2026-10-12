"""Simulador de pruebas de fondeo (prop firm) a partir de una lista de operaciones en R.

Reglas de la prueba (Rules): objetivo de ganancia, drawdown máximo (estático desde el saldo
inicial o trailing desde el máximo de saldo, sin pasar del inicial) y pérdida diaria máxima
medida desde el saldo al inicio del día de trading (17:00 NY). Se evalúa con saldo realizado:
como cada operación sale en su SL o TP, la pérdida flotante nunca supera el riesgo abierto.

Gestión de riesgo (Sizing): % del saldo inicial arriesgado por operación, con protecciones
opcionales:
- `cushion`: nunca arriesgar más de (distancia al límite de DD) / cushion, para aguantar
  `cushion` pérdidas seguidas antes de quemar la cuenta (con un mínimo de `min_risk`, para que
  la cuenta no se quede viva sin poder avanzar).
- `daily_guard`: no abrir una operación si la pérdida del día + el riesgo abierto + el nuevo
  riesgo superan ese % del límite diario (no se puede romper la regla diaria).
- `to_target`: cerca del objetivo, arriesgar solo lo necesario para llegar con un TP (con un 20%
  de margen y un mínimo de `min_risk`, para no quedarse a las puertas por los costes).

Cada día de trading del histórico se usa como inicio de una prueba (los resultados de inicios
cercanos están correlacionados). Sin límite de tiempo (como las empresas de fondeo actuales): una
prueba acaba al alcanzar el objetivo (aprobada) o al romper una regla (suspendida); si se acaban
los datos antes queda "sin resolver". Solo se usan inicios con al menos `min_horizon` días de
trading por delante. `max_days` opcional para simular pruebas con plazo.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass

import numpy as np
import pandas as pd

NY = "America/New_York"


@dataclass(frozen=True)
class Rules:
    target: float = 0.10
    max_dd: float = 0.10
    daily: float = 0.05
    trailing: bool = False
    max_days: int | None = None  # días de trading; None = sin límite
    min_horizon: int = 250  # días de datos que debe tener por delante cada inicio


@dataclass(frozen=True)
class Sizing:
    risk: float = 0.01
    cushion: float | None = None
    daily_guard: float | None = None
    to_target: bool = False
    min_risk: float = 0.0025
    by_strategy: tuple[tuple[str, float], ...] = ()  # riesgo propio por estrategia (columna `estrategia`)

    def risk_for(self, strategy: str | None) -> float:
        return dict(self.by_strategy).get(strategy, self.risk)

    def label(self) -> str:
        parts = ([", ".join(f"{k} {100 * v:g}%" for k, v in self.by_strategy)] if self.by_strategy
                 else [f"{100 * self.risk:g}%"])
        if self.cushion:
            parts.append(f"colchón/{self.cushion:g}")
        if self.daily_guard:
            parts.append("guarda diaria")
        if self.to_target:
            parts.append("justo al objetivo")
        return " + ".join(parts)


def trading_day(ts: pd.Series) -> pd.Series:
    """Día de trading que acaba a las 17:00 NY."""
    return (ts.dt.tz_convert(NY) + pd.Timedelta(hours=7)).dt.normalize().dt.tz_localize(None)


def prepare(trades: pd.DataFrame) -> pd.DataFrame:
    """`trades`: columnas fill_time, exit_time (UTC), r (resultado en R), tp_r (R del objetivo)."""
    t = trades.dropna(subset=["fill_time", "exit_time"]).sort_values("fill_time").reset_index(drop=True)
    t["fill_day"] = trading_day(t.fill_time)
    t["exit_day"] = trading_day(t.exit_time)
    return t


def events(t: pd.DataFrame, days: pd.DatetimeIndex) -> list[tuple]:
    """(día como índice en `days`, tipo 0=salida/1=entrada, nº de operación), en orden temporal."""
    fill_d, exit_d = days.searchsorted(t.fill_day), days.searchsorted(t.exit_day)
    ev = [(row.fill_time, 1, i, int(fill_d[i])) for i, row in enumerate(t.itertuples())]
    ev += [(row.exit_time, 0, i, int(exit_d[i])) for i, row in enumerate(t.itertuples())]
    ev.sort(key=lambda e: (e[0], e[1]))  # en el mismo instante, primero las salidas
    return [(d, kind, i) for _, kind, i, d in ev]


def run_challenge(ev: list[tuple], ev_days: list[int], r_col, tp_col, base_col, first: int,
                  rules: Rules, sizing: Sizing) -> tuple[str, int]:
    """Prueba que empieza el día `first` (índice en el calendario). Devuelve (resultado, días usados)."""
    bal, peak = 1.0, 1.0
    floor = 1.0 - rules.max_dd
    goal = 1.0 + rules.target
    day, day_start = -1, 1.0
    open_risk: dict[int, float] = {}
    for k in range(bisect_left(ev_days, first), len(ev)):
        d, kind, i = ev[k]
        if kind == 0 and i not in open_risk:
            continue  # operación abierta antes de empezar la prueba
        used = d - first + 1
        if d != day:
            day, day_start = d, bal
            if rules.max_days and used > rules.max_days:
                return "tiempo", rules.max_days
        if kind == 1:
            risk = base_col[i]
            if sizing.cushion:
                risk = min(risk, max((bal - floor) / sizing.cushion, min(sizing.min_risk, risk)))
            if sizing.to_target:
                # con margen: el TP real paga algo menos que tp_r (spread, deslizamiento)
                risk = min(risk, max((goal - bal) / (0.8 * tp_col[i]), min(sizing.min_risk, risk)))
            if sizing.daily_guard:
                room = sizing.daily_guard * rules.daily - (day_start - bal) - sum(open_risk.values())
                risk = min(risk, room)
            if risk > 1e-6:
                open_risk[i] = risk
            continue
        bal += open_risk.pop(i) * r_col[i]
        if rules.trailing:
            peak = max(peak, bal)
            floor = min(peak - rules.max_dd, 1.0)
        if bal <= floor + 1e-12:
            return "DD máximo", used
        if day_start - bal >= rules.daily - 1e-12:
            return "límite diario", used
        if bal >= goal - 1e-12:
            return "aprobada", used
    return ("tiempo", rules.max_days) if rules.max_days else ("sin resolver", 0)


def simulate(trades: pd.DataFrame, rules: Rules, sizing: Sizing, start: str | None = None) -> dict:
    t = prepare(trades)
    all_days = pd.bdate_range(min(t.fill_day.min(), t.exit_day.min()), max(t.fill_day.max(), t.exit_day.max()))
    ev = events(t, all_days)
    ev_days = [e[0] for e in ev]
    r_col, tp_col = t.r.tolist(), t.tp_r.tolist()
    strategies = t["estrategia"] if "estrategia" in t else pd.Series([None] * len(t))
    base_col = [sizing.risk_for(x) for x in strategies]
    starts = all_days[: max(len(all_days) - (rules.max_days or rules.min_horizon), 0)]
    if start is not None:
        starts = starts[starts >= pd.Timestamp(start)]
    res = [run_challenge(ev, ev_days, r_col, tp_col, base_col, int(all_days.searchsorted(s)), rules, sizing) for s in starts]
    df = pd.DataFrame(res, columns=["resultado", "dias"])
    done = df
    n = max(len(done), 1)
    durations = done.dias[done.resultado != "sin resolver"]
    passed = done[done.resultado == "aprobada"]
    return {
        "pruebas": len(done),
        "aprobadas %": round(100 * len(passed) / n, 1),
        "DD máx %": round(100 * (done.resultado == "DD máximo").sum() / n, 1),
        "diario %": round(100 * (done.resultado == "límite diario").sum() / n, 1),
        "sin resolver %": round(100 * done.resultado.isin(["tiempo", "sin resolver"]).sum() / n, 1),
        "días mediana (aprob.)": int(passed.dias.median()) if len(passed) else None,
        "días p75 (aprob.)": int(passed.dias.quantile(0.75)) if len(passed) else None,
        "días mediana (fin)": int(durations.median()) if len(durations) else None,
        # tiempo medio hasta tener la cuenta, encadenando intentos hasta aprobar
        "días esperados (reintentos)": round(durations.mean() / (len(passed) / n)) if len(passed) else None,
        "intentos esperados": round(n / len(passed), 2) if len(passed) else None,
    }

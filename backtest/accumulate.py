"""Acumular un índice con aportaciones mensuales: DCA, comprar solo en señales y CFD apalancados.

- Las aportaciones llegan el primer día hábil de cada mes.
- `tr` es el índice con dividendos reinvertidos (total return): lo que vale una participación.
- DCA: se compra todo el mes de la aportación y se mantiene.
- Señales: el dinero espera en liquidez remunerada (`rate`, tipo anual en %) y se invierte entero
  al cierre de cada día de señal; lo comprado se mantiene para siempre.
- CFD: cada compra abre `leverage` veces su importe en nocional y se mantiene (no se reequilibra).
  Se paga financiación diaria sobre el nocional (`rate` + `markup`) por día natural; los
  dividendos llegan como ajuste (por eso se usa `tr`). Si el capital baja del `stop_out` del
  margen exigido (`margin` del nocional) el broker cierra todo: lo que quede vuelve a liquidez.
  Sin reequilibrar, el apalancamiento real sube solo cuando el índice cae o la financiación
  se come el capital; con `rebalance` se devuelve a `leverage` en cada compra.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Plan:
    name: str
    buy_on: str = "monthly"  # "monthly" (DCA) o "signals"
    leverage: float = 0.0  # 0 = contado (ETF); >0 = CFD con ese apalancamiento
    markup: float = 2.5  # % anual sobre el tipo de referencia que cobra el broker en los largos
    margin: float = 0.05  # margen exigido sobre el nocional (1:20)
    stop_out: float = 0.5  # cierre forzoso si capital < stop_out * margen
    rebalance: bool = False  # CFD: cada aportación devuelve el nocional a `leverage` veces el capital


@dataclass
class PlanResult:
    plan: Plan
    value: pd.Series  # valor de la cuenta (posiciones + liquidez) cada día
    contributed: pd.Series  # aportado acumulado cada día
    flows: list[tuple[pd.Timestamp, float]]
    stop_outs: list[pd.Timestamp]

    def stats(self) -> dict:
        final, paid = float(self.value.iloc[-1]), float(self.contributed.iloc[-1])
        ratio = self.value / self.contributed.replace(0, np.nan)
        return {
            "plan": self.plan.name,
            "aportado": paid,
            "valor_final": final,
            "multiplo": final / paid if paid else np.nan,
            "tir_pct": 100 * irr(self.flows, self.value.index[-1], final),
            "max_dd_pct": 100 * float((1 - self.value / self.value.cummax()).max()),
            "peor_valor_vs_aportado_pct": 100 * (float(ratio.min()) - 1),
            "stop_outs": len(self.stop_outs),
        }


def contribution_days(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Primer día con datos de cada mes."""
    s = pd.Series(index, index=index)
    return pd.DatetimeIndex(s.groupby(index.to_period("M")).first().values)


def irr(flows: list[tuple[pd.Timestamp, float]], end: pd.Timestamp, final: float) -> float:
    """TIR anual de aportaciones (negativas) y el valor final, por bisección."""
    if not flows:
        return float("nan")
    t = np.array([(end - d).days / 365.25 for d, _ in flows])
    amounts = np.array([a for _, a in flows])

    def fv(r: float) -> float:
        return float((-amounts * (1 + r) ** t).sum()) - final

    lo, hi = -0.99, 1.0
    if fv(lo) * fv(hi) > 0:
        return float("nan")
    for _ in range(200):
        mid = (lo + hi) / 2
        if fv(lo) * fv(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def simulate(
    tr: pd.Series,
    plan: Plan,
    amount: float = 50.0,
    rate: pd.Series | None = None,
    signals: pd.DatetimeIndex | None = None,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    trades: list[tuple[pd.Timestamp, pd.Timestamp, float]] | None = None,
    trade_risk: float = 0.02,
) -> PlanResult:
    """`trades` = operaciones tácticas (entrada, salida, R) con CFD encima de la cartera: cada una
    arriesga `trade_risk` del valor total al entrar y su resultado pasa a liquidez al cerrarse
    (se invierte con la siguiente compra; si es negativo, se descuenta de las próximas).
    """
    tr = tr.dropna()
    if start is not None:
        tr = tr[tr.index >= start]
    if end is not None:
        tr = tr[tr.index <= end]
    days = tr.index
    px = tr.to_numpy(float)
    r = (rate.reindex(days, method="ffill").fillna(0).to_numpy(float) / 100) if rate is not None else np.zeros(len(days))
    pay = set(contribution_days(days))
    sig = set(pd.DatetimeIndex(signals)) if signals is not None else set()
    opens: dict[pd.Timestamp, list[int]] = {}
    closes: dict[pd.Timestamp, list[int]] = {}
    for k, (t_in, t_out, _) in enumerate(trades or []):
        if t_in < days[0] or t_out > days[-1]:
            continue
        opens.setdefault(days[min(days.searchsorted(t_in), len(days) - 1)], []).append(k)
        closes.setdefault(days[min(days.searchsorted(t_out), len(days) - 1)], []).append(k)
    stakes: dict[int, float] = {}
    gap = np.r_[0, np.diff(days.values).astype("timedelta64[D]").astype(float)]

    cash = units = notional = equity_cfd = paid = 0.0
    values, contrib, flows, stops = np.empty(len(days)), np.empty(len(days)), [], []
    lev = plan.leverage
    for i, day in enumerate(days):
        # Evolución desde el día anterior.
        cash *= 1 + r[i] * gap[i] / 365
        if i and notional:
            ret = px[i] / px[i - 1] - 1
            equity_cfd += notional * ret - notional * (r[i] + plan.markup / 100) * gap[i] / 365
            notional *= 1 + ret
            if equity_cfd < plan.stop_out * plan.margin * notional:
                stops.append(day)
                cash += max(equity_cfd, 0.0)
                equity_cfd = notional = 0.0
        for k in closes.get(day, []):
            if k in stakes:
                cash += stakes.pop(k) * trades[k][2]
        if day in pay:
            cash += amount
            paid += amount
            flows.append((day, -amount))
        buy = (day in pay) if plan.buy_on == "monthly" else (day in sig)
        if buy and cash > 0:
            if lev <= 0:
                units += cash / px[i]
            else:
                equity_cfd += cash
                notional += lev * cash
            cash = 0.0
        if buy and lev > 0 and plan.rebalance and equity_cfd > 0:
            notional = lev * equity_cfd
        values[i] = cash + units * px[i] + equity_cfd
        for k in opens.get(day, []):
            if values[i] > 0:
                stakes[k] = trade_risk * values[i]
        contrib[i] = paid
    return PlanResult(plan, pd.Series(values, index=days), pd.Series(contrib, index=days), flows, stops)

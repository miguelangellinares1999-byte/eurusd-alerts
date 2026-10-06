"""Barridas semanales con confluencias de mercado, y SMT alcista entre S&P 500 y Nasdaq 100.

Señales en acciones (solo largos, entrada al cierre semanal, SL en el mínimo de la semana):
- PWL: la semana perfora el mínimo de la anterior y cierra por encima. TP: máximo de la anterior.
- Rango: perfora el mínimo de las `range_weeks` semanas previas (barre varios mínimos semanales
  de una acumulación) y cierra por encima. TP: máximo de esas semanas.
- PML: perfora el mínimo del mes anterior y cierra la semana por encima. TP: máximo del mes anterior.

Filtros de mercado, todos conocidos al cierre de la semana de la señal:
- Índices: S&P 500 y Nasdaq 100 han completado una barrida de PWL, PML o PYL con cierre por
  encima en esa semana o la anterior.
- Amplitud: el % de valores del S&P 500 sobre su media de 50 sesiones ha estado por debajo
  del umbral en esa semana o la anterior.
- VIX: cierre semanal del VIX por debajo del umbral.
- MOVE: cierre semanal del MOVE por debajo de su media de 20 semanas (volatilidad de bonos a la baja).

SMT alcista: en la misma semana (o mes) un índice perfora el mínimo del periodo anterior y el
otro no. Se compra al cierre del periodo; SL en el mínimo del periodo del índice comprado y
TP en el máximo del periodo anterior.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from backtest.weekly import WeeklyTrade, r_stats, simulate, weekly_bars

SIGNAL_TYPES = ("pwl", "rng", "pml")


def _prev_period(daily: pd.DataFrame, freq: str, days: pd.Index) -> pd.DataFrame:
    """Mínimo y máximo del periodo (mes "M" / año "Y") anterior al de cada día de `days`."""
    per = daily.index.to_period(freq)
    agg = daily.groupby(per).agg(low=("low", "min"), high=("high", "max"))
    prev = agg.shift(1)
    return prev.reindex(pd.DatetimeIndex(days).to_period(freq)).set_axis(days)


def stock_signals(daily: pd.DataFrame, range_weeks: int = 4) -> pd.DataFrame:
    """Una fila por semana cerrada con las barridas que da y el TP de cada una."""
    w = weekly_bars(daily)
    prev_low, prev_high = w["low"].shift(1), w["high"].shift(1)
    rng_low = w["low"].shift(1).rolling(range_weeks).min()
    rng_high = w["high"].shift(1).rolling(range_weeks).max()
    month = _prev_period(daily, "M", w["last_day"])
    out = pd.DataFrame(index=w.index)
    out["last_day"] = w["last_day"]
    out["entry"] = w["close"]
    out["stop_loss"] = w["low"]
    out["pwl"] = (w["low"] < prev_low) & (w["close"] > prev_low)
    out["rng"] = (w["low"] < rng_low) & (w["close"] > rng_low)
    out["pml"] = (w["low"] < month["low"].values) & (w["close"] > month["low"].values)
    out["tp_pwl"], out["tp_rng"], out["tp_pml"] = prev_high, rng_high, month["high"].values
    return out


def index_manipulation(daily: pd.DataFrame) -> pd.Series:
    """Semanas en que el índice cerró por encima de un PWL, PML o PYL que acababa de perforar (o la anterior)."""
    w = weekly_bars(daily)
    prev_low = w["low"].shift(1)
    month = _prev_period(daily, "M", w["last_day"])["low"].values
    year = _prev_period(daily, "Y", w["last_day"])["low"].values
    swept = ((w["low"] < prev_low) & (w["close"] > prev_low)) | (
        (w["low"] < month) & (w["close"] > month)) | ((w["low"] < year) & (w["close"] > year))
    return (swept | swept.shift(1, fill_value=False)).rename("idx")


def market_frame(
    spx: pd.DataFrame,
    ndx: pd.DataFrame,
    vix: pd.DataFrame | None = None,
    move: pd.DataFrame | None = None,
    breadth: pd.Series | None = None,
) -> pd.DataFrame:
    """Una fila por semana (viernes) con los datos de cada filtro."""
    m = pd.DataFrame({"idx_spx": index_manipulation(spx), "idx_ndx": index_manipulation(ndx)})
    if vix is not None:
        m["vix"] = vix["close"].resample("W-FRI").last()
    if move is not None:
        mc = move["close"].resample("W-FRI").last()
        m["move_down"] = mc < mc.rolling(20).mean()
    if breadth is not None:
        b = breadth.resample("W-FRI").min()
        m["breadth_min"] = pd.concat([b, b.shift(1)], axis=1).min(axis=1)
    return m


@dataclass(frozen=True)
class Variant:
    name: str
    types: tuple[str, ...] = ("pwl",)
    index_aligned: bool = False
    breadth_below: float | None = None
    vix_below: float | None = None
    move_down: bool = False
    years: tuple[int, int] | None = None  # filtro de años de la señal (inclusive)
    # Riesgo mínimo (entrada - SL) en tanto por uno del precio: con un SL pegado al cierre
    # cualquier hueco da R absurdos (±50R) que distorsionan las medias.
    min_risk: float = 0.01


def select(sig: pd.DataFrame, market: pd.DataFrame, v: Variant) -> pd.DataFrame:
    """Señales de un activo que cumplen la variante, con el TP de la barrida más grande que dan."""
    s = sig.join(market, how="left")
    mask = s[list(v.types)].any(axis=1)
    if v.index_aligned:
        mask &= s["idx_spx"].fillna(False).astype(bool) & s["idx_ndx"].fillna(False).astype(bool)
    if v.breadth_below is not None:
        mask &= s["breadth_min"] < v.breadth_below
    if v.vix_below is not None:
        mask &= s["vix"] < v.vix_below
    if v.move_down:
        mask &= s["move_down"].fillna(False).astype(bool)
    if v.years is not None:
        mask &= (s.index.year >= v.years[0]) & (s.index.year <= v.years[1])
    s = s[mask].copy()
    # TP de la estructura mayor barrida: PML > rango > PWL.
    tp = s["tp_pwl"].where(s["pwl"])
    if "rng" in v.types:
        tp = s["tp_rng"].where(s["rng"], tp)
    if "pml" in v.types:
        tp = s["tp_pml"].where(s["pml"], tp)
    s["take_profit"] = tp
    ok = (s["take_profit"] > s["entry"]) & (s["entry"] - s["stop_loss"] >= v.min_risk * s["entry"])
    return s[ok]


@dataclass
class VariantResult:
    variant: Variant
    trades: list[WeeklyTrade] = field(default_factory=list)

    def stats(self) -> dict:
        s = r_stats(self.trades)
        years = {t.signal_week.year for t in self.trades}
        span = (max(years) - min(years) + 1) if years else 1
        return {"variante": self.variant.name} | s | {"ops_por_año": s["trades"] / span}


def run_variants(
    daily_by_ticker: dict[str, pd.DataFrame],
    market: pd.DataFrame,
    variants: list[Variant],
    cost: float = 0.001,
    range_weeks: int = 4,
) -> list[VariantResult]:
    results = [VariantResult(v) for v in variants]
    for ticker, daily in daily_by_ticker.items():
        if len(daily) < 120:
            continue
        sig = stock_signals(daily, range_weeks)
        for res in results:
            res.trades.extend(simulate(ticker, daily, select(sig, market, res.variant), cost))
    return results


# ------------------------------------------------------------------------- SMT
def _bars(daily: pd.DataFrame, freq: str) -> pd.DataFrame:
    if freq == "W":
        return weekly_bars(daily)
    d = daily.assign(last_day=daily.index)
    bars = d.resample("ME").agg({"open": "first", "high": "max", "low": "min", "close": "last", "last_day": "last"}).dropna()
    if len(bars) and bars.index[-1] > daily.index[-1]:
        bars = bars.iloc[:-1]
    return bars


def smt_signals(a: pd.DataFrame, b: pd.DataFrame, freq: str = "W") -> pd.DataFrame:
    """Periodos con SMT alcista entre `a` y `b`: uno perfora el mínimo anterior y el otro no.

    Columnas por índice (sufijos _a/_b): entrada, SL, TP y si barrió (swept_*).
    """
    ba, bb = _bars(a, freq), _bars(b, freq)
    j = ba.join(bb, lsuffix="_a", rsuffix="_b", how="inner")
    for s in ("a", "b"):
        j[f"swept_{s}"] = j[f"low_{s}"] < j[f"low_{s}"].shift(1)
        j[f"tp_{s}"] = j[f"high_{s}"].shift(1)
    return j[j["swept_a"] ^ j["swept_b"]]


def smt_trades(
    daily: dict[str, pd.DataFrame],
    a: str,
    b: str,
    freq: str = "W",
    buy: str = "lagger",
    market: pd.DataFrame | None = None,
    vix_below: float | None = None,
    breadth_below: float | None = None,
    start_year: int | None = None,
    cost: float = 0.0005,
    min_risk: float = 0.005,
) -> list[WeeklyTrade]:
    """Opera el SMT comprando el índice que barre (`sweeper`), el que no barre (`lagger`) o ambos (`both`)."""
    sig = smt_signals(daily[a], daily[b], freq)
    if start_year is not None:
        sig = sig[sig.index.year >= start_year]
    if market is not None and (vix_below is not None or breadth_below is not None):
        weeks = sig["last_day_a"].dt.to_period("W-FRI").dt.end_time.dt.normalize()
        m = market.reindex(weeks.values).set_axis(sig.index)
        if vix_below is not None:
            sig = sig[m["vix"] < vix_below]
            m = m.loc[sig.index]
        if breadth_below is not None:
            sig = sig[m["breadth_min"] < breadth_below]
    trades: list[WeeklyTrade] = []
    for name, s in ((a, "a"), (b, "b")):
        swept = sig[f"swept_{s}"]
        pick = {"sweeper": swept, "lagger": ~swept, "both": swept | ~swept}[buy]
        rows = sig[pick]
        frame = pd.DataFrame(
            {
                "last_day": rows[f"last_day_{s}"],
                "entry": rows[f"close_{s}"],
                "stop_loss": rows[f"low_{s}"],
                "take_profit": rows[f"tp_{s}"],
            }
        )
        risk_ok = frame["entry"] - frame["stop_loss"] >= min_risk * frame["entry"]
        frame = frame[(frame["take_profit"] > frame["entry"]) & risk_ok]
        trades.extend(simulate(name, daily[name], frame, cost))
    return trades

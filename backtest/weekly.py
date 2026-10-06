"""Backtest semanal de manipulación en acciones y cripto: barrida del mínimo de la semana anterior.

Reglas (solo largos):
- Señal: la vela semanal perfora el mínimo de la semana anterior y cierra por encima de él.
- Entrada al cierre de esa semana. SL en el mínimo de la vela de la barrida. TP en el máximo
  de la semana anterior (la liquidez del lado contrario). Si el cierre ya está en el TP, no hay trade.
- La gestión se simula con velas diarias: si un día abre con hueco más allá del SL o del TP se
  sale a la apertura (las pérdidas por hueco pueden ser mayores de 1R). Si un mismo día toca SL
  y TP se cuenta la pérdida.
- Una posición por activo: mientras hay una abierta se ignoran las señales nuevas de ese activo.
- `cost` es el coste de ida y vuelta (comisión + deslizamiento) en tanto por uno del precio.

Las semanas se cierran en viernes; si el activo cotiza en fin de semana (cripto), en domingo.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

STOCKS = (
    "AAPL MSFT NVDA AMZN GOOGL META TSLA BRK-B JPM V MA UNH JNJ XOM CVX PG KO PEP WMT HD "
    "COST LLY MRK ABBV AVGO ORCL CSCO ADBE CRM NFLX AMD INTC QCOM TXN BAC WFC DIS NKE MCD "
    "CAT BA GE IBM"
).split()
CRYPTO = ["BTC-USD", "ETH-USD"]
UNIVERSE = STOCKS + CRYPTO
BENCHMARK = "SPY"


@dataclass(frozen=True)
class WeeklyTrade:
    ticker: str
    signal_week: pd.Timestamp  # último día de la semana de la barrida (= día de entrada)
    entry: float
    stop_loss: float
    take_profit: float
    result: str  # win / loss / open
    exit_date: pd.Timestamp | None
    exit_price: float
    cost: float = 0.0

    @property
    def risk(self) -> float:
        return self.entry - self.stop_loss

    @property
    def r(self) -> float:
        return (self.exit_price - self.entry - self.cost * self.entry) / self.risk

    @property
    def ret(self) -> float:
        """Rentabilidad de la operación con todo el capital del activo invertido."""
        return self.exit_price / self.entry - 1 - self.cost


def weekly_bars(daily: pd.DataFrame) -> pd.DataFrame:
    """Agrupa velas diarias en semanales; `last_day` es el último día con datos de cada semana."""
    weekend = (daily.index.dayofweek >= 5).any()
    rule = "W-SUN" if weekend else "W-FRI"
    d = daily.assign(last_day=daily.index)
    weeks = d.resample(rule).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "last_day": "last"}
    ).dropna()
    # La última semana puede estar a medias: solo cuenta si ya terminó.
    if len(weeks) and weeks.index[-1] > daily.index[-1]:
        weeks = weeks.iloc[:-1]
    return weeks


def find_signals(weeks: pd.DataFrame) -> pd.DataFrame:
    """Semanas que barren el mínimo de la anterior y cierran por encima, con TP sobre el cierre."""
    prev_low, prev_high = weeks["low"].shift(1), weeks["high"].shift(1)
    mask = (weeks["low"] < prev_low) & (weeks["close"] > prev_low) & (prev_high > weeks["close"])
    out = weeks.loc[mask, ["last_day", "close", "low"]].copy()
    out["take_profit"] = prev_high[mask]
    return out.rename(columns={"close": "entry", "low": "stop_loss"})


def simulate(ticker: str, daily: pd.DataFrame, signals: pd.DataFrame, cost: float = 0.0) -> list[WeeklyTrade]:
    dates = daily.index
    o, h, l, c = (daily[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    trades: list[WeeklyTrade] = []
    busy_until = pd.Timestamp.min
    for sig in signals.itertuples():
        day = sig.last_day
        if day <= busy_until or sig.entry <= sig.stop_loss:
            continue
        entry, sl, tp = sig.entry, sig.stop_loss, sig.take_profit
        result, exit_i, exit_px = "open", len(dates) - 1, c[-1]
        for i in range(int(dates.searchsorted(day, side="right")), len(dates)):
            if o[i] <= sl:
                result, exit_i, exit_px = "loss", i, o[i]
            elif o[i] >= tp:
                result, exit_i, exit_px = "win", i, o[i]
            elif l[i] <= sl:
                result, exit_i, exit_px = "loss", i, sl
            elif h[i] >= tp:
                result, exit_i, exit_px = "win", i, tp
            else:
                continue
            break
        exit_date = dates[exit_i] if result != "open" else None
        trades.append(WeeklyTrade(ticker, day, entry, sl, tp, result, exit_date, float(exit_px), cost))
        busy_until = dates[exit_i] if result != "open" else pd.Timestamp.max
    return trades


def equity_curve(daily: pd.DataFrame, trades: list[WeeklyTrade]) -> pd.Series:
    """Valor diario del capital del activo (empieza en 1): invertido solo durante las operaciones."""
    close = daily["close"]
    value = pd.Series(np.nan, index=daily.index)
    cash = 1.0
    last = daily.index[0]
    for t in trades:
        value[(value.index >= last) & (value.index <= t.signal_week)] = cash
        end = t.exit_date if t.exit_date is not None else daily.index[-1]
        held = (value.index > t.signal_week) & (value.index <= end)
        value[held] = cash * close[held] / t.entry
        if t.exit_date is None:
            value.iloc[-1] = cash * (1 + t.ret)
            return value.ffill()
        cash *= 1 + t.ret
        value[t.exit_date] = cash
        last = t.exit_date
    value[value.index >= last] = value[value.index >= last].fillna(cash)
    return value.ffill().fillna(1.0)


def r_stats(trades: list[WeeklyTrade]) -> dict:
    done = sorted((t for t in trades if t.result != "open"), key=lambda t: t.exit_date)
    rs = np.array([t.r for t in done])
    wins = sum(t.result == "win" for t in done)
    equity = np.cumsum(rs) if len(rs) else np.array([0.0])
    dd = float((np.maximum.accumulate(np.concatenate([[0.0], equity]))[1:] - equity).max()) if len(rs) else 0.0
    return {
        "trades": len(done),
        "open": len(trades) - len(done),
        "winrate": 100 * wins / len(done) if done else float("nan"),
        "avg_r": float(rs.mean()) if len(rs) else float("nan"),
        "total_r": float(rs.sum()),
        "max_dd_r": dd,
        "avg_ret_pct": 100 * float(np.mean([t.ret for t in done])) if done else float("nan"),
        "avg_weeks": float(np.mean([(t.exit_date - t.signal_week).days / 7 for t in done])) if done else float("nan"),
    }


def curve_stats(curve: pd.Series) -> dict:
    years = max((curve.index[-1] - curve.index[0]).days / 365.25, 1e-9)
    total = curve.iloc[-1] / curve.iloc[0]
    dd = float((1 - curve / curve.cummax()).max())
    return {"return_pct": 100 * (total - 1), "cagr_pct": 100 * (total ** (1 / years) - 1), "max_dd_pct": 100 * dd}


@dataclass
class WeeklyResult:
    per_ticker: pd.DataFrame
    by_year: pd.DataFrame
    portfolio: pd.DataFrame
    trades: list[WeeklyTrade]
    start: pd.Timestamp
    end: pd.Timestamp

    def trades_frame(self) -> pd.DataFrame:
        return pd.DataFrame([asdict(t) | {"r": t.r, "ret_pct": 100 * t.ret} for t in self.trades])


def run_weekly(
    daily_by_ticker: dict[str, pd.DataFrame],
    benchmark: pd.DataFrame | None = None,
    cost: float = 0.0,
) -> WeeklyResult:
    rows, all_trades, curves, holds = [], [], {}, {}
    for ticker, daily in daily_by_ticker.items():
        if len(daily) < 30:
            continue
        trades = simulate(ticker, daily, find_signals(weekly_bars(daily)), cost)
        all_trades.extend(trades)
        curves[ticker] = equity_curve(daily, trades)
        holds[ticker] = daily["close"] / daily["close"].iloc[0]
        in_market = curves[ticker].index.isin(_held_days(daily.index, trades)).mean()
        strat, hold = curve_stats(curves[ticker]), curve_stats(holds[ticker])
        rows.append(
            {"ticker": ticker, "desde": daily.index[0].date()}
            | r_stats(trades)
            | {f"estr_{k}": v for k, v in strat.items()}
            | {f"bh_{k}": v for k, v in hold.items()}
            | {"exposicion_pct": 100 * in_market}
        )

    # Cartera equiponderada: cada activo tiene su parte del capital; antes de cotizar queda en liquidez.
    calendar = sorted(set().union(*(c.index for c in curves.values())))
    align = lambda s: s.reindex(calendar).ffill().fillna(1.0)  # noqa: E731
    port = {
        "Estrategia (cartera equiponderada)": pd.concat([align(c) for c in curves.values()], axis=1).mean(axis=1),
        "Comprar y mantener (misma cartera)": pd.concat([align(c) for c in holds.values()], axis=1).mean(axis=1),
    }
    if benchmark is not None and len(benchmark):
        b = benchmark["close"][benchmark.index >= calendar[0]]
        port[f"Comprar y mantener {BENCHMARK}"] = align(b / b.iloc[0])
    portfolio = pd.DataFrame([{"cartera": k} | curve_stats(v) for k, v in port.items()])

    years = []
    for year in sorted({t.exit_date.year for t in all_trades if t.exit_date is not None}):
        ts = [t for t in all_trades if t.exit_date is not None and t.exit_date.year == year]
        s = r_stats(ts)
        years.append({"año": year, "trades": s["trades"], "winrate": s["winrate"], "avg_r": s["avg_r"], "total_r": s["total_r"]})

    return WeeklyResult(
        pd.DataFrame(rows), pd.DataFrame(years), portfolio, all_trades, pd.Timestamp(calendar[0]), pd.Timestamp(calendar[-1])
    )


def _held_days(dates: pd.DatetimeIndex, trades: list[WeeklyTrade]) -> pd.DatetimeIndex:
    held = np.zeros(len(dates), bool)
    for t in trades:
        end = t.exit_date if t.exit_date is not None else dates[-1]
        held |= (dates > t.signal_week) & (dates <= end)
    return dates[held]

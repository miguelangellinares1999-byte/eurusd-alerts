"""Simulación de operaciones con orden límite sobre velas (idealmente M1).

Reglas, iguales para todas las estrategias:
- La orden límite se coloca al cierre de la vela de la señal y se cancela si el precio toca
  el TP antes de llenarla o si termina el día de trading (17:00 NY) sin llenarse.
- Una vez dentro, gana si toca el TP y pierde si toca el SL. Si en la misma vela se tocan los
  dos (o el TP en la vela de entrada), se cuenta lo peor: pérdida / sin entrada.
- Las velas de MT5 son bid. Con `spread` (en precio) las compras se llenan cuando el ask toca
  la entrada y las ventas salen cuando el ask toca el SL/TP. Sin comisiones.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

CHUNK = 20_000


@dataclass(frozen=True)
class Trade:
    signal_time: pd.Timestamp
    direction: str  # "long" / "short"
    entry: float
    stop_loss: float
    take_profit: float
    result: str  # win / loss / no_fill / open
    fill_time: pd.Timestamp | None = None
    exit_time: pd.Timestamp | None = None

    @property
    def r(self) -> float:
        if self.result == "win":
            return abs(self.take_profit - self.entry) / abs(self.entry - self.stop_loss)
        return -1.0 if self.result == "loss" else 0.0


def trading_day_end(ts: pd.Timestamp, close_hour: int = 17, tz: str = "America/New_York") -> pd.Timestamp:
    local = ts.tz_convert(tz)
    end = local.normalize() + pd.Timedelta(hours=close_hour)
    if local >= end:
        end = (local + pd.Timedelta(days=1)).normalize() + pd.Timedelta(hours=close_hour)
    return end.tz_convert("UTC")


class Simulator:
    def __init__(self, candles: pd.DataFrame, spread: float = 0.0):
        self.times = candles.index
        self.high = candles["high"].to_numpy(float)
        self.low = candles["low"].to_numpy(float)
        self.spread = spread

    def _first(self, mask_fn, start: int, stop: int) -> int | None:
        for a in range(start, stop, CHUNK):
            b = min(a + CHUNK, stop)
            hit = np.flatnonzero(mask_fn(a, b))
            if len(hit):
                return a + int(hit[0])
        return None

    def run(self, signal_time, direction: str, entry: float, stop: float, tp: float) -> Trade:
        long = direction == "long"
        h, l = self.high, self.low
        mk = lambda res, f=None, x=None: Trade(signal_time, direction, entry, stop, tp, res, f, x)  # noqa: E731
        # Niveles traducidos a precio bid (lo que muestran las velas).
        if long:
            entry_b, stop_b, tp_b = entry - self.spread, stop, tp
        else:
            entry_b, stop_b, tp_b = entry, stop - self.spread, tp - self.spread
        start = int(self.times.searchsorted(signal_time))
        cancel = int(self.times.searchsorted(trading_day_end(signal_time)))

        fill = self._first((lambda a, b: l[a:b] <= entry_b) if long else (lambda a, b: h[a:b] >= entry_b), start, cancel)
        tp_hit = self._first((lambda a, b: h[a:b] >= tp_b) if long else (lambda a, b: l[a:b] <= tp_b), start, cancel)
        if fill is None or (tp_hit is not None and tp_hit <= fill):
            return mk("no_fill")
        fill_time = self.times[fill]
        end = len(self.times)
        sl_i = self._first((lambda a, b: l[a:b] <= stop_b) if long else (lambda a, b: h[a:b] >= stop_b), fill, end)
        tp_i = self._first((lambda a, b: h[a:b] >= tp_b) if long else (lambda a, b: l[a:b] <= tp_b), fill + 1, end)
        if sl_i is None and tp_i is None:
            return mk("open", fill_time)
        if tp_i is None or (sl_i is not None and sl_i <= tp_i):
            return mk("loss", fill_time, self.times[sl_i])
        return mk("win", fill_time, self.times[tp_i])


def summarize(trades: list[Trade]) -> dict:
    done = [t for t in trades if t.result in ("win", "loss")]
    wins = sum(t.result == "win" for t in done)
    rs = np.array([t.r for t in sorted(done, key=lambda t: t.exit_time)])
    equity = np.cumsum(rs) if len(rs) else np.array([0.0])
    drawdown = float((np.maximum.accumulate(np.concatenate([[0.0], equity]))[1:] - equity).max()) if len(rs) else 0.0
    streak = longest = 0
    for r in rs:
        streak = streak + 1 if r < 0 else 0
        longest = max(longest, streak)
    return {
        "signals": len(trades),
        "trades": len(done),
        "wins": wins,
        "losses": len(done) - wins,
        "winrate": 100 * wins / len(done) if done else float("nan"),
        "total_r": float(rs.sum()) if len(rs) else 0.0,
        "r_per_trade": float(rs.mean()) if len(rs) else float("nan"),
        "max_dd_r": drawdown,
        "max_loss_streak": longest,
    }

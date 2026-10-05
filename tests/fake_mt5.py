"""Mock del módulo MetaTrader5: misma API y mismos tipos de retorno que se usan en el proyecto."""

from __future__ import annotations

import fnmatch
from types import SimpleNamespace

import numpy as np
import pandas as pd

from data.validation import market_closed

# Mismo dtype que devuelven copy_rates_range / copy_rates_from_pos.
RATES_DTYPE = np.dtype([
    ("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"),
    ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8"),
])


def make_rates(server_times, ohlc=None) -> np.ndarray:
    """Velas con horas de servidor (naive). Sin `ohlc`: velas válidas de 1 pip."""
    stamps = pd.DatetimeIndex(pd.to_datetime(list(server_times)))
    rates = np.zeros(len(stamps), dtype=RATES_DTYPE)
    rates["time"] = stamps.as_unit("s").asi8
    if ohlc is None:
        ohlc = [(1.1000, 1.1001, 1.0999, 1.1000)] * len(stamps)
    for field, col in zip(("open", "high", "low", "close"), np.array(ohlc, dtype=float).T):
        rates[field] = col
    rates["tick_volume"] = 100
    return rates


def market_rates(start_utc, end_utc, offset_hours=None, server_timezone=None, seed=1) -> np.ndarray:
    """Histórico M15 realista (paseo aleatorio, sin fines de semana) en hora del servidor."""
    utc = pd.date_range(pd.Timestamp(start_utc).tz_convert("UTC").ceil("15min"), pd.Timestamp(end_utc).tz_convert("UTC"), freq="15min")
    utc = utc[~market_closed(utc)]
    rng = np.random.default_rng(seed)
    close = 1.10 + np.cumsum(rng.normal(0, 0.0004, len(utc)))
    open_ = np.r_[close[0], close[:-1]]
    wick = np.abs(rng.normal(0, 0.0003, (2, len(utc))))
    high = np.maximum(open_, close) + wick[0]
    low = np.minimum(open_, close) - wick[1]
    if server_timezone == "NY+7":
        server = utc.tz_convert("America/New_York").tz_localize(None) + pd.Timedelta(hours=7)
    else:
        server = utc.tz_localize(None) + pd.Timedelta(hours=offset_hours or 0)
    return make_rates(server, list(zip(open_, high, low, close)))


class FakeMT5:
    TIMEFRAME_M1 = 1
    TIMEFRAME_M5 = 5
    TIMEFRAME_M15 = 15
    TIMEFRAME_M30 = 30
    TIMEFRAME_H1 = 16385
    TIMEFRAME_H4 = 16388
    TIMEFRAME_D1 = 16408

    def __init__(self, rates=None, symbols=("EURUSD",), init_ok=True, connected=True, logged_in=True,
                 tick_time=None, maxbars=100000):
        self.rates = make_rates([]) if rates is None else rates
        self.symbols = list(symbols)
        self.init_ok = init_ok
        self.connected = connected
        self.logged_in = logged_in
        self.tick_time = tick_time
        self.maxbars = maxbars
        self.initialized = False
        self.shutdowns = 0
        self.calls: list[tuple] = []
        self._error = (1, "Success")

    def _require_init(self):
        if not self.initialized:
            raise AssertionError("llamada a MT5 sin initialize()")

    def initialize(self, **kwargs):
        self.calls.append(("initialize", kwargs))
        if not self.init_ok:
            self._error = (-10003, "IPC initialize failed, MetaTrader 5 x64 not found")
            return False
        self.initialized = True
        return True

    def shutdown(self):
        self.calls.append(("shutdown",))
        self.shutdowns += 1
        self.initialized = False
        return True

    def last_error(self):
        return self._error

    def version(self):
        self._require_init()
        return (500, 4620, "26 Sep 2026")

    def terminal_info(self):
        self._require_init()
        return SimpleNamespace(connected=self.connected, maxbars=self.maxbars)

    def account_info(self):
        self._require_init()
        if not self.logged_in:
            self._error = (-6, "Terminal: Authorization failed")
            return None
        return SimpleNamespace(login=12345678, name="Demo", server="Broker-Demo", company="Broker Ltd", currency="USD")

    def symbol_select(self, symbol, enable):
        self._require_init()
        self.calls.append(("symbol_select", symbol, enable))
        if symbol in self.symbols:
            return True
        self._error = (-1, "Terminal: Call failed")
        return False

    def symbol_info(self, symbol):
        self._require_init()
        if symbol not in self.symbols:
            return None
        return SimpleNamespace(name=symbol, description="Euro vs US Dollar", digits=5)

    def symbols_get(self, group=None):
        self._require_init()
        names = [s for s in self.symbols if group is None or fnmatch.fnmatch(s, group)]
        return tuple(SimpleNamespace(name=n) for n in names)

    def symbol_info_tick(self, symbol):
        self._require_init()
        return None if self.tick_time is None else SimpleNamespace(time=int(self.tick_time))

    def copy_rates_range(self, symbol, timeframe, date_from, date_to):
        self._require_init()
        self.calls.append(("copy_rates_range", symbol, timeframe, date_from, date_to))
        if symbol not in self.symbols:
            return None
        lo, hi = int(date_from.timestamp()), int(date_to.timestamp())
        return self.rates[(self.rates["time"] >= lo) & (self.rates["time"] <= hi)]

    def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
        self._require_init()
        self.calls.append(("copy_rates_from_pos", symbol, timeframe, start_pos, count))
        if symbol not in self.symbols:
            return None
        end = len(self.rates) - start_pos
        return self.rates[max(0, end - count):end]

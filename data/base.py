from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

import pandas as pd

from data.validation import validate_candles

# Formato que devuelven las fuentes: una fila por vela, timestamp de apertura en UTC (tz-aware).
CANDLE_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]
# Formato que usa el detector: índice UTC (apertura de la vela) y columnas OHLC.
COLUMNS = ["open", "high", "low", "close"]


class DataSource(ABC):
    """Fuente abstracta de velas. `symbol` es el símbolo por defecto del modo en vivo."""

    symbol: str = "EURUSD"

    @abstractmethod
    def get_range(self, symbol: str, timeframe, date_from: datetime, date_to: datetime) -> pd.DataFrame:
        """Velas con apertura entre date_from y date_to (UTC), columnas CANDLE_COLUMNS."""

    @abstractmethod
    def get_last(self, symbol: str, timeframe, n: int) -> pd.DataFrame:
        """Últimas `n` velas (la última puede estar en formación), columnas CANDLE_COLUMNS."""

    def get_closed_candles(
        self, timeframe_minutes: int, lookback_days: int, now: datetime | None = None
    ) -> pd.DataFrame:
        """Modo en vivo: últimas velas cerradas y validadas, en el formato del detector."""
        n = int(lookback_days * 24 * 60 / timeframe_minutes)
        raw = self.get_last(self.symbol, timeframe_minutes, n)
        candles, _ = validate_candles(raw, timeframe_minutes, now=now)
        return to_engine_frame(candles)

    def close(self) -> None:
        """Libera recursos (conexiones)."""


def empty_candles() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.Series([], dtype="datetime64[ns, UTC]"),
            **{c: pd.Series([], dtype=float) for c in CANDLE_COLUMNS[1:]},
        }
    )


def to_engine_frame(candles: pd.DataFrame) -> pd.DataFrame:
    """CANDLE_COLUMNS -> índice UTC + open/high/low/close (lo que espera el detector)."""
    out = candles.set_index("timestamp")[COLUMNS].astype(float)
    out.index = pd.DatetimeIndex(out.index).tz_convert("UTC")
    out.index.name = "time"
    return out


def from_engine_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Índice UTC + OHLC -> CANDLE_COLUMNS (volume = 0 si no existe)."""
    out = df.rename(columns=str.lower).copy()
    if "volume" not in out.columns:
        out["volume"] = 0.0
    out = out[COLUMNS + ["volume"]].astype(float)
    out.insert(0, "timestamp", pd.DatetimeIndex(out.index).tz_convert("UTC"))
    return out.reset_index(drop=True)


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Columnas en minúsculas, índice UTC ordenado, sin NaN ni duplicados (CSV)."""
    out = df.rename(columns=str.lower)[COLUMNS].astype(float).dropna()
    idx = pd.DatetimeIndex(out.index)
    out.index = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    out.index.name = "time"
    return out[~out.index.duplicated(keep="last")].sort_index()

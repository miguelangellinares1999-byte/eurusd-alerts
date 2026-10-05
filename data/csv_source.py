"""Lectura de CSV históricos para el backtest.

Acepta columnas time/datetime/date/timestamp (o date + time por separado, como el
export de MetaTrader con <DATE> <TIME>) y open/high/low/close, en cualquier mayúscula.
Si el CSV es de menor temporalidad (p.ej. 1m) se agrega a la temporalidad pedida.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from data.base import COLUMNS, DataSource, from_engine_frame, normalize

TIME_COLUMNS = ("time", "datetime", "date", "timestamp", "gmt time", "local time")


def load_csv(path: str | Path, tz: str = "UTC") -> pd.DataFrame:
    df = pd.read_csv(path, sep=None, engine="python")
    df.columns = [str(c).strip().strip("<>").lower() for c in df.columns]
    if "date" in df.columns and "time" in df.columns:
        stamps = pd.to_datetime(df["date"].astype(str) + " " + df["time"].astype(str))
    else:
        col = next((c for c in TIME_COLUMNS if c in df.columns), None)
        if col is None:
            raise ValueError(f"CSV sin columna de fecha reconocible: {list(df.columns)}")
        stamps = pd.to_datetime(df[col], utc=False, format="mixed", dayfirst=False)
    idx = pd.DatetimeIndex(stamps)
    idx = idx.tz_localize(tz) if idx.tz is None else idx
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Faltan columnas en el CSV: {missing}")
    out = df[COLUMNS].copy()
    out.index = idx
    return normalize(out)


def resample_ohlc(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Agrega a `minutes` si los datos son de menor temporalidad."""
    if len(df) < 2:
        return df
    step = df.index.to_series().diff().median()
    if step >= pd.Timedelta(minutes=minutes):
        return df
    agg = df.resample(f"{minutes}min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    )
    return agg.dropna()


class CsvSource(DataSource):
    """Fuente basada en fichero (útil para reproducir datos sin conexión)."""

    def __init__(self, path: str | Path, tz: str = "UTC", symbol: str = "EURUSD"):
        self.path = Path(path)
        self.tz = tz
        self.symbol = symbol

    def _load(self, timeframe) -> pd.DataFrame:
        return from_engine_frame(resample_ohlc(load_csv(self.path, self.tz), int(timeframe)))

    def get_range(self, symbol, timeframe, date_from, date_to) -> pd.DataFrame:
        df = self._load(timeframe)
        lo, hi = (pd.Timestamp(d).tz_localize("UTC") if pd.Timestamp(d).tz is None else pd.Timestamp(d) for d in (date_from, date_to))
        return df[(df["timestamp"] >= lo) & (df["timestamp"] <= hi)].reset_index(drop=True)

    def get_last(self, symbol, timeframe, n) -> pd.DataFrame:
        return self._load(timeframe).tail(n).reset_index(drop=True)


def export_candles_csv(candles: pd.DataFrame, path: str | Path) -> Path:
    """Guarda velas (CANDLE_COLUMNS) para compararlas con TradingView.

    `timestamp` en UTC ISO y `unix_time` en segundos (el formato del export de TradingView);
    el fichero se puede volver a leer con load_csv / `backtest --csv`.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = candles.copy()
    out.insert(1, "unix_time", out["timestamp"].astype("datetime64[s, UTC]").astype("int64"))
    out["timestamp"] = out["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S+00:00")
    out.to_csv(path, index=False)
    return path

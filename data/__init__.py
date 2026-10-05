"""Fuentes de velas OHLC. Devuelven un DataFrame con columnas timestamp (apertura, UTC
tz-aware), open, high, low, close y volume."""

from data.base import DataSource, from_engine_frame, to_engine_frame
from data.validation import CandleValidationError, validate_candles


def create_source(cfg) -> DataSource:
    """Crea la fuente indicada en config.yaml (import perezoso de MetaTrader5)."""
    kind = cfg.data_source.type.lower()
    if kind == "mt5":
        from data.mt5_source import MT5Source

        ds = cfg.data_source
        return MT5Source(ds.mt5_symbol, ds.mt5_server_utc_offset_hours, ds.mt5_auto_detect_offset, ds.mt5_server_timezone)
    if kind == "yahoo":
        from data.yahoo_source import YahooSource

        return YahooSource(cfg.symbol, cfg.data_source.yahoo_ticker)
    raise ValueError(f"Fuente de datos desconocida: {cfg.data_source.type} ('mt5' o 'yahoo')")


__all__ = [
    "DataSource", "create_source", "validate_candles", "CandleValidationError",
    "to_engine_frame", "from_engine_frame",
]

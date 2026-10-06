"""Fuente de velas de Yahoo Finance (gratis, sin clave, funciona en Linux / GitHub Actions).

Yahoo da el intradía de los últimos ~60 días, con timestamps en UTC. La última fila suele
ser el tick en curso (no alineado a 15 min): se descarta junto con las filas vacías.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import requests

from data.base import CANDLE_COLUMNS, DataSource, empty_candles

URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
INTERVALS = {1: "1m", 5: "5m", 15: "15m", 30: "30m", 60: "60m"}
HEADERS = {"User-Agent": "Mozilla/5.0 (eurusd-alerts)"}


class YahooError(RuntimeError):
    pass


class YahooSource(DataSource):
    def __init__(self, symbol: str = "EURUSD", ticker: str = "EURUSD=X", session=None):
        self.symbol = symbol
        self.ticker = ticker
        self.http = session or requests.Session()

    def _fetch(self, timeframe, params: dict) -> pd.DataFrame:
        tf = int(timeframe)
        if tf not in INTERVALS:
            raise YahooError(f"Temporalidad no soportada por Yahoo: {timeframe}")
        resp = self.http.get(
            URL.format(ticker=self.ticker),
            params={"interval": INTERVALS[tf], **params},
            headers=HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        chart = resp.json().get("chart", {})
        if chart.get("error") or not chart.get("result"):
            raise YahooError(f"Yahoo no devolvió velas de {self.ticker}: {chart.get('error')}")
        res = chart["result"][0]
        if not res.get("timestamp"):
            return empty_candles()
        q = res["indicators"]["quote"][0]
        out = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(res["timestamp"], unit="s", utc=True),
                "open": q["open"],
                "high": q["high"],
                "low": q["low"],
                "close": q["close"],
                "volume": [v or 0 for v in q.get("volume", [0] * len(res["timestamp"]))],
            }
        )
        ts = pd.DatetimeIndex(out["timestamp"])
        aligned = ((ts.minute % tf) == 0) & (ts.second == 0)
        out = out[aligned].dropna(subset=["open", "high", "low", "close"])
        out[["open", "high", "low", "close", "volume"]] = out[["open", "high", "low", "close", "volume"]].astype(float)
        # Yahoo redondea distinto OHLC: asegurar high/low coherentes.
        out["high"] = out[["open", "high", "low", "close"]].max(axis=1)
        out["low"] = out[["open", "high", "low", "close"]].min(axis=1)
        return out[CANDLE_COLUMNS].reset_index(drop=True)

    def get_range(self, symbol: str, timeframe, date_from: datetime, date_to: datetime) -> pd.DataFrame:
        lo, hi = (int(pd.Timestamp(d).timestamp()) for d in (date_from, date_to))
        return self._fetch(timeframe, {"period1": lo, "period2": hi})

    def get_last(self, symbol: str, timeframe, n: int) -> pd.DataFrame:
        days = max(1, -(-n * int(timeframe) // 1440) + 3)  # + margen para el fin de semana
        return self._fetch(timeframe, {"range": f"{min(days, 59)}d"}).tail(n).reset_index(drop=True)


def get_daily_history(ticker: str, start: datetime, end: datetime | None = None, session=None) -> pd.DataFrame:
    """Velas diarias de Yahoo indexadas por fecha de la bolsa (sin hora), ajustadas por splits.

    Para acciones, cripto, índices... (backtest semanal). La vela del día en curso se descarta.
    """
    http = session or requests.Session()
    end = pd.Timestamp.now(tz="UTC") if end is None else pd.Timestamp(end)
    resp = http.get(
        URL.format(ticker=ticker),
        params={
            "interval": "1d",
            "period1": int(pd.Timestamp(start).timestamp()),
            "period2": int(end.timestamp()),
            "events": "split",
        },
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    chart = resp.json().get("chart", {})
    if chart.get("error") or not chart.get("result"):
        raise YahooError(f"Yahoo no devolvió velas de {ticker}: {chart.get('error')}")
    res = chart["result"][0]
    if not res.get("timestamp"):
        return pd.DataFrame(columns=["open", "high", "low", "close"], dtype=float)
    tz = res.get("meta", {}).get("exchangeTimezoneName") or "UTC"
    q = res["indicators"]["quote"][0]
    days = pd.to_datetime(res["timestamp"], unit="s", utc=True).tz_convert(tz).tz_localize(None).normalize()
    out = pd.DataFrame({k: q[k] for k in ("open", "high", "low", "close")}, index=days).dropna().astype(float)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out["high"] = out[["open", "high", "low", "close"]].max(axis=1)
    out["low"] = out[["open", "high", "low", "close"]].min(axis=1)
    today = end.tz_convert(tz).tz_localize(None).normalize()
    return out[out.index < today].rename_axis("date")

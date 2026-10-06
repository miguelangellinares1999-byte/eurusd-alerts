"""Datos de mercado para el backtest semanal: descarga con caché y amplitud del S&P 500.

La amplitud se calcula como el % de componentes ACTUALES del S&P 500 por encima de su media
de 50 sesiones (equivalente al S5FI de TradingView, con sesgo de supervivencia: faltan las
empresas que salieron del índice).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from data.yahoo_source import get_daily_history

log = logging.getLogger(__name__)

SP500_FILE = Path(__file__).with_name("sp500.csv")
CACHE_DIR = Path(__file__).with_name("cache")


def sp500_members() -> pd.DataFrame:
    return pd.read_csv(SP500_FILE)


def load_daily(
    tickers: list[str], start: str, cache_dir: Path | None = CACHE_DIR, workers: int = 8, fetch=get_daily_history
) -> dict[str, pd.DataFrame]:
    """Velas diarias por ticker. Con `cache_dir` guarda cada serie en CSV y no la vuelve a pedir el mismo día."""
    today = pd.Timestamp.now().normalize()

    def one(ticker: str) -> tuple[str, pd.DataFrame | None]:
        path = None if cache_dir is None else cache_dir / f"{ticker.replace('^', '_')}_{start}.csv"
        if path is not None and path.exists() and pd.Timestamp(path.stat().st_mtime, unit="s") >= today:
            return ticker, pd.read_csv(path, index_col="date", parse_dates=True)
        try:
            df = fetch(ticker, pd.Timestamp(start, tz="UTC"))
        except Exception as exc:  # un ticker caído no debe parar el resto
            log.warning("Sin datos de %s: %s", ticker, exc)
            return ticker, None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(path)
        return ticker, df

    with ThreadPoolExecutor(workers) as pool:
        return {t: df for t, df in pool.map(one, tickers) if df is not None and len(df)}


def breadth(daily_by_ticker: dict[str, pd.DataFrame], window: int = 50) -> pd.Series:
    """% diario de valores por encima de su media de `window` sesiones (solo los que ya la tienen)."""
    closes = pd.concat({t: d["close"] for t, d in daily_by_ticker.items()}, axis=1).sort_index()
    ma = closes.rolling(window, min_periods=window).mean()
    valid = ma.notna() & closes.notna()
    count = valid.sum(axis=1)
    above = ((closes > ma) & valid).sum(axis=1)
    return (100 * above / count.where(count >= 20)).dropna()

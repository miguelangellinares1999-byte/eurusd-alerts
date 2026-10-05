"""Fuente de velas MetaTrader 5 (solo Windows, con el terminal abierto y con sesión iniciada).

MT5 entrega las horas en la hora del *servidor del bróker* (como si fuera UTC). Aquí se
convierten a UTC real con, por orden de prioridad:
  1. `server_timezone` ("NY+7", nombre IANA...): respeta el horario de verano del bróker.
  2. `auto_detect_offset`: desfase estimado con la hora del último tick frente a la hora UTC.
  3. `server_utc_offset_hours`: desfase fijo.
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone

import pandas as pd

from data.base import CANDLE_COLUMNS, DataSource, empty_candles
from data.validation import market_closed

log = logging.getLogger(__name__)

# Minutos -> sufijo de la constante oficial mt5.TIMEFRAME_<sufijo>.
TIMEFRAME_NAMES = {1: "M1", 5: "M5", 15: "M15", 30: "M30", 60: "H1", 240: "H4", 1440: "D1"}
# Un tick más viejo que esto no sirve para estimar el desfase.
MAX_TICK_AGE_SECONDS = 15 * 60


class MT5Error(RuntimeError):
    pass


def timeframe_minutes(timeframe) -> int:
    """15 o "M15" -> 15."""
    if isinstance(timeframe, int):
        return timeframe
    by_name = {v: k for k, v in TIMEFRAME_NAMES.items()}
    try:
        return by_name[str(timeframe).upper()]
    except KeyError:
        raise ValueError(f"Temporalidad no soportada: {timeframe}") from None


def server_to_utc(times, offset_hours: float | None = None, server_timezone: str | None = None) -> pd.DatetimeIndex:
    """Segundos de `rates['time']` (hora del servidor) -> DatetimeIndex UTC tz-aware.

    Con `server_timezone` las horas inexistentes/ambiguas (cambio de hora) quedan como NaT.
    """
    naive = pd.DatetimeIndex(pd.to_datetime(pd.Series(times), unit="s"))
    if server_timezone:
        m = re.fullmatch(r"NY([+-]\d+)", server_timezone.replace(" ", ""))
        if m:
            local = (naive - pd.Timedelta(hours=int(m.group(1)))).tz_localize(
                "America/New_York", ambiguous="NaT", nonexistent="NaT"
            )
        else:
            local = naive.tz_localize(server_timezone, ambiguous="NaT", nonexistent="NaT")
        return local.tz_convert("UTC")
    return (naive - pd.Timedelta(hours=offset_hours or 0)).tz_localize("UTC")


def utc_to_server(dt: datetime, offset_hours: float | None = None, server_timezone: str | None = None) -> datetime:
    """Hora UTC -> hora del servidor, expresada como datetime UTC (lo que espera copy_rates_range)."""
    ts = pd.Timestamp(dt)
    ts = ts.tz_localize("UTC") if ts.tz is None else ts.tz_convert("UTC")
    if server_timezone:
        m = re.fullmatch(r"NY([+-]\d+)", server_timezone.replace(" ", ""))
        if m:
            wall = ts.tz_convert("America/New_York").tz_localize(None) + pd.Timedelta(hours=int(m.group(1)))
        else:
            wall = ts.tz_convert(server_timezone).tz_localize(None)
    else:
        wall = ts.tz_localize(None) + pd.Timedelta(hours=offset_hours or 0)
    return wall.to_pydatetime().replace(tzinfo=timezone.utc)


def detect_offset(tick_time: int, now: datetime | None = None) -> float | None:
    """Desfase servidor-UTC en horas a partir de la hora del último tick.

    Devuelve None si no es fiable: mercado cerrado o tick viejo (no cae a una hora exacta).
    """
    now_ts = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now).tz_convert("UTC")
    if market_closed(pd.DatetimeIndex([now_ts]))[0]:
        return None
    diff = tick_time - now_ts.timestamp()
    hours = round(diff / 3600)
    if abs(diff - hours * 3600) > MAX_TICK_AGE_SECONDS or not -12 <= hours <= 14:
        return None
    return float(hours)


class MT5Source(DataSource):
    def __init__(
        self,
        symbol: str = "EURUSD",
        server_utc_offset_hours: float = 0.0,
        auto_detect_offset: bool = False,
        server_timezone: str | None = None,
        mt5_module=None,
    ):
        if mt5_module is None:
            import MetaTrader5 as mt5_module  # solo Windows
        self.mt5 = mt5_module
        self.symbol = symbol
        self.server_utc_offset_hours = float(server_utc_offset_hours)
        self.auto_detect_offset = auto_detect_offset
        self.server_timezone = server_timezone or None
        self.last_offset_hours: float | None = None  # desfase usado en la última descarga

    # ------------------------------------------------------------------ conexión
    def _init_kwargs(self) -> dict:
        kwargs = {}
        if os.getenv("MT5_PATH"):
            kwargs["path"] = os.getenv("MT5_PATH")
        if os.getenv("MT5_LOGIN"):
            kwargs.update(
                login=int(os.environ["MT5_LOGIN"]),
                password=os.getenv("MT5_PASSWORD", ""),
                server=os.getenv("MT5_SERVER", ""),
            )
        return kwargs

    @contextmanager
    def session(self):
        """mt5.initialize() ... mt5.shutdown() (siempre, aunque algo falle)."""
        mt5 = self.mt5
        try:
            if not mt5.initialize(**self._init_kwargs()):
                raise MT5Error(
                    f"No se pudo conectar con MetaTrader 5: {mt5.last_error()}. "
                    "Abre el terminal MT5 e inicia sesión en tu cuenta antes de ejecutar."
                )
            yield mt5
        finally:
            mt5.shutdown()

    def select_symbol(self, symbol: str) -> None:
        """Activa el símbolo en la Observación de mercado; error claro si el bróker no lo tiene."""
        mt5 = self.mt5
        if mt5.symbol_select(symbol, True):
            return
        error = mt5.last_error()
        if mt5.symbol_info(symbol) is None:
            found = mt5.symbols_get(group=f"*{symbol[:6]}*") or ()
            similar = ", ".join(s.name for s in found[:10]) or "ninguno"
            raise MT5Error(
                f"El símbolo '{symbol}' no existe en este bróker ({error}). Símbolos parecidos: "
                f"{similar}. Cámbialo en config.yaml -> data_source.mt5.symbol"
            )
        raise MT5Error(f"No se pudo activar '{symbol}' en la Observación de mercado: {error}")

    def resolve_offset(self, symbol: str) -> float | None:
        """Desfase en horas a usar (None si se usa server_timezone)."""
        if self.server_timezone:
            return None
        if not self.auto_detect_offset:
            return self.server_utc_offset_hours
        tick = self.mt5.symbol_info_tick(symbol)
        detected = detect_offset(tick.time) if tick is not None else None
        if detected is None:
            log.warning(
                "No se pudo estimar el desfase del servidor (mercado cerrado o sin ticks recientes: %s); "
                "se usa server_utc_offset_hours=%s",
                self.mt5.last_error(), self.server_utc_offset_hours,
            )
            return self.server_utc_offset_hours
        if detected != self.server_utc_offset_hours:
            log.info("Desfase detectado UTC%+g (configurado UTC%+g)", detected, self.server_utc_offset_hours)
        return detected

    # ------------------------------------------------------------------ datos
    def _timeframe(self, timeframe):
        return getattr(self.mt5, f"TIMEFRAME_{TIMEFRAME_NAMES[timeframe_minutes(timeframe)]}")

    def _to_frame(self, rates, symbol: str, offset: float | None) -> pd.DataFrame:
        if rates is None:
            raise MT5Error(f"MT5 no devolvió velas de {symbol}: {self.mt5.last_error()}")
        self.last_offset_hours = offset
        if len(rates) == 0:
            return empty_candles()
        raw = pd.DataFrame(rates)
        out = pd.DataFrame(
            {
                "timestamp": server_to_utc(raw["time"], offset, self.server_timezone),
                "open": raw["open"].astype(float),
                "high": raw["high"].astype(float),
                "low": raw["low"].astype(float),
                "close": raw["close"].astype(float),
                "volume": raw["tick_volume"].astype(float),
            }
        )
        bad = out["timestamp"].isna()
        if bad.any():
            log.warning("Descartadas %d velas con hora inexistente/ambigua por el cambio de hora", int(bad.sum()))
            out = out[~bad]
        return out[CANDLE_COLUMNS].reset_index(drop=True)

    def get_range(self, symbol: str, timeframe, date_from: datetime, date_to: datetime) -> pd.DataFrame:
        tf = self._timeframe(timeframe)
        with self.session() as mt5:
            self.select_symbol(symbol)
            offset = self.resolve_offset(symbol)
            rates = mt5.copy_rates_range(
                symbol, tf,
                utc_to_server(date_from, offset, self.server_timezone),
                utc_to_server(date_to, offset, self.server_timezone),
            )
            return self._to_frame(rates, symbol, offset)

    def get_last(self, symbol: str, timeframe, n: int) -> pd.DataFrame:
        tf = self._timeframe(timeframe)
        with self.session() as mt5:
            self.select_symbol(symbol)
            offset = self.resolve_offset(symbol)
            return self._to_frame(mt5.copy_rates_from_pos(symbol, tf, 0, n), symbol, offset)

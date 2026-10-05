"""Día de trading (cierre 17:00 NY) y niveles del día anterior (PDH/PDL)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

# El primer día de la serie solo se usa si su primera vela está a menos de esto de la
# apertura teórica (si no, es un día cortado por el inicio de los datos).
MAX_FIRST_DAY_GAP = timedelta(hours=3)


def trading_days(index: pd.DatetimeIndex, close_hour: int = 17, tz: str = "America/New_York") -> pd.Index:
    """Fecha del día de trading de cada vela (según su hora de apertura).

    Con cierre a las 17:00 NY, una vela que abre a las 17:00 NY o más tarde pertenece
    al día siguiente: se desplaza la hora local (24 - close_hour) horas y se toma la fecha.
    """
    if index.tz is None:
        raise ValueError("El índice de velas debe tener zona horaria (UTC)")
    local = index.tz_convert(ZoneInfo(tz))
    shifted = local.tz_localize(None) + timedelta(hours=(24 - close_hour) % 24)
    return pd.Index(shifted.date, name="trading_day")


def trading_day_of(ts: datetime, close_hour: int = 17, tz: str = "America/New_York") -> date:
    """Día de trading de una vela (hora de apertura tz-aware): de 17:00 a 17:00 en Nueva York."""
    if ts.tzinfo is None:
        raise ValueError("La hora debe tener zona horaria")
    local = ts.astimezone(ZoneInfo(tz))
    return (local.replace(tzinfo=None) + timedelta(hours=(24 - close_hour) % 24)).date()


def day_open(day, close_hour: int = 17, tz: str = "America/New_York") -> pd.Timestamp:
    """Apertura teórica (UTC) del día de trading `day`."""
    start = pd.Timestamp(day) - timedelta(days=1) if close_hour else pd.Timestamp(day)
    return start.replace(hour=close_hour).tz_localize(ZoneInfo(tz)).tz_convert("UTC")


def add_daily_levels(
    df: pd.DataFrame, close_hour: int = 17, tz: str = "America/New_York"
) -> pd.DataFrame:
    """Añade columnas trading_day, pdh y pdl.

    PDH/PDL son el máximo/mínimo del día de trading válido anterior: se ignoran los
    "días" de sábado/domingo (velas sueltas tras el cierre del viernes que dan algunos
    feeds), así que el lunes usa el viernes. Si el primer día de la serie está cortado,
    tampoco se usa. Las velas sin día anterior válido quedan con NaN.
    """
    out = df.copy()
    days = trading_days(out.index, close_hour, tz)
    out["trading_day"] = days
    daily = out.groupby("trading_day").agg(day_high=("high", "max"), day_low=("low", "min"))
    first_bar = out.index.to_series(index=days).groupby(level=0).min()

    weekday = pd.Series([pd.Timestamp(d).weekday() for d in daily.index], index=daily.index)
    valid = weekday < 5
    if len(daily):
        first_day = daily.index[0]
        if first_bar[first_day] - day_open(first_day, close_hour, tz) > MAX_FIRST_DAY_GAP:
            valid[first_day] = False

    prev = daily[valid].shift(1)
    out["pdh"] = out["trading_day"].map(prev["day_high"])
    out["pdl"] = out["trading_day"].map(prev["day_low"])
    return out

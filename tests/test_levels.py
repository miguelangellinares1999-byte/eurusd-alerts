import math

import pandas as pd

from detector.levels import add_daily_levels, trading_days


def _idx(*stamps):
    return pd.DatetimeIndex([pd.Timestamp(s, tz="UTC") for s in stamps])


def test_trading_day_boundary_winter():
    # Invierno: 17:00 NY = 22:00 UTC
    days = trading_days(_idx("2024-01-10 21:45", "2024-01-10 22:00"))
    assert [str(d) for d in days] == ["2024-01-10", "2024-01-11"]


def test_trading_day_boundary_summer():
    # Verano (EDT): 17:00 NY = 21:00 UTC
    days = trading_days(_idx("2024-07-10 20:45", "2024-07-10 21:00"))
    assert [str(d) for d in days] == ["2024-07-10", "2024-07-11"]


def test_close_hour_configurable():
    days = trading_days(_idx("2024-01-10 23:45", "2024-01-11 00:00"), close_hour=0, tz="UTC")
    assert [str(d) for d in days] == ["2024-01-10", "2024-01-11"]


def test_pdh_pdl_from_previous_day(short_candles):
    df = add_daily_levels(short_candles)
    first_day = df[df["trading_day"].astype(str) == "2024-01-10"]
    second_day = df[df["trading_day"].astype(str) == "2024-01-11"]
    assert first_day["pdh"].isna().all()
    assert (second_day["pdh"] == 1.1000).all()
    assert (second_day["pdl"] == 1.0900).all()


def _ohlc(idx, highs, lows):
    return pd.DataFrame({"open": 1.1, "high": highs, "low": lows, "close": 1.1}, index=idx)


def test_monday_uses_friday_levels_ignoring_weekend_stray_bars():
    idx = _idx(
        "2024-01-11 22:00",  # viernes (apertura del día)
        "2024-01-12 15:00",  # viernes
        "2024-01-12 22:15",  # vela suelta tras el cierre del viernes -> "sábado"
        "2024-01-14 22:00",  # domingo 17:00 NY = lunes
    )
    out = add_daily_levels(_ohlc(idx, [1.15, 1.2, 1.5, 1.15], [1.05, 1.0, 0.5, 1.05]))
    assert [str(d) for d in out["trading_day"]] == ["2024-01-12", "2024-01-12", "2024-01-13", "2024-01-15"]
    assert out["pdh"].iloc[3] == 1.2 and out["pdl"].iloc[3] == 1.0
    assert math.isnan(out["pdh"].iloc[2])  # sin sweeps en velas de fin de semana


def test_truncated_first_day_is_not_used():
    # Los datos empiezan a media sesión del día 10: su rango no es un PDH/PDL real.
    idx = _idx("2024-01-10 12:00", "2024-01-10 22:00")
    out = add_daily_levels(_ohlc(idx, [1.2, 1.15], [1.0, 1.05]))
    assert out["pdh"].isna().all()

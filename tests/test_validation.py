"""validate_candles: orden, duplicados, NaN, coherencia OHLC, vela en formación y huecos."""

import numpy as np
import pandas as pd
import pytest

from data.validation import CandleValidationError, Gap, find_gaps, validate_candles

NOW = pd.Timestamp("2026-01-01", tz="UTC")  # todas las velas de prueba están cerradas


def candles(start="2024-01-10 08:00", n=8, freq="15min", stamps=None):
    ts = pd.DatetimeIndex(stamps, tz="UTC") if stamps is not None else pd.date_range(start, periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({
        "timestamp": ts,
        "open": 1.1000, "high": 1.1010, "low": 1.0990, "close": 1.1005, "volume": 50.0,
    })


def test_clean_data_passes_unchanged():
    df = candles()
    out, report = validate_candles(df, 15, now=NOW)
    pd.testing.assert_frame_equal(out, df)
    assert report.gaps == [] and report.duplicates_removed == 0 and not report.was_unsorted
    assert report.rows_in == report.rows_out == 8


def test_unsorted_is_sorted():
    df = candles().iloc[[3, 0, 2, 1, 4, 5, 6, 7]]
    out, report = validate_candles(df, 15, now=NOW)
    assert out["timestamp"].is_monotonic_increasing and report.was_unsorted


def test_duplicates_removed_keeping_last():
    df = candles()
    dup = df.iloc[[2]].assign(close=1.1008)
    out, report = validate_candles(pd.concat([df, dup]), 15, now=NOW)
    assert len(out) == 8 and report.duplicates_removed == 1
    assert out["timestamp"].is_unique
    assert out.loc[out["timestamp"] == df["timestamp"][2], "close"].item() == 1.1008


def test_nan_fails():
    df = candles()
    df.loc[3, "close"] = np.nan
    with pytest.raises(CandleValidationError, match="1 vela\\(s\\) con NaN"):
        validate_candles(df, 15, now=NOW)


@pytest.mark.parametrize("col,value,msg", [
    ("high", 1.1003, "1 con high < max\\(open, close\\)"),  # high por debajo del close
    ("low", 1.1002, "1 con low > min\\(open, close\\)"),  # low por encima del open
])
def test_incoherent_ohlc_fails(col, value, msg):
    df = candles()
    df.loc[5, col] = value
    with pytest.raises(CandleValidationError, match=msg):
        validate_candles(df, 15, now=NOW)


def test_naive_timestamps_fail():
    df = candles()
    df["timestamp"] = df["timestamp"].dt.tz_localize(None)
    with pytest.raises(CandleValidationError, match="zona horaria"):
        validate_candles(df, 15, now=NOW)


def test_misaligned_timestamps_fail():
    df = candles()
    df["timestamp"] += pd.Timedelta(minutes=30 + 7)
    with pytest.raises(CandleValidationError, match="no alineadas"):
        validate_candles(df, 15, now=NOW)


def test_missing_columns_fail():
    with pytest.raises(CandleValidationError, match="volume"):
        validate_candles(candles().drop(columns="volume"), 15, now=NOW)


def test_forming_last_candle_dropped():
    df = candles()  # última vela abre a las 09:45
    out, report = validate_candles(df, 15, now=pd.Timestamp("2024-01-10 09:50", tz="UTC"))
    assert len(out) == 7 and report.dropped_forming
    out, report = validate_candles(df, 15, now=pd.Timestamp("2024-01-10 10:00", tz="UTC"))
    assert len(out) == 8 and not report.dropped_forming  # cerrada justo a las 10:00


def test_future_candles_before_last_fail():
    with pytest.raises(CandleValidationError, match="desfase horario"):
        validate_candles(candles(), 15, now=pd.Timestamp("2024-01-10 09:00", tz="UTC"))


def test_midweek_gap_reported():
    df = candles(n=12).drop(index=[4, 5, 6])  # faltan 09:00, 09:15, 09:30 del miércoles
    out, report = validate_candles(df, 15, now=NOW)
    assert report.gaps == [Gap(pd.Timestamp("2024-01-10 09:00", tz="UTC"), pd.Timestamp("2024-01-10 09:30", tz="UTC"), 3)]
    assert "2024-01-10 09:00 -> 2024-01-10 09:30 UTC (3 velas)" in report.summary()
    with pytest.raises(CandleValidationError, match="1 hueco"):
        validate_candles(df, 15, now=NOW, fail_on_gaps=True)


@pytest.mark.parametrize("friday_last,sunday_first", [
    ("2024-01-12 21:45", "2024-01-14 22:00"),  # invierno: cierre/apertura 22:00 UTC
    ("2024-07-12 20:45", "2024-07-14 21:00"),  # verano: 21:00 UTC
])
def test_weekend_is_not_a_gap(friday_last, sunday_first):
    friday = pd.date_range(end=friday_last, periods=4, freq="15min", tz="UTC")
    sunday = pd.date_range(sunday_first, periods=4, freq="15min", tz="UTC")
    _, report = validate_candles(candles(stamps=friday.append(sunday).tz_localize(None)), 15, now=NOW)
    assert report.gaps == []


def test_gap_around_weekend_is_split():
    # Faltan las últimas velas del viernes (16:00-16:45 EST) y las primeras del domingo (17:00-17:45 EST).
    friday = pd.date_range(end="2024-01-12 20:45", periods=4, freq="15min", tz="UTC")
    sunday = pd.date_range("2024-01-14 23:00", periods=4, freq="15min", tz="UTC")
    gaps = find_gaps(friday.append(sunday), 15)
    assert gaps == [
        Gap(pd.Timestamp("2024-01-12 21:00", tz="UTC"), pd.Timestamp("2024-01-12 21:45", tz="UTC"), 4),
        Gap(pd.Timestamp("2024-01-14 22:00", tz="UTC"), pd.Timestamp("2024-01-14 22:45", tz="UTC"), 4),
    ]


def test_full_week_without_gaps():
    utc = pd.date_range("2024-03-03 00:00", "2024-03-17 00:00", freq="15min", tz="UTC")  # cruza el cambio de hora
    ny = utc.tz_convert("America/New_York")
    open_ = ~(((ny.weekday == 4) & (ny.hour >= 17)) | (ny.weekday == 5) | ((ny.weekday == 6) & (ny.hour < 17)))
    _, report = validate_candles(candles(stamps=utc[open_].tz_localize(None)), 15, now=NOW)
    assert report.gaps == []

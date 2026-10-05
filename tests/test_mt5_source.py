"""MT5Source con el módulo MetaTrader5 simulado: conexión, símbolo, timeframes y zona horaria."""

from datetime import date, datetime, timezone

import pandas as pd
import pytest

from data.base import CANDLE_COLUMNS, to_engine_frame
from data.mt5_source import MT5Error, MT5Source, detect_offset, server_to_utc, utc_to_server
from detector.levels import add_daily_levels, trading_day_of, trading_days
from tests.fake_mt5 import FakeMT5, make_rates

UTC = timezone.utc


def source(fake, **kw):
    return MT5Source(kw.pop("symbol", "EURUSD"), mt5_module=fake, **kw)


# --------------------------------------------------------------- conexión y formato
def test_get_range_returns_utc_candle_columns():
    fake = FakeMT5(make_rates(["2024-01-10 12:00", "2024-01-10 12:15"]))
    df = source(fake, server_utc_offset_hours=2).get_range(
        "EURUSD", 15, datetime(2024, 1, 10, tzinfo=UTC), datetime(2024, 1, 11, tzinfo=UTC)
    )
    assert list(df.columns) == CANDLE_COLUMNS
    assert str(df["timestamp"].dt.tz) == "UTC"
    assert list(df["timestamp"]) == [pd.Timestamp("2024-01-10 10:00", tz="UTC"), pd.Timestamp("2024-01-10 10:15", tz="UTC")]
    assert list(df["volume"]) == [100.0, 100.0]


def test_get_range_requests_server_time_and_selects_symbol_first():
    fake = FakeMT5(make_rates(["2024-01-10 13:00"]))
    source(fake, server_utc_offset_hours=3).get_range(
        "EURUSD", "M15", datetime(2024, 1, 10, 10, tzinfo=UTC), datetime(2024, 1, 10, 12, tzinfo=UTC)
    )
    names = [c[0] for c in fake.calls]
    assert names == ["initialize", "symbol_select", "copy_rates_range", "shutdown"]
    _, symbol, tf, date_from, date_to = fake.calls[2]
    assert (symbol, tf) == ("EURUSD", FakeMT5.TIMEFRAME_M15)
    assert date_from == datetime(2024, 1, 10, 13, tzinfo=UTC)  # 10:00 UTC = 13:00 servidor
    assert date_to == datetime(2024, 1, 10, 15, tzinfo=UTC)


def test_get_last_uses_copy_rates_from_pos():
    fake = FakeMT5(make_rates(pd.date_range("2024-01-10 10:00", periods=10, freq="15min")))
    df = source(fake).get_last("EURUSD", 15, 3)
    assert len(df) == 3 and df["timestamp"].iloc[-1] == pd.Timestamp("2024-01-10 12:15", tz="UTC")
    assert ("copy_rates_from_pos", "EURUSD", FakeMT5.TIMEFRAME_M15, 0, 3) in fake.calls


@pytest.mark.parametrize("tf,const", [(15, "TIMEFRAME_M15"), ("M15", "TIMEFRAME_M15"), ("D1", "TIMEFRAME_D1"), (1440, "TIMEFRAME_D1")])
def test_timeframes_use_official_constants(tf, const):
    fake = FakeMT5()
    source(fake).get_last("EURUSD", tf, 1)
    assert fake.calls[2][2] == getattr(FakeMT5, const)


def test_unknown_timeframe_rejected():
    with pytest.raises(ValueError, match="no soportada"):
        source(FakeMT5()).get_last("EURUSD", "M7", 1)


def test_initialize_failure_explains_and_still_shuts_down():
    fake = FakeMT5(init_ok=False)
    with pytest.raises(MT5Error, match=r"IPC initialize failed.*Abre el terminal"):
        source(fake).get_last("EURUSD", 15, 10)
    assert fake.shutdowns == 1


def test_no_data_error_includes_last_error_and_shuts_down():
    fake = FakeMT5(symbols=["EURUSD"])
    fake.copy_rates_from_pos = lambda *a: None
    fake._error = (-2, "Terminal: Invalid params")
    with pytest.raises(MT5Error, match="Invalid params"):
        source(fake).get_last("EURUSD", 15, 10)
    assert fake.shutdowns == 1 and not fake.initialized


def test_missing_symbol_suggests_broker_names():
    fake = FakeMT5(symbols=["EURUSD.r", "EURUSDm", "GBPUSD"])
    with pytest.raises(MT5Error) as exc:
        source(fake).get_last("EURUSD", 15, 10)
    msg = str(exc.value)
    assert "no existe" in msg and "EURUSD.r" in msg and "EURUSDm" in msg and "GBPUSD" not in msg
    assert "config.yaml" in msg
    assert fake.shutdowns == 1


def test_symbol_with_suffix_works():
    fake = FakeMT5(make_rates(["2024-01-10 12:00"]), symbols=["EURUSD.r"])
    assert len(source(fake, symbol="EURUSD.r").get_last("EURUSD.r", 15, 5)) == 1


# --------------------------------------------------------------- desfase horario
def test_server_to_utc_fixed_offset():
    secs = [pd.Timestamp("2024-01-10 02:00").timestamp()]
    assert server_to_utc(secs, 2)[0] == pd.Timestamp("2024-01-10 00:00", tz="UTC")
    assert server_to_utc(secs, -5)[0] == pd.Timestamp("2024-01-10 07:00", tz="UTC")
    assert utc_to_server(datetime(2024, 1, 10, tzinfo=UTC), 2) == datetime(2024, 1, 10, 2, tzinfo=UTC)


def test_detect_offset_from_tick():
    now = pd.Timestamp("2026-09-30 12:00:00", tz="UTC")  # miércoles, mercado abierto
    tick = int(now.timestamp()) + 3 * 3600 - 4  # tick de hace 4 s en un servidor GMT+3
    assert detect_offset(tick, now) == 3.0
    assert detect_offset(int(now.timestamp()) - 2 * 3600, now) == -2.0


def test_detect_offset_rejects_stale_tick_and_closed_market():
    now = pd.Timestamp("2026-09-30 12:00:00", tz="UTC")
    assert detect_offset(int(now.timestamp()) + 3 * 3600 - 40 * 60, now) is None  # tick de hace 40 min
    saturday = pd.Timestamp("2026-10-03 12:00:00", tz="UTC")
    assert detect_offset(int(saturday.timestamp()) + 3 * 3600, saturday) is None


def test_auto_detect_offset_used_for_conversion(monkeypatch):
    now = pd.Timestamp.now(tz="UTC")
    monkeypatch.setattr("data.mt5_source.market_closed", lambda idx: [False])
    fake = FakeMT5(make_rates(["2024-01-10 13:00"]), tick_time=now.timestamp() + 3 * 3600)
    src = source(fake, server_utc_offset_hours=2, auto_detect_offset=True)
    df = src.get_last("EURUSD", 15, 1)
    assert src.last_offset_hours == 3.0
    assert df["timestamp"].iloc[0] == pd.Timestamp("2024-01-10 10:00", tz="UTC")


def test_auto_detect_falls_back_to_configured_offset(monkeypatch, caplog):
    monkeypatch.setattr("data.mt5_source.market_closed", lambda idx: [True])  # fin de semana
    fake = FakeMT5(make_rates(["2024-01-10 13:00"]), tick_time=pd.Timestamp.now(tz="UTC").timestamp())
    src = source(fake, server_utc_offset_hours=2, auto_detect_offset=True)
    df = src.get_last("EURUSD", 15, 1)
    assert src.last_offset_hours == 2.0
    assert df["timestamp"].iloc[0] == pd.Timestamp("2024-01-10 11:00", tz="UTC")
    assert "No se pudo estimar el desfase" in caplog.text


def test_server_timezone_takes_precedence_over_offset():
    fake = FakeMT5(make_rates(["2024-07-10 00:00"]))
    df = source(fake, server_utc_offset_hours=2, server_timezone="NY+7").get_last("EURUSD", 15, 1)
    assert df["timestamp"].iloc[0] == pd.Timestamp("2024-07-09 21:00", tz="UTC")  # GMT+3 en verano


# --------------------------------------------------------------- día de trading y cambio de hora
# Bróker NY+7: la medianoche del servidor es siempre las 17:00 de Nueva York.
DST_CASES = {
    # EE. UU. pasa a horario de verano el domingo 2024-03-10.
    "marzo": [
        ("2024-03-07 23:45", "2024-03-07 21:45", date(2024, 3, 7)),  # 16:45 EST
        ("2024-03-08 00:00", "2024-03-07 22:00", date(2024, 3, 8)),  # 17:00 EST -> día siguiente
        ("2024-03-08 23:45", "2024-03-08 21:45", date(2024, 3, 8)),  # última vela del viernes
        ("2024-03-11 00:00", "2024-03-10 21:00", date(2024, 3, 11)),  # apertura dom 17:00 EDT
        ("2024-03-11 23:45", "2024-03-11 20:45", date(2024, 3, 11)),  # 16:45 EDT
        ("2024-03-12 00:00", "2024-03-11 21:00", date(2024, 3, 12)),  # 17:00 EDT
    ],
    # EE. UU. vuelve a horario de invierno el domingo 2024-11-03.
    "noviembre": [
        ("2024-10-31 23:45", "2024-10-31 20:45", date(2024, 10, 31)),  # 16:45 EDT
        ("2024-11-01 00:00", "2024-10-31 21:00", date(2024, 11, 1)),  # 17:00 EDT
        ("2024-11-01 23:45", "2024-11-01 20:45", date(2024, 11, 1)),  # última vela del viernes
        ("2024-11-04 00:00", "2024-11-03 22:00", date(2024, 11, 4)),  # apertura dom 17:00 EST
        ("2024-11-04 23:45", "2024-11-04 21:45", date(2024, 11, 4)),  # 16:45 EST
        ("2024-11-05 00:00", "2024-11-04 22:00", date(2024, 11, 5)),  # 17:00 EST
    ],
}


@pytest.mark.parametrize("month", DST_CASES)
def test_trading_day_around_dst_change(month):
    server, utc, days = zip(*DST_CASES[month])
    fake = FakeMT5(make_rates(server))
    df = source(fake, server_timezone="NY+7").get_last("EURUSD", 15, 100)
    assert list(df["timestamp"]) == [pd.Timestamp(u, tz="UTC") for u in utc]
    assert list(trading_days(pd.DatetimeIndex(df["timestamp"]))) == list(days)
    assert [trading_day_of(t.to_pydatetime()) for t in df["timestamp"]] == list(days)


@pytest.mark.parametrize("utc,day", [
    ("2024-03-08 21:59", date(2024, 3, 8)), ("2024-03-08 22:00", date(2024, 3, 9)),  # invierno: 22:00 UTC
    ("2024-03-11 20:59", date(2024, 3, 11)), ("2024-03-11 21:00", date(2024, 3, 12)),  # verano: 21:00 UTC
    ("2024-11-01 20:59", date(2024, 11, 1)), ("2024-11-01 21:00", date(2024, 11, 2)),
    ("2024-11-04 21:59", date(2024, 11, 4)), ("2024-11-04 22:00", date(2024, 11, 5)),
])
def test_trading_day_of_boundaries(utc, day):
    assert trading_day_of(pd.Timestamp(utc, tz="UTC").to_pydatetime()) == day


def test_pdh_pdl_after_dst_weekend_use_friday():
    # Viernes 2024-03-08 completo (hora servidor NY+7) y lunes 2024-03-11 tras el cambio de hora.
    thursday = pd.date_range("2024-03-07 00:00", "2024-03-07 23:45", freq="15min")
    friday = pd.date_range("2024-03-08 00:00", "2024-03-08 23:45", freq="15min")
    monday = pd.date_range("2024-03-11 00:00", periods=8, freq="15min")
    times = thursday.append(friday).append(monday)
    ohlc = [(1.09, 1.0910, 1.0890, 1.09)] * len(thursday)
    ohlc += [(1.10, 1.1050 if i == 40 else 1.1010, 1.0950 if i == 70 else 1.0990, 1.10) for i in range(len(friday))]
    ohlc += [(1.10, 1.1020, 1.0980, 1.10)] * len(monday)
    fake = FakeMT5(make_rates(times, ohlc))
    df = add_daily_levels(to_engine_frame(source(fake, server_timezone="NY+7").get_last("EURUSD", 15, 1000)))
    mon = df[df["trading_day"] == date(2024, 3, 11)]
    assert len(mon) == 8 and mon.index[0] == pd.Timestamp("2024-03-10 21:00", tz="UTC")
    assert (mon["pdh"] == 1.1050).all() and (mon["pdl"] == 1.0950).all()
    fri = df[df["trading_day"] == date(2024, 3, 8)]
    assert (fri["pdh"] == 1.0910).all() and (fri["pdl"] == 1.0890).all()

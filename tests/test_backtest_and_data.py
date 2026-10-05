import pandas as pd
import pytest

from backtest import run_backtest
from data.csv_source import load_csv, resample_ohlc
from data.mt5_source import server_to_utc
from detector import DetectorConfig


@pytest.mark.parametrize("fixture,direction,level", [("short_candles", "short", "PDH"), ("long_candles", "long", "PDL")])
def test_backtest_lists_alert(request, fixture, direction, level):
    result = run_backtest(request.getfixturevalue(fixture), DetectorConfig())
    assert len(result.alerts) == 1
    row = result.alerts.iloc[0]
    assert row["direction"] == direction and row["swept_level"] == level
    assert "Alertas: 1" in result.summary()


def test_backtest_from_csv_roundtrip(tmp_path, short_candles):
    path = tmp_path / "eurusd.csv"
    short_candles.rename_axis("time").reset_index().assign(
        time=lambda d: d["time"].dt.strftime("%Y-%m-%d %H:%M:%S")
    ).to_csv(path, index=False)
    loaded = load_csv(path, tz="UTC")
    pd.testing.assert_frame_equal(loaded, short_candles, check_names=False, check_freq=False)
    assert len(run_backtest(loaded, DetectorConfig()).alerts) == 1


def test_load_mt5_export_format(tmp_path):
    path = tmp_path / "mt5.csv"
    path.write_text(
        "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\n"
        "2024.01.10\t10:00:00\t1.1\t1.2\t1.0\t1.15\t100\n"
        "2024.01.10\t10:15:00\t1.15\t1.25\t1.1\t1.2\t100\n"
    )
    df = load_csv(path)
    assert list(df.columns) == ["open", "high", "low", "close"]
    assert df.index[1] == pd.Timestamp("2024-01-10 10:15", tz="UTC")


def test_resample_from_5m():
    idx = pd.date_range("2024-01-10 10:00", periods=6, freq="5min", tz="UTC")
    df = pd.DataFrame(
        {"open": [1, 2, 3, 4, 5, 6], "high": [2, 5, 4, 5, 9, 7], "low": [0.5, 1, 2, 3, 4, 2], "close": [2, 3, 4, 5, 6, 7]},
        index=idx, dtype=float,
    )
    out = resample_ohlc(df, 15)
    assert out.iloc[0].tolist() == [1, 5, 0.5, 4]
    assert out.iloc[1].tolist() == [4, 9, 2, 7]


def test_mt5_server_time_ny_plus_7():
    # 2024-01-10 00:00 servidor (NY+7, invierno) = 17:00 NY = 22:00 UTC
    # 2024-07-10 00:00 servidor (NY+7, verano)  = 17:00 NY = 21:00 UTC
    secs = pd.Series([pd.Timestamp("2024-01-10").timestamp(), pd.Timestamp("2024-07-10").timestamp()])
    out = server_to_utc(secs, server_timezone="NY+7")
    assert list(out) == [pd.Timestamp("2024-01-09 22:00", tz="UTC"), pd.Timestamp("2024-07-09 21:00", tz="UTC")]
    assert server_to_utc(secs, server_timezone="UTC")[0] == pd.Timestamp("2024-01-10", tz="UTC")

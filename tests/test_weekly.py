import pandas as pd
import pytest

from backtest.weekly import equity_curve, find_signals, run_weekly, simulate, weekly_bars


def make_daily(rows, start="2024-01-01", weekends=False):
    """rows = [(open, high, low, close), ...] en días hábiles (o naturales si weekends)."""
    freq = "D" if weekends else "B"
    idx = pd.date_range(start, periods=len(rows), freq=freq, name="date")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx, dtype=float)


def flat_week(price, high=None, low=None):
    high = price + 1 if high is None else high
    low = price - 1 if low is None else low
    return [(price, high, low, price)] * 5


# Semana 1: rango 99-110. Semana 2: barre 99 (mínimo 95) y cierra en 100 -> largo, SL 95, TP 110.
SWEEP = [(105, 110, 99, 105)] * 5 + [(104, 104, 95, 100), (100, 101, 96, 100), (100, 101, 97, 100),
                                     (100, 101, 97, 100), (100, 101, 97, 100)]


def test_weekly_bars_and_signal():
    weeks = weekly_bars(make_daily(SWEEP))
    assert len(weeks) == 2
    assert weeks.iloc[1][["high", "low", "close"]].tolist() == [104, 95, 100]
    sig = find_signals(weeks)
    assert len(sig) == 1
    s = sig.iloc[0]
    assert (s.entry, s.stop_loss, s.take_profit) == (100, 95, 110)
    assert s.last_day == pd.Timestamp("2024-01-12")


def test_no_signal_if_close_below_previous_low_or_above_tp():
    below = SWEEP[:5] + [(104, 104, 95, 98)] * 5  # cierra por debajo del mínimo anterior
    assert find_signals(weekly_bars(make_daily(below))).empty
    above = SWEEP[:5] + [(104, 112, 95, 111)] * 5  # cierra por encima del TP
    assert find_signals(weekly_bars(make_daily(above))).empty


def test_incomplete_last_week_is_ignored():
    weeks = weekly_bars(make_daily(SWEEP[:8]))  # la semana 2 solo tiene 3 días
    assert len(weeks) == 1


@pytest.mark.parametrize(
    "after,result,exit_px",
    [
        ([(101, 111, 100, 110)], "win", 110),          # toca el TP
        ([(101, 102, 94, 96)], "loss", 95),            # toca el SL
        ([(101, 111, 94, 100)], "loss", 95),           # los dos el mismo día: pérdida
        ([(90, 92, 88, 91)], "loss", 90),              # hueco bajo el SL: sale a la apertura
        ([(112, 115, 111, 114)], "win", 112),          # hueco sobre el TP
        ([(101, 102, 99, 101)], "open", 101),          # sigue abierta
    ],
)
def test_simulate_exits(after, result, exit_px):
    daily = make_daily(SWEEP + after)
    trades = simulate("X", daily, find_signals(weekly_bars(daily)))
    assert len(trades) == 1
    t = trades[0]
    assert (t.result, t.exit_price) == (result, exit_px)
    if result == "win" and exit_px == 110:
        assert t.r == pytest.approx(2.0) and t.ret == pytest.approx(0.10)
    if exit_px == 90:
        assert t.r == pytest.approx(-2.0)  # el hueco cuesta más de 1R


def test_cost_reduces_r():
    daily = make_daily(SWEEP + [(101, 111, 100, 110)])
    t = simulate("X", daily, find_signals(weekly_bars(daily)), cost=0.01)[0]
    assert t.r == pytest.approx((110 - 100 - 1) / 5)


def test_one_position_per_ticker():
    # Semana 3 (98-104) y semana 4 barre 98 sin llegar al SL 95: señal con la operación abierta.
    week3 = [(102, 104, 98, 102)] * 5
    week4 = [(101, 101, 96, 100)] + [(100, 101, 99, 100)] * 4
    daily = make_daily(SWEEP + week3 + week4)
    weeks = weekly_bars(daily)
    assert len(find_signals(weeks)) == 2
    trades = simulate("X", daily, find_signals(weeks))
    assert len(trades) == 1 and trades[0].result == "open"


def test_equity_curve_tracks_trade():
    daily = make_daily(SWEEP + [(101, 105, 100, 104), (104, 111, 103, 110)] + flat_week(120))
    trades = simulate("X", daily, find_signals(weekly_bars(daily)))
    curve = equity_curve(daily, trades)
    assert curve.iloc[0] == 1.0
    assert curve[pd.Timestamp("2024-01-15")] == pytest.approx(1.04)
    assert curve.iloc[-1] == pytest.approx(1.10)  # fuera de mercado tras el TP


def test_crypto_weeks_end_on_sunday():
    rows = [(105, 110, 99, 105)] * 7 + [(104, 104, 95, 100)] + [(100, 101, 97, 100)] * 6
    daily = make_daily(rows, start="2024-01-01", weekends=True)  # lunes
    weeks = weekly_bars(daily)
    assert list(weeks.index.dayofweek) == [6, 6]
    assert len(find_signals(weeks)) == 1


def test_run_weekly_summary_and_portfolio():
    daily = make_daily(SWEEP + [(101, 111, 100, 110)] + flat_week(110) * 5)
    bench = make_daily([(100, 101, 99, 100)] * len(daily))
    res = run_weekly({"AAA": daily, "BBB": daily}, bench)
    assert list(res.per_ticker["ticker"]) == ["AAA", "BBB"]
    row = res.per_ticker.iloc[0]
    assert row["trades"] == 1 and row["winrate"] == 100 and row["total_r"] == pytest.approx(2.0)
    assert row["estr_return_pct"] == pytest.approx(10.0)
    assert row["bh_return_pct"] == pytest.approx(110 / 105 * 100 - 100)
    assert len(res.portfolio) == 3
    assert res.portfolio.iloc[0]["return_pct"] == pytest.approx(10.0)
    assert res.by_year.iloc[0]["trades"] == 2

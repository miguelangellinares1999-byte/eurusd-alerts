import numpy as np
import pandas as pd
import pytest

from backtest.index_strategy import Exit, curve_stats, equity_curve, kelly, manage, mfe_r, monte_carlo, rotate
from backtest.weekly import WeeklyTrade


def daily(rows, start="2024-01-01"):
    idx = pd.date_range(start, periods=len(rows), freq="B", name="date")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx, dtype=float)


# Señal el viernes 2024-01-05: entrada 100, SL 95 (riesgo 5). Luego sube 2 por semana.
BASE = [(100, 101, 95, 100)] * 5
UP = [(100 + 2 * w, 101 + 2 * w, 99 + 2 * w, 100 + 2 * w) for w in range(1, 12) for _ in range(5)]
SIG = pd.DataFrame({"last_day": [pd.Timestamp("2024-01-05")], "entry": [100.0], "stop_loss": [95.0], "take_profit": [104.0]})


@pytest.mark.parametrize(
    "rule,exit_px,result",
    [
        (Exit("estructura"), 104, "win"),
        (Exit("2R", 2.0), 110, "win"),
        (Exit("2 semanas", None, 2), 104, "win"),  # cierre del día que se cumplen 2 semanas
    ],
)
def test_manage_exits(rule, exit_px, result):
    t = manage("X", daily(BASE + UP), SIG, rule)[0]
    assert (t.exit_price, t.result) == (exit_px, result)


def test_stop_loss_and_gap():
    down = [(99, 99, 96, 97)] * 3 + [(90, 91, 89, 90)]
    t = manage("X", daily(BASE + down), SIG, Exit("2R", 2.0))[0]
    assert t.result == "loss" and t.exit_price == 90 and t.r == pytest.approx(-2.0)


def test_trailing_exits_on_close_below_previous_week_low():
    week2 = [(104, 105, 103, 104)] * 5      # semana fuerte: mínimo 103
    week3 = [(103, 103, 100, 101)] * 5      # cierra 101 < 103 -> fuera al cierre del viernes
    t = manage("X", daily(BASE + [(102, 103, 101, 102)] * 5 + week2 + week3), SIG, Exit("trail", None, None, True))[0]
    assert t.exit_price == 101 and t.exit_date == pd.Timestamp("2024-01-26")


def test_open_trade_and_one_position_at_a_time():
    sigs = pd.concat([SIG, SIG.assign(last_day=pd.Timestamp("2024-01-12"))], ignore_index=True)
    trades = manage("X", daily(BASE + UP[:10]), sigs, Exit("sin TP", None))
    assert len(trades) == 1 and trades[0].result == "open"


def test_mfe_stops_at_stop_loss():
    rows = BASE + [(100, 110, 99, 105)] + [(100, 100, 94, 95)] + [(95, 130, 95, 130)]
    assert mfe_r(daily(rows), SIG).iloc[0] == pytest.approx(2.0)


def trade(day, exit_day, r, ticker="X"):
    entry, sl = 100.0, 95.0
    return WeeklyTrade(ticker, pd.Timestamp(day), entry, sl, 0, "win" if r > 0 else "loss", pd.Timestamp(exit_day), entry + r * 5)


def test_equity_curve_compounds_and_sizes_at_entry():
    trades = [trade("2024-01-05", "2024-02-02", 2), trade("2024-01-12", "2024-01-19", -1)]
    curve = equity_curve(trades, 0.01)
    # Las dos se abren con capital 1: -1 % y luego +2 %.
    assert curve.tolist() == pytest.approx([0.99, 1.01])
    st = curve_stats(curve, pd.Timestamp("2024-01-01"), pd.Timestamp("2024-02-02"))
    assert st["max_dd_pct"] == pytest.approx(1.0)


def test_kelly_and_monte_carlo():
    rs = np.array([2.0, -1.0] * 50)  # p=0.5, b=2 -> Kelly 25 %
    assert kelly(rs) == pytest.approx(0.25, abs=0.01)
    mc = monte_carlo(rs, 10, 5, [0.01, 0.05], sims=500)
    assert list(mc["riesgo_pct"]) == [1, 5]
    assert mc["dd_p95_pct"].iloc[1] > mc["dd_p95_pct"].iloc[0]


def test_rotate_picks_by_score_and_keeps_solo_signals():
    a = [trade("2024-01-05", "2024-01-19", 1, "A"), trade("2024-03-01", "2024-03-15", 1, "A")]
    b = [trade("2024-01-05", "2024-01-19", -1, "B"), trade("2024-05-03", "2024-05-17", 1, "B")]
    score = pd.DataFrame({"A": [1.0], "B": [2.0]}, index=[pd.Timestamp("2024-01-05")])
    picked = rotate({"A": a, "B": b}, score, "high")
    assert [t.ticker for t in picked] == ["B", "A", "B"]
    assert [t.ticker for t in rotate({"A": a, "B": b}, score, "low")] == ["A", "A", "B"]

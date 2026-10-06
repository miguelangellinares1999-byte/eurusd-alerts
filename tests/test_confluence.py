import pandas as pd
import pytest

from backtest.confluence import (
    Variant, index_manipulation, market_frame, run_variants, select, smt_signals, smt_trades, stock_signals,
)
from backtest.market import breadth, load_daily


def daily(rows, start="2024-01-01"):
    idx = pd.date_range(start, periods=len(rows), freq="B", name="date")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx, dtype=float)


def week(o, h, l, c):
    return [(o, h, l, c)] * 5


# 4 semanas de rango 100-110 y una quinta que barre el mínimo de las cuatro (95) y cierra en 102.
RANGE = week(105, 110, 100, 105) * 4 + [(104, 104, 95, 100)] + [(100, 103, 96, 102)] * 4


def test_range_sweep_signal_and_tp():
    sig = stock_signals(daily(RANGE), range_weeks=4)
    last = sig.iloc[-1]
    assert bool(last.rng) and bool(last.pwl)
    assert (last.entry, last.stop_loss, last.tp_rng) == (102, 95, 110)


def test_pml_sweep_uses_previous_month():
    # Enero entero en 100-110, primera semana de febrero barre 100 y cierra encima.
    jan = daily([(105, 110, 100, 105)] * 23, start="2024-01-01")
    feb = daily([(104, 104, 98, 103)] + [(103, 104, 101, 103)] * 4, start="2024-02-05")
    sig = stock_signals(pd.concat([jan, feb]))
    first_feb = sig.loc["2024-02-09"]
    assert bool(first_feb.pml) and first_feb.tp_pml == 110


def test_select_applies_market_filters_and_min_risk():
    d = daily(RANGE)
    sig = stock_signals(d)
    week_end = sig.index[-1]
    market = pd.DataFrame({"idx_spx": True, "idx_ndx": True, "breadth_min": 15.0, "vix": 25.0, "move_down": True}, index=[week_end])
    assert len(select(sig, market, Variant("x", ("rng",), True, 20))) == 1
    assert select(sig, market, Variant("x", ("rng",), True, 20, vix_below=20)).empty
    assert select(sig, market, Variant("x", ("rng",), breadth_below=10)).empty
    assert select(sig, market, Variant("x", ("rng",), min_risk=0.1)).empty  # riesgo 7/102 < 10 %


def test_run_variants_trades_to_target():
    # 20 semanas de rango (run_variants ignora activos con menos de 120 velas) y luego la barrida.
    long = daily(week(105, 110, 100, 105) * 20 + RANGE + [(103, 111, 102, 110)], start="2023-01-02")
    market = pd.DataFrame(index=stock_signals(long).index).assign(idx_spx=True, idx_ndx=True)
    t = run_variants({"AAA": long}, market, [Variant("acum", ("rng",), True)])[0].trades
    assert len(t) == 1 and t[0].result == "win" and t[0].take_profit == 110


def test_index_manipulation_flags_sweep_week_and_next():
    d = daily(week(105, 110, 100, 105) + [(104, 104, 98, 102)] * 5 + week(103, 106, 101, 104) * 2)
    flags = index_manipulation(d)
    assert flags.tolist() == [False, True, True, False]


def test_market_frame_breadth_takes_min_of_two_weeks():
    d = daily(week(105, 110, 100, 105) * 3)
    b = pd.Series([50.0] * 5 + [10.0] + [50.0] * 9, index=d.index)
    m = market_frame(d, d, breadth=b)
    assert m["breadth_min"].tolist()[1:] == [10.0, 10.0]


def test_breadth_counts_share_above_average():
    up = daily([(i, i, i, i) for i in range(1, 80)])
    down = daily([(i, i, i, i) for i in range(80, 1, -1)])
    many = {f"U{i}": up for i in range(15)} | {f"D{i}": down for i in range(5)}
    assert breadth(many, window=10).iloc[-1] == pytest.approx(75.0)


def test_smt_buys_lagger_or_sweeper():
    base = week(105, 110, 100, 105)
    a = daily(base + [(104, 106, 98, 104)] * 5 + [(104, 111, 103, 110)] * 5)   # a barre 100
    b = daily(base + [(104, 106, 101, 104)] * 5 + [(104, 111, 103, 110)] * 5)  # b no barre
    sig = smt_signals(a, b)
    assert len(sig) == 1 and bool(sig.iloc[0].swept_a) and not bool(sig.iloc[0].swept_b)
    data = {"A": a, "B": b}
    lag = smt_trades(data, "A", "B", buy="lagger", min_risk=0)
    assert [(t.ticker, t.result, t.stop_loss) for t in lag] == [("B", "win", 101)]
    sw = smt_trades(data, "A", "B", buy="sweeper", min_risk=0)
    assert [(t.ticker, t.result, t.stop_loss) for t in sw] == [("A", "win", 98)]


def test_load_daily_uses_cache(tmp_path):
    calls = []

    def fetch(ticker, start):
        calls.append(ticker)
        return daily(week(1, 2, 0.5, 1.5)).rename_axis("date")

    first = load_daily(["AAA"], "2024-01-01", tmp_path, fetch=fetch)
    second = load_daily(["AAA"], "2024-01-01", tmp_path, fetch=fetch)
    assert calls == ["AAA"]
    pd.testing.assert_frame_equal(first["AAA"], second["AAA"], check_freq=False)

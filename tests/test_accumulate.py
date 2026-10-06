import pandas as pd
import pytest

from backtest.accumulate import Plan, contribution_days, irr, simulate


def series(values, start="2024-01-01"):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq="B"), dtype=float)


def test_contribution_days_first_of_each_month():
    days = contribution_days(series([1.0] * 70).index)
    assert list(days.strftime("%Y-%m-%d")) == ["2024-01-01", "2024-02-01", "2024-03-01", "2024-04-01"]


def test_irr_recovers_known_rate():
    flows = [(pd.Timestamp("2020-01-01"), -100.0)]
    assert irr(flows, pd.Timestamp("2022-01-01"), 121.0) == pytest.approx(0.10, abs=0.002)


def test_dca_buys_each_month():
    tr = series([100.0] * 22 + [200.0] * 22)  # enero a 100, febrero a 200
    res = simulate(tr, Plan("dca"), amount=50)
    assert res.contributed.iloc[-1] == 100
    assert res.value.iloc[-1] == pytest.approx(50 * 2 + 50)


def test_signals_wait_in_cash_with_interest_then_buy():
    tr = series([100.0] * 30 + [50.0] * 30)
    signal = tr.index[40]
    rate = pd.Series(0.0, index=tr.index)
    res = simulate(tr, Plan("sig", "signals"), amount=50, rate=rate, signals=pd.DatetimeIndex([signal]))
    # Tres aportaciones (ene, feb, mar) en liquidez y todo se compra a 50 el día de la señal.
    assert res.value[signal] == pytest.approx(100)
    assert res.value.iloc[-1] == pytest.approx(150)


def test_cfd_pays_financing_and_doubles_moves():
    tr = series([100.0] * 5 + [110.0] * 5)
    rate = pd.Series(0.0, index=tr.index)
    res = simulate(tr, Plan("2x", leverage=2, markup=0.0), amount=50, rate=rate)
    assert res.value.iloc[-1] == pytest.approx(50 + 2 * 50 * 0.10)
    costly = simulate(tr, Plan("2x", leverage=2, markup=36.5), amount=50, rate=rate)
    assert costly.value.iloc[-1] < res.value.iloc[-1]


def test_stop_out_closes_position():
    tr = series([100.0] * 3 + [60.0] * 3)
    res = simulate(tr, Plan("3x", leverage=3, markup=0.0), amount=50, rate=pd.Series(0.0, index=tr.index))
    assert len(res.stop_outs) == 1
    assert res.value.iloc[-1] == 0  # 50 - 3*50*0.4 < 0 -> se pierde todo


def test_rebalance_resets_leverage_on_each_contribution():
    # Mes 1 a 100, mes 2 a 50 (-50 %), mes 3 a 100 (+100 %).
    days = pd.bdate_range("2024-01-01", "2024-03-29")
    tr = pd.Series(days.month.map({1: 100.0, 2: 50.0, 3: 100.0}), index=days, dtype=float)
    zero = pd.Series(0.0, index=tr.index)
    plain = simulate(tr, Plan("1.5x", leverage=1.5, markup=0.0), 50, zero)
    reb = simulate(tr, Plan("1.5x", leverage=1.5, markup=0.0, rebalance=True), 50, zero)
    # Tras la caída: 50 - 0.5*75 = 12.5, +50 = 62.5 en los dos.
    assert plain.value.iloc[30] == pytest.approx(62.5) and reb.value.iloc[30] == pytest.approx(62.5)
    # Sin reajustar el nocional es 37.5 + 75 = 112.5; reajustado, 1.5 * 62.5 = 93.75.
    assert plain.value.iloc[-1] == pytest.approx(62.5 + 112.5 + 50)
    assert reb.value.iloc[-1] == pytest.approx(62.5 + 93.75 + 50)


def test_tactical_trades_add_risked_fraction():
    tr = series([100.0] * 44)
    trades = [(tr.index[2], tr.index[10], 3.0)]
    res = simulate(tr, Plan("dca"), 50, trades=trades, trade_risk=0.02)
    assert res.value.iloc[-1] == pytest.approx(100 + 0.02 * 50 * 3)

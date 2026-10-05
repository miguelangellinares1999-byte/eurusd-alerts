import numpy as np
import pandas as pd
import pytest

from backtest.compare import compare
from backtest.trades import Simulator, summarize, trading_day_end
from detector import DetectorConfig
from detector.chartz import ChartzConfig, detect_signals, htf_structure, pivots
from detector.swings import swing_highs, swing_lows
from settings import load_config
from tests.conftest import make_df


def random_walk(n, freq="1min", start="2024-01-08 00:00", seed=1):
    rng = np.random.default_rng(seed)
    close = 1.10 + np.cumsum(rng.normal(0, 0.00012, n))
    open_ = np.concatenate([[close[0]], close[:-1]])
    wick = np.abs(rng.normal(0, 0.00008, (2, n)))
    idx = pd.date_range(pd.Timestamp(start, tz="UTC"), periods=n, freq=freq)
    return pd.DataFrame(
        {"open": open_, "high": np.maximum(open_, close) + wick[0], "low": np.minimum(open_, close) - wick[1], "close": close},
        index=idx,
    )


@pytest.mark.parametrize("n", [1, 2, 3])
def test_pivots_match_swings(n):
    df = random_walk(400, seed=n)
    assert list(np.flatnonzero(pivots(df["high"].to_numpy(), n, True))) == swing_highs(list(df["high"]), n)
    assert list(np.flatnonzero(pivots(df["low"].to_numpy(), n, False))) == swing_lows(list(df["low"]), n)


def test_htf_structure_bos_and_tr():
    bars = [
        (1.098, 1.100, 1.097, 1.099),
        (1.10, 1.105, 1.099, 1.104),
        (1.104, 1.110, 1.103, 1.108),  # 2 swing high 1.110
        (1.108, 1.109, 1.100, 1.101),
        (1.101, 1.102, 1.095, 1.096),  # 4 swing low 1.095 (strong low)
        (1.096, 1.104, 1.096, 1.103),
        (1.103, 1.108, 1.102, 1.107),
        (1.107, 1.115, 1.106, 1.114),  # 7 BOS: cierre > 1.110 -> alcista
        (1.114, 1.120, 1.113, 1.118),  # 8 máximo del impulso
        (1.118, 1.119, 1.112, 1.113),
        (1.113, 1.114, 1.108, 1.109),  # 10 confirma el pivote de 8 (n=2) -> TR definido
        (1.109, 1.110, 1.090, 1.092),  # 11 cierre < strong low -> CDC bajista
    ]
    st = htf_structure(make_df(bars, freq="60min"), 2, 60)
    assert list(st["trend"].iloc[6:8]) == [0, 1]
    assert not st["defined"].iat[9]
    assert st["defined"].iat[10]
    assert st["tr_high"].iat[10] == pytest.approx(1.120)
    assert st["tr_low"].iat[10] == pytest.approx(1.095)
    assert st["trend"].iat[11] == -1
    # el índice es la hora de cierre de la vela
    assert st.index[0] == make_df(bars, freq="60min").index[0] + pd.Timedelta(minutes=60)


def _sim(bars):
    return Simulator(make_df(bars, start="2024-01-10 12:00", freq="1min"))


T0 = pd.Timestamp("2024-01-10 12:00", tz="UTC")


def test_trade_win_loss_and_no_fill():
    # long: entrada 1.1000, SL 1.0990, TP 1.1020
    win = _sim([(1.1005, 1.1006, 1.0999, 1.1001), (1.1001, 1.1021, 1.1000, 1.1020)])
    assert win.run(T0, "long", 1.1000, 1.0990, 1.1020).result == "win"
    loss = _sim([(1.1005, 1.1006, 1.0999, 1.1001), (1.1001, 1.1002, 1.0989, 1.0990)])
    assert loss.run(T0, "long", 1.1000, 1.0990, 1.1020).r == -1.0
    no_fill = _sim([(1.1005, 1.1021, 1.1004, 1.1020), (1.1020, 1.1020, 1.0995, 1.0996)])
    assert no_fill.run(T0, "long", 1.1000, 1.0990, 1.1020).result == "no_fill"


def test_same_bar_sl_and_tp_counts_as_loss_and_short_mirror():
    both = _sim([(1.1005, 1.1006, 1.0999, 1.1001), (1.1001, 1.1025, 1.0985, 1.1000)])
    assert both.run(T0, "long", 1.1000, 1.0990, 1.1020).result == "loss"
    short = _sim([(1.0995, 1.1001, 1.0994, 1.0999), (1.0999, 1.1000, 1.0979, 1.0980)])
    assert short.run(T0, "short", 1.1000, 1.1010, 1.0980).result == "win"


def test_spread_needs_ask_to_reach_entry():
    bars = [(1.1005, 1.1006, 1.0999, 1.1001), (1.1001, 1.1021, 1.1000, 1.1020)]
    sim = Simulator(make_df(bars, start="2024-01-10 12:00", freq="1min"), spread=0.0002)
    assert sim.run(T0, "long", 1.1000, 1.0990, 1.1020).result == "no_fill"


def test_order_cancelled_at_trading_day_end():
    assert trading_day_end(pd.Timestamp("2024-01-10 21:30", tz="UTC")) == pd.Timestamp("2024-01-10 22:00", tz="UTC")
    assert trading_day_end(pd.Timestamp("2024-07-10 21:30", tz="UTC")) == pd.Timestamp("2024-07-11 21:00", tz="UTC")


def test_summarize_drawdown_and_streak():
    sim = _sim([(1.1, 1.1, 1.1, 1.1)])
    trades = [
        sim.run(T0, "long", 1.1, 1.09, 1.12),  # se queda abierta: no cuenta
    ]
    assert summarize(trades)["trades"] == 0


def test_signals_are_causal_and_consistent():
    df = random_walk(6 * 24 * 60 // 5 * 5, freq="5min", seed=7)
    cfg = ChartzConfig(killzones=[])
    full = detect_signals(df, cfg)
    for s in full:
        assert s.stop_loss < s.entry if s.direction.value == "long" else s.stop_loss > s.entry
        assert s.tr_low <= s.tr_high
        # recortando los datos en el momento de la señal se obtiene la misma señal
        cut = detect_signals(df[df.index < s.time], cfg)
        assert any(c.time == s.time and c.entry == s.entry for c in cut)


def test_compare_runs_on_m1():
    m1 = random_walk(5 * 24 * 60, seed=3)
    table, trades = compare(m1, m1.index[0], m1.index[-1], DetectorConfig(), ChartzConfig(killzones=[]))
    assert set(table["strategy"]) <= {"PDH/PDL", "Chartz"}
    assert set(trades) == {"PDH/PDL", "Chartz"}


def test_config_loads_chartz_section():
    cfg = load_config()
    assert cfg.chartz.ltf_minutes in (1, 5)
    assert {k.name for k in cfg.chartz.killzones} == {"london", "newyork"}

from detector.models import Direction
from detector.sweep import detect_sweeps, is_pdh_sweep, is_pdl_sweep

PDH, PDL = 1.1000, 1.0900


class TestBearishSweepOfPDH:
    def test_wick_above_and_close_back_inside(self):
        assert is_pdh_sweep(high=1.1010, close=1.0990, pdh=PDH)

    def test_close_above_is_breakout_not_sweep(self):
        assert not is_pdh_sweep(high=1.1010, close=1.1005, pdh=PDH)

    def test_close_exactly_at_level_is_not_back_inside(self):
        assert not is_pdh_sweep(high=1.1010, close=PDH, pdh=PDH)

    def test_no_wick_above(self):
        assert not is_pdh_sweep(high=1.0999, close=1.0990, pdh=PDH)

    def test_detect_returns_short_with_sl_at_wick(self):
        assert detect_sweeps(1.1010, 1.0980, 1.0990, PDH, PDL) == [("PDH", Direction.SHORT, PDH, 1.1010)]


class TestBullishSweepOfPDL:
    def test_wick_below_and_close_back_inside(self):
        assert is_pdl_sweep(low=1.0890, close=1.0910, pdl=PDL)

    def test_close_below_is_breakout_not_sweep(self):
        assert not is_pdl_sweep(low=1.0890, close=1.0895, pdl=PDL)

    def test_no_wick_below(self):
        assert not is_pdl_sweep(low=1.0901, close=1.0910, pdl=PDL)

    def test_detect_returns_long_with_sl_at_wick(self):
        assert detect_sweeps(1.0920, 1.0890, 1.0910, PDH, PDL) == [("PDL", Direction.LONG, PDL, 1.0890)]


def test_missing_levels_never_sweep():
    nan = float("nan")
    assert detect_sweeps(2.0, 0.5, 1.0, nan, nan) == []


def test_outside_bar_sweeps_both():
    result = detect_sweeps(1.1010, 1.0890, 1.0950, PDH, PDL)
    assert [r[0] for r in result] == ["PDH", "PDL"]

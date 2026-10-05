import pytest

from detector.fvg import find_fvgs, fvg_at
from detector.models import Direction
from tests.conftest import make_df


class TestBearishFVG:
    bars = [
        (1.0990, 1.1000, 1.0980, 1.0985),  # vela 1: low 1.0980
        (1.0985, 1.0986, 1.0940, 1.0945),  # desplazamiento
        (1.0945, 1.0960, 1.0930, 1.0935),  # vela 3: high 1.0960 < 1.0980
    ]

    def test_detects_gap(self):
        fvg = fvg_at(make_df(self.bars), 2, Direction.SHORT)
        assert fvg is not None
        assert (fvg.low, fvg.high) == (1.0960, 1.0980)
        assert fvg.size == pytest.approx(0.0020)

    def test_wrong_direction_returns_none(self):
        assert fvg_at(make_df(self.bars), 2, Direction.LONG) is None

    def test_overlapping_candles_no_gap(self):
        bars = [b for b in self.bars]
        bars[2] = (1.0945, 1.0985, 1.0930, 1.0935)  # high 1.0985 >= low1 1.0980
        assert fvg_at(make_df(bars), 2, Direction.SHORT) is None

    def test_min_size_filter(self):
        assert fvg_at(make_df(self.bars), 2, Direction.SHORT, min_size=0.0025) is None


class TestBullishFVG:
    bars = [
        (1.0900, 1.0920, 1.0895, 1.0915),  # vela 1: high 1.0920
        (1.0915, 1.0965, 1.0912, 1.0960),
        (1.0960, 1.0975, 1.0940, 1.0970),  # vela 3: low 1.0940 > 1.0920
    ]

    def test_detects_gap(self):
        fvg = fvg_at(make_df(self.bars), 2, Direction.LONG)
        assert (fvg.low, fvg.high) == (1.0920, 1.0940)
        assert fvg.mid == pytest.approx(1.0930)

    def test_equal_high_low_is_not_gap(self):
        bars = list(self.bars)
        bars[2] = (1.0960, 1.0975, 1.0920, 1.0970)
        assert fvg_at(make_df(bars), 2, Direction.LONG) is None

    def test_find_respects_leg_bounds(self):
        df = make_df([(1.09, 1.09, 1.09, 1.09)] * 2 + self.bars)
        assert len(find_fvgs(df, start=2, end=4, direction=Direction.LONG)) == 1
        # Si la pierna empieza en la vela 2 del patrón, no hay FVG completo dentro.
        assert find_fvgs(df, start=3, end=4, direction=Direction.LONG) == []
        assert find_fvgs(df, start=2, end=3, direction=Direction.LONG) == []


def test_index_bounds():
    df = make_df(TestBullishFVG.bars)
    assert fvg_at(df, 1, Direction.LONG) is None
    assert fvg_at(df, 5, Direction.LONG) is None

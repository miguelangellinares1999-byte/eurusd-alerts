import numpy as np

from detector.choch import choch_reference, is_choch
from detector.models import Direction
from tests.conftest import SHORT_DAY, mirror


def _arrays(bars):
    a = np.array(bars)
    return a[:, 1], a[:, 2]


class TestBearishCHoCH:
    def test_reference_is_last_swing_low_before_sweep(self):
        highs, lows = _arrays(SHORT_DAY)
        assert choch_reference(highs, lows, sweep_idx=5, direction=Direction.SHORT) == (2, 1.0930)

    def test_body_close_below_triggers(self):
        assert is_choch(close=1.0925, level=1.0930, direction=Direction.SHORT)

    def test_wick_below_but_close_above_does_not_trigger(self):
        # La vela puede tener low 1.0920, pero cuenta el cierre.
        assert not is_choch(close=1.0935, level=1.0930, direction=Direction.SHORT)

    def test_close_equal_to_level_does_not_trigger(self):
        assert not is_choch(close=1.0930, level=1.0930, direction=Direction.SHORT)


class TestBullishCHoCH:
    def test_reference_is_last_swing_high_before_sweep(self):
        highs, lows = _arrays(mirror(SHORT_DAY))
        idx, level = choch_reference(highs, lows, sweep_idx=5, direction=Direction.LONG)
        assert idx == 2 and round(level, 5) == 1.0970

    def test_body_close_above_triggers(self):
        assert is_choch(close=1.0975, level=1.0970, direction=Direction.LONG)

    def test_close_below_does_not_trigger(self):
        assert not is_choch(close=1.0965, level=1.0970, direction=Direction.LONG)


def test_no_reference_without_confirmed_swing():
    highs = [1.0, 1.1, 1.2, 1.3]
    lows = [0.9, 1.0, 1.1, 1.2]
    assert choch_reference(highs, lows, 3, Direction.SHORT) is None
    assert choch_reference(highs, lows, 3, Direction.LONG) is None

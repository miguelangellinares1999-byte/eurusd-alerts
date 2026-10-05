from detector.swings import last_confirmed_swing, swing_highs, swing_lows

HIGHS = [1.0, 1.2, 1.5, 1.3, 1.1, 1.4, 1.35, 1.8, 1.7, 1.6]
LOWS = [1.5, 1.3, 1.0, 1.2, 1.4, 1.1, 0.9, 1.0, 1.2, 1.3]


class TestSwingHighs:
    def test_pivot_with_two_bars_each_side(self):
        assert swing_highs(HIGHS, n=2) == [2, 7]

    def test_n_configurable(self):
        # 1.4 (i=5) solo es pivote con 1 vela a cada lado.
        assert swing_highs(HIGHS, n=1) == [2, 5, 7]

    def test_equal_highs_are_not_pivot(self):
        assert swing_highs([1.0, 1.1, 1.5, 1.5, 1.1, 1.0], n=2) == []

    def test_last_confirmed_needs_n_bars_after(self):
        # El pivote de 1.8 (i=7) con n=2 necesita las velas 8 y 9.
        highs = [1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.8, 1.7, 1.6]
        assert last_confirmed_swing(highs, upto=8, n=2, high=True) is None
        assert last_confirmed_swing(highs, upto=9, n=2, high=True) == 7

    def test_no_lookahead(self):
        # Hasta la vela 3 el pivote de i=2 aún no está confirmado.
        assert last_confirmed_swing(HIGHS, upto=3, n=2, high=True) is None
        assert last_confirmed_swing(HIGHS, upto=4, n=2, high=True) == 2


class TestSwingLows:
    def test_pivot_with_two_bars_each_side(self):
        assert swing_lows(LOWS, n=2) == [2, 6]

    def test_n_configurable(self):
        assert swing_lows(LOWS, n=3) == [6]

    def test_last_confirmed_returns_most_recent(self):
        assert last_confirmed_swing(LOWS, upto=9, n=2, high=False) == 6
        assert last_confirmed_swing(LOWS, upto=7, n=2, high=False) == 2

    def test_none_when_no_pivot(self):
        assert last_confirmed_swing([1.0, 0.9, 0.8, 0.7], upto=3, n=1, high=False) is None

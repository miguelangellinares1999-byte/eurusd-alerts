import pandas as pd
import pytest

MID = 1.0950  # eje para reflejar escenarios bajistas en alcistas


def make_df(bars, start="2024-01-10 08:00", freq="15min"):
    """DataFrame OHLC con índice UTC a partir de tuplas (open, high, low, close)."""
    idx = pd.date_range(pd.Timestamp(start, tz="UTC"), periods=len(bars), freq=freq)
    return pd.DataFrame(bars, columns=["open", "high", "low", "close"], index=idx)


def mirror(bars, mid=MID):
    """Refleja velas respecto a `mid`: un escenario bajista pasa a ser alcista."""
    return [
        (round(2 * mid - o, 5), round(2 * mid - l, 5), round(2 * mid - h, 5), round(2 * mid - c, 5))
        for o, h, l, c in bars
    ]


# Día de trading 2024-01-10 (de 2024-01-09 22:00 UTC a 2024-01-10 22:00 UTC, invierno):
# PDH = 1.1000, PDL = 1.0900 para el día siguiente.
PREV_DAY = [
    (1.0950, 1.1000, 1.0940, 1.0960),
    (1.0960, 1.0970, 1.0900, 1.0920),
    (1.0920, 1.0950, 1.0910, 1.0945),
]
PREV_DAY_START = "2024-01-09 22:00"  # apertura del día (17:00 NY)

# Día 2024-01-11 (empieza 2024-01-10 22:00 UTC). Escenario bajista completo:
SHORT_DAY = [
    (1.0950, 1.0960, 1.0940, 1.0955),  # 0
    (1.0955, 1.0965, 1.0945, 1.0960),  # 1
    (1.0960, 1.0962, 1.0930, 1.0950),  # 2 swing low 1.0930
    (1.0950, 1.0970, 1.0945, 1.0965),  # 3
    (1.0965, 1.0985, 1.0960, 1.0980),  # 4
    (1.0980, 1.1010, 1.0975, 1.0990),  # 5 SWEEP PDH (mecha 1.1010, cierre dentro)
    (1.0990, 1.0992, 1.0950, 1.0955),  # 6
    (1.0955, 1.0958, 1.0920, 1.0925),  # 7 CHoCH (cierre < 1.0930); FVG 5-7: [1.0958, 1.0975]
]
DAY_START = "2024-01-10 22:00"


def two_days(day_bars, mirrored=False):
    prev = mirror(PREV_DAY) if mirrored else PREV_DAY
    day = mirror(day_bars) if mirrored else day_bars
    return pd.concat([make_df(prev, PREV_DAY_START), make_df(day, DAY_START)])


@pytest.fixture
def short_candles():
    return two_days(SHORT_DAY)


@pytest.fixture
def long_candles():
    return two_days(SHORT_DAY, mirrored=True)

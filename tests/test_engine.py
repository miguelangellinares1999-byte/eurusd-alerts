"""Secuencia completa SWEEP -> CHOCH -> FVG, invalidaciones y deduplicación."""

from datetime import time

import pandas as pd
import pytest

from detector import DetectorConfig, Direction, SessionWindow, SetupState, SMCEngine
from tests.conftest import DAY_START, SHORT_DAY, mirror, two_days

T7 = pd.Timestamp(DAY_START, tz="UTC") + pd.Timedelta(minutes=15 * 7)


def run(candles, **cfg):
    setups = {}
    engine = SMCEngine(DetectorConfig(**cfg))
    ready, last = engine.process_new(candles, setups, None)
    return engine, setups, ready, last


def m(x):
    return round(2 * 1.0950 - x, 5)


EXPECTED = {
    "short": dict(direction=Direction.SHORT, level_name="PDH", level=1.1000, extreme=1.1010,
                  choch=1.0930, fvg=(1.0958, 1.0975)),
    "long": dict(direction=Direction.LONG, level_name="PDL", level=1.0900, extreme=1.0890,
                 choch=1.0970, fvg=(m(1.0975), m(1.0958))),
}


@pytest.mark.parametrize("side", ["short", "long"])
def test_full_sequence(side):
    exp = EXPECTED[side]
    _, setups, ready, last = run(two_days(SHORT_DAY, mirrored=side == "long"))
    assert len(ready) == 1
    s = ready[0]
    assert s.state is SetupState.FVG
    assert s.direction is exp["direction"]
    assert s.swept_level_name == exp["level_name"]
    assert s.swept_level == pytest.approx(exp["level"])
    assert s.sweep_extreme == pytest.approx(exp["extreme"])
    assert s.choch_level == pytest.approx(exp["choch"])
    assert (s.fvg_low, s.fvg_high) == pytest.approx(exp["fvg"])
    assert s.choch_time == T7
    assert s.ready_time == T7 + pd.Timedelta(minutes=15)
    assert last == T7
    assert len(s.history) == 3  # ->SWEEP, SWEEP->CHOCH, CHOCH->FVG


@pytest.mark.parametrize("side", ["short", "long"])
def test_invalidated_when_extreme_broken_before_choch(side):
    bars = SHORT_DAY[:6] + [
        (1.0990, 1.1015, 1.0980, 1.0985),  # rompe 1.1010 (y es un nuevo sweep del PDH)
        (1.0985, 1.0990, 1.0960, 1.0970),
    ]
    _, setups, ready, _ = run(two_days(bars, mirrored=side == "long"))
    assert ready == []
    states = sorted((s.sweep_extreme, s.state, s.invalidated_reason) for s in setups.values())
    first, second = (states if side == "short" else states[::-1])
    assert first[1] is SetupState.INVALIDATED and first[2] == "sweep_extreme_broken"
    # El nuevo sweep abre un setup nuevo con su propio extremo.
    assert second[1] is SetupState.SWEEP
    assert second[0] == pytest.approx(1.1015 if side == "short" else m(1.1015))


@pytest.mark.parametrize("side", ["short", "long"])
def test_timeout_discards_setup(side):
    _, setups, ready, _ = run(two_days(SHORT_DAY, mirrored=side == "long"), max_setup_hours=0.25)
    assert ready == []
    (s,) = setups.values()
    assert s.state is SetupState.INVALIDATED and s.invalidated_reason == "timeout"


FVG_AFTER_CHOCH = SHORT_DAY[:6] + [
    (1.0990, 1.0995, 1.0960, 1.0965),  # 6
    (1.0965, 1.0978, 1.0925, 1.0928),  # 7 CHoCH sin FVG en la pierna
    (1.0928, 1.0950, 1.0910, 1.0915),  # 8 completa FVG 6-8: [1.0950, 1.0960]
]


@pytest.mark.parametrize("side", ["short", "long"])
def test_fvg_completed_on_bar_after_choch(side):
    _, setups, ready, _ = run(two_days(FVG_AFTER_CHOCH, mirrored=side == "long"))
    assert len(ready) == 1
    expected = (1.0950, 1.0960) if side == "short" else (m(1.0960), m(1.0950))
    assert (ready[0].fvg_low, ready[0].fvg_high) == pytest.approx(expected)
    assert ready[0].choch_time == T7


@pytest.mark.parametrize("side", ["short", "long"])
def test_no_fvg_in_leg_discards_setup(side):
    _, setups, ready, _ = run(two_days(FVG_AFTER_CHOCH, mirrored=side == "long"), fvg_max_bars_after_choch=0)
    assert ready == []
    (s,) = setups.values()
    assert s.state is SetupState.INVALIDATED and s.invalidated_reason == "no_fvg"


@pytest.mark.parametrize("side", ["short", "long"])
def test_no_choch_without_body_close(side):
    bars = SHORT_DAY[:7] + [(1.0955, 1.0958, 1.0920, 1.0935)]  # mecha bajo 1.0930, cierre encima
    _, setups, ready, _ = run(two_days(bars, mirrored=side == "long"))
    assert ready == []
    (s,) = setups.values()
    assert s.state is SetupState.SWEEP


def test_incremental_processing_does_not_duplicate(short_candles):
    engine = SMCEngine(DetectorConfig())
    setups = {}
    ready, last = engine.process_new(short_candles.iloc[:-1], setups, None)
    assert ready == []
    ready, last = engine.process_new(short_candles, setups, last)
    assert len(ready) == 1
    # Mismas velas otra vez: nada nuevo.
    ready_again, _ = engine.process_new(short_candles, setups, last)
    assert ready_again == []
    # Reprocesar desde cero con el mismo estado tampoco crea un setup duplicado.
    engine.process_new(short_candles, setups, None)
    assert len(setups) == 1


def test_session_filter_blocks_sweep_outside_session(short_candles):
    london = SessionWindow("london", "Europe/London", time(7), time(16))
    _, setups, _, _ = run(short_candles, sessions=[london])
    assert setups == {}


def test_session_filter_allows_sweep_inside_session(short_candles):
    # La vela del sweep abre 2024-01-10 23:15 UTC (18:15 NY).
    evening = SessionWindow("ny_evening", "America/New_York", time(18), time(19))
    _, setups, ready, _ = run(short_candles, sessions=[evening])
    assert len(ready) == 1

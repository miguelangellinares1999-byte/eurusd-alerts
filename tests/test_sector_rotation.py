import pandas as pd

from backtest.sector_rotation import setups, simulate_exits


def make_weekly(weeks, start="2024-01-01"):
    """weeks = [(open, high, low, close), ...]; cada semana son 5 días hábiles iguales."""
    rows = [w for w in weeks for _ in range(5)]
    idx = pd.date_range(start, periods=len(rows), freq="B", name="date")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx, dtype=float)


BASE = (100, 102, 98, 100)  # vela previa al impulso: FVG por encima de 102
IMPULSE = [(100, 108, 99, 107), (107, 115, 104, 114)]  # dos alcistas, mínimo 104 > 102
PULLBACK = (114, 116, 106, 108)  # mínimo 106


def test_sweep_after_fvg_impulse():
    sweep = (108, 110, 105, 109)  # perfora 106 y cierra encima
    sig = setups(make_weekly([BASE, *IMPULSE, PULLBACK, sweep]))
    assert len(sig) == 1
    s = sig.iloc[0]
    assert (s.entry, s.stop_loss, s.take_profit) == (109, 105, 116)
    sig = setups(make_weekly([BASE, *IMPULSE, PULLBACK, sweep]), tp="2R")
    assert sig.iloc[0].take_profit == 117


def test_no_setup_without_fvg_or_after_close_below_fvg():
    no_gap = [(100, 105, 98, 100), *IMPULSE, PULLBACK, (108, 110, 105, 109)]  # 104 < 105
    assert setups(make_weekly(no_gap)).empty
    broken = [BASE, *IMPULSE, (114, 115, 100, 101), (101, 104, 99, 103)]  # cierra bajo 102
    assert setups(make_weekly(broken)).empty


def _signal(df, entry=100.0, sl=90.0):
    return pd.DataFrame({"last_day": [df.index[4]], "entry": [entry], "stop_loss": [sl], "take_profit": [0.0]})


def test_mixed_exit_3r_then_breakeven():
    # Semana de entrada en 100 (SL 90); sube a 130 (3R) y luego vuelve a 100: el resto sale en BE.
    df = make_weekly([(100, 101, 95, 100), (100, 131, 99, 130), (130, 130, 99, 100)])
    t = simulate_exits("X", df, _signal(df), "3R+5R")[0]
    assert t.result == "win" and t.exit_price == (130 + 100) / 2


def test_trailing_stop_and_no_stop():
    # Sube semana a semana; el stop dinámico (mínimo de las 2 últimas semanas) sube a 105.
    df = make_weekly([(100, 101, 95, 100), (100, 112, 105, 110), (110, 122, 115, 120),
                      (120, 121, 100, 101)])
    t = simulate_exits("X", df, _signal(df), "trail")[0]
    assert t.exit_price == 105  # el mínimo de las 2 últimas semanas cerradas antes del desplome
    # Sin stop: cae por debajo de 90 y no sale; llega a 130 y cierra en el objetivo 3R.
    df = make_weekly([(100, 101, 95, 100), (100, 100, 80, 85), (85, 131, 85, 130)])
    t = simulate_exits("X", df, _signal(df), "sinstop:3R")[0]
    assert t.result == "win" and t.exit_price == 130

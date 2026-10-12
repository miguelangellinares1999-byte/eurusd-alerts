import pandas as pd
import pytest

from etf_alerts import EtfAlerts, EtfStore, evaluate, format_report, run_etf_alerts, size
from settings import load_config


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, subject, body, html_body=None):
        self.sent.append((subject, body, html_body))


def daily(closes, start="2024-01-01"):
    idx = pd.date_range(start, periods=len(closes), freq="B", name="date")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c + 0.5, "low": c - 0.5, "close": c})


# 260 sesiones subiendo de 100 a ~152 y tres días cayendo: RSI(2) muy bajo, muy por encima de la MM200.
UP = [100 + 0.2 * i for i in range(260)]
DIP = UP + [UP[-1] - 1, UP[-1] - 2.5, UP[-1] - 4]


def at(df, hh=15, mm=45, offset_days=0):
    day = df.index[-1] + pd.Timedelta(days=offset_days)
    return pd.Timestamp(f"{day.date()} {hh:02d}:{mm:02d}", tz="America/New_York").tz_convert("UTC")


def cfg(tmp_path, **kw):
    return EtfAlerts(enabled=True, dry_run=False, capital=10_000, alloc_pct=50, state_path=tmp_path / "s.json", **kw)


def data_for(df):
    return {"SPY": df, "QQQ": daily(UP + [UP[-1] + 0.2] * (len(df) - len(UP)))}  # QQQ sin señal, mismas fechas


def test_entry_signal_sends_email_and_opens_position(tmp_path):
    c, df, note = cfg(tmp_path), daily(DIP), FakeNotifier()
    store = EtfStore(c.state_path).load()
    rep = run_etf_alerts(c, store, note, at(df), data_for(df))
    spy = rep["etfs"][0]
    assert spy["action"] == "entry" and rep["etfs"][1]["action"] == "none"
    assert spy["entry"] == pytest.approx(DIP[-1])
    assert spy["stop"] < spy["entry"]
    assert spy["units"] == 10_000 * 0.5 // DIP[-1]
    subject, body, page = note.sent[0]
    assert subject.startswith("[ETF] COMPRA SPY (SPY.P)") and "COMPRAR al cierre" in body and "<html>" in page
    saved = EtfStore(c.state_path).load()
    assert saved.positions["SPY"]["entry_day"] == str(df.index[-1].date())


def test_outside_window_or_same_day_does_nothing(tmp_path):
    c, df, note = cfg(tmp_path), daily(DIP), FakeNotifier()
    store = EtfStore(c.state_path).load()
    assert run_etf_alerts(c, store, note, at(df, 10, 0), data_for(df)) is None  # 10:00 Nueva York
    assert run_etf_alerts(c, store, note, at(df), data_for(df)) is not None
    assert run_etf_alerts(c, store, note, at(df, 15, 55), data_for(df)) is None  # ya procesado hoy
    assert len(note.sent) == 1


def test_market_closed_today_is_skipped(tmp_path):
    c, df, note = cfg(tmp_path), daily(DIP), FakeNotifier()
    store = EtfStore(c.state_path).load()
    assert run_etf_alerts(c, store, note, at(df, offset_days=1), data_for(df)) is None  # la última vela es de ayer
    assert not note.sent and not store.done


def test_rebound_above_sma5_exits(tmp_path):
    c, note = cfg(tmp_path), FakeNotifier()
    store = EtfStore(c.state_path).load()
    df = daily(DIP)
    run_etf_alerts(c, store, note, at(df), data_for(df))
    df2 = daily(DIP + [DIP[-1] + 5])  # rebote fuerte: cierre por encima de la MM5
    rep = run_etf_alerts(c, store, note, at(df2), data_for(df2))
    assert rep["etfs"][0]["action"] == "exit"
    assert note.sent[-1][0].startswith("[ETF] VENTA SPY")
    assert "SPY" not in store.positions and store.history[-1]["exit"] == "exit"


def test_stop_touched_closes_position(tmp_path):
    c, note = cfg(tmp_path), FakeNotifier()
    store = EtfStore(c.state_path).load()
    df = daily(DIP)
    run_etf_alerts(c, store, note, at(df), data_for(df))
    stop = store.positions["SPY"]["stop"]
    df2 = daily(DIP + [stop - 2])
    rep = run_etf_alerts(c, store, note, at(df2), data_for(df2))
    assert rep["etfs"][0]["action"] == "stop"
    assert rep["etfs"][0]["pnl_pct"] < 0 and note.sent[-1][0].startswith("[ETF] STOP SPY")


def test_time_exit_after_max_days(tmp_path):
    c = cfg(tmp_path, max_days=2)
    df = daily(DIP + [DIP[-1] - 0.1, DIP[-1] - 0.2])  # dos sesiones sin rebote ni stop
    pos = {"entry_day": str(df.index[-3].date()), "entry": DIP[-1], "stop": DIP[-1] - 50}
    assert evaluate(c, c.symbols[0], df, pos)["action"] == "exit_time"
    assert evaluate(cfg(tmp_path), c.symbols[0], df, pos)["action"] == "hold"


def test_no_signal_sends_nothing_unless_daily_summary(tmp_path):
    flat = daily(UP + [UP[-1] + 0.2] * 3)
    data = {"SPY": flat, "QQQ": flat}
    quiet, note = cfg(tmp_path), FakeNotifier()
    assert run_etf_alerts(quiet, EtfStore(quiet.state_path).load(), note, at(flat), data) is not None
    assert not note.sent
    loud = cfg(tmp_path / "b", daily_summary=True)
    run_etf_alerts(loud, EtfStore(loud.state_path).load(), note, at(flat), data)
    assert note.sent[0][0].endswith("sin operaciones")


def test_size_rounds_down_and_flags_minimum(tmp_path):
    out = size(cfg(tmp_path), entry=600, stop=580)  # 5.000 / 600 = 8,33 acciones
    assert out["units"] == 8 and out["risk_money"] == 160 and not out["below_min_lot"]
    assert size(EtfAlerts(capital=500, alloc_pct=50), 600, 580)["below_min_lot"]


def test_format_report_without_actions(tmp_path):
    flat = daily(UP + [UP[-1] + 0.2] * 3)
    c = cfg(tmp_path)
    rep = {"day": "2025-01-02", "time_ny": "15:45", "etfs": [evaluate(c, s, flat, None) for s in c.symbols],
           "capital": 10_000, "alloc_pct": 50, "stop_atr": 3, "max_days": 10}
    subject, body, _ = format_report(rep)
    assert subject == "[ETF] 2025-01-02: sin operaciones" and "Sin señal" in body


def test_config_yaml_loads_etf_alerts():
    ea = load_config(env_file=None).etf_alerts
    assert [s.ticker for s in ea.symbols] == ["SPY", "QQQ"] and ea.stop_atr == 3
    assert ea.window[0].hour == 15

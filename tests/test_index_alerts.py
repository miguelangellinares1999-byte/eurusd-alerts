import pandas as pd
import pytest

from index_alerts import IndexAlerts, IndexStore, IndexSymbol, MarketData, format_report, lots, run_index_alerts


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, subject, body, html_body=None):
        self.sent.append((subject, body, html_body))


def daily(rows, start="2024-01-01"):
    idx = pd.date_range(start, periods=len(rows), freq="B", name="date")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx, dtype=float)


def week(o, h, l, c):
    return [(o, h, l, c)] * 5


# 30 semanas en 100-110 y una que barre el mínimo (95) y cierra en 102 -> SL 95, TP 3R = 123.
SWEEP = week(105, 110, 100, 105) * 30 + [(104, 104, 95, 100)] + [(100, 103, 96, 102)] * 4


def market(rows=SWEEP, n_members=20):
    falling = daily([(200 - i, 201 - i, 199 - i, 200 - i) for i in range(len(rows))])
    members = {f"M{i}": falling for i in range(n_members)}
    idx = daily(rows)
    return MarketData(indices={"^GSPC": idx, "^NDX": idx, "^VIX": daily([(20, 21, 19, 20)] * len(rows))}, sp500=members, ndx=members)


def cfg(tmp_path, **kw):
    return IndexAlerts(enabled=True, dry_run=False, capital=10_000, risk_pct=2, state_path=tmp_path / "s.json", **kw)


def test_lots_rounds_down_and_flags_minimum():
    c = IndexAlerts(capital=10_000, risk_pct=2, lot_step=0.01)
    sym = IndexSymbol("^NDX", "Nasdaq 100", "NAS100", contract_size=1)
    out = lots(c, sym, entry=30_000, stop=29_100)  # 900 puntos -> 200 / 900 = 0,222 lotes
    assert out["lots"] == pytest.approx(0.22) and out["risk_money"] == 200
    small = lots(IndexAlerts(capital=100, risk_pct=2), sym, 30_000, 29_100)
    assert small["below_min_lot"] and small["lots"] == 0


def test_first_run_alerts_signal_and_tracks_position(tmp_path):
    data, c = market(), cfg(tmp_path)
    store = IndexStore(c.state_path).load()
    note = FakeNotifier()
    now = pd.Timestamp(data.indices["^GSPC"].index[-1] + pd.Timedelta(days=1), tz="UTC")
    rep = run_index_alerts(c, store, note, now, data)
    chosen = [s for s in rep["signals"] if s["chosen"]]
    assert len(rep["signals"]) == 2 and len(chosen) == 1  # mismo cierre en los dos índices: uno elegido
    s = chosen[0]
    assert (s["entry"], s["stop_loss"], s["take_profit"]) == (102, 95, 123)
    subject, body, page = note.sent[0]
    assert subject.startswith("[Índices] SEÑAL") and "Take profit 3R: 123,00" in body
    assert "<html>" in page and "Elegido por rotación" in page
    saved = IndexStore(c.state_path).load()
    assert saved.alerted == [s["week"]] and saved.positions[0]["status"] == "open"


def test_second_run_does_not_repeat_and_reports_position(tmp_path):
    data, c = market(), cfg(tmp_path)
    now = pd.Timestamp(data.indices["^GSPC"].index[-1] + pd.Timedelta(days=1), tz="UTC")
    run_index_alerts(c, IndexStore(c.state_path).load(), FakeNotifier(), now, data)
    note = FakeNotifier()
    rep = run_index_alerts(c, IndexStore(c.state_path).load(), note, now, data)
    assert rep["signals"] == []
    assert "sin señal · 1 operación abierta" in note.sent[0][0]


def test_no_signal_without_oversold_breadth(tmp_path):
    rows = SWEEP
    data = market(rows)
    rising = daily([(100 + i, 101 + i, 99 + i, 100 + i) for i in range(len(rows))])
    data.sp500 = {f"M{i}": rising for i in range(20)}
    note = FakeNotifier()
    now = pd.Timestamp(data.indices["^GSPC"].index[-1] + pd.Timedelta(days=1), tz="UTC")
    rep = run_index_alerts(cfg(tmp_path), IndexStore(tmp_path / "s.json").load(), note, now, data)
    assert rep["signals"] == [] and not rep["breadth_ok"]
    assert "sin señal" in note.sent[0][0]


def test_weekly_summary_can_be_disabled(tmp_path):
    data = market(SWEEP[:-5] + week(105, 110, 100, 105))  # sin barrida la última semana
    note = FakeNotifier()
    now = pd.Timestamp(data.indices["^GSPC"].index[-1] + pd.Timedelta(days=1), tz="UTC")
    run_index_alerts(cfg(tmp_path, weekly_summary=False), IndexStore(tmp_path / "s.json").load(), note, now, data)
    assert note.sent == []


def test_format_report_marks_closed_positions():
    rep = {"week": "2026-10-02", "breadth": 25.0, "breadth_min_2w": 21.0, "breadth_ok": True, "breadth_below": 30, "vix": 16.0,
           "indices": [], "signals": [], "capital": 10_000, "risk_pct": 2, "tp_r": 3,
           "positions": [{"name": "Nasdaq 100", "week": "2026-09-18", "status": "win", "exit_date": "2026-11-20", "r_now": 3.0, "last": 32_400.0}]}
    subject, body, page = format_report(rep)
    assert "cerrada en TP (2026-11-20)" in body and "TP · 2026-11-20" in page

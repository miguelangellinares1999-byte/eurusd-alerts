"""Ciclo en vivo + persistencia: sin duplicados, reintentos y alertas caducadas."""

from datetime import timedelta

import pandas as pd
import pytest

from data.base import DataSource, from_engine_frame
from detector import SetupState
from main import latest_closed_open, market_closed, run_cycle
from notifier import Notifier, format_alert
from settings import ROOT, load_config
from state import StateStore
from tests.conftest import DAY_START

T7 = pd.Timestamp(DAY_START, tz="UTC") + pd.Timedelta(minutes=15 * 7)
AFTER_CLOSE = T7 + timedelta(minutes=15, seconds=20)


class FakeSource(DataSource):
    def __init__(self, candles):
        self.candles = candles
        self.calls = 0

    def get_last(self, symbol, timeframe, n):
        self.calls += 1
        return from_engine_frame(self.candles).tail(n)

    def get_range(self, symbol, timeframe, date_from, date_to):
        raise NotImplementedError


class FakeNotifier(Notifier):
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send(self, subject, body):
        if self.fail:
            raise ConnectionError("SMTP caído")
        self.sent.append((subject, body))


@pytest.fixture
def cfg(tmp_path):
    c = load_config(ROOT / "config.yaml", env_file=None)
    c.state_path = tmp_path / "state.json"
    return c


def test_alert_sent_once_and_persisted(cfg, short_candles):
    source, notifier = FakeSource(short_candles), FakeNotifier()
    store = StateStore(cfg.state_path).load()
    assert run_cycle(cfg, source, store, notifier, AFTER_CLOSE) == 1
    assert run_cycle(cfg, source, store, notifier, AFTER_CLOSE + timedelta(minutes=1)) == 0
    assert len(notifier.sent) == 1
    assert "SHORT" in notifier.sent[0][0]

    # Proceso nuevo (reinicio): carga el JSON y no repite la alerta.
    reloaded = StateStore(cfg.state_path).load()
    assert reloaded.last_processed == T7
    (setup,) = reloaded.setups.values()
    assert setup.state is SetupState.ALERTED
    assert run_cycle(cfg, source, reloaded, notifier, AFTER_CLOSE + timedelta(minutes=15)) == 0
    assert len(notifier.sent) == 1


def test_failed_email_is_retried(cfg, short_candles):
    source = FakeSource(short_candles)
    store = StateStore(cfg.state_path)
    assert run_cycle(cfg, source, store, FakeNotifier(fail=True), AFTER_CLOSE) == 0
    assert StateStore(cfg.state_path).load().pending_alerts()
    ok = FakeNotifier()
    assert run_cycle(cfg, source, store, ok, AFTER_CLOSE + timedelta(minutes=1)) == 1
    assert len(ok.sent) == 1


def test_stale_alert_not_sent(cfg, short_candles):
    store = StateStore(cfg.state_path)
    notifier = FakeNotifier()
    assert run_cycle(cfg, FakeSource(short_candles), store, notifier, T7 + timedelta(hours=3)) == 0
    (setup,) = store.setups.values()
    assert setup.state is SetupState.INVALIDATED and setup.invalidated_reason == "stale"


def test_does_not_fetch_until_next_candle_closes(cfg, short_candles):
    source = FakeSource(short_candles)
    store = StateStore(cfg.state_path)
    run_cycle(cfg, source, store, FakeNotifier(), AFTER_CLOSE)
    calls = source.calls
    for minute in range(1, 14):
        run_cycle(cfg, source, store, FakeNotifier(), AFTER_CLOSE + timedelta(minutes=minute))
    assert source.calls == calls


def test_open_candle_is_ignored(cfg, short_candles):
    # A las T7+10min la vela del CHoCH sigue abierta: no hay alerta todavía.
    store = StateStore(cfg.state_path)
    notifier = FakeNotifier()
    run_cycle(cfg, FakeSource(short_candles), store, notifier, T7 + timedelta(minutes=10))
    assert notifier.sent == []
    assert store.last_processed == T7 - timedelta(minutes=15)


def test_state_roundtrip_and_prune(cfg, short_candles):
    store = StateStore(cfg.state_path)
    run_cycle(cfg, FakeSource(short_candles), store, FakeNotifier(), AFTER_CLOSE)
    loaded = StateStore(cfg.state_path).load()
    assert {k: s.to_dict() for k, s in loaded.setups.items()} == {k: s.to_dict() for k, s in store.setups.items()}
    assert loaded.prune(AFTER_CLOSE + timedelta(days=31), keep_days=30) == 1
    assert loaded.setups == {}


def test_corrupt_state_is_backed_up(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{no es json")
    store = StateStore(path).load()
    assert store.setups == {} and (tmp_path / "state.corrupt.json").exists()


def test_timing_helpers():
    now = pd.Timestamp("2024-01-10 10:31:05", tz="UTC")
    assert latest_closed_open(now, 15) == pd.Timestamp("2024-01-10 10:15", tz="UTC")
    assert market_closed(pd.Timestamp("2024-01-13 12:00", tz="UTC"))  # sábado
    assert market_closed(pd.Timestamp("2024-01-12 22:00", tz="UTC"))  # viernes 17:00 NY
    assert not market_closed(pd.Timestamp("2024-01-12 21:45", tz="UTC"))
    assert not market_closed(pd.Timestamp("2024-01-14 22:00", tz="UTC"))  # domingo 17:00 NY


def test_email_format(cfg, short_candles):
    store = StateStore(None)
    notifier = FakeNotifier()
    run_cycle(cfg, FakeSource(short_candles), store, notifier, AFTER_CLOSE)
    (setup,) = store.setups.values()
    subject, body = format_alert(setup)
    assert "PDH 1.10000" in body
    assert "1.09300" in body  # nivel del CHoCH
    assert "1.09580 - 1.09750" in body  # zona FVG
    assert "Stop loss:        1.10100" in body

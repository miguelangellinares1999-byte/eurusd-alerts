import pandas as pd

from daily_cycle_alerts import DailyCycleAlerts, SignalStore, format_signal, run_daily_cycle, take_profit
from detector.daily_cycle import DailyCycleConfig, detect_signals
from settings import load_config
from tests.test_chartz import random_walk


class FakeSource:
    def __init__(self, df):
        self.df = df

    def get_closed_candles(self, tf, lookback_days, now):
        return self.df[self.df.index + pd.Timedelta(minutes=tf) <= now]


class FakeNotifier:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, subject, body):
        if self.fail:
            raise RuntimeError("smtp caído")
        self.sent.append(subject)


def _setup(tmp_path):
    det = DailyCycleConfig(ltf_minutes=15, htf_minutes=60, single_candle_inf=True, entry="fvg_mid", windows=[(2, 5), (8, 11)])
    dc = DailyCycleAlerts(enabled=True, state_path=tmp_path / "dc.json", detector=det)
    df = random_walk(30 * 24 * 4, freq="15min", start="2024-01-01 00:00", seed=5)
    sigs = detect_signals(df, det)
    assert sigs, "el escenario debe generar alguna señal"
    return dc, df, sigs


def test_new_signal_alerted_once_and_journaled(tmp_path):
    dc, df, sigs = _setup(tmp_path)
    sig = sigs[-1]
    store, notifier = SignalStore(dc.state_path), FakeNotifier()
    assert run_daily_cycle(dc, FakeSource(df), store, notifier, 30, sig.time + pd.Timedelta(minutes=2)) == 1
    assert run_daily_cycle(dc, FakeSource(df), store, notifier, 30, sig.time + pd.Timedelta(minutes=17)) == 0
    reloaded = SignalStore(dc.state_path).load()
    assert reloaded.last_alerted == sig.time
    assert reloaded.journal[-1]["status"] == "avisada (dry run)"
    assert any(j["status"] == "antigua, no avisada" for j in reloaded.journal) == (len(sigs) > 1)
    assert "[DRY RUN]" in notifier.sent[0]


def test_failed_send_is_retried(tmp_path):
    dc, df, sigs = _setup(tmp_path)
    sig = sigs[-1]
    store = SignalStore(dc.state_path)
    at = sig.time + pd.Timedelta(minutes=2)
    assert run_daily_cycle(dc, FakeSource(df), store, FakeNotifier(fail=True), 30, at) == 0
    assert run_daily_cycle(dc, FakeSource(df), store, FakeNotifier(), 30, at) == 1


def test_take_profit_and_format(tmp_path):
    dc, _, sigs = _setup(tmp_path)
    sig = sigs[0]
    risk = abs(sig.entry - sig.stop_loss)
    assert abs(take_profit(sig, 2) - sig.entry) == pytest_approx(2 * risk)
    subject, body = format_signal("GBPUSD", sig, 2, 0.0001, True)
    assert subject.startswith("[DRY RUN] [GBPUSD]") and "Stop loss" in body


def pytest_approx(v):
    import pytest

    return pytest.approx(v)


def test_config_has_gbpusd_dry_run():
    dc = load_config().daily_cycle
    assert dc.enabled and dc.dry_run and dc.symbol == "GBPUSD"
    assert dc.detector.ltf_minutes == 15 and dc.detector.htf_minutes == 240 and dc.detector.single_candle_inf

import pandas as pd

from detector.daily_cycle import DailyCycleConfig, detect_signals
from tests.test_chartz import random_walk


def test_signals_are_causal_and_one_per_day():
    df = random_walk(20 * 24 * 12, freq="5min", start="2024-01-01 00:00", seed=11)
    cfg = DailyCycleConfig(htf_minutes=60, windows=[(2, 5), (8, 11)])
    sigs = detect_signals(df, cfg)
    days = [s.time.tz_convert("America/New_York").date() for s in sigs]
    assert len(days) == len(set(days))
    for s in sigs:
        long = s.direction.value == "long"
        assert (s.stop_loss < s.entry) if long else (s.stop_loss > s.entry)
        assert s.variation in cfg.variations
        hour = s.time.tz_convert("America/New_York") - pd.Timedelta(minutes=5)
        assert 2 <= hour.hour < 5 or 8 <= hour.hour < 11
        cut = detect_signals(df[df.index < s.time], cfg)
        assert any(c.time == s.time and c.entry == s.entry for c in cut)

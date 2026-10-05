"""Filtro opcional de sesión (Londres / Nueva York)."""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from detector.config import SessionWindow


def in_session(ts: datetime, window: SessionWindow) -> bool:
    local = pd.Timestamp(ts).tz_convert(window.tz).time()
    if window.start <= window.end:
        return window.start <= local < window.end
    return local >= window.start or local < window.end  # ventana que cruza medianoche


def session_allowed(ts: datetime, windows: list[SessionWindow]) -> bool:
    """Sin ventanas configuradas no hay filtro."""
    return not windows or any(in_session(ts, w) for w in windows)

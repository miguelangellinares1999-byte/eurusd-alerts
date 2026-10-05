"""Fase 2: CHoCH en 15m.

Tras barrer el PDH (setup short) el nivel de CHoCH es el último swing low confirmado
en el momento del sweep; el CHoCH se produce cuando una vela CIERRA por debajo.
Tras barrer el PDL (setup long) es el último swing high y se exige cierre por encima.
"""

from __future__ import annotations

from collections.abc import Sequence

from detector.models import Direction
from detector.swings import last_confirmed_swing


def choch_reference(
    highs: Sequence[float],
    lows: Sequence[float],
    sweep_idx: int,
    direction: Direction,
    n: int = 2,
) -> tuple[int, float] | None:
    """(índice, precio) del swing que hay que romper, o None si no hay ninguno."""
    if direction is Direction.SHORT:
        idx = last_confirmed_swing(lows, sweep_idx, n, high=False)
        return None if idx is None else (idx, lows[idx])
    idx = last_confirmed_swing(highs, sweep_idx, n, high=True)
    return None if idx is None else (idx, highs[idx])


def is_choch(close: float, level: float, direction: Direction) -> bool:
    """Cierre de cuerpo más allá del nivel (la mecha sola no cuenta)."""
    if direction is Direction.SHORT:
        return close < level
    return close > level

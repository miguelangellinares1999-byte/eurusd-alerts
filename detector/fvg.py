"""Fase 3: Fair Value Gap de 3 velas.

Alcista: high(vela1) < low(vela3)  -> zona [high1, low3]
Bajista: low(vela1)  > high(vela3) -> zona [high3, low1]
"""

from __future__ import annotations

import pandas as pd

from detector.models import FVG, Direction


def fvg_at(df: pd.DataFrame, i3: int, direction: Direction, min_size: float = 0.0) -> FVG | None:
    """FVG cuya vela 3 es la de posición i3 (velas i3-2, i3-1, i3)."""
    if i3 < 2 or i3 >= len(df):
        return None
    c1 = df.iloc[i3 - 2]
    c3 = df.iloc[i3]
    if direction is Direction.LONG and c1["high"] < c3["low"]:
        low, high = float(c1["high"]), float(c3["low"])
    elif direction is Direction.SHORT and c1["low"] > c3["high"]:
        low, high = float(c3["high"]), float(c1["low"])
    else:
        return None
    if high - low < min_size:
        return None
    return FVG(direction, low, high, df.index[i3 - 2], df.index[i3])


def find_fvgs(
    df: pd.DataFrame, start: int, end: int, direction: Direction, min_size: float = 0.0
) -> list[FVG]:
    """Todos los FVG con vela 1 >= start y vela 3 <= end (posiciones), en orden temporal."""
    found = []
    for i3 in range(max(start + 2, 2), min(end, len(df) - 1) + 1):
        fvg = fvg_at(df, i3, direction, min_size)
        if fvg is not None:
            found.append(fvg)
    return found

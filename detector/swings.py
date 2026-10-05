"""Swings: pivotes de N velas a cada lado.

Un swing high en i cumple high[i] > high de las N velas anteriores y de las N
posteriores (estricto; con máximos iguales no hay pivote). Un pivote en i solo se
conoce al cerrar la vela i+N, así que "confirmado en j" significa i + N <= j.
"""

from __future__ import annotations

from collections.abc import Sequence


def _is_pivot(values: Sequence[float], i: int, n: int, high: bool) -> bool:
    if i - n < 0 or i + n >= len(values):
        return False
    v = values[i]
    neighbours = [values[k] for k in range(i - n, i + n + 1) if k != i]
    if high:
        return all(v > x for x in neighbours)
    return all(v < x for x in neighbours)


def swing_highs(highs: Sequence[float], n: int = 2) -> list[int]:
    return [i for i in range(len(highs)) if _is_pivot(highs, i, n, high=True)]


def swing_lows(lows: Sequence[float], n: int = 2) -> list[int]:
    return [i for i in range(len(lows)) if _is_pivot(lows, i, n, high=False)]


def last_confirmed_swing(
    values: Sequence[float], upto: int, n: int = 2, high: bool = True
) -> int | None:
    """Índice del último pivote confirmado al cierre de la vela `upto` (sin lookahead)."""
    # Con i <= upto - n el pivote solo mira velas <= upto.
    for i in range(min(upto, len(values) - 1) - n, n - 1, -1):
        if _is_pivot(values, i, n, high):
            return i
    return None

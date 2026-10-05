"""Fase 1: SWEEP de PDH/PDL.

Una vela barre el PDH si su mecha lo supera (high > PDH) y cierra de vuelta
por debajo (close < PDH). Simétrico para el PDL.
"""

from __future__ import annotations

import math

from detector.models import Direction


def is_pdh_sweep(high: float, close: float, pdh: float) -> bool:
    return not math.isnan(pdh) and high > pdh and close < pdh


def is_pdl_sweep(low: float, close: float, pdl: float) -> bool:
    return not math.isnan(pdl) and low < pdl and close > pdl


def detect_sweeps(
    high: float, low: float, close: float, pdh: float, pdl: float
) -> list[tuple[str, Direction, float, float]]:
    """Devuelve [(nombre_nivel, dirección_del_trade, nivel, extremo_del_sweep)].

    Barrer el PDH da un setup SHORT con SL en el high de la vela; barrer el PDL
    un setup LONG con SL en el low. Una vela envolvente puede barrer ambos.
    """
    result = []
    if is_pdh_sweep(high, close, pdh):
        result.append(("PDH", Direction.SHORT, pdh, high))
    if is_pdl_sweep(low, close, pdl):
        result.append(("PDL", Direction.LONG, pdl, low))
    return result

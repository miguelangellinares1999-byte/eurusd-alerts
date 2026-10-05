"""Parámetros del detector (independientes de la fuente de datos y del notificador)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time


@dataclass(frozen=True)
class SessionWindow:
    """Ventana horaria de una sesión en su zona horaria local (gestiona DST)."""

    name: str
    tz: str
    start: time
    end: time


@dataclass
class DetectorConfig:
    # Velas a cada lado para confirmar un pivote (swing).
    swing_n: int = 2
    # Horas máximas desde el sweep hasta encontrar el FVG antes de descartar el setup.
    max_setup_hours: float = 8.0
    # Velas posteriores al CHoCH en las que todavía puede cerrarse el FVG
    # (la 3ª vela del hueco puede ser la siguiente a la vela del CHoCH).
    fvg_max_bars_after_choch: int = 1
    # Tamaño mínimo del FVG en pips (0 = cualquiera).
    min_fvg_pips: float = 0.0
    pip_size: float = 0.0001
    # Definición del día de trading: cierre a las 17:00 de Nueva York.
    day_close_hour: int = 17
    day_close_tz: str = "America/New_York"
    # Si True, un nivel (PDH o PDL) solo puede generar una alerta por día.
    one_alert_per_level_per_day: bool = True
    # Filtro de sesión aplicado a la vela del sweep. Lista vacía = sin filtro.
    sessions: list[SessionWindow] = field(default_factory=list)
    timeframe_minutes: int = 15

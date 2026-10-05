"""Modelos del detector: dirección, FVG y setup con su máquina de estados."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum

import pandas as pd


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"


class SetupState(str, Enum):
    SWEEP = "SWEEP"  # nivel barrido, esperando CHoCH
    CHOCH = "CHOCH"  # CHoCH confirmado, esperando FVG
    FVG = "FVG"  # FVG encontrado, pendiente de enviar alerta
    ALERTED = "ALERTED"  # alerta enviada (estado final)
    INVALIDATED = "INVALIDATED"  # descartado (estado final)

    @property
    def is_active(self) -> bool:
        return self in (SetupState.SWEEP, SetupState.CHOCH)


@dataclass(frozen=True)
class FVG:
    direction: Direction
    low: float
    high: float
    # Hora de apertura de la vela 1 y de la vela 3 del patrón.
    start_time: datetime
    end_time: datetime

    @property
    def size(self) -> float:
        return self.high - self.low

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2


def _ts(value: datetime | None) -> str | None:
    return None if value is None else pd.Timestamp(value).isoformat()


def _parse_ts(value: str | None) -> pd.Timestamp | None:
    return None if value is None else pd.Timestamp(value)


@dataclass
class Setup:
    id: str
    symbol: str
    direction: Direction
    trading_day: str  # YYYY-MM-DD del día de trading del sweep
    swept_level_name: str  # "PDH" o "PDL"
    swept_level: float
    sweep_time: datetime  # apertura de la vela del sweep
    sweep_extreme: float  # mecha del sweep = stop loss
    choch_level: float  # swing que hay que romper con cierre de cuerpo
    choch_swing_time: datetime
    state: SetupState = SetupState.SWEEP
    choch_time: datetime | None = None
    fvg_low: float | None = None
    fvg_high: float | None = None
    fvg_start_time: datetime | None = None
    fvg_end_time: datetime | None = None
    ready_time: datetime | None = None  # cierre de la vela que completó la secuencia
    alerted_at: datetime | None = None
    invalidated_reason: str | None = None
    updated_at: datetime | None = None
    history: list[str] = field(default_factory=list)

    def transition(self, new_state: SetupState, at: datetime, note: str = "") -> None:
        msg = f"{_ts(at)} {self.state.value}->{new_state.value}"
        if note:
            msg += f" ({note})"
        self.history.append(msg)
        self.state = new_state
        self.updated_at = at

    def set_fvg(self, fvg: FVG) -> None:
        self.fvg_low = fvg.low
        self.fvg_high = fvg.high
        self.fvg_start_time = fvg.start_time
        self.fvg_end_time = fvg.end_time

    @property
    def fvg_mid(self) -> float | None:
        if self.fvg_low is None or self.fvg_high is None:
            return None
        return (self.fvg_low + self.fvg_high) / 2

    def to_dict(self) -> dict:
        d = asdict(self)
        d["direction"] = self.direction.value
        d["state"] = self.state.value
        for key in (
            "sweep_time",
            "choch_swing_time",
            "choch_time",
            "fvg_start_time",
            "fvg_end_time",
            "ready_time",
            "alerted_at",
            "updated_at",
        ):
            d[key] = _ts(d[key])
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Setup":
        d = dict(d)
        d["direction"] = Direction(d["direction"])
        d["state"] = SetupState(d["state"])
        for key in (
            "sweep_time",
            "choch_swing_time",
            "choch_time",
            "fvg_start_time",
            "fvg_end_time",
            "ready_time",
            "alerted_at",
            "updated_at",
        ):
            d[key] = _parse_ts(d.get(key))
        return cls(**d)

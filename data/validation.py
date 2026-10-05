"""Limpieza y validación de velas antes de usarlas (en vivo y en el backtest)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

NY = ZoneInfo("America/New_York")
REQUIRED = ["timestamp", "open", "high", "low", "close", "volume"]
MAX_LISTED = 5  # filas problemáticas que se muestran en un error


class CandleValidationError(ValueError):
    """Los datos no son utilizables (el mensaje explica qué falla y dónde)."""


@dataclass(frozen=True)
class Gap:
    """Velas que faltan en un tramo en el que el mercado está abierto."""

    start: pd.Timestamp  # apertura de la primera vela que falta (UTC)
    end: pd.Timestamp  # apertura de la última vela que falta (UTC)
    missing: int

    def __str__(self) -> str:
        return f"{self.start:%Y-%m-%d %H:%M} -> {self.end:%Y-%m-%d %H:%M} UTC ({self.missing} velas)"


@dataclass
class CandleReport:
    rows_in: int = 0
    rows_out: int = 0
    was_unsorted: bool = False
    duplicates_removed: int = 0
    dropped_forming: bool = False
    gaps: list[Gap] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"Velas recibidas: {self.rows_in}, válidas: {self.rows_out}",
            f"Desordenadas: {'sí (se ordenaron)' if self.was_unsorted else 'no'}",
            f"Duplicados eliminados: {self.duplicates_removed}",
            f"Vela en formación descartada: {'sí' if self.dropped_forming else 'no'}",
            f"Huecos lunes-viernes: {len(self.gaps)}",
        ]
        lines += [f"  - {g}" for g in self.gaps[:20]]
        if len(self.gaps) > 20:
            lines.append(f"  ... y {len(self.gaps) - 20} más")
        return "\n".join(lines)


def market_closed(index: pd.DatetimeIndex) -> np.ndarray:
    """FX cerrado de viernes 17:00 a domingo 17:00 (hora de Nueva York, con su horario de verano)."""
    ny = index.tz_convert(NY)
    wd, hour = ny.weekday, ny.hour
    return np.asarray(((wd == 4) & (hour >= 17)) | (wd == 5) | ((wd == 6) & (hour < 17)))


def find_gaps(timestamps, timeframe_minutes: int) -> list[Gap]:
    """Huecos entre velas consecutivas, ignorando el cierre de fin de semana."""
    idx = pd.DatetimeIndex(timestamps).sort_values()
    step = pd.Timedelta(minutes=timeframe_minutes)
    gaps: list[Gap] = []
    if len(idx) < 2:
        return gaps
    jumps = np.flatnonzero((idx[1:] - idx[:-1]) > step)
    for k in jumps:
        missing = pd.date_range(idx[k] + step, idx[k + 1] - step, freq=step)
        missing = missing[~market_closed(missing)]
        if not len(missing):
            continue
        # Un hueco que cruza el fin de semana se parte en dos tramos.
        breaks = np.flatnonzero((missing[1:] - missing[:-1]) > step) + 1
        for run in np.split(np.arange(len(missing)), breaks):
            gaps.append(Gap(missing[run[0]], missing[run[-1]], len(run)))
    return gaps


def _rows(df: pd.DataFrame, mask) -> str:
    bad = df[mask].head(MAX_LISTED)
    return "\n".join("    " + " ".join(f"{c}={r[c]}" for c in REQUIRED) for _, r in bad.iterrows())


def validate_candles(
    candles: pd.DataFrame,
    timeframe_minutes: int = 15,
    now: datetime | None = None,
    fail_on_gaps: bool = False,
) -> tuple[pd.DataFrame, CandleReport]:
    """Ordena, quita duplicados y la vela en formación, y valida el resto.

    Lanza CandleValidationError si hay NaN, timestamps sin zona horaria o desalineados,
    velas imposibles (high/low que no contienen open/close) o velas en el futuro.
    Los huecos se informan en el log y en el informe; con fail_on_gaps=True también fallan.
    """
    report = CandleReport(rows_in=len(candles))
    missing_cols = [c for c in REQUIRED if c not in candles.columns]
    if missing_cols:
        raise CandleValidationError(f"Faltan columnas: {missing_cols}")
    df = candles[REQUIRED].reset_index(drop=True)
    if df.empty:
        raise CandleValidationError("No hay velas")
    if not isinstance(df["timestamp"].dtype, pd.DatetimeTZDtype):
        raise CandleValidationError("La columna timestamp no tiene zona horaria (debe ser UTC tz-aware)")
    df["timestamp"] = df["timestamp"].dt.tz_convert("UTC")

    nan = df.isna().any(axis=1)
    if nan.any():
        raise CandleValidationError(f"{int(nan.sum())} vela(s) con NaN:\n{_rows(df, nan)}")

    if not df["timestamp"].is_monotonic_increasing:
        report.was_unsorted = True
        df = df.sort_values("timestamp", kind="stable").reset_index(drop=True)
    dup = df["timestamp"].duplicated(keep="last")
    if dup.any():
        report.duplicates_removed = int(dup.sum())
        log.warning("Eliminadas %d velas duplicadas", report.duplicates_removed)
        df = df[~dup].reset_index(drop=True)

    ts = pd.DatetimeIndex(df["timestamp"])
    if timeframe_minutes < 1440:
        misaligned = ((ts.hour * 60 + ts.minute) % timeframe_minutes != 0) | (ts.second != 0)
        if misaligned.any():
            raise CandleValidationError(
                f"{int(misaligned.sum())} vela(s) no alineadas a {timeframe_minutes} min "
                f"(¿desfase horario del servidor mal configurado?):\n{_rows(df, misaligned)}"
            )

    hi_bad = df["high"] < df[["open", "close"]].max(axis=1)
    lo_bad = df["low"] > df[["open", "close"]].min(axis=1)
    if hi_bad.any() or lo_bad.any():
        raise CandleValidationError(
            f"Velas incoherentes: {int(hi_bad.sum())} con high < max(open, close) y "
            f"{int(lo_bad.sum())} con low > min(open, close):\n{_rows(df, hi_bad | lo_bad)}"
        )

    now_ts = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now).tz_convert("UTC")
    open_ = (ts + pd.Timedelta(minutes=timeframe_minutes)) > now_ts
    if open_[:-1].any():
        raise CandleValidationError(
            f"{int(open_[:-1].sum())} vela(s) sin cerrar antes de la última (hora futura: "
            f"¿desfase horario del servidor mal configurado?):\n{_rows(df, np.append(open_[:-1], False))}"
        )
    if open_[-1]:
        report.dropped_forming = True
        df = df.iloc[:-1].reset_index(drop=True)

    if timeframe_minutes < 1440:
        report.gaps = find_gaps(df["timestamp"], timeframe_minutes)
        for g in report.gaps:
            log.warning("Hueco en los datos: %s", g)
        if fail_on_gaps and report.gaps:
            listed = "\n".join(f"    {g}" for g in report.gaps[:MAX_LISTED])
            raise CandleValidationError(f"{len(report.gaps)} hueco(s) lunes-viernes:\n{listed}")

    report.rows_out = len(df)
    return df, report

"""Máquina de estados SWEEP -> CHOCH -> FVG que recorre velas cerradas en orden.

El mismo motor lo usan el modo en vivo (procesa solo las velas nuevas desde la última
procesada, guardada en el estado JSON) y el backtest (procesa todo el CSV).
Al procesar la vela i solo se leen velas <= i, así que no hay lookahead.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import pandas as pd

from detector.choch import choch_reference, is_choch
from detector.config import DetectorConfig
from detector.fvg import fvg_at, find_fvgs
from detector.levels import add_daily_levels
from detector.models import Direction, Setup, SetupState
from detector.sessions import session_allowed
from detector.sweep import detect_sweeps

log = logging.getLogger(__name__)


class SMCEngine:
    def __init__(self, cfg: DetectorConfig, symbol: str = "EURUSD"):
        self.cfg = cfg
        self.symbol = symbol
        self.tf = timedelta(minutes=cfg.timeframe_minutes)

    # ------------------------------------------------------------------ API
    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        """Ordena, elimina duplicados y añade trading_day / pdh / pdl."""
        df = candles[["open", "high", "low", "close"]].astype(float)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return add_daily_levels(df, self.cfg.day_close_hour, self.cfg.day_close_tz)

    def process_new(
        self,
        candles: pd.DataFrame,
        setups: dict[str, Setup],
        last_processed: pd.Timestamp | None,
    ) -> tuple[list[Setup], pd.Timestamp | None]:
        """Procesa las velas con apertura posterior a `last_processed`.

        Devuelve (setups que han completado la secuencia, nueva última vela procesada).
        `candles` debe contener solo velas cerradas.
        """
        if candles.empty:
            return [], last_processed
        df = self.prepare(candles)
        start = 0 if last_processed is None else int(df.index.searchsorted(last_processed, side="right"))
        ready: list[Setup] = []
        for i in range(start, len(df)):
            ready.extend(self.process_bar(df, i, setups))
        return ready, df.index[-1]

    def process_bar(self, df: pd.DataFrame, i: int, setups: dict[str, Setup]) -> list[Setup]:
        """Avanza los setups activos con la vela i y crea setups nuevos si hay sweep."""
        ready = []
        for setup in list(setups.values()):
            if setup.state.is_active:
                self._advance(setup, df, i)
                if setup.state is SetupState.FVG:
                    ready.append(setup)
        self._detect_new(df, i, setups)
        return ready

    # ------------------------------------------------------------ internals
    def _advance(self, setup: Setup, df: pd.DataFrame, i: int) -> None:
        ts = df.index[i]
        close_time = ts + self.tf
        try:
            sweep_idx = df.index.get_loc(setup.sweep_time)
        except KeyError:
            setup.transition(SetupState.INVALIDATED, close_time, "vela del sweep fuera de los datos")
            setup.invalidated_reason = "data_gap"
            return
        if i <= sweep_idx:
            return

        row = df.iloc[i]
        short = setup.direction is Direction.SHORT

        elapsed = close_time - (setup.sweep_time + self.tf)
        if elapsed > timedelta(hours=self.cfg.max_setup_hours):
            self._invalidate(setup, close_time, "timeout", f"{self.cfg.max_setup_hours}h sin completar")
            return

        broke = row["high"] > setup.sweep_extreme if short else row["low"] < setup.sweep_extreme
        if broke:
            self._invalidate(setup, close_time, "sweep_extreme_broken", "precio rompió el extremo del sweep")
            return

        if setup.state is SetupState.SWEEP:
            if not is_choch(row["close"], setup.choch_level, setup.direction):
                return
            setup.choch_time = ts
            setup.transition(SetupState.CHOCH, close_time, f"cierre {row['close']:.5f} vs {setup.choch_level:.5f}")
            fvgs = find_fvgs(df, sweep_idx, i, setup.direction, self._min_fvg())
            if fvgs:
                self._complete(setup, fvgs[-1], close_time)
            elif self.cfg.fvg_max_bars_after_choch <= 0:
                self._invalidate(setup, close_time, "no_fvg", "sin FVG en la pierna del CHoCH")
            return

        # Estado CHOCH: el FVG aún puede cerrarse en las velas siguientes al CHoCH.
        choch_idx = df.index.get_loc(setup.choch_time)
        fvg = fvg_at(df, i, setup.direction, self._min_fvg()) if i - 2 >= sweep_idx else None
        if fvg is not None:
            self._complete(setup, fvg, close_time)
        elif i - choch_idx >= self.cfg.fvg_max_bars_after_choch:
            self._invalidate(setup, close_time, "no_fvg", "sin FVG en la pierna del CHoCH")

    def _complete(self, setup: Setup, fvg, close_time: pd.Timestamp) -> None:
        setup.set_fvg(fvg)
        setup.ready_time = close_time
        setup.transition(SetupState.FVG, close_time, f"FVG {fvg.low:.5f}-{fvg.high:.5f}")
        log.info("Setup %s completo: %s FVG %.5f-%.5f", setup.id, setup.direction.value, fvg.low, fvg.high)

    def _invalidate(self, setup: Setup, at: pd.Timestamp, reason: str, note: str) -> None:
        setup.invalidated_reason = reason
        setup.transition(SetupState.INVALIDATED, at, note)
        log.info("Setup %s descartado: %s", setup.id, note)

    def _min_fvg(self) -> float:
        return self.cfg.min_fvg_pips * self.cfg.pip_size

    def _blocked(self, setups: dict[str, Setup], day: str, level_name: str) -> bool:
        for s in setups.values():
            if s.trading_day != day or s.swept_level_name != level_name:
                continue
            if s.state in (SetupState.SWEEP, SetupState.CHOCH, SetupState.FVG):
                return True
            if s.state is SetupState.ALERTED and self.cfg.one_alert_per_level_per_day:
                return True
        return False

    def _detect_new(self, df: pd.DataFrame, i: int, setups: dict[str, Setup]) -> None:
        row = df.iloc[i]
        ts = df.index[i]
        sweeps = detect_sweeps(row["high"], row["low"], row["close"], row["pdh"], row["pdl"])
        if not sweeps:
            return
        if not session_allowed(ts, self.cfg.sessions):
            log.debug("Sweep en %s fuera de sesión, ignorado", ts)
            return
        day = str(row["trading_day"])
        highs = df["high"].to_numpy()
        lows = df["low"].to_numpy()
        for level_name, direction, level, extreme in sweeps:
            if self._blocked(setups, day, level_name):
                continue
            ref = choch_reference(highs, lows, i, direction, self.cfg.swing_n)
            if ref is None:
                log.info("Sweep de %s en %s sin swing previo para el CHoCH, ignorado", level_name, ts)
                continue
            swing_idx, choch_level = ref
            setup_id = f"{self.symbol}-{day}-{level_name}-{ts:%Y%m%dT%H%M}"
            setup = Setup(
                id=setup_id,
                symbol=self.symbol,
                direction=direction,
                trading_day=day,
                swept_level_name=level_name,
                swept_level=float(level),
                sweep_time=ts,
                sweep_extreme=float(extreme),
                choch_level=float(choch_level),
                choch_swing_time=df.index[swing_idx],
                updated_at=ts + self.tf,
            )
            setup.history.append(f"{(ts + self.tf).isoformat()} ->SWEEP ({level_name} {level:.5f})")
            setups[setup_id] = setup
            log.info(
                "Sweep %s %.5f en %s -> setup %s (CHoCH en %.5f)",
                level_name, level, ts, direction.value, choch_level,
            )

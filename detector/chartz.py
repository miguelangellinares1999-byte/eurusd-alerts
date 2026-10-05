"""Estrategia del curso de chart.wzrd ("Chartz"), combo intradía: estructura 1H -> POI M15 -> CDC M5.

Traducción mecánica de las reglas del libro (página entre paréntesis):

1. Estructura 1H (págs. 12-33). BOS = cierre de cuerpo más allá del weak high/low del TR;
   CDC = cierre más allá del strong low/high, cambia la tendencia. El TR va del strong point
   al extremo del impulso y solo queda definido cuando empieza el retroceso, es decir, cuando
   se confirma un pivote en ese extremo (pág. 29).
2. Solo pro-tendencia (pág. 89: "8 de cada 10 trades deben ser continuaciones"): compras con
   tendencia 1H alcista, ventas con tendencia bajista.
3. POI M15 = Order Block o wick (págs. 56-57, 67, 74): la vela con el mínimo (máximo) de la
   pierna que rompe con cierre el último swing high (low) de M15. Reglas: barre liquidez (su
   mecha supera el swing anterior), hace BOS, deja ineficiencia (FVG) en el impulso, no está
   mitigado (solo cuenta el primer toque) y está en descuento (premium) del TR 1H cuando el
   precio llega a él (págs. 26-28, 96).
4. IRL (págs. 42, 57, 96): tras el BOS se forma un swing interno de M15 en el retroceso y el
   precio lo barre camino del POI.
5. Entrada con confirmación, CE (págs. 25, 84): con el POI tocado, en M5 el precio barre un
   swing (LG) y cierra más allá del último swing contrario (CDC) dentro de una killzone
   (pág. 51: 3 h desde la apertura de Londres y de Nueva York). La entrada es una orden límite
   en el OB de M5 que origina el CDC: en su apertura (entrada 1) o en su 50% (entrada 2, pág.
   58), con el SL en su extremo. Objetivo natural: el weak high/low del TR 1H (ERL, pág. 40).

Todo es causal: cada decisión usa solo velas cerradas hasta ese momento.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import time

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from detector.config import SessionWindow
from detector.models import Direction
from detector.sessions import session_allowed

DEFAULT_KILLZONES = [
    SessionWindow("london", "Europe/London", time(8, 0), time(11, 0)),
    SessionWindow("newyork", "America/New_York", time(8, 0), time(11, 0)),
]


@dataclass
class ChartzConfig:
    htf_minutes: int = 60
    poi_minutes: int = 15
    ltf_minutes: int = 5
    # Velas a cada lado para confirmar un pivote en cada temporalidad.
    htf_swing_n: int = 3
    poi_swing_n: int = 2
    ltf_swing_n: int = 2
    # Velas M15 máximas entre el OB y la vela que hace el BOS (impulso).
    ob_max_bars: int = 12
    require_fvg: bool = True
    require_liquidity_grab: bool = True
    require_irl: bool = True
    # Un POI que no se toca en estas horas se descarta.
    max_poi_age_hours: float = 72.0
    # Horas tras el toque del POI para ver el CDC en M5.
    max_cdc_hours: float = 4.0
    # "open": entrada en la apertura del OB M5 (entrada 1); "mid": en su 50% (entrada 2);
    # "close": al cierre de la vela del CDC.
    entry: str = "open"
    min_risk_pips: float = 1.0
    pip_size: float = 0.0001
    killzones: list[SessionWindow] = field(default_factory=lambda: list(DEFAULT_KILLZONES))


@dataclass(frozen=True)
class ChartzSignal:
    time: pd.Timestamp  # cierre de la vela M5 del CDC = momento de la alerta
    direction: Direction
    entry: float
    stop_loss: float
    target_erl: float  # weak high (long) / weak low (short) del TR 1H
    tr_high: float
    tr_low: float
    poi_low: float
    poi_high: float
    poi_time: pd.Timestamp  # apertura de la vela M15 del OB
    touch_time: pd.Timestamp  # apertura de la vela M5 que toca el POI

    def risk_pips(self, pip_size: float = 0.0001) -> float:
        return abs(self.entry - self.stop_loss) / pip_size


# ---------------------------------------------------------------------- utilidades
def resample(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    agg = df.resample(f"{minutes}min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    )
    return agg.dropna()


def pivots(values: np.ndarray, n: int, high: bool) -> np.ndarray:
    """Máscara de pivotes estrictos con n velas a cada lado (mismo criterio que swings.py)."""
    v = np.asarray(values, dtype=float)
    out = np.zeros(len(v), dtype=bool)
    if len(v) < 2 * n + 1:
        return out
    win = sliding_window_view(v, 2 * n + 1)
    centre = win[:, n : n + 1]
    others = np.delete(win, n, axis=1)
    out[n : len(v) - n] = (centre > others).all(axis=1) if high else (centre < others).all(axis=1)
    return out


def _last_confirmed(pivot_idx: list[int], at: int, n: int) -> int | None:
    """Último pivote confirmado al cierre de la vela `at` (pivote p con p + n <= at)."""
    k = bisect_right(pivot_idx, at - n)
    return pivot_idx[k - 1] if k else None


def _has_fvg(h: np.ndarray, l: np.ndarray, start: int, end: int, long: bool) -> bool:
    """FVG con vela 1 >= start y vela 3 <= end."""
    for k in range(max(start + 2, 2), end + 1):
        if (long and h[k - 2] < l[k]) or (not long and l[k - 2] > h[k]):
            return True
    return False


# ---------------------------------------------------------------- estructura 1H
def htf_structure(df: pd.DataFrame, n: int, minutes: int) -> pd.DataFrame:
    """Tendencia y TR de la temporalidad alta, indexados por la hora de CIERRE de cada vela.

    trend: 1 alcista, -1 bajista, 0 sin definir. tr_high/tr_low solo valen si defined.
    """
    h, l, c = (df[k].to_numpy(float) for k in ("high", "low", "close"))
    ph, pl = pivots(h, n, True), pivots(l, n, False)
    size = len(df)
    trend_a = np.zeros(size, dtype=int)
    hi_a = np.full(size, np.nan)
    lo_a = np.full(size, np.nan)
    def_a = np.zeros(size, dtype=bool)

    trend, defined = 0, False
    strong = weak = np.nan
    strong_idx = weak_idx = leg_start = 0
    last_ph = last_pl = None
    for i in range(size):
        p = i - n
        new_ph = p >= 0 and ph[p]
        new_pl = p >= 0 and pl[p]
        if new_ph:
            last_ph = p
        if new_pl:
            last_pl = p

        if trend == 0:
            if last_ph is not None and c[i] > h[last_ph]:
                s = last_ph + int(np.argmin(l[last_ph : i + 1]))
                trend, strong, strong_idx, leg_start, defined = 1, l[s], s, i, False
            elif last_pl is not None and c[i] < l[last_pl]:
                s = last_pl + int(np.argmax(h[last_pl : i + 1]))
                trend, strong, strong_idx, leg_start, defined = -1, h[s], s, i, False
        elif trend == 1:
            if not defined and new_ph and p >= leg_start:
                w = strong_idx + int(np.argmax(h[strong_idx : i + 1]))
                weak, weak_idx, defined = h[w], w, True
            if defined and c[i] > weak:  # BOS alcista: nuevo TR
                s = weak_idx + int(np.argmin(l[weak_idx : i + 1]))
                strong, strong_idx, leg_start, defined = l[s], s, i, False
            elif c[i] < strong:  # CDC: pasa a bajista
                s = strong_idx + int(np.argmax(h[strong_idx : i + 1]))
                trend, strong, strong_idx, leg_start, defined = -1, h[s], s, i, False
        else:
            if not defined and new_pl and p >= leg_start:
                w = strong_idx + int(np.argmin(l[strong_idx : i + 1]))
                weak, weak_idx, defined = l[w], w, True
            if defined and c[i] < weak:  # BOS bajista
                s = weak_idx + int(np.argmax(h[weak_idx : i + 1]))
                strong, strong_idx, leg_start, defined = h[s], s, i, False
            elif c[i] > strong:  # CDC: pasa a alcista
                s = strong_idx + int(np.argmin(l[strong_idx : i + 1]))
                trend, strong, strong_idx, leg_start, defined = 1, l[s], s, i, False

        trend_a[i] = trend
        def_a[i] = defined
        if defined:
            hi_a[i], lo_a[i] = (weak, strong) if trend == 1 else (strong, weak)

    idx = df.index + pd.Timedelta(minutes=minutes)
    return pd.DataFrame({"trend": trend_a, "tr_high": hi_a, "tr_low": lo_a, "defined": def_a}, index=idx)


# --------------------------------------------------------------------- POIs M15
@dataclass(frozen=True)
class POI:
    direction: Direction
    ob: int  # posición de la vela del OB en M15
    bos: int  # posición de la vela que hace el BOS
    low: float
    high: float


def find_pois(df: pd.DataFrame, cfg: ChartzConfig) -> list[POI]:
    n = cfg.poi_swing_n
    h, l, c = (df[k].to_numpy(float) for k in ("high", "low", "close"))
    ph_mask, pl_mask = pivots(h, n, True), pivots(l, n, False)
    ph_list = list(np.flatnonzero(ph_mask))
    pl_list = list(np.flatnonzero(pl_mask))
    pois: list[POI] = []
    used_ph = used_pl = -1
    for j in range(len(df)):
        sh = _last_confirmed(ph_list, j, n)
        if sh is not None and sh != used_ph and c[j] > h[sh]:
            used_ph = sh
            ob = sh + int(np.argmin(l[sh : j + 1]))
            if j - ob <= cfg.ob_max_bars and _ob_rules(h, l, ob, j, pl_list, n, cfg, long=True):
                pois.append(POI(Direction.LONG, ob, j, float(l[ob]), float(h[ob])))
        sl = _last_confirmed(pl_list, j, n)
        if sl is not None and sl != used_pl and c[j] < l[sl]:
            used_pl = sl
            ob = sl + int(np.argmax(h[sl : j + 1]))
            if j - ob <= cfg.ob_max_bars and _ob_rules(h, l, ob, j, ph_list, n, cfg, long=False):
                pois.append(POI(Direction.SHORT, ob, j, float(l[ob]), float(h[ob])))
    return pois


def _ob_rules(h, l, ob, bos, opposite_pivots, n, cfg: ChartzConfig, long: bool) -> bool:
    if cfg.require_fvg and not _has_fvg(h, l, ob, bos, long):
        return False
    if cfg.require_liquidity_grab:
        prev = _last_confirmed(opposite_pivots, ob, n)
        if prev is None or prev == ob:
            return False
        if long and not l[ob] < l[prev]:
            return False
        if not long and not h[ob] > h[prev]:
            return False
    return True


def _irl_swept(h, l, poi: POI, touch: int, n: int, pivots_idx: list[int]) -> bool:
    """Un swing interno de M15 formado tras el BOS y barrido antes de (o en) el toque."""
    long = poi.direction is Direction.LONG
    start = bisect_right(pivots_idx, poi.bos)
    for q in pivots_idx[start:]:
        if q + n > touch:
            break
        after = slice(q + n + 1, touch + 1)
        if long and (l[after] < l[q]).any():
            return True
        if not long and (h[after] > h[q]).any():
            return True
    return False


# ------------------------------------------------------------------- señales
def detect_signals(ltf: pd.DataFrame, cfg: ChartzConfig | None = None) -> list[ChartzSignal]:
    """Recorre velas M5 cerradas (índice UTC = apertura) y devuelve las señales CE."""
    cfg = cfg or ChartzConfig()
    ltf = ltf[["open", "high", "low", "close"]].astype(float).sort_index()
    m15 = resample(ltf, cfg.poi_minutes)
    htf = htf_structure(resample(ltf, cfg.htf_minutes), cfg.htf_swing_n, cfg.htf_minutes)

    h15, l15, c15 = (m15[k].to_numpy(float) for k in ("high", "low", "close"))
    t15 = m15.index
    n15 = cfg.poi_swing_n
    ph15 = list(np.flatnonzero(pivots(h15, n15, True)))
    pl15 = list(np.flatnonzero(pivots(l15, n15, False)))

    o5, h5, l5, c5 = (ltf[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    t5 = ltf.index
    n5 = cfg.ltf_swing_n
    ph5 = list(np.flatnonzero(pivots(h5, n5, True)))
    pl5 = list(np.flatnonzero(pivots(l5, n5, False)))

    htf_times = htf.index
    htf_trend = htf["trend"].to_numpy()
    htf_hi = htf["tr_high"].to_numpy()
    htf_lo = htf["tr_low"].to_numpy()
    htf_def = htf["defined"].to_numpy()

    tf15 = pd.Timedelta(minutes=cfg.poi_minutes)
    tf5 = pd.Timedelta(minutes=cfg.ltf_minutes)
    max_age = int(cfg.max_poi_age_hours * 60 / cfg.poi_minutes)
    max_cdc = int(cfg.max_cdc_hours * 60 / cfg.ltf_minutes)
    min_risk = cfg.min_risk_pips * cfg.pip_size

    signals: dict[tuple, ChartzSignal] = {}
    for poi in find_pois(m15, cfg):
        long = poi.direction is Direction.LONG
        # 1) primer toque del POI en M15 tras el BOS
        seg = slice(poi.bos + 1, min(poi.bos + 1 + max_age, len(m15)))
        hits = np.flatnonzero(l15[seg] <= poi.high) if long else np.flatnonzero(h15[seg] >= poi.low)
        if not len(hits):
            continue
        touch15 = poi.bos + 1 + int(hits[0])
        if cfg.require_irl and not _irl_swept(h15, l15, poi, touch15, n15, pl15 if long else ph15):
            continue

        # 2) vela M5 exacta del toque
        s0 = int(t5.searchsorted(t15[touch15]))
        s_end = min(int(t5.searchsorted(t15[touch15] + tf15)), len(t5))
        k_touch = next(
            (k for k in range(s0, s_end) if (l5[k] <= poi.high if long else h5[k] >= poi.low)), None
        )
        if k_touch is None:
            continue

        # 3) contexto 1H en el momento del toque: tendencia y descuento/premium
        hi_pos = int(htf_times.searchsorted(t5[k_touch], side="right")) - 1
        if hi_pos < 0 or not htf_def[hi_pos]:
            continue
        trend, tr_hi, tr_lo = htf_trend[hi_pos], htf_hi[hi_pos], htf_lo[hi_pos]
        eq = (tr_hi + tr_lo) / 2
        mid = (poi.low + poi.high) / 2
        if long and not (trend == 1 and mid < eq):
            continue
        if not long and not (trend == -1 and mid > eq):
            continue

        # 4) LG + CDC en M5 dentro del POI
        sig = _find_cdc(
            poi, k_touch, min(k_touch + max_cdc, len(t5)), o5, h5, l5, c5, t5, ph5, pl5, n5, cfg, min_risk
        )
        if sig is None:
            continue
        k_cdc, entry, stop = sig
        key = (poi.direction, k_cdc)
        if key in signals:
            continue
        signals[key] = ChartzSignal(
            time=t5[k_cdc] + tf5,
            direction=poi.direction,
            entry=round(entry, 5),
            stop_loss=round(stop, 5),
            target_erl=round(float(tr_hi if long else tr_lo), 5),
            tr_high=round(float(tr_hi), 5),
            tr_low=round(float(tr_lo), 5),
            poi_low=poi.low,
            poi_high=poi.high,
            poi_time=t15[poi.ob],
            touch_time=t5[k_touch],
        )
    return sorted(signals.values(), key=lambda s: s.time)


def _find_cdc(poi, k0, k_end, o5, h5, l5, c5, t5, ph5, pl5, n, cfg, min_risk):
    long = poi.direction is Direction.LONG
    ext_idx = None
    ref = None  # nivel del CDC: último swing contrario confirmado al hacer el extremo
    grabbed = False
    for k in range(k0, k_end):
        if (long and c5[k] < poi.low) or (not long and c5[k] > poi.high):
            return None  # el POI se rompe con cierre: inválido
        new_ext = ext_idx is None or (l5[k] < l5[ext_idx] if long else h5[k] > h5[ext_idx])
        if new_ext:
            ext_idx = k
            sw = _last_confirmed(ph5 if long else pl5, k, n)
            ref = None if sw is None else (h5[sw] if long else l5[sw])
            prev = _last_confirmed(pl5 if long else ph5, k, n)
            grabbed = prev is not None and (l5[k] < l5[prev] if long else h5[k] > h5[prev])
            continue
        if ref is None or (cfg.require_liquidity_grab and not grabbed):
            continue
        if not (c5[k] > ref if long else c5[k] < ref):
            continue
        # CDC confirmado en k
        if not session_allowed(t5[k], cfg.killzones):
            return None
        if cfg.require_fvg and not _has_fvg(h5, l5, ext_idx, k, long):
            return None
        body = max(o5[ext_idx], c5[ext_idx]) if long else min(o5[ext_idx], c5[ext_idx])
        stop = l5[ext_idx] if long else h5[ext_idx]
        entry = {"open": body, "mid": (h5[ext_idx] + l5[ext_idx]) / 2, "close": c5[k]}[cfg.entry]
        if abs(entry - stop) < min_risk:
            return None
        return k, float(entry), float(stop)
    return None

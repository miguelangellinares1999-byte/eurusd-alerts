"""Estrategia "Daily Cycle" (libro de chart.wzrd) con el modelo de entrada del curso Chartz.

Ciclo diario (págs. 36-46): Asia (17:00-02:00 NY) crea la liquidez, Frankfurt (02:00-03:00)
hace una falsa tendencia o el inducement, Londres (killzone 03:00-05:00) mitiga el POI y
Nueva York (killzone 08:00-11:00) continúa o revierte. Todo se opera a favor de la estructura
intradía (el TR de la temporalidad alta, págs. 47-66).

Variaciones pro-tendencia que se detectan (alcista; la bajista es el espejo):
  V1 Normal Day / V2 Whipsaw: se barre el Asia Low (V2 si antes se barrió el Asia High) y
     aparece la confirmación. El curso exige el POI en descuento del TR.
  V3 Midline / V4 Descuento / V5 Extremo: Frankfurt rompe el Asia High y Londres vuelve
     dentro del rango sin tocar el Asia Low; la confirmación sale del 50% (V3), de la mitad
     inferior (V4) o de la parte alta (V5).
  V6 No Sweep: Frankfurt no toca ningún extremo y la confirmación aparece dentro del rango.
La variación contra tendencia de V2 (vender el barrido del Asia High) no se opera.

Entrada (curso Chartz, CE): en la temporalidad baja el precio hace un nuevo extremo que barre
un swing previo (inducement) y después cierra más allá del último swing contrario (CDC) con
ineficiencia en la pierna. Entrada al cierre de la vela del CDC, SL en el extremo. Una señal
por día, la primera.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from detector.chartz import _has_fvg, _last_confirmed, htf_structure, pivots, resample
from detector.models import Direction

NY = "America/New_York"


@dataclass
class DailyCycleConfig:
    htf_minutes: int = 240  # TR que da el sesgo (4H: el más estable en el backtest 2024-26)
    htf_swing_n: int = 3
    ltf_minutes: int = 5  # temporalidad de la confirmación (CDC)
    ltf_swing_n: int = 2
    asia_start_hour: int = 17  # hora de NY del día anterior
    frankfurt_hour: int = 2
    london_hour: int = 3
    # Ventanas (horas NY, [inicio, fin)) en las que puede cerrarse la vela del CDC.
    windows: list[tuple[int, int]] = field(default_factory=lambda: [(2, 5)])
    require_fvg: bool = True
    # True: el CHoCH lo hace una sola vela de desplazamiento que deja FVG con la anterior y la
    # siguiente (la señal sale al cierre de esa vela siguiente).
    single_candle_inf: bool = False
    # "close": al cierre de la vela de la señal | "fvg_mid": límite al 50% del FVG del CHoCH.
    entry: str = "close"
    require_discount_v1: bool = True  # V1/V2: POI en descuento (premium) del TR de 1H
    variations: tuple[str, ...] = ("V1", "V2", "V3", "V4", "V5", "V6")
    min_risk_pips: float = 1.5
    pip_size: float = 0.0001


@dataclass(frozen=True)
class DailyCycleSignal:
    time: pd.Timestamp  # cierre de la vela que completa la señal
    direction: Direction
    variation: str
    entry: float
    stop_loss: float
    asia_high: float
    asia_low: float
    day_extreme: float  # máximo (long) / mínimo (short) del día hasta la señal: liquidez a por la que ir
    tr_high: float
    tr_low: float


def detect_signals(ltf: pd.DataFrame, cfg: DailyCycleConfig | None = None) -> list[DailyCycleSignal]:
    """`ltf`: velas cerradas (índice UTC = apertura) de la temporalidad de confirmación o menor."""
    cfg = cfg or DailyCycleConfig()
    ltf = ltf[["open", "high", "low", "close"]].astype(float).sort_index()
    if cfg.ltf_minutes > 1 or len(ltf) < 2 or (ltf.index[1] - ltf.index[0]) < pd.Timedelta(minutes=cfg.ltf_minutes):
        ltf = resample(ltf, cfg.ltf_minutes)
    htf = htf_structure(resample(ltf, cfg.htf_minutes), cfg.htf_swing_n, cfg.htf_minutes)

    t = ltf.index
    tny = t.tz_convert(NY)
    o, h, l, c = (ltf[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    n = cfg.ltf_swing_n
    ph = list(np.flatnonzero(pivots(h, n, True)))
    pl = list(np.flatnonzero(pivots(l, n, False)))
    bar = pd.Timedelta(minutes=cfg.ltf_minutes)
    min_risk = cfg.min_risk_pips * cfg.pip_size
    last_hour = max(e for _, e in cfg.windows)

    signals = []
    for day in pd.Index(tny.normalize()).unique():
        if day.dayofweek >= 5:
            continue
        frank = day + pd.Timedelta(hours=cfg.frankfurt_hour)
        asia = slice(t.searchsorted((frank - pd.Timedelta(hours=24 + cfg.frankfurt_hour - cfg.asia_start_hour)).tz_convert("UTC")),
                     t.searchsorted(frank.tz_convert("UTC")))
        if asia.stop - asia.start < 6 * 60 / cfg.ltf_minutes:  # rango de Asia incompleto
            continue
        ah, al = h[asia].max(), l[asia].min()
        pos = int(htf.index.searchsorted(frank.tz_convert("UTC"), side="right")) - 1
        if pos < 0 or not htf["defined"].iat[pos]:
            continue
        trend = int(htf["trend"].iat[pos])
        tr_hi, tr_lo = float(htf["tr_high"].iat[pos]), float(htf["tr_low"].iat[pos])
        start = asia.stop
        end = int(t.searchsorted((day + pd.Timedelta(hours=last_hour)).tz_convert("UTC")))
        sig = _scan_day(trend, ah, al, tr_hi, tr_lo, start, end, o, h, l, c, t, tny, ph, pl, n, cfg, min_risk)
        if sig is not None:
            k, variation, entry, stop, day_ext = sig
            signals.append(DailyCycleSignal(
                time=t[k] + bar, direction=Direction.LONG if trend == 1 else Direction.SHORT,
                variation=variation, entry=round(entry, 5), stop_loss=round(stop, 5),
                asia_high=round(ah, 5), asia_low=round(al, 5), day_extreme=round(day_ext, 5),
                tr_high=round(tr_hi, 5), tr_low=round(tr_lo, 5),
            ))
    return signals


def _in_windows(ts_ny: pd.Timestamp, windows) -> bool:
    hour = ts_ny.hour + ts_ny.minute / 60
    return any(a <= hour < b for a, b in windows)


def _scan_day(trend, ah, al, tr_hi, tr_lo, start, end, o, h, l, c, t, tny, ph, pl, n, cfg, min_risk):
    long = trend == 1
    # Para no duplicar código, en cortos se trabaja con precios invertidos.
    s = 1.0 if long else -1.0
    lo_, hi_ = (l, h) if long else (-h, -l)
    cl = s * c
    a_lo, a_hi = (al, ah) if long else (-ah, -al)  # extremo a barrer / extremo "a favor"
    piv_ext, piv_ref = (pl, ph) if long else (ph, pl)
    eq = (tr_hi + tr_lo) / 2

    swept_against = swept_favor = False  # Asia Low / High (en long) barridos desde las 02:00
    favor_first = False
    frank_broke_favor = frank_broke_any = False
    ext = ref = None
    grabbed = False
    for k in range(start, end):
        ny_hour = tny[k].hour
        in_frank = ny_hour < cfg.london_hour
        if hi_[k] > a_hi and not swept_favor:
            swept_favor = True
            favor_first = not swept_against
        if lo_[k] < a_lo:
            swept_against = True
        if in_frank:
            frank_broke_favor |= hi_[k] > a_hi
            frank_broke_any |= hi_[k] > a_hi or lo_[k] < a_lo

        if ext is None or lo_[k] < lo_[ext]:
            ext = k
            sw = _last_confirmed(piv_ref, k, n)
            ref = None if sw is None else (h[sw] if long else -l[sw])
            prev = _last_confirmed(piv_ext, k, n)
            grabbed = prev is not None and (lo_[k] < (l[prev] if long else -h[prev]))
            continue
        if ref is None or not grabbed or not cl[k] > ref:
            continue
        # CDC en k: cierre más allá del último swing contrario tras el barrido
        if not _in_windows(tny[k], cfg.windows):
            ext = None  # el CDC fuera de horario no vale; se busca otro
            continue
        sig_k, entry = k, float(c[k])
        if cfg.single_candle_inf:
            j = k + 1
            if j >= len(c):
                return None
            if not ((h[k - 1] < l[j]) if long else (l[k - 1] > h[j])):
                ext = None
                continue
            if cfg.entry == "fvg_mid":
                entry = float((h[k - 1] + l[j]) / 2 if long else (l[k - 1] + h[j]) / 2)
            else:
                entry = float(c[j])
            sig_k = j
        elif cfg.require_fvg and not _has_fvg(h, l, ext, k, long):
            ext = None
            continue
        extreme = lo_[ext]
        if extreme < a_lo:
            variation = "V2" if favor_first else "V1"
            real_ext = l[ext] if long else h[ext]
            if cfg.require_discount_v1 and not (real_ext < eq if long else real_ext > eq):
                return None
        elif extreme <= a_hi:
            if frank_broke_favor and not swept_against:
                frac = (extreme - a_lo) / (a_hi - a_lo)
                variation = "V5" if frac >= 0.6 else ("V3" if frac >= 0.4 else "V4")
            elif not frank_broke_any:
                variation = "V6"
            else:
                ext = None
                continue
        else:
            ext = None  # confirmación por encima del rango: no es un Daily Cycle
            continue
        if variation not in cfg.variations:
            return None
        stop = float(l[ext] if long else h[ext])
        if abs(entry - stop) < min_risk:
            return None
        day_ext = float(h[start - 1 : sig_k + 1].max() if long else l[start - 1 : sig_k + 1].min())
        day_ext = max(day_ext, ah) if long else min(day_ext, al)
        return sig_k, variation, entry, stop, day_ext
    return None

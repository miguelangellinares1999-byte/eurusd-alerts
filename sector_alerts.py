"""Alerta semanal de rotación sectorial en S&P 500 y Nasdaq 100 (estrategia de backtest/sector_rotation.py).

Cada semana (sábado, con el cierre del viernes ya publicado en Yahoo):
- ordena los ETFs sectoriales SPDR por fuerza relativa frente a SPY (13 y 26 semanas) y se queda
  con los `top` mejores que estén sobre su media de 40 semanas (sectores líderes),
- comprueba que SPY está sobre su media de 40 semanas (mercado alcista),
- busca en las acciones de esos sectores el modelo: dos semanas alcistas con FVG, barrida del
  último low semanal y cierre por encima. Entrada al cierre del viernes, SL en el mínimo de la
  semana de la barrida, TP en `tp_r` R.

Como en el backtest, cada operación arriesga `risk_pct` del capital, con un máximo de
`max_positions` abiertas y `max_alloc_pct` del capital por posición. Si hay más señales que
huecos libres se eligen las de los sectores más fuertes. Sigue las operaciones avisadas hasta
SL o TP. Manda un email HTML cada semana y guarda en JSON lo avisado para no repetirlo.
"""

from __future__ import annotations

import html
import json
import logging
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from backtest.market import load_daily
from backtest.sector_rotation import SECTOR_ETF, market_bull, sector_strength, setups, simulate_exits, universe
from backtest.weekly import weekly_bars
from notifier import Notifier

log = logging.getLogger("eurusd_alerts.sector")

LOOKBACK_DAYS = 800  # MM de 40 semanas + fuerza relativa a 26 semanas + margen


@dataclass
class SectorAlerts:
    enabled: bool = False
    dry_run: bool = True
    universe: str = "ambos"  # sp500, ndx o ambos
    capital: float = 10_000.0
    risk_pct: float = 1.0
    max_positions: int = 10
    max_alloc_pct: float = 20.0  # nocional máximo por posición en % del capital
    top: int = 4  # sectores líderes
    window: int = 10  # semanas tras el impulso en las que vale la barrida
    tp_r: float = 3.0
    min_risk_pct: float = 1.0  # se ignoran barridas con el SL a menos de este % de la entrada
    lot_step: float = 1.0  # acciones mínimas por orden
    weekly_summary: bool = True  # email también las semanas sin señal
    state_path: Path = Path("state/sector_alerts.json")


# ------------------------------------------------------------------- estado
class SectorStore:
    def __init__(self, path: Path):
        self.path = path
        self.alerted: list[str] = []  # "TICKER:semana" ya avisados
        self.positions: list[dict] = []  # operaciones avisadas y su estado

    def load(self) -> "SectorStore":
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.alerted = list(raw.get("alerted", []))
                self.positions = list(raw.get("positions", []))
            except (json.JSONDecodeError, ValueError):
                log.error("Estado de alertas sectoriales corrupto en %s; se empieza de cero", self.path)
        return self

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"alerted": self.alerted[-1000:], "positions": self.positions[-300:]}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".sec-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


# ------------------------------------------------------------------- datos
@dataclass
class SectorData:
    spy: pd.DataFrame
    etfs: dict[str, pd.DataFrame]  # ticker del ETF -> velas diarias
    stocks: dict[str, pd.DataFrame]
    sectors: dict[str, str]  # ticker -> sector


def download(cfg: SectorAlerts, now: pd.Timestamp, cache_dir: Path | None = None) -> SectorData:
    start = (now - pd.Timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    sectors = universe(cfg.universe)
    ref = load_daily(["SPY", *SECTOR_ETF.values()], start, cache_dir)
    return SectorData(spy=ref.pop("SPY"), etfs=ref, stocks=load_daily(list(sectors), start, cache_dir), sectors=sectors)


# ------------------------------------------------------------------ informe
def size(cfg: SectorAlerts, entry: float, stop: float, capital: float | None = None) -> dict:
    """Acciones para arriesgar `risk_pct` del capital, sin pasar de `max_alloc_pct` en nocional."""
    capital = cfg.capital if capital is None else capital
    risk_money = capital * cfg.risk_pct / 100
    raw = min(risk_money / (entry - stop), capital * cfg.max_alloc_pct / 100 / entry)
    units = math.floor(raw / cfg.lot_step) * cfg.lot_step
    return {
        "units": units,
        "notional": round(units * entry, 2),
        "risk_money": round(units * (entry - stop), 2),
        "capped": raw < risk_money / (entry - stop),
        "below_min_lot": units < cfg.lot_step,
    }


def build_report(cfg: SectorAlerts, data: SectorData, store: SectorStore, now: pd.Timestamp, window_days: int = 14) -> dict:
    """Sectores líderes de la última semana cerrada y señales de las últimas `window_days` aún no avisadas."""
    week = weekly_bars(data.spy).index[-1]
    rs, trend, rank = sector_strength(data.etfs, data.spy)
    bull = market_bull(data.spy)
    spy_w = data.spy["close"].resample("W-FRI").last()
    sectors = []
    for sec, etf in SECTOR_ETF.items():
        if sec not in rs.columns:
            continue
        r = rank[sec].get(week)
        sectors.append({
            "sector": sec, "etf": etf,
            "rs_pct": round(100 * float(rs[sec].get(week, float("nan"))), 2),
            "above_ma40": bool(trend[sec].get(week, False)),
            "rank": None if pd.isna(r) else int(r),
            "leader": bool(not pd.isna(r) and r <= cfg.top),
        })
    sectors.sort(key=lambda s: (s["rank"] is None, s["rank"] or 0, -s["rs_pct"]))

    positions = update_positions(cfg, data, store)
    open_now = sum(p["status"] == "open" for p in positions)
    candidates = []
    for tk, daily in data.stocks.items():
        sec = data.sectors.get(tk)
        if sec not in rank.columns:
            continue
        sig = setups(daily, cfg.window, "1R")
        sig = sig[(sig.index > week - pd.Timedelta(days=window_days)) & (sig.index <= week)]
        for when, s in sig.iterrows():
            key = f"{tk}:{when.date()}"
            r = rank[sec].get(when)
            risk = s["entry"] - s["stop_loss"]
            if key in store.alerted or risk < cfg.min_risk_pct / 100 * s["entry"]:
                continue
            if pd.isna(r) or r > cfg.top or not bool(bull.get(when, False)):
                continue
            if any(p["ticker"] == tk and p["status"] == "open" for p in positions):
                continue  # una posición por valor, como en el backtest
            candidates.append({
                "ticker": tk, "sector": sec, "etf": SECTOR_ETF[sec], "sector_rank": int(r),
                "week": str(when.date()), "last_day": str(s["last_day"].date()),
                "entry": round(float(s["entry"]), 2), "stop_loss": round(float(s["stop_loss"]), 2),
                "take_profit": round(float(s["entry"] + cfg.tp_r * risk), 2),
                "risk_pct": round(float(100 * risk / s["entry"]), 2),
                "last": round(float(daily["close"].iloc[-1]), 2),
                # Señal de una semana anterior (p. ej. si falló la ejecución del sábado): la entrada
                # al cierre ya pasó, se muestra solo como aviso y no ocupa hueco.
                "stale": when != week,
            } | size(cfg, float(s["entry"]), float(s["stop_loss"])))

    # Huecos libres: primero los sectores más fuertes y, dentro de cada uno, el stop más ajustado.
    candidates.sort(key=lambda c: (c["stale"], c["sector_rank"], c["risk_pct"], c["ticker"]))
    free = max(cfg.max_positions - open_now, 0)
    for c in candidates:
        c["chosen"] = free > 0 and not c["below_min_lot"] and not c["stale"]
        free -= c["chosen"]

    return {
        "generated": now.isoformat(),
        "week": str(week.date()),
        "spy_close": round(float(spy_w.get(week, float("nan"))), 2),
        "spy_ma40": round(float(spy_w.rolling(40).mean().get(week, float("nan"))), 2),
        "market_bull": bool(bull.get(week, False)),
        "sectors": sectors,
        "signals": candidates,
        "positions": positions,
        "open_positions": open_now,
        "universe": cfg.universe,
        "n_stocks": len(data.stocks),
        "capital": cfg.capital, "risk_pct": cfg.risk_pct, "tp_r": cfg.tp_r,
        "top": cfg.top, "max_positions": cfg.max_positions, "max_alloc_pct": cfg.max_alloc_pct,
    }


def update_positions(cfg: SectorAlerts, data: SectorData, store: SectorStore) -> list[dict]:
    """Recalcula el estado de cada operación avisada con las velas diarias más recientes."""
    out = []
    for p in store.positions:
        if p.get("status") in ("win", "loss") and p.get("reported"):
            continue
        daily = data.stocks.get(p["ticker"])
        if daily is None:
            continue
        sig = pd.DataFrame([{"last_day": pd.Timestamp(p["last_day"]), "entry": p["entry"], "stop_loss": p["stop_loss"],
                             "take_profit": p["take_profit"]}])
        r_mult = (p["take_profit"] - p["entry"]) / (p["entry"] - p["stop_loss"])
        t = simulate_exits(p["ticker"], daily, sig, f"{r_mult:g}R")
        if not t:
            continue
        t = t[0]
        last = float(daily["close"].iloc[-1])
        p["status"] = t.result
        p["exit_date"] = None if t.exit_date is None else str(t.exit_date.date())
        p["r_now"] = round((last - p["entry"]) / (p["entry"] - p["stop_loss"]), 2) if t.result == "open" else round(t.r, 2)
        p["last"] = round(last, 2)
        out.append(dict(p))
    return out


# -------------------------------------------------------------------- email
def _num(v: float, d: int = 2) -> str:
    return f"{v:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _tag(s: dict) -> str:
    if s["chosen"]:
        return "COMPRAR"
    if s["stale"]:
        return "señal de una semana anterior (la entrada ya pasó)"
    return "bajo el lote mínimo" if s["below_min_lot"] else "sin hueco libre"


def format_report(rep: dict) -> tuple[str, str, str]:
    """Asunto, texto plano y HTML del informe semanal."""
    chosen = [s for s in rep["signals"] if s.get("chosen")]
    open_n = rep["open_positions"]
    if chosen:
        subject = "[Sectores] COMPRA " + " | ".join(f"{s['ticker']} {_num(s['entry'])} SL {_num(s['stop_loss'])}" for s in chosen[:4])
        if len(chosen) > 4:
            subject += f" | +{len(chosen) - 4}"
    else:
        subject = f"[Sectores] Semana {rep['week']}: sin señal" + (f" · {open_n} abiertas" if open_n else "")

    leaders = [s for s in rep["sectors"] if s["leader"]]
    lines = [f"Rotación sectorial · semana que cierra el {rep['week']}", ""]
    lines.append(f"SPY {_num(rep['spy_close'])} vs MM40 {_num(rep['spy_ma40'])} -> mercado {'alcista' if rep['market_bull'] else 'NO alcista (no se opera)'}")
    lines.append("Sectores líderes: " + (", ".join(f"{s['rank']}. {s['sector']} ({s['etf']}, {_num(s['rs_pct'])} %)" for s in leaders) or "ninguno"))
    lines.append("")
    for s in rep["signals"]:
        tag = _tag(s)
        lines += [
            f"{s['ticker']} · {s['sector']} (#{s['sector_rank']}) · {tag}",
            f"  Entrada (cierre del {s['last_day']}): {_num(s['entry'])}" + (f" · precio actual {_num(s['last'])}" if s["stale"] else ""),
            f"  Stop loss: {_num(s['stop_loss'])} ({_num(s['risk_pct'])} %)",
            f"  Take profit {rep['tp_r']:g}R: {_num(s['take_profit'])}",
            f"  Acciones: {_num(s['units'], 0)} · {_num(s['notional'])} invertidos · riesgo {_num(s['risk_money'])}"
            + (f" (limitado al {rep['max_alloc_pct']:g} % del capital)" if s["capped"] else ""),
            "",
        ]
    for p in rep["positions"]:
        state = {"open": f"abierta, {_num(p['r_now'])}R", "win": f"cerrada en TP ({p['exit_date']})",
                 "loss": f"cerrada en SL ({p['exit_date']})"}[p["status"]]
        lines.append(f"{p['ticker']} del {p['week']}: {state} · último cierre {_num(p['last'])}")
    lines += ["", f"Posiciones abiertas: {open_n} de {rep['max_positions']}.",
              "Precios de Yahoo (ajustados por splits). Alerta automática, no es una recomendación de inversión."]
    return subject, "\n".join(lines), render_html(rep)


def render_html(rep: dict) -> str:
    """Email con aspecto de página web: estilos en línea y tablas (lo que entienden los clientes de correo)."""
    e = html.escape
    ink, muted, line, blue, green, red, bg = "#14171c", "#6b7280", "#e1e4ea", "#2a5bd7", "#14804a", "#c2362f", "#f6f7f9"

    def badge(ok: bool, yes: str = "Sí", no: str = "No") -> str:
        c, b = (green, "#e8f5ee") if ok else (muted, "#eef0f3")
        return f'<span style="display:inline-block;padding:2px 10px;border-radius:99px;background:{b};color:{c};font-size:12px;font-weight:600">{e(yes if ok else no)}</span>'

    def row(label: str, value: str) -> str:
        return (f'<tr><td style="padding:8px 0;color:{muted};font-size:14px;border-bottom:1px solid {line}">{e(label)}</td>'
                f'<td style="padding:8px 0;text-align:right;font-family:Consolas,monospace;font-size:14px;color:{ink};border-bottom:1px solid {line}">{value}</td></tr>')

    def card(inner: str, accent: str | None = None) -> str:
        border = f"border-left:4px solid {accent};" if accent else ""
        return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#fff;border:1px solid {line};'
                f'{border}border-radius:8px;margin:0 0 16px"><tr><td style="padding:18px 20px">{inner}</td></tr></table>')

    def table(rows: str) -> str:
        return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:8px">{rows}</table>'

    chosen = [s for s in rep["signals"] if s.get("chosen")]
    title = (f"Comprar {', '.join(s['ticker'] for s in chosen)}" if chosen else "Sin señal esta semana")
    parts = [card(f'<div style="font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:{muted}">'
                  f'Rotación sectorial · semana que cierra el {e(rep["week"])}</div>'
                  f'<h1 style="margin:6px 0 4px;font-size:26px;line-height:1.2;color:{ink}">{e(title)}</h1>'
                  f'<p style="margin:0;color:{muted};font-size:14px">Barrida del low semanal tras dos semanas alcistas con FVG, '
                  f'en los {rep["top"]} sectores líderes. TP {rep["tp_r"]:g}R.</p>')]

    for s in rep["signals"]:
        tag = badge(s["chosen"], "Comprar", _tag(s).capitalize())
        body = (f'<strong style="font-size:18px;color:{ink}">{e(s["ticker"])}</strong> '
                f'<span style="color:{muted};font-size:14px">· {e(s["sector"])} (#{s["sector_rank"]}, {e(s["etf"])})</span>'
                f'<div style="margin:6px 0 8px">{tag}</div>'
                + table(row(f"Entrada (cierre del {s['last_day']})", f'<strong>{_num(s["entry"])}</strong>')
                        + (row("Precio actual", _num(s["last"])) if s["stale"] else "")
                        + row("Stop loss", f'<span style="color:{red}">{_num(s["stop_loss"])}</span> ({_num(s["risk_pct"])} %)')
                        + row(f"Take profit {rep['tp_r']:g}R", f'<span style="color:{green}">{_num(s["take_profit"])}</span>')
                        + row(f"Acciones ({_num(rep['capital'], 0)} · {rep['risk_pct']:g} %)", f'<strong>{_num(s["units"], 0)}</strong>'
                              + (f' <span style="color:{muted}">(tope {rep["max_alloc_pct"]:g} %)</span>' if s["capped"] else ""))
                        + row("Invertido / riesgo", f'{_num(s["notional"])} / {_num(s["risk_money"])}')))
        parts.append(card(body, blue if s["chosen"] else line))

    sec_rows = row("SPY sobre su MM40", f'{_num(rep["spy_close"])} / {_num(rep["spy_ma40"])} ' + badge(rep["market_bull"]))
    for s in rep["sectors"]:
        label = f'{"#" + str(s["rank"]) if s["rank"] else "—"} {s["sector"]} ({s["etf"]})'
        color = green if s["rs_pct"] > 0 else red
        val = f'<span style="color:{color}">{_num(s["rs_pct"])} %</span> ' + (badge(True, "Líder") if s["leader"] else
                                                                           badge(False, no="bajo MM40" if not s["above_ma40"] else "—"))
        sec_rows += row(label, val)
    parts.append(card(f'<strong style="font-size:16px;color:{ink}">Sectores: fuerza relativa frente a SPY</strong>' + table(sec_rows)))

    if rep["positions"]:
        rows = ""
        for p in rep["positions"]:
            color = {"open": blue, "win": green, "loss": red}[p["status"]]
            label = {"open": f"Abierta · {_num(p['r_now'])}R", "win": f"TP · {p['exit_date']}", "loss": f"SL · {p['exit_date']}"}[p["status"]]
            rows += row(f"{p['ticker']} del {p['week']}", f'<span style="color:{color}">{e(label)}</span>')
        parts.append(card(f'<strong style="font-size:16px;color:{ink}">Operaciones avisadas ({rep["open_positions"]} de '
                          f'{rep["max_positions"]} abiertas)</strong>' + table(rows)))

    foot = (f'<p style="margin:8px 0 0;color:{muted};font-size:12px;line-height:1.5">Precios de Yahoo ajustados por splits; '
            f'{rep["n_stocks"]} valores ({e(rep["universe"])}). Alerta automática, no es una recomendación de inversión.</p>')
    return (f'<!doctype html><html><body style="margin:0;padding:24px 12px;background:{bg};font-family:Segoe UI,Helvetica,Arial,sans-serif">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px"><tr><td>{"".join(parts)}{foot}</td></tr></table>'
            "</td></tr></table></body></html>")


# ------------------------------------------------------------------- pasada
def _remember(store: SectorStore, signals: list[dict]) -> None:
    """Marca las señales como avisadas y empieza a seguir las elegidas."""
    for s in signals:
        key = f"{s['ticker']}:{s['week']}"
        if key not in store.alerted:
            store.alerted.append(key)
        if s.get("chosen"):
            store.positions.append({k: s[k] for k in ("ticker", "sector", "week", "last_day", "entry", "stop_loss", "take_profit", "units")}
                                   | {"status": "open"})


def run_sector_alerts(
    cfg: SectorAlerts,
    store: SectorStore,
    notifier: Notifier,
    now: pd.Timestamp | None = None,
    data: SectorData | None = None,
) -> dict:
    """Descarga, calcula el informe, lo envía y actualiza el estado. Devuelve el informe."""
    now = pd.Timestamp.now(tz="UTC") if now is None else now
    data = data or download(cfg, now)
    if not store.alerted and not store.positions:
        # Primera pasada: las señales de semanas anteriores se dan por avisadas sin email.
        seed = build_report(cfg, data, store, now)
        _remember(store, [s for s in seed["signals"] if s["stale"]])
    rep = build_report(cfg, data, store, now)
    if rep["signals"] or cfg.weekly_summary:
        subject, text, page = format_report(rep)
        notifier.send(subject, text, html_body=page)
    _remember(store, rep["signals"])
    for p in store.positions:
        if p.get("status") in ("win", "loss"):
            p["reported"] = True
    store.save()
    return rep

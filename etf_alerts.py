"""Alerta diaria de reversión a la media en SPY y QQQ (estrategia de backtest/etf_meanrev.py).

Se ejecuta poco antes del cierre de EE. UU. (por defecto entre las 15:00 y las 16:00 de Nueva York)
con la vela del día aún abierta, para poder entrar al cierre como en el backtest:
- Entrada: cierre por encima de la MM200 y RSI(2) < 10 -> COMPRAR al cierre, stop a `stop_atr` ATR(14).
- Salida: cierre por encima de la MM5 o `max_days` sesiones -> VENDER al cierre.
- Stop: si el mínimo toca el stop desde el día siguiente a la entrada, la operación se da por cerrada.

Solo manda email cuando hay algo que hacer (comprar, vender o stop tocado), salvo `daily_summary`.
Guarda en JSON las posiciones avisadas y los días ya procesados para no repetir avisos.
"""

from __future__ import annotations

import html
import json
import logging
import math
import os
import tempfile
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

import pandas as pd
import requests

from backtest.etf_meanrev import MAX_DAYS, RSI_IN, indicators
from data.yahoo_source import HEADERS, URL, YahooError
from notifier import Notifier

log = logging.getLogger("eurusd_alerts.etf")

NY = "America/New_York"


@dataclass(frozen=True)
class EtfSymbol:
    ticker: str  # Yahoo
    mt5: str


@dataclass
class EtfAlerts:
    enabled: bool = False
    dry_run: bool = True
    capital: float = 10_000.0
    alloc_pct: float = 50.0  # % del capital por ETF (backtest: a partes iguales, sin apalancamiento)
    stop_atr: float = 3.0
    max_days: int = MAX_DAYS
    lot_step: float = 1.0  # acciones por lote mínimo en MT5
    window: tuple[time, time] = (time(15, 0), time(16, 0))  # hora de Nueva York
    daily_summary: bool = False  # email también los días sin nada que hacer
    state_path: Path = Path("state/etf_alerts.json")
    symbols: list[EtfSymbol] = field(default_factory=lambda: [EtfSymbol("SPY", "SPY.P"), EtfSymbol("QQQ", "QQQ.OQ")])


# ------------------------------------------------------------------- estado
class EtfStore:
    def __init__(self, path: Path):
        self.path = path
        self.positions: dict[str, dict] = {}  # ticker -> operación avisada abierta
        self.done: list[str] = []  # días (Nueva York) ya procesados
        self.history: list[dict] = []  # operaciones cerradas

    def load(self) -> "EtfStore":
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.positions = dict(raw.get("positions", {}))
                self.done = list(raw.get("done", []))
                self.history = list(raw.get("history", []))
            except (json.JSONDecodeError, ValueError):
                log.error("Estado de alertas de ETFs corrupto en %s; se empieza de cero", self.path)
        return self

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"positions": self.positions, "done": self.done[-400:], "history": self.history[-400:]}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".etf-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


# ------------------------------------------------------------------- datos
def get_daily_live(ticker: str, session=None) -> pd.DataFrame:
    """Velas diarias de ~2 años de Yahoo INCLUYENDO la del día en curso (cierre = último precio)."""
    http = session or requests.Session()
    resp = http.get(URL.format(ticker=ticker), params={"interval": "1d", "range": "2y"}, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    chart = resp.json().get("chart", {})
    if chart.get("error") or not chart.get("result"):
        raise YahooError(f"Yahoo no devolvió velas de {ticker}: {chart.get('error')}")
    res = chart["result"][0]
    q = res["indicators"]["quote"][0]
    days = pd.to_datetime(res["timestamp"], unit="s", utc=True).tz_convert(NY).tz_localize(None).normalize()
    out = pd.DataFrame({k: q[k] for k in ("open", "high", "low", "close")}, index=days).dropna().astype(float)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    price = res.get("meta", {}).get("regularMarketPrice")
    if price:
        out.iloc[-1, out.columns.get_loc("close")] = float(price)
    out["high"] = out[["open", "high", "low", "close"]].max(axis=1)
    out["low"] = out[["open", "high", "low", "close"]].min(axis=1)
    out.index.name = "date"
    return out


def download(cfg: EtfAlerts) -> dict[str, pd.DataFrame]:
    session = requests.Session()
    return {s.ticker: get_daily_live(s.ticker, session) for s in cfg.symbols}


# ------------------------------------------------------------------ informe
def size(cfg: EtfAlerts, entry: float, stop: float) -> dict:
    """Acciones del CFD para invertir `alloc_pct` del capital y el dinero que se pierde si salta el stop."""
    raw = cfg.capital * cfg.alloc_pct / 100 / entry
    units = math.floor(raw / cfg.lot_step) * cfg.lot_step
    return {
        "units": units,
        "notional": round(units * entry, 2),
        "risk_money": round(units * (entry - stop), 2),
        "below_min_lot": units < cfg.lot_step,
    }


def evaluate(cfg: EtfAlerts, sym: EtfSymbol, daily: pd.DataFrame, position: dict | None) -> dict:
    """Qué hacer hoy con un ETF: entry / exit / exit_time / stop / hold / none."""
    ind = indicators(daily)
    last, today = ind.iloc[-1], ind.index[-1]
    info = {
        "ticker": sym.ticker, "mt5": sym.mt5, "day": str(today.date()),
        "close": round(float(last["close"]), 2), "rsi2": round(float(last["rsi2"]), 1),
        "sma200": round(float(last["sma200"]), 2), "sma5": round(float(last["sma5"]), 2),
        "above_sma200": bool(last["close"] > last["sma200"]),
    }
    if position:
        entry_day = pd.Timestamp(position["entry_day"])
        after = daily[daily.index > entry_day]
        held = len(after)
        pnl = round(100 * (float(last["close"]) / position["entry"] - 1), 2)
        info |= {"position": position, "days_held": held, "pnl_pct": pnl}
        if held and (after["low"] <= position["stop"]).any():
            hit = after[after["low"] <= position["stop"]].index[0]
            return info | {"action": "stop", "stop_day": str(hit.date()),
                           "pnl_pct": round(100 * (position["stop"] / position["entry"] - 1), 2)}
        if held and last["close"] > last["sma5"]:
            return info | {"action": "exit"}
        if held >= cfg.max_days:
            return info | {"action": "exit_time"}
        return info | {"action": "hold"}
    if bool(last["entry_signal"]):
        entry = float(last["close"])
        stop = entry - cfg.stop_atr * float(last["atr"])
        return info | {"action": "entry", "entry": round(entry, 2), "stop": round(stop, 2),
                       "stop_pct": round(100 * (stop / entry - 1), 2)} | size(cfg, entry, stop)
    return info | {"action": "none"}


ACTION_TEXT = {
    "entry": "COMPRAR al cierre", "exit": "VENDER al cierre (rebote: cierre sobre la MM5)",
    "exit_time": f"VENDER al cierre (límite de {MAX_DAYS} sesiones)", "stop": "Stop tocado: operación cerrada",
    "hold": "Mantener", "none": "Sin señal",
}


def _num(v: float, d: int = 2) -> str:
    return f"{v:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def format_report(rep: dict) -> tuple[str, str, str]:
    """Asunto, texto plano y HTML."""
    acts = [r for r in rep["etfs"] if r["action"] in ("entry", "exit", "exit_time", "stop")]
    if acts:
        parts = []
        for r in acts:
            if r["action"] == "entry":
                parts.append(f"COMPRA {r['ticker']} ({r['mt5']}) ~{_num(r['entry'])} stop {_num(r['stop'])}")
            elif r["action"] == "stop":
                parts.append(f"STOP {r['ticker']} {_num(r['pnl_pct'])} %")
            else:
                parts.append(f"VENTA {r['ticker']} ({r['mt5']}) {_num(r['pnl_pct'])} %")
        subject = "[ETF] " + " | ".join(parts)
    else:
        subject = f"[ETF] {rep['day']}: sin operaciones"

    lines = [f"Reversión a la media en SPY y QQQ · {rep['day']} {rep['time_ny']} (Nueva York)", ""]
    for r in rep["etfs"]:
        lines.append(f"{r['ticker']} ({r['mt5']}): {ACTION_TEXT[r['action']]}")
        lines.append(f"  Precio {_num(r['close'])} · RSI(2) {_num(r['rsi2'], 1)} · MM200 {_num(r['sma200'])} · MM5 {_num(r['sma5'])}")
        if r["action"] == "entry":
            lines += [
                f"  Entrada aprox.: {_num(r['entry'])} (precio actual; entra al cierre)",
                f"  Stop loss {rep['stop_atr']:g} ATR: {_num(r['stop'])} ({_num(r['stop_pct'])} %)",
                f"  Salida: primer cierre por encima de la MM5, o a las {rep['max_days']} sesiones",
                f"  Tamaño ({_num(rep['alloc_pct'], 0)} % de {_num(rep['capital'], 0)}): {_num(r['units'], 0)} acciones"
                f" · {_num(r['notional'])} invertidos · pérdida si salta el stop {_num(r['risk_money'])}"
                + (" · POR DEBAJO DEL LOTE MÍNIMO" if r["below_min_lot"] else ""),
            ]
        elif "position" in r:
            p = r["position"]
            lines.append(f"  Comprado el {p['entry_day']} a {_num(p['entry'])} · stop {_num(p['stop'])} · "
                         f"{r['days_held']} sesiones · {_num(r['pnl_pct'])} %")
        lines.append("")
    lines += ["Precios de Yahoo con la vela de hoy aún abierta: si al cierre cambia mucho, revisa la señal.",
              "Alerta automática, no es una recomendación de inversión."]
    return subject, "\n".join(lines), render_html(rep)


def render_html(rep: dict) -> str:
    e = html.escape
    ink, muted, line, blue, green, red, bg = "#14171c", "#6b7280", "#e1e4ea", "#2a5bd7", "#14804a", "#c2362f", "#f6f7f9"
    accent = {"entry": blue, "exit": green, "exit_time": green, "stop": red}

    def row(label: str, value: str) -> str:
        return (f'<tr><td style="padding:8px 0;color:{muted};font-size:14px;border-bottom:1px solid {line}">{e(label)}</td>'
                f'<td style="padding:8px 0;text-align:right;font-family:Consolas,monospace;font-size:14px;color:{ink};border-bottom:1px solid {line}">{value}</td></tr>')

    def card(inner: str, color: str | None = None) -> str:
        border = f"border-left:4px solid {color};" if color else ""
        return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#fff;border:1px solid {line};'
                f'{border}border-radius:8px;margin:0 0 16px"><tr><td style="padding:18px 20px">{inner}</td></tr></table>')

    acts = [r for r in rep["etfs"] if r["action"] in accent]
    title = " · ".join(f'{ACTION_TEXT[r["action"]].split(" (")[0]} {r["ticker"]}' for r in acts) or "Sin operaciones hoy"
    parts = [card(f'<div style="font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:{muted}">'
                  f'SPY y QQQ · {e(rep["day"])} · {e(rep["time_ny"])} Nueva York</div>'
                  f'<h1 style="margin:6px 0 4px;font-size:26px;line-height:1.2;color:{ink}">{e(title)}</h1>'
                  f'<p style="margin:0;color:{muted};font-size:14px">RSI(2) &lt; {RSI_IN} sobre la MM200, salida sobre la MM5, '
                  f'stop {rep["stop_atr"]:g} ATR.</p>')]
    for r in rep["etfs"]:
        rows = (row("Precio ahora", _num(r["close"])) + row("RSI(2)", _num(r["rsi2"], 1))
                + row("MM200", _num(r["sma200"]) + ("" if r["above_sma200"] else f' <span style="color:{red}">(por debajo)</span>'))
                + row("MM5", _num(r["sma5"])))
        if r["action"] == "entry":
            rows += (row("Entrada aprox. (al cierre)", f'<strong>{_num(r["entry"])}</strong>')
                     + row(f"Stop {rep['stop_atr']:g} ATR", f'<span style="color:{red}">{_num(r["stop"])}</span> ({_num(r["stop_pct"])} %)')
                     + row("Salida", f"cierre sobre MM5 o {rep['max_days']} sesiones")
                     + row(f"Acciones ({_num(rep['alloc_pct'], 0)} % de {_num(rep['capital'], 0)})",
                           f'<strong>{_num(r["units"], 0)}</strong>' + (f' <span style="color:{red}">bajo el lote mínimo</span>' if r["below_min_lot"] else ""))
                     + row("Pérdida si salta el stop", _num(r["risk_money"])))
        elif "position" in r:
            p = r["position"]
            rows += (row("Comprado", f'{e(p["entry_day"])} a {_num(p["entry"])}') + row("Stop", _num(p["stop"]))
                     + row("Sesiones / resultado", f'{r["days_held"]} · {_num(r["pnl_pct"])} %'))
        head = (f'<strong style="font-size:18px;color:{ink}">{e(r["ticker"])} · {e(r["mt5"])}</strong>'
                f'<div style="margin:4px 0 8px;color:{accent.get(r["action"], muted)};font-weight:600">{e(ACTION_TEXT[r["action"]])}</div>')
        parts.append(card(head + f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table>',
                          accent.get(r["action"])))
    foot = (f'<p style="margin:8px 0 0;color:{muted};font-size:12px;line-height:1.5">Precios de Yahoo con la vela de hoy aún abierta: '
            "si al cierre cambian mucho, revisa la señal. Alerta automática, no es una recomendación de inversión.</p>")
    return (f'<!doctype html><html><body style="margin:0;padding:24px 12px;background:{bg};font-family:Segoe UI,Helvetica,Arial,sans-serif">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px"><tr><td>{"".join(parts)}{foot}</td></tr></table>'
            "</td></tr></table></body></html>")


# ------------------------------------------------------------------- pasada
def run_etf_alerts(
    cfg: EtfAlerts,
    store: EtfStore,
    notifier: Notifier,
    now: pd.Timestamp | None = None,
    data: dict[str, pd.DataFrame] | None = None,
    force: bool = False,
) -> dict | None:
    """Una pasada: None si está fuera de la ventana horaria, el mercado no ha abierto hoy o ya se procesó."""
    now = pd.Timestamp.now(tz="UTC") if now is None else now
    ny = now.tz_convert(NY)
    day = str(ny.date())
    if not force and not (cfg.window[0] <= ny.time() < cfg.window[1]):
        log.info("ETF: %s Nueva York fuera de la ventana %s-%s", ny.strftime("%H:%M"), *cfg.window)
        return None
    if not force and day in store.done:
        log.info("ETF: el %s ya se procesó", day)
        return None
    data = data or download(cfg)
    if any(str(data[s.ticker].index[-1].date()) != day for s in cfg.symbols):
        log.info("ETF: sin vela de hoy (%s) en Yahoo, mercado cerrado", day)
        return None

    etfs = [evaluate(cfg, s, data[s.ticker], store.positions.get(s.ticker)) for s in cfg.symbols]
    rep = {"day": day, "time_ny": ny.strftime("%H:%M"), "etfs": etfs, "capital": cfg.capital,
           "alloc_pct": cfg.alloc_pct, "stop_atr": cfg.stop_atr, "max_days": cfg.max_days}
    if cfg.daily_summary or any(r["action"] in ("entry", "exit", "exit_time", "stop") for r in etfs):
        subject, text, page = format_report(rep)
        notifier.send(subject, text, html_body=page)

    for r in etfs:
        if r["action"] == "entry":
            store.positions[r["ticker"]] = {"entry_day": day, "entry": r["entry"], "stop": r["stop"]}
        elif r["action"] in ("exit", "exit_time", "stop"):
            store.history.append(store.positions.pop(r["ticker"]) | {
                "ticker": r["ticker"], "exit_day": r.get("stop_day", day), "exit": r["action"], "pnl_pct": r["pnl_pct"]})
    if day not in store.done:
        store.done.append(day)
    store.save()
    return rep

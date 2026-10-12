"""Alerta semanal de barridas en S&P 500 y Nasdaq 100 (estrategia de backtest/index_strategy.py).

Cada semana (sábado, con el cierre del viernes ya publicado en Yahoo) calcula:
- la amplitud del S&P 500 (% de valores sobre su media de 50 sesiones),
- si los dos índices han completado una manipulación (PWL, PML o PYL con cierre por encima),
- si alguno ha barrido su PWL, rango de 4 semanas o PML con cierre semanal por encima,
y cuando se cumplen las tres cosas elige el índice con más empresas sobre su media (rotación),
calcula entrada, SL, TP en `tp_r` R y los lotes para el capital y riesgo configurados.

Además sigue las operaciones avisadas hasta que tocan SL o TP. Manda un email HTML cada semana
(con o sin señal) y guarda en JSON lo avisado para no repetirlo.
"""

from __future__ import annotations

import html
import json
import logging
import math
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from backtest.confluence import Variant, index_manipulation, market_frame, select, stock_signals
from backtest.index_strategy import Exit, manage
from backtest.market import breadth, load_daily, sp500_members
from notifier import Notifier

log = logging.getLogger("eurusd_alerts.index")

ROOT = Path(__file__).resolve().parent
NDX_FILE = ROOT / "backtest" / "ndx100.csv"
LOOKBACK_DAYS = 800  # PYL necesita el año anterior completo y la amplitud 50 sesiones


@dataclass(frozen=True)
class IndexSymbol:
    ticker: str  # Yahoo
    name: str
    mt5: str
    contract_size: float = 1.0  # unidades del índice por lote en el broker


@dataclass
class IndexAlerts:
    enabled: bool = False
    dry_run: bool = True
    capital: float = 10_000.0
    risk_pct: float = 2.0
    tp_r: float = 3.0
    breadth_below: float = 30.0
    min_risk_pct: float = 0.5
    lot_step: float = 0.01
    weekly_summary: bool = True  # email también las semanas sin señal
    state_path: Path = Path("state/index_alerts.json")
    symbols: list[IndexSymbol] = field(
        default_factory=lambda: [IndexSymbol("^GSPC", "S&P 500", "US500"), IndexSymbol("^NDX", "Nasdaq 100", "NAS100")]
    )


# ------------------------------------------------------------------- estado
class IndexStore:
    def __init__(self, path: Path):
        self.path = path
        self.alerted: list[str] = []  # semanas (viernes) ya avisadas
        self.positions: list[dict] = []  # operaciones avisadas y su estado

    def load(self) -> "IndexStore":
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.alerted = list(raw.get("alerted", []))
                self.positions = list(raw.get("positions", []))
            except (json.JSONDecodeError, ValueError):
                log.error("Estado de alertas de índices corrupto en %s; se empieza de cero", self.path)
        return self

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"alerted": self.alerted[-200:], "positions": self.positions[-200:]}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".idx-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


# ------------------------------------------------------------------- datos
@dataclass
class MarketData:
    indices: dict[str, pd.DataFrame]  # ticker -> velas diarias
    sp500: dict[str, pd.DataFrame]
    ndx: dict[str, pd.DataFrame]


def download(cfg: IndexAlerts, now: pd.Timestamp, cache_dir: Path | None = None) -> MarketData:
    start = (now - pd.Timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    tickers = [s.ticker for s in cfg.symbols]
    return MarketData(
        indices=load_daily(tickers + ["^VIX"], start, cache_dir),
        sp500=load_daily(list(sp500_members().ticker), start, cache_dir),
        ndx=load_daily(list(pd.read_csv(NDX_FILE).ticker), start, cache_dir),
    )


# ------------------------------------------------------------------ informe
def lots(cfg: IndexAlerts, sym: IndexSymbol, entry: float, stop: float) -> dict:
    """Lotes para arriesgar `risk_pct` del capital con el SL a (entry - stop) puntos."""
    risk_money = cfg.capital * cfg.risk_pct / 100
    per_lot = (entry - stop) * sym.contract_size
    raw = risk_money / per_lot if per_lot > 0 else 0.0
    rounded = math.floor(raw / cfg.lot_step) * cfg.lot_step
    return {
        "risk_money": round(risk_money, 2),
        "lots": round(rounded, 4),
        "lots_exact": round(raw, 4),
        "notional": round(rounded * entry * sym.contract_size, 2),
        "below_min_lot": rounded < cfg.lot_step,
    }


def build_report(cfg: IndexAlerts, data: MarketData, store: IndexStore, now: pd.Timestamp, window_days: int = 14) -> dict:
    """Estado de la semana cerrada más reciente y señales de las últimas `window_days` aún no avisadas."""
    syms = {s.ticker: s for s in cfg.symbols}
    spx, ndx = (data.indices[s.ticker] for s in cfg.symbols)
    b_sp, b_nd = breadth(data.sp500), breadth(data.ndx)
    market = market_frame(spx, ndx, breadth=b_sp)
    # Última semana cerrada en los dos índices (weekly_bars descarta la semana en curso).
    week = min(stock_signals(d).index[-1] for d in (spx, ndx))
    row = market.loc[week]
    rule = Variant("alerta", ("pwl", "rng", "pml"), True, cfg.breadth_below, min_risk=cfg.min_risk_pct / 100)

    indices = []
    candidates = []
    for tk, daily in ((s.ticker, data.indices[s.ticker]) for s in cfg.symbols):
        sig = stock_signals(daily)
        w = sig.loc[week] if week in sig.index else None
        picked = select(sig, market, rule)
        recent = picked[(picked.index > week - pd.Timedelta(days=window_days)) & (picked.index <= week)]
        members = b_sp if tk == cfg.symbols[0].ticker else b_nd
        info = {
            "ticker": tk,
            "name": syms[tk].name,
            "mt5": syms[tk].mt5,
            "close": round(float(daily["close"].iloc[-1]), 2),
            "last_day": str(daily.index[-1].date()),
            "aligned": bool(index_manipulation(daily).get(week, False)),
            "sweeps": [k for k in ("pwl", "rng", "pml") if w is not None and bool(w[k])],
            "members_above_ma": round(float(members.asof(week)), 1),
        }
        indices.append(info)
        for when, r in recent.iterrows():
            if str(when.date()) in store.alerted:
                continue
            risk = r["entry"] - r["stop_loss"]
            sym = syms[tk]
            candidates.append({
                "ticker": tk, "name": sym.name, "mt5": sym.mt5, "week": str(when.date()),
                "entry": round(float(r["entry"]), 2), "stop_loss": round(float(r["stop_loss"]), 2),
                "take_profit": round(float(r["entry"] + cfg.tp_r * risk), 2),
                "structure_tp": round(float(r["take_profit"]), 2),
                "risk_points": round(float(risk), 2), "risk_pct": round(float(100 * risk / r["entry"]), 2),
                "sweeps": [k for k in ("pwl", "rng", "pml") if bool(r[k])],
                "members_above_ma": info["members_above_ma"],
            } | lots(cfg, sym, float(r["entry"]), float(r["stop_loss"])))

    # Rotación: si los dos dan señal la misma semana, el de más empresas sobre su media.
    signals = []
    for wk in sorted({c["week"] for c in candidates}):
        same = sorted((c for c in candidates if c["week"] == wk), key=lambda c: -c["members_above_ma"])
        same[0]["chosen"] = True
        for c in same[1:]:
            c["chosen"] = False
        signals.extend(same)

    b_now = float(b_sp.asof(week))
    return {
        "generated": now.isoformat(),
        "week": str(week.date()),
        "breadth": round(b_now, 1),
        "breadth_min_2w": round(float(row.get("breadth_min", float("nan"))), 1),
        "breadth_ok": bool(row.get("breadth_min", 100) < cfg.breadth_below),
        "breadth_below": cfg.breadth_below,
        "vix": round(float(data.indices["^VIX"]["close"].asof(week)), 2) if "^VIX" in data.indices else None,
        "indices": indices,
        "signals": signals,
        "positions": update_positions(cfg, data, store),
        "capital": cfg.capital,
        "risk_pct": cfg.risk_pct,
        "tp_r": cfg.tp_r,
    }


def update_positions(cfg: IndexAlerts, data: MarketData, store: IndexStore) -> list[dict]:
    """Recalcula el estado de cada operación avisada con las velas diarias más recientes."""
    out = []
    for p in store.positions:
        if p.get("status") in ("win", "loss") and p.get("reported"):
            continue
        daily = data.indices.get(p["ticker"])
        if daily is None:
            continue
        sig = pd.DataFrame([{
            "last_day": pd.Timestamp(p["week"]), "entry": p["entry"], "stop_loss": p["stop_loss"], "take_profit": p["take_profit"],
        }])
        r_mult = (p["take_profit"] - p["entry"]) / (p["entry"] - p["stop_loss"])
        t = manage(p["ticker"], daily, sig, Exit("tp", r_mult))
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
SWEEP_NAMES = {"pwl": "mínimo de la semana anterior (PWL)", "rng": "mínimo de las 4 semanas previas", "pml": "mínimo del mes anterior (PML)"}


def _num(v: float, d: int = 2) -> str:
    return f"{v:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def format_report(rep: dict) -> tuple[str, str, str]:
    """Asunto, texto plano y HTML del informe semanal."""
    chosen = [s for s in rep["signals"] if s.get("chosen")]
    if chosen:
        s = chosen[0]
        subject = f"[Índices] SEÑAL {s['name']} ({s['mt5']}) | entrada {_num(s['entry'])} SL {_num(s['stop_loss'])} TP {_num(s['take_profit'])}"
    else:
        open_n = sum(p["status"] == "open" for p in rep["positions"])
        subject = f"[Índices] Semana {rep['week']}: sin señal" + (f" · {open_n} operación abierta" if open_n == 1 else f" · {open_n} operaciones abiertas" if open_n else "")

    lines = [f"Informe semanal de barridas · semana que cierra el {rep['week']}", ""]
    lines.append(f"Amplitud S&P 500: {_num(rep['breadth'], 1)} % (mínimo 2 semanas {_num(rep['breadth_min_2w'], 1)} %, umbral {_num(rep['breadth_below'], 0)} %) -> {'OK' if rep['breadth_ok'] else 'no'}")
    for i in rep["indices"]:
        sw = ", ".join(SWEEP_NAMES[k] for k in i["sweeps"]) or "ninguna"
        lines.append(f"{i['name']}: alineado {'sí' if i['aligned'] else 'no'} · barrida esta semana: {sw} · empresas sobre su media {_num(i['members_above_ma'], 1)} %")
    lines.append("")
    for s in rep["signals"]:
        tag = "ELEGIDO" if s.get("chosen") else "no elegido (rotación)"
        lines += [
            f"SEÑAL {s['name']} ({s['mt5']}) · {tag}",
            f"  Entrada (cierre del viernes): {_num(s['entry'])}",
            f"  Stop loss: {_num(s['stop_loss'])} ({_num(s['risk_points'])} puntos, {_num(s['risk_pct'])} %)",
            f"  Take profit {rep['tp_r']:g}R: {_num(s['take_profit'])}",
            f"  Lotes para {_num(rep['capital'], 0)} y {rep['risk_pct']:g} % de riesgo: {_num(s['lots'], 2)} (riesgo {_num(s['risk_money'])})"
            + (" · POR DEBAJO DEL LOTE MÍNIMO" if s["below_min_lot"] else ""),
            "",
        ]
    for p in rep["positions"]:
        state = {"open": f"abierta, {_num(p['r_now'])}R", "win": f"cerrada en TP ({p['exit_date']})", "loss": f"cerrada en SL ({p['exit_date']})"}[p["status"]]
        lines.append(f"Operación {p['name']} del {p['week']}: {state} · último cierre {_num(p['last'])}")
    lines += ["", "Precios del índice en Yahoo; en MT5 los niveles del CFD serán algo distintos: usa la distancia al SL en puntos.",
              "Alerta automática, no es una recomendación de inversión."]
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
        return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#fff;border:1px solid {line};{border}border-radius:8px;margin:0 0 16px"><tr><td style="padding:18px 20px">{inner}</td></tr></table>'

    chosen = [s for s in rep["signals"] if s.get("chosen")]
    head = (f'<div style="font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:{muted}">Informe semanal · semana que cierra el {e(rep["week"])}</div>'
            f'<h1 style="margin:6px 0 4px;font-size:26px;line-height:1.2;color:{ink}">'
            + (f'Señal de compra en {e(chosen[0]["name"])}' if chosen else "Sin señal esta semana") + "</h1>"
            f'<p style="margin:0;color:{muted};font-size:14px">Barridas semanales en S&amp;P 500 y Nasdaq 100 con amplitud en sobreventa.</p>')
    parts = [card(head)]

    for s in rep["signals"]:
        sw = ", ".join(SWEEP_NAMES[k] for k in s["sweeps"])
        tag = badge(bool(s.get("chosen")), "Elegido por rotación", "No elegido")
        body = (f'<div style="display:flex;justify-content:space-between"><strong style="font-size:18px;color:{ink}">{e(s["name"])} · {e(s["mt5"])}</strong></div>'
                f'<div style="margin:6px 0 12px">{tag}</div>'
                f'<p style="margin:0 0 10px;color:{muted};font-size:14px">Barrió el {e(sw)} y cerró la semana por encima.</p>'
                '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
                + row("Entrada (cierre del viernes)", _num(s["entry"]))
                + row("Stop loss", f'{_num(s["stop_loss"])} <span style="color:{muted}">({_num(s["risk_points"])} pts · {_num(s["risk_pct"])} %)</span>')
                + row(f"Take profit {rep['tp_r']:g}R", f'<span style="color:{green}">{_num(s["take_profit"])}</span>')
                + row("TP en la estructura", _num(s["structure_tp"]))
                + row(f"Lotes ({_num(rep['capital'], 0)} · {rep['risk_pct']:g} %)",
                      f'<strong>{_num(s["lots"], 2)}</strong>' + (f' <span style="color:{red}">bajo el lote mínimo</span>' if s["below_min_lot"] else ""))
                + row("Riesgo en dinero", _num(s["risk_money"]))
                + "</table>")
        parts.append(card(body, blue if s.get("chosen") else line))

    cond = ('<strong style="font-size:16px;color:#14171c">Condiciones</strong>'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:8px">'
            + row(f"Amplitud S&P 500 bajo {_num(rep['breadth_below'], 0)} % (2 semanas)",
                  f'{_num(rep["breadth_min_2w"], 1)} % ' + badge(rep["breadth_ok"]))
            + "".join(row(f"{i['name']} alineado", badge(i["aligned"])) for i in rep["indices"])
            + "".join(row(f"Barrida en {i['name']}", e(", ".join(k.upper() for k in i["sweeps"]) or "—")) for i in rep["indices"])
            + "".join(row(f"Empresas del {i['name']} sobre su media", f'{_num(i["members_above_ma"], 1)} %') for i in rep["indices"])
            + (row("VIX (informativo)", _num(rep["vix"])) if rep.get("vix") is not None else "")
            + "</table>")
    parts.append(card(cond))

    if rep["positions"]:
        rows = ""
        for p in rep["positions"]:
            color = {"open": blue, "win": green, "loss": red}[p["status"]]
            label = {"open": f"Abierta · {_num(p['r_now'])}R", "win": f"TP · {p['exit_date']}", "loss": f"SL · {p['exit_date']}"}[p["status"]]
            rows += row(f"{p['name']} del {p['week']}", f'<span style="color:{color}">{e(label)}</span>')
        parts.append(card(f'<strong style="font-size:16px;color:{ink}">Operaciones avisadas</strong>'
                          f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:8px">{rows}</table>'))

    foot = (f'<p style="margin:8px 0 0;color:{muted};font-size:12px;line-height:1.5">Precios del índice al contado (Yahoo). En MT5 los niveles del CFD serán algo '
            "distintos: lleva la distancia al SL en puntos a tu gráfico. Alerta automática, no es una recomendación de inversión.</p>")
    return (f'<!doctype html><html><body style="margin:0;padding:24px 12px;background:{bg};font-family:Segoe UI,Helvetica,Arial,sans-serif">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px"><tr><td>{"".join(parts)}{foot}</td></tr></table>'
            "</td></tr></table></body></html>")


# ------------------------------------------------------------------- pasada
SEED_DAYS = 120


def _remember(store: IndexStore, signals: list[dict]) -> None:
    """Marca las semanas como avisadas y empieza a seguir la operación elegida de cada una."""
    for s in signals:
        if s["week"] not in store.alerted:
            store.alerted.append(s["week"])
        if s.get("chosen"):
            store.positions.append({k: s[k] for k in ("ticker", "name", "mt5", "week", "entry", "stop_loss", "take_profit")} | {"status": "open"})

def run_index_alerts(
    cfg: IndexAlerts,
    store: IndexStore,
    notifier: Notifier,
    now: pd.Timestamp | None = None,
    data: MarketData | None = None,
) -> dict:
    """Descarga, calcula el informe, lo envía y actualiza el estado. Devuelve el informe."""
    now = pd.Timestamp.now(tz="UTC") if now is None else now
    data = data or download(cfg, now)
    if not store.alerted and not store.positions:
        # Primera pasada: las señales de los últimos meses (anteriores a la ventana normal de
        # 14 días) se siguen como ya avisadas, sin email; las recientes se avisan como siempre.
        seed = build_report(cfg, data, store, now, window_days=SEED_DAYS)
        cutoff = pd.Timestamp(seed["week"]) - pd.Timedelta(days=14)
        _remember(store, [s for s in seed["signals"] if pd.Timestamp(s["week"]) <= cutoff])
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

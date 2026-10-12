"""Informe del bot IndexPropBot en la cuenta demo: estado de la prueba, operaciones y comparación con el backtest.

Lee de MT5 (terminal abierto y con sesión) el historial de las operaciones del bot (magic base + 1 = ORB,
base + 2 = RSI2), el deslizamiento de cada entrada frente al precio de la orden, el estado de las reglas
de la prueba (+10% / DD 10% / diario 5% sobre el saldo inicial) y los avisos del bot en el diario de
Expertos de hoy. Compara el acierto, la R por operación y la frecuencia con el backtest
(backtest/index_prop_trades.csv) y con el deslizamiento supuesto en backtest/index_prop_optimize.py.

    python -m backtest.demo_report            # informe por pantalla y en reports/demo/
    python -m backtest.demo_report --email    # además lo envía por email (SMTP de .env)
"""

from __future__ import annotations

import argparse
import html
from pathlib import Path

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

from backtest.index_prop_optimize import SLIP_PTS

NY = "America/New_York"
MAGIC = 20261009
STRATEGY = {MAGIC + 1: "ORB 30", MAGIC + 2: "RSI2 diario"}
SYMBOL_INDEX = {"NAS100": "NAS100", "SPX500": "SP500"}
SERVER_MINUS_NY = 7
LOGS = Path.home() / "AppData/Roaming/MetaQuotes/Terminal/16D9C17040576AD13C62C316983027D5/MQL5/Logs"
OUT = Path("reports/demo")


def server_to_ny(seconds) -> pd.Series:
    t = pd.to_datetime(pd.Series(seconds), unit="s") - pd.Timedelta(hours=SERVER_MINUS_NY)
    return t.dt.tz_localize(NY, ambiguous="NaT", nonexistent="NaT")


def trading_day(t: pd.Series) -> pd.Series:
    """Día de trading (acaba a las 17:00 NY)."""
    return (t + pd.Timedelta(hours=7)).dt.date


def closed_trades(since: pd.Timestamp) -> pd.DataFrame:
    deals = mt5.history_deals_get(since.to_pydatetime(), pd.Timestamp.now(tz="UTC").to_pydatetime() + pd.Timedelta(days=1))
    if not deals:
        return pd.DataFrame()
    d = pd.DataFrame([x._asdict() for x in deals])
    d = d[d.position_id.isin(d[d.magic.isin(STRATEGY)].position_id)]
    if d.empty:
        return pd.DataFrame()
    d["hora_ny"] = server_to_ny(d.time).to_numpy()
    rows = []
    for pid, g in d.groupby("position_id"):
        ins, outs = g[g.entry == mt5.DEAL_ENTRY_IN], g[g.entry == mt5.DEAL_ENTRY_OUT]
        if ins.empty:
            continue
        e = ins.iloc[0]
        orders = mt5.history_orders_get(position=int(pid)) or []
        entry_order = next((o for o in orders if o.ticket == e.order), None)
        planned = entry_order.price_open if entry_order is not None and entry_order.price_open else e.price
        sl = entry_order.sl if entry_order is not None else 0.0
        side = 1 if e.type == mt5.DEAL_TYPE_BUY else -1
        otype = mt5.ORDER_TYPE_BUY if side == 1 else mt5.ORDER_TYPE_SELL
        risk_money = abs(mt5.order_calc_profit(otype, e.symbol, e.volume, e.price, sl) or 0) if sl else np.nan
        row = {"estrategia": STRATEGY.get(e.magic, "?"), "simbolo": e.symbol, "lado": "compra" if side == 1 else "venta",
               "entrada_ny": e.hora_ny, "precio_orden": planned, "precio_entrada": e.price,
               # positivo = peor que el precio de la orden
               "deslizamiento_pts": side * (e.price - planned) if STRATEGY.get(e.magic) == "ORB 30" else np.nan,
               "sl": sl, "volumen": e.volume, "riesgo_usd": risk_money}
        if outs.empty:
            rows.append(row | {"estado": "abierta"})
            continue
        profit = float((outs.profit + outs.commission + outs.swap).sum() + ins.commission.sum())
        rows.append(row | {"estado": "cerrada", "salida_ny": outs.hora_ny.iloc[-1], "precio_salida": outs.price.iloc[-1],
                           "beneficio_usd": profit, "R": profit / risk_money if risk_money else np.nan})
    return pd.DataFrame(rows).sort_values("entrada_ny").reset_index(drop=True)


def orb_order_days(since: pd.Timestamp) -> set:
    orders = mt5.history_orders_get(since.to_pydatetime(), pd.Timestamp.now(tz="UTC").to_pydatetime() + pd.Timedelta(days=1)) or []
    times = [o.time_setup for o in orders if o.magic == MAGIC + 1]
    return set(server_to_ny(times).dt.date) if times else set()


def expected_days(since: pd.Timestamp) -> list:
    """Días laborables desde el inicio en los que ya debería haber puesto las órdenes del ORB (10:00 NY)."""
    now = pd.Timestamp.now(tz=NY)
    last = now.date() if now.hour * 60 + now.minute >= 10 * 60 + 5 else (now - pd.Timedelta(days=1)).date()
    start = since.tz_convert(NY).date()
    return [d.date() for d in pd.bdate_range(start, last)]


def benchmark() -> dict:
    """Acierto, R/op y frecuencia del backtest con deslizamiento realista."""
    t = pd.read_csv("backtest/index_prop_trades.csv")
    orb = t[(t.estrategia == "ORB 30") & (t.indice == "NAS100")].copy()
    orb["r"] -= SLIP_PTS["NAS100"] / orb.riesgo_pts
    rsi = t[t.estrategia == "RSI2 diario"]
    years = (pd.to_datetime(t.exit_time, utc=True, format="mixed").max()
             - pd.to_datetime(t.fill_time, utc=True, format="mixed").min()).days / 365.25
    return {"ORB 30": {"acierto %": 100 * (orb.r > 0).mean(), "R/op": orb.r.mean(), "ops/mes": len(orb) / years / 12},
            "RSI2 diario": {"acierto %": 100 * (rsi.r > 0).mean(), "R/op": rsi.r.mean(), "ops/mes": len(rsi) / years / 12}}


def bot_log_today() -> list[str]:
    logs = sorted(LOGS.glob("*.log"))
    if not logs:
        return []
    lines = [ln.split("\t", 3)[-1] for ln in logs[-1].read_text(encoding="utf-16", errors="ignore").splitlines()
             if "IndexPropBot" in ln]
    starts = [i for i, ln in enumerate(lines) if "saldo inicial" in ln]
    return lines[starts[-1]:] if starts else lines  # solo desde el último arranque del bot


def build(initial: float, since: pd.Timestamp) -> tuple[str, str, pd.DataFrame]:
    acc = mt5.account_info()
    term = mt5.terminal_info()
    trades = closed_trades(since)
    closed = trades[trades.estado == "cerrada"] if len(trades) else trades
    goal, floor = initial * 1.10, initial * 0.90
    now = pd.Timestamp.now(tz=NY)
    day_start_t = (now - pd.Timedelta(hours=17)).normalize() + pd.Timedelta(hours=17)
    today_pnl = float(closed[closed.salida_ny >= day_start_t].beneficio_usd.sum()) if len(closed) else 0.0
    day_start_bal = acc.balance - today_pnl
    day_loss = day_start_bal - acc.equity
    worst = initial + closed.beneficio_usd.cumsum().min() if len(closed) else initial

    lines = [
        f"Cuenta {acc.login} ({acc.server}) - {'DEMO' if acc.trade_mode == 0 else 'NO ES DEMO'}",
        f"Trading algorítmico activado en el terminal: {'sí' if term.trade_allowed else 'NO (pulsa Algo Trading)'}",
        "",
        "PRUEBA DE FONDEO (+10% / DD 10% / diario 5%)",
        f"  Saldo {acc.balance:,.2f} | equity {acc.equity:,.2f} | inicial {initial:,.0f}",
        f"  Progreso: {100 * (acc.equity / initial - 1):+.2f}% (objetivo +10% = {goal:,.0f}, faltan {goal - acc.equity:,.0f})",
        f"  Distancia al DD máximo: {acc.equity - floor:,.0f} USD (suelo {floor:,.0f}); peor saldo cerrado {worst:,.0f}",
        f"  Pérdida del día: {max(day_loss, 0):,.0f} USD de {0.05 * initial:,.0f} permitidos",
    ]
    if acc.equity >= goal:
        lines.append("  >>> OBJETIVO ALCANZADO: el bot habrá cerrado todo y dejado de operar.")
    if acc.equity <= floor:
        lines.append("  >>> DD MÁXIMO ALCANZADO: prueba suspendida.")

    bench = benchmark()
    months = max((now - since.tz_convert(NY)).days / 30.44, 1 / 30.44)
    lines += ["", "COMPARACIÓN CON EL BACKTEST (desde el inicio de la demo)"]
    for strat, b in bench.items():
        g = closed[closed.estrategia == strat] if len(closed) else closed
        if len(g):
            lines.append(f"  {strat}: {len(g)} ops ({len(g) / months:.1f}/mes, backtest {b['ops/mes']:.1f}) | acierto "
                         f"{100 * (g.R > 0).mean():.0f}% (backtest {b['acierto %']:.0f}%) | R/op {g.R.mean():+.3f} "
                         f"(backtest {b['R/op']:+.3f}) | total {g.beneficio_usd.sum():+,.0f} USD")
        else:
            lines.append(f"  {strat}: sin operaciones cerradas todavía (backtest: {b['ops/mes']:.1f}/mes)")
    orb = closed[closed.estrategia == "ORB 30"] if len(closed) else closed
    if len(orb):
        lines.append(f"  Deslizamiento medio en la entrada del ORB: {orb.deslizamiento_pts.mean():+.1f} pts "
                     f"(el backtest supone {SLIP_PTS['NAS100']:+.1f} en NAS100)")
    expected = expected_days(since)
    placed = orb_order_days(since)
    missing = [d for d in expected if d not in placed]
    lines.append(f"  Días con órdenes del ORB: {len(placed & set(expected))}/{len(expected)}"
                 + (f" - sin órdenes: {', '.join(map(str, missing))} (festivo, rango pequeño o el bot no estaba activo)"
                    if missing else ""))

    lines += ["", "OPERACIONES"]
    if len(trades):
        show = trades.assign(entrada=trades.entrada_ny.dt.strftime("%m-%d %H:%M"),
                             salida=trades.get("salida_ny", pd.Series(dtype=object)).apply(
                                 lambda x: x.strftime("%m-%d %H:%M") if pd.notna(x) else ""))
        cols = ["estrategia", "simbolo", "lado", "entrada", "precio_orden", "precio_entrada", "deslizamiento_pts",
                "sl", "salida", "precio_salida", "beneficio_usd", "R", "estado"]
        lines.append(show.reindex(columns=cols).round(2).to_string(index=False))
    else:
        lines.append("  Ninguna todavía.")
    log = bot_log_today()
    lines += ["", "DIARIO DEL BOT (hoy)"] + ([f"  {x}" for x in log[-15:]] or ["  Sin mensajes hoy (¿está el bot en el gráfico?)"])
    problems = [x for x in log if any(k in x.lower() for k in ("error", "failed", "fallo", "real y", "sin riesgo"))]
    if problems:
        lines += ["", "AVISOS"] + [f"  {x}" for x in problems[-10:]]

    text = "\n".join(lines)
    subject = (f"[Demo IndexPropBot] {now:%d/%m}: {100 * (acc.equity / initial - 1):+.2f}% "
               f"| {len(closed)} ops cerradas | DD libre {acc.equity - floor:,.0f} USD")
    return subject, text, trades


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--initial", type=float, default=100_000)
    ap.add_argument("--since", default=None, help="inicio de la demo (por defecto, primera operación del bot o hace 30 días)")
    ap.add_argument("--email", action="store_true")
    args = ap.parse_args()
    if not mt5.initialize():
        raise SystemExit(f"No se pudo conectar con MT5 (¿terminal abierto y con sesión?): {mt5.last_error()}")
    try:
        since = pd.Timestamp(args.since, tz=NY).tz_convert("UTC") if args.since else pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=30)
        subject, text, trades = build(args.initial, since)
    finally:
        mt5.shutdown()
    print(subject + "\n\n" + text)
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz=NY)
    (OUT / f"{stamp:%Y-%m-%d}.txt").write_text(subject + "\n\n" + text, encoding="utf-8")
    if len(trades):
        trades.to_csv(OUT / "operaciones.csv", index=False)
    if args.email:
        from main import build_notifier
        from settings import load_config

        cfg = load_config()
        body_html = f"<pre style='font-family:Consolas,monospace;font-size:13px'>{html.escape(text)}</pre>"
        build_notifier(cfg).send(subject, text, body_html)


if __name__ == "__main__":
    main()

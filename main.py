"""EURUSD SMC alerts: sweep PDH/PDL -> CHoCH 15m -> FVG -> email.

Uso:
    python main.py run                     # bucle continuo (comprueba cada minuto)
    python main.py once                    # un ciclo y sale (para cron / Task Scheduler)
    python main.py backtest [--months 12] [--export-csv velas.csv]   # histórico de MT5
    python main.py backtest --csv datos.csv [--out alerts.csv] [--tz UTC]
    python main.py compare [--from 2024-01-01 --to 2026-01-01]       # PDH/PDL vs Chartz (M1 de MT5)
    python main.py test-email              # envía un email de prueba
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pandas as pd

from detector import SetupState, SMCEngine
from notifier import EmailNotifier, LogNotifier, Notifier, format_alert
from settings import AppConfig, load_config
from state import StateStore

log = logging.getLogger("eurusd_alerts")


# --------------------------------------------------------------------- logging
def setup_logging(cfg: AppConfig) -> None:
    cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(
        cfg.log_file, maxBytes=cfg.log_max_bytes, backupCount=cfg.log_backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    handlers: list[logging.Handler] = [file_handler]
    if sys.stdout is not None:  # con pythonw.exe no hay consola
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        handlers.append(console)
    root = logging.getLogger()
    root.setLevel(cfg.log_level.upper())
    root.handlers[:] = handlers
    for noisy in ("urllib3",):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# --------------------------------------------------------------------- timing
def latest_closed_open(now: pd.Timestamp, timeframe_minutes: int) -> pd.Timestamp:
    """Hora de apertura de la última vela cerrada en `now`."""
    return now.floor(f"{timeframe_minutes}min") - timedelta(minutes=timeframe_minutes)


def market_closed(candle_open: pd.Timestamp) -> bool:
    """FX cerrado de viernes 17:00 a domingo 17:00 (hora de Nueva York)."""
    ny = candle_open.tz_convert("America/New_York")
    wd, hour = ny.weekday(), ny.hour
    return (wd == 4 and hour >= 17) or wd == 5 or (wd == 6 and hour < 17)


# --------------------------------------------------------------------- ciclo
def run_cycle(
    cfg: AppConfig,
    source,
    store: StateStore,
    notifier: Notifier,
    now: pd.Timestamp | None = None,
) -> int:
    """Un ciclo: si ha cerrado una vela nueva la procesa y envía las alertas pendientes.

    Devuelve el número de alertas enviadas.
    """
    now = pd.Timestamp.now(tz="UTC") if now is None else now
    tf = cfg.detector.timeframe_minutes
    expected = latest_closed_open(now - timedelta(seconds=cfg.candle_close_delay_seconds), tf)
    pending = store.pending_alerts()
    up_to_date = store.last_processed is not None and store.last_processed >= expected
    if (up_to_date or market_closed(expected)) and not pending:
        return 0

    if not up_to_date and not market_closed(expected):
        candles = source.get_closed_candles(tf, cfg.data_source.lookback_days, now)
        engine = SMCEngine(cfg.detector, cfg.symbol)
        _, last = engine.process_new(candles, store.setups, store.last_processed)
        if last != store.last_processed:
            log.info("Procesadas velas hasta %s", last)
        else:
            log.debug("Sin velas nuevas todavía (esperada %s)", expected)
        store.last_processed = last

    sent = 0
    max_age = timedelta(minutes=cfg.max_alert_age_minutes)
    for setup in store.pending_alerts():
        if now - setup.ready_time > max_age:
            setup.invalidated_reason = "stale"
            setup.transition(SetupState.INVALIDATED, now, "alerta demasiado antigua, no se envía")
            log.info("Setup %s completado en %s: demasiado antiguo para alertar", setup.id, setup.ready_time)
            continue
        subject, body = format_alert(setup, cfg.detector.pip_size)
        try:
            notifier.send(subject, body)
        except Exception:
            log.exception("Fallo enviando la alerta %s; se reintentará", setup.id)
            continue
        setup.alerted_at = now
        setup.transition(SetupState.ALERTED, now, "email enviado")
        sent += 1

    store.prune(now, cfg.state_keep_days)
    store.save()
    return sent


def build_notifier(cfg: AppConfig) -> Notifier:
    if cfg.dry_run:
        return LogNotifier()
    s = cfg.smtp
    return EmailNotifier(s.user, s.password, s.to, s.host, s.port)


# --------------------------------------------------------------------- comandos
def cmd_live(cfg: AppConfig, loop: bool) -> int:
    from daily_cycle_alerts import SignalStore, run_daily_cycle
    from data import create_source

    notifier = build_notifier(cfg)
    source = create_source(cfg)
    store = StateStore(cfg.state_path).load()
    dc = cfg.daily_cycle
    dc_source = dc_store = dc_notifier = None
    if dc.enabled:
        dc_source = create_source(cfg, dc.symbol, dc.mt5_symbol, dc.yahoo_ticker)
        dc_store = SignalStore(dc.state_path).load()
        dc_notifier = LogNotifier() if dc.dry_run else notifier
    dc_done: pd.Timestamp | None = None
    log.info(
        "Arrancando (%s, fuente=%s, estado=%s%s)",
        "bucle" if loop else "una pasada", cfg.data_source.type, cfg.state_path,
        f", Daily Cycle {dc.symbol}{' dry run' if dc.dry_run else ''}" if dc.enabled else "",
    )
    try:
        while True:
            failed = False
            try:
                sent = run_cycle(cfg, source, store, notifier)
                if sent:
                    log.info("%d alerta(s) enviada(s)", sent)
            except Exception:
                log.exception("Error en el ciclo")
                failed = True
            # Daily Cycle: independiente de EURUSD; una pasada por vela de 15m cerrada.
            now = pd.Timestamp.now(tz="UTC")
            candle = latest_closed_open(now - timedelta(seconds=cfg.candle_close_delay_seconds), dc.detector.ltf_minutes)
            if dc.enabled and candle != dc_done and not market_closed(candle):
                try:
                    n = run_daily_cycle(dc, dc_source, dc_store, dc_notifier, cfg.max_alert_age_minutes, now)
                    if n:
                        log.info("%d señal(es) Daily Cycle %s", n, dc.symbol)
                    dc_done = candle
                except Exception:
                    log.exception("Error en el ciclo Daily Cycle %s", dc.symbol)
            if not loop:
                return 1 if failed else 0
            # Dormir hasta el siguiente minuto (+ margen de cierre de vela).
            now = time.time()
            interval = cfg.loop_interval_seconds
            delay = interval - (now % interval) + min(cfg.candle_close_delay_seconds, interval - 1)
            time.sleep(delay)
    except KeyboardInterrupt:
        log.info("Detenido por el usuario")
        return 0
    finally:
        source.close()
        if dc_source is not None:
            dc_source.close()


def download_history(cfg: AppConfig, date_from, date_to, source=None, timeframe: int | None = None) -> pd.DataFrame:
    """Velas de MT5 con copy_rates_range (M15 por defecto; formato CANDLE_COLUMNS, sin validar)."""
    from data import create_source

    source = source or create_source(cfg)
    try:
        tf = timeframe or cfg.detector.timeframe_minutes
        return source.get_range(cfg.data_source.mt5_symbol, tf, date_from, date_to)
    finally:
        source.close()


def cmd_backtest(
    cfg: AppConfig,
    out: str,
    csv_path: str | None = None,
    tz: str = "UTC",
    months: int = 12,
    date_from: str | None = None,
    date_to: str | None = None,
    export_csv: str | None = None,
    strict_gaps: bool = False,
    source=None,
    now: pd.Timestamp | None = None,
) -> int:
    from backtest import run_backtest
    from data import from_engine_frame, to_engine_frame, validate_candles
    from data.csv_source import export_candles_csv, load_csv, resample_ohlc

    tf = cfg.detector.timeframe_minutes
    now = pd.Timestamp.now(tz="UTC") if now is None else now
    if csv_path:
        raw = from_engine_frame(resample_ohlc(load_csv(csv_path, tz), tf))
        log.info("Velas leídas de %s", csv_path)
    else:
        end = pd.Timestamp(date_to, tz="UTC") if date_to else now
        start = pd.Timestamp(date_from, tz="UTC") if date_from else end - pd.DateOffset(months=months)
        log.info("Descargando %s M%d de MT5: %s -> %s", cfg.data_source.mt5_symbol, tf, start, end)
        raw = download_history(cfg, start.to_pydatetime(), end.to_pydatetime(), source)
    candles, report = validate_candles(raw, tf, now=now, fail_on_gaps=strict_gaps)
    print(report.summary())
    print()
    if export_csv:
        print(f"Velas exportadas a {export_candles_csv(candles, export_csv)}")

    engine_candles = to_engine_frame(candles)
    log.info("Backtest sobre %d velas (%s -> %s)", len(engine_candles), engine_candles.index[0], engine_candles.index[-1])
    result = run_backtest(engine_candles, cfg.detector, cfg.symbol)
    if len(result.alerts):
        with pd.option_context("display.width", 200, "display.max_columns", 20, "display.max_rows", 500):
            print(result.alerts.drop(columns=["setup_id"]).to_string(index=False))
    print()
    print(result.summary())
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    result.alerts.to_csv(out, index=False)
    print(f"\nAlertas exportadas a {out}")
    return 0


def cmd_compare(
    cfg: AppConfig,
    date_from: str,
    date_to: str,
    spread: float = 0.0,
    out_dir: str = "backtest",
    source=None,
) -> int:
    """Backtest de PDH/PDL y Chartz sobre M1 de MT5 con las mismas reglas de ejecución."""
    from backtest.compare import compare
    from data import to_engine_frame, validate_candles

    start, end = pd.Timestamp(date_from, tz="UTC"), pd.Timestamp(date_to, tz="UTC")
    warmup = start - pd.Timedelta(days=45)  # estructura HTF y PDH/PDL previos al inicio
    log.info("Descargando %s M1 de MT5: %s -> %s", cfg.data_source.mt5_symbol, warmup, end)
    raw = download_history(cfg, warmup.to_pydatetime(), end.to_pydatetime(), source, timeframe=1)
    candles, report = validate_candles(raw, 1)
    print(report.summary())
    table, trades = compare(to_engine_frame(candles), start, end, cfg.detector, cfg.chartz, spread)
    with pd.option_context("display.width", 250, "display.max_columns", 30):
        print(table.round(2).to_string(index=False))
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    table.to_csv(Path(out_dir) / "compare.csv", index=False)
    for name, rows in trades.items():
        path = Path(out_dir) / f"trades_2R_{name.replace('/', '').lower()}.csv"
        pd.DataFrame([t.__dict__ | {"r": t.r} for t in rows]).to_csv(path, index=False)
    print(f"\nResultados en {Path(out_dir) / 'compare.csv'} y trades_2R_*.csv")
    return 0


def cmd_test_email(cfg: AppConfig) -> int:
    build_notifier(cfg).send(f"[{cfg.symbol}] Email de prueba", "Si lees esto, la configuración SMTP funciona.")
    log.info("Email de prueba enviado")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Alertas SMC de EURUSD por email")
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.yaml")))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="bucle continuo")
    sub.add_parser("once", help="un ciclo (cron / Task Scheduler)")
    bt = sub.add_parser("backtest", help="recorre el histórico de MT5 (o un CSV)")
    bt.add_argument("--csv", help="usar un CSV en vez de descargar de MT5")
    bt.add_argument("--tz", default="UTC", help="zona horaria de las fechas del CSV si no la incluyen")
    bt.add_argument("--months", type=int, default=12, help="meses de histórico M15 a pedir a MT5")
    bt.add_argument("--from", dest="date_from", help="inicio UTC (YYYY-MM-DD), sustituye a --months")
    bt.add_argument("--to", dest="date_to", help="fin UTC (YYYY-MM-DD), por defecto ahora")
    bt.add_argument("--out", default="backtest/alerts.csv", help="CSV de alertas")
    bt.add_argument("--export-csv", help="guarda también las velas descargadas (para compararlas con TradingView)")
    bt.add_argument("--strict-gaps", action="store_true", help="falla si hay huecos lunes-viernes")
    cp = sub.add_parser("compare", help="backtest PDH/PDL vs Chartz con velas M1 de MT5")
    cp.add_argument("--from", dest="date_from", default="2024-01-01", help="inicio UTC (YYYY-MM-DD)")
    cp.add_argument("--to", dest="date_to", default="2026-01-01", help="fin UTC, exclusivo (YYYY-MM-DD)")
    cp.add_argument("--spread", type=float, default=0.0, help="spread + comisión en pips")
    cp.add_argument("--out-dir", default="backtest", help="carpeta de los CSV de resultados")
    sub.add_parser("test-email", help="envía un email de prueba")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    setup_logging(cfg)
    if args.command == "run":
        return cmd_live(cfg, loop=True)
    if args.command == "once":
        return cmd_live(cfg, loop=False)
    if args.command == "backtest":
        from data import CandleValidationError
        from data.mt5_source import MT5Error

        try:
            return cmd_backtest(
                cfg, args.out, args.csv, args.tz, args.months, args.date_from, args.date_to,
                args.export_csv, args.strict_gaps,
            )
        except (MT5Error, CandleValidationError) as exc:
            log.error("%s", exc)
            return 1
    if args.command == "compare":
        from data import CandleValidationError
        from data.mt5_source import MT5Error

        try:
            return cmd_compare(cfg, args.date_from, args.date_to, args.spread, args.out_dir)
        except (MT5Error, CandleValidationError) as exc:
            log.error("%s", exc)
            return 1
    return cmd_test_email(cfg)


if __name__ == "__main__":
    sys.exit(main())

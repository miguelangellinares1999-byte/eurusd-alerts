"""Comprobación de la conexión con MetaTrader 5 y del histórico disponible.

Uso (con el terminal MT5 abierto y con sesión iniciada):
    python check_mt5.py [--months 12]

Sale con código 0 si todo es utilizable (puede haber avisos) y 1 si algo falla.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

from data.mt5_source import MT5Error, MT5Source, detect_offset
from data.validation import CandleValidationError, validate_candles
from settings import load_config

# El histórico se da por completo si empieza como mucho esto después de lo pedido
# (la fecha de inicio puede caer en fin de semana).
START_TOLERANCE = pd.Timedelta(days=4)


def ok(msg: str) -> None:
    print(f"[OK]     {msg}")


def warn(msg: str) -> None:
    print(f"[AVISO]  {msg}")


def fail(msg: str) -> None:
    print(f"[FALLO]  {msg}")


def weekly_opens_ny(candles: pd.DataFrame) -> Counter:
    """Hora de Nueva York de la primera vela tras cada fin de semana ("Sun 17:00": n semanas)."""
    ts = pd.DatetimeIndex(candles["timestamp"])
    after_weekend = ts[1:][(ts[1:] - ts[:-1]) > pd.Timedelta(hours=24)]
    return Counter(t.tz_convert("America/New_York").strftime("%a %H:%M") for t in after_weekend)


def main(argv: list[str] | None = None, mt5_module=None, now: pd.Timestamp | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.yaml")))
    parser.add_argument("--months", type=int, default=12)
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    ds = cfg.data_source
    symbol = ds.mt5_symbol
    now = pd.Timestamp.now(tz="UTC") if now is None else now
    try:
        source = MT5Source(symbol, ds.mt5_server_utc_offset_hours, ds.mt5_auto_detect_offset,
                           ds.mt5_server_timezone, mt5_module=mt5_module)
    except ImportError as exc:
        fail(f"No se puede importar MetaTrader5 ({exc}). Instala con: pip install MetaTrader5 (solo Windows)")
        return 1
    errors = 0

    print("== Terminal y cuenta")
    try:
        with source.session() as mt5:
            version = mt5.version()
            ok(f"Terminal abierto. Versión MT5: {version[0]} build {version[1]} ({version[2]})")
            term = mt5.terminal_info()
            if term is None or not term.connected:
                fail(f"El terminal no está conectado al servidor del bróker: {mt5.last_error()}")
                errors += 1
            account = mt5.account_info()
            if account is None:
                fail(f"No hay sesión iniciada en ninguna cuenta: {mt5.last_error()}")
                errors += 1
            else:
                ok(f"Cuenta: {account.login} ({account.name}), servidor: {account.server}, "
                   f"bróker: {account.company}, divisa: {account.currency}")
            if term is not None:
                print(f"         Máx. barras por gráfico (maxbars): {term.maxbars}")

            print("\n== Símbolo y zona horaria")
            source.select_symbol(symbol)
            info = mt5.symbol_info(symbol)
            ok(f"Símbolo: {symbol} ({info.description}), dígitos: {info.digits}")
            tick = mt5.symbol_info_tick(symbol)
            detected = detect_offset(tick.time, now) if tick is not None else None
            if tick is not None:
                print(f"         Último tick (hora servidor): {pd.Timestamp(tick.time, unit='s')}, "
                      f"UTC ahora: {now:%Y-%m-%d %H:%M:%S}")
            if ds.mt5_server_timezone:
                print(f"         Conversión usada: server_timezone = {ds.mt5_server_timezone}")
            else:
                print(f"         Conversión usada: server_utc_offset_hours = {ds.mt5_server_utc_offset_hours:+g}"
                      f"{' (auto_detect_offset activado)' if ds.mt5_auto_detect_offset else ''}")
            if detected is None:
                warn("No se pudo estimar el desfase actual del servidor (mercado cerrado o sin ticks recientes)")
            else:
                print(f"         Desfase actual estimado del servidor: UTC{detected:+g}")
                if not ds.mt5_server_timezone and not ds.mt5_auto_detect_offset \
                        and detected != ds.mt5_server_utc_offset_hours:
                    warn(f"server_utc_offset_hours={ds.mt5_server_utc_offset_hours:+g} no coincide con el "
                         f"desfase estimado UTC{detected:+g}")
    except MT5Error as exc:
        fail(str(exc))
        errors += 1
    if errors:
        print("\nResultado: FALLO")
        return 1

    print(f"\n== Histórico M15 ({args.months} meses)")
    start = now - pd.DateOffset(months=args.months)
    try:
        raw = source.get_range(symbol, "M15", start.to_pydatetime(), now.to_pydatetime())
        candles, report = validate_candles(raw, 15, now=now)
    except (MT5Error, CandleValidationError) as exc:
        fail(f"Descarga/validación de velas: {exc}")
        print("\nResultado: FALLO")
        return 1
    first, last = candles["timestamp"].iloc[0], candles["timestamp"].iloc[-1]
    ok(f"Velas M15 descargadas: {len(candles)}")
    print(f"         Rango disponible: {first:%Y-%m-%d %H:%M} -> {last:%Y-%m-%d %H:%M} UTC "
          f"({(last - first).days} días)")
    if first > start + START_TOLERANCE:
        warn(f"Hay menos de {args.months} meses de histórico M15 (empieza el {first:%Y-%m-%d}, se pidió desde "
             f"{start:%Y-%m-%d}). Sube 'Máx. barras en el gráfico' en Herramientas > Opciones > Gráficos, "
             "abre un gráfico M15 del símbolo y desplázate hacia atrás para que el terminal lo descargue.")
    else:
        ok(f"Hay al menos {args.months} meses de histórico M15")
    print("\n" + report.summary())

    opens = weekly_opens_ny(candles)
    print("\n== Apertura semanal en hora de Nueva York (debería ser siempre la misma)")
    for when, n in opens.most_common():
        print(f"         {when}: {n} semanas")
    if len(opens) > 1:
        warn("La apertura semanal cambia de hora a lo largo del año: la conversión horaria no sigue el "
             "horario de verano de tu bróker. Prueba server_timezone: \"NY+7\" en config.yaml.")
    elif opens:
        ok("Conversión horaria coherente todo el año")

    print("\n== Últimas 5 velas cerradas (UTC)")
    with pd.option_context("display.width", 120):
        print(candles.tail(5).to_string(index=False))

    print("\nResultado: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Backtest de 12 meses y check_mt5.py con un MetaTrader5 simulado."""

import pandas as pd
import pytest
import yaml

import check_mt5
from data.base import to_engine_frame
from data.csv_source import load_csv
from data.mt5_source import MT5Source
from data.validation import validate_candles
from detector import DetectorConfig, SMCEngine
from main import cmd_backtest
from settings import ROOT, load_config
from tests.fake_mt5 import FakeMT5, market_rates

NOW = pd.Timestamp("2026-10-02 12:07", tz="UTC")  # viernes, mercado abierto
START = NOW - pd.DateOffset(months=12)


@pytest.fixture(scope="module")
def year_rates():
    # Bróker NY+7 (GMT+2 / GMT+3), un poco más de 12 meses, con la vela en formación al final.
    return market_rates(START - pd.Timedelta(days=3), NOW.floor("15min"), server_timezone="NY+7")


def write_config(tmp_path, **mt5):
    raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    raw["data_source"]["mt5"].update(mt5)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


# --------------------------------------------------------------- backtest
def test_backtest_12_months_from_mt5(tmp_path, year_rates, capsys):
    cfg = load_config(write_config(tmp_path, server_timezone="NY+7"), env_file=None)
    fake = FakeMT5(year_rates)
    src = MT5Source("EURUSD", server_timezone="NY+7", mt5_module=fake)
    out, candles_csv = tmp_path / "alerts.csv", tmp_path / "velas.csv"
    assert cmd_backtest(cfg, str(out), source=src, export_csv=str(candles_csv), now=NOW) == 0

    # Se pidieron 12 meses con copy_rates_range y la conexión se cerró.
    req = [c for c in fake.calls if c[0] == "copy_rates_range"]
    assert len(req) == 1 and req[0][2] == FakeMT5.TIMEFRAME_M15
    assert fake.shutdowns == 1

    printed = capsys.readouterr().out
    assert "Huecos lunes-viernes: 0" in printed and "Vela en formación descartada: sí" in printed

    alerts = pd.read_csv(out)
    assert {"alert_time_utc", "direction", "swept_level", "swept_price", "choch_level",
            "fvg_low", "fvg_high", "stop_loss"} <= set(alerts.columns)
    assert len(alerts) > 0, "con un año de paseo aleatorio debería haber alguna alerta"
    assert set(alerts["direction"]) <= {"long", "short"} and set(alerts["swept_level"]) <= {"PDH", "PDL"}

    # El CSV de velas se puede volver a leer y coincide (para compararlo con TradingView).
    exported = pd.read_csv(candles_csv)
    assert list(exported.columns) == ["timestamp", "unix_time", "open", "high", "low", "close", "volume"]
    assert len(exported) > 24_000
    reloaded = load_csv(candles_csv)
    assert reloaded.index[0] >= START and reloaded.index[-1] < NOW.floor("15min")
    assert (reloaded.index.as_unit("s").asi8 == exported["unix_time"].to_numpy()).all()


def test_pdh_pdl_use_previous_complete_trading_day(year_rates):
    raw = MT5Source("EURUSD", server_timezone="NY+7", mt5_module=FakeMT5(year_rates)).get_range(
        "EURUSD", 15, START.to_pydatetime(), NOW.to_pydatetime()
    )
    candles, _ = validate_candles(raw, 15, now=NOW)
    df = SMCEngine(DetectorConfig()).prepare(to_engine_frame(candles))

    # Cálculo independiente: máximo/mínimo de cada día de trading (17:00-17:00 NY), lunes-viernes.
    ny = df.index.tz_convert("America/New_York")
    day = (ny.tz_localize(None) + pd.Timedelta(hours=7)).date
    daily = df.groupby(day).agg(h=("high", "max"), l=("low", "min"))
    daily = daily[[pd.Timestamp(d).weekday() < 5 for d in daily.index]]
    prev = daily.shift(1)
    expected_pdh = pd.Series(day, index=df.index).map(prev["h"])
    expected_pdl = pd.Series(day, index=df.index).map(prev["l"])

    checked = df["pdh"].notna()
    assert checked.sum() > 24_000
    assert (df.loc[checked, "pdh"] == expected_pdh[checked]).all()
    assert (df.loc[checked, "pdl"] == expected_pdl[checked]).all()
    # Nunca usa el día en curso: el PDH no cambia dentro de un mismo día.
    assert (df[checked].groupby("trading_day")["pdh"].nunique() == 1).all()


# --------------------------------------------------------------- check_mt5.py
def run_check(tmp_path, capsys, fake, **mt5):
    code = check_mt5.main(["--config", str(write_config(tmp_path, **mt5))], mt5_module=fake, now=NOW)
    return code, capsys.readouterr().out


def test_check_ok_with_12_months(tmp_path, capsys, year_rates):
    fake = FakeMT5(year_rates, tick_time=NOW.timestamp() + 3 * 3600 - 5)
    code, out = run_check(tmp_path, capsys, fake, server_timezone="NY+7")
    assert code == 0
    for text in ("Versión MT5: 500 build 4620", "Cuenta: 12345678", "servidor: Broker-Demo",
                 "Símbolo: EURUSD", "Desfase actual estimado del servidor: UTC+3",
                 "Velas M15 descargadas:", "Rango disponible:", "Hay al menos 12 meses",
                 "Conversión horaria coherente", "Últimas 5 velas", "Resultado: OK"):
        assert text in out, text
    assert "[AVISO]" not in out and "[FALLO]" not in out
    assert fake.shutdowns >= 2 and not fake.initialized


def test_check_warns_with_less_than_12_months(tmp_path, capsys, year_rates):
    short = year_rates[-3000:]  # ~1,5 meses
    code, out = run_check(tmp_path, capsys, FakeMT5(short, tick_time=NOW.timestamp() + 3 * 3600), server_timezone="NY+7")
    assert code == 0
    assert "[AVISO]  Hay menos de 12 meses de histórico M15" in out


def test_check_detects_fixed_offset_vs_broker_dst(tmp_path, capsys, year_rates):
    # Bróker NY+7 pero configurado con desfase fijo +2: la apertura semanal se mueve con el horario de verano.
    code, out = run_check(tmp_path, capsys, FakeMT5(year_rates, tick_time=NOW.timestamp() + 3 * 3600),
                          server_utc_offset_hours=2, server_timezone=None)
    assert code == 0
    assert "no coincide con el desfase estimado UTC+3" in out
    assert "La apertura semanal cambia de hora" in out and 'server_timezone: "NY+7"' in out


def test_check_fails_when_terminal_closed(tmp_path, capsys):
    code, out = run_check(tmp_path, capsys, FakeMT5(init_ok=False))
    assert code == 1
    assert "[FALLO]" in out and "IPC initialize failed" in out and "Abre el terminal" in out


def test_check_fails_when_not_logged_in(tmp_path, capsys, year_rates):
    code, out = run_check(tmp_path, capsys, FakeMT5(year_rates, logged_in=False), server_timezone="NY+7")
    assert code == 1
    assert "No hay sesión iniciada" in out and "Resultado: FALLO" in out


def test_check_fails_on_unknown_symbol(tmp_path, capsys):
    code, out = run_check(tmp_path, capsys, FakeMT5(symbols=["EURUSD.r"]), symbol="EURUSD")
    assert code == 1
    assert "El símbolo 'EURUSD' no existe" in out and "EURUSD.r" in out

"""Carga de config.yaml y .env en objetos tipados."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

from daily_cycle_alerts import DailyCycleAlerts
from detector.chartz import ChartzConfig
from detector.daily_cycle import DailyCycleConfig
from detector.config import DetectorConfig, SessionWindow

ROOT = Path(__file__).resolve().parent


@dataclass
class DataSourceConfig:
    type: str = "mt5"
    lookback_days: int = 5
    mt5_symbol: str = "EURUSD"
    mt5_server_utc_offset_hours: float = 0.0
    mt5_auto_detect_offset: bool = False
    mt5_server_timezone: str | None = None  # si se define, prevalece sobre el desfase fijo
    yahoo_ticker: str = "EURUSD=X"


@dataclass
class SmtpConfig:
    user: str = ""
    password: str = ""
    to: list[str] = field(default_factory=list)
    host: str = "smtp.gmail.com"
    port: int = 465


@dataclass
class AppConfig:
    symbol: str
    detector: DetectorConfig
    data_source: DataSourceConfig
    smtp: SmtpConfig
    state_path: Path
    state_keep_days: int
    max_alert_age_minutes: int
    dry_run: bool
    loop_interval_seconds: int
    candle_close_delay_seconds: int
    log_file: Path
    log_level: str
    log_max_bytes: int
    log_backup_count: int
    chartz: ChartzConfig = field(default_factory=ChartzConfig)
    daily_cycle: DailyCycleAlerts = field(default_factory=DailyCycleAlerts)


def _parse_time(value: str) -> time:
    if not isinstance(value, str):
        raise ValueError(f"Hora de sesión inválida {value!r}: escríbela entre comillas, p.ej. \"07:00\"")
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def _sessions(raw: dict) -> list[SessionWindow]:
    if not raw.get("enabled", False):
        return []
    definitions = raw.get("definitions", {})
    windows = []
    for name in raw.get("sessions", []):
        if name not in definitions:
            raise ValueError(f"Sesión '{name}' no definida en session_filter.definitions")
        d = definitions[name]
        windows.append(SessionWindow(name, d["timezone"], _parse_time(d["start"]), _parse_time(d["end"])))
    return windows


# Combos de temporalidades del curso (pág. 72): estructura / POI / CDC en minutos.
CHARTZ_COMBOS = {
    "intradia": dict(htf_minutes=60, poi_minutes=15, ltf_minutes=5),
    "scalping": dict(htf_minutes=15, poi_minutes=5, ltf_minutes=1, max_poi_age_hours=24, max_cdc_hours=2),
}


def _chartz(raw: dict, pip_size: float) -> ChartzConfig:
    combo = raw.get("combo", "scalping")
    if combo not in CHARTZ_COMBOS:
        raise ValueError(f"chartz.combo debe ser uno de {list(CHARTZ_COMBOS)}")
    kwargs = dict(CHARTZ_COMBOS[combo])
    for key in ("entry", "require_fvg", "require_liquidity_grab", "require_irl", "min_risk_pips"):
        if key in raw:
            kwargs[key] = raw[key]
    if raw.get("killzones") is not None:
        kwargs["killzones"] = _sessions({"enabled": True, **raw["killzones"]}) if raw["killzones"].get("enabled", True) else []
    return ChartzConfig(pip_size=pip_size, **kwargs)


def _daily_cycle(raw: dict) -> DailyCycleAlerts:
    if not raw:
        return DailyCycleAlerts()
    det = DailyCycleConfig(
        ltf_minutes=15,
        htf_minutes=int(raw.get("bias_minutes", 240)),
        single_candle_inf=True,
        entry="fvg_mid",
        windows=[tuple(w) for w in raw.get("windows", [[2, 5]])],
        min_risk_pips=float(raw.get("min_risk_pips", 1.5)),
        pip_size=float(raw.get("pip_size", 0.0001)),
    )
    return DailyCycleAlerts(
        enabled=bool(raw.get("enabled", False)),
        dry_run=bool(raw.get("dry_run", True)),
        symbol=str(raw.get("symbol", "GBPUSD")),
        mt5_symbol=str(raw.get("mt5_symbol", raw.get("symbol", "GBPUSD"))),
        yahoo_ticker=str(raw.get("yahoo_ticker", f"{raw.get('symbol', 'GBPUSD')}=X")),
        lookback_days=int(raw.get("lookback_days", 40)),
        tp_r=float(raw.get("tp_r", 2)),
        state_path=_path(raw.get("state_path", "state/daily_cycle_gbpusd.json")),
        detector=det,
    )


def _path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else ROOT / p


def load_config(path: str | Path = ROOT / "config.yaml", env_file: str | Path | None = ROOT / ".env") -> AppConfig:
    if env_file is not None and Path(env_file).exists():
        load_dotenv(env_file)
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    det = raw.get("detector", {})
    day = raw.get("trading_day", {})
    tf = int(raw.get("timeframe_minutes", 15))
    detector = DetectorConfig(
        swing_n=int(det.get("swing_n", 2)),
        max_setup_hours=float(det.get("max_setup_hours", 8)),
        fvg_max_bars_after_choch=int(det.get("fvg_max_bars_after_choch", 1)),
        min_fvg_pips=float(det.get("min_fvg_pips", 0)),
        pip_size=float(det.get("pip_size", 0.0001)),
        one_alert_per_level_per_day=bool(det.get("one_alert_per_level_per_day", True)),
        day_close_hour=int(day.get("close_hour", 17)),
        day_close_tz=day.get("timezone", "America/New_York"),
        sessions=_sessions(raw.get("session_filter", {})),
        timeframe_minutes=tf,
    )

    ds = raw.get("data_source", {})
    mt5 = ds.get("mt5", {}) or {}
    data_source = DataSourceConfig(
        type=os.getenv("DATA_SOURCE") or ds.get("type", "mt5"),  # GitHub Actions: DATA_SOURCE=yahoo
        lookback_days=int(ds.get("lookback_days", 5)),
        mt5_symbol=str(mt5.get("symbol", "EURUSD")),
        mt5_server_utc_offset_hours=float(mt5.get("server_utc_offset_hours", 0)),
        mt5_auto_detect_offset=bool(mt5.get("auto_detect_offset", False)),
        mt5_server_timezone=mt5.get("server_timezone") or None,
        yahoo_ticker=str((ds.get("yahoo", {}) or {}).get("ticker", "EURUSD=X")),
    )

    alerts = raw.get("alerts", {})
    smtp = SmtpConfig(
        user=os.getenv("SMTP_USER", ""),
        password=os.getenv("SMTP_APP_PASSWORD", ""),
        to=[a.strip() for a in os.getenv("ALERT_TO", "").split(",") if a.strip()],
        host=alerts.get("smtp_host", "smtp.gmail.com"),
        port=int(alerts.get("smtp_port", 465)),
    )

    state = raw.get("state", {})
    loop = raw.get("loop", {})
    logging_cfg = raw.get("logging", {})
    return AppConfig(
        symbol=raw.get("symbol", "EURUSD"),
        detector=detector,
        data_source=data_source,
        smtp=smtp,
        state_path=_path(state.get("path", "state/state.json")),
        state_keep_days=int(state.get("keep_days", 30)),
        max_alert_age_minutes=int(os.getenv("MAX_ALERT_AGE_MINUTES") or alerts.get("max_alert_age_minutes", 30)),
        dry_run=bool(alerts.get("dry_run", False)),
        loop_interval_seconds=int(loop.get("interval_seconds", 60)),
        candle_close_delay_seconds=int(loop.get("candle_close_delay_seconds", 10)),
        log_file=_path(logging_cfg.get("file", "logs/eurusd_alerts.log")),
        log_level=logging_cfg.get("level", "INFO"),
        log_max_bytes=int(logging_cfg.get("max_bytes", 5_000_000)),
        log_backup_count=int(logging_cfg.get("backup_count", 5)),
        chartz=_chartz(raw.get("chartz", {}) or {}, detector.pip_size),
        daily_cycle=_daily_cycle(raw.get("daily_cycle", {}) or {}),
    )

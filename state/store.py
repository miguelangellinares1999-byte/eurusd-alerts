"""Persistencia de setups y de la última vela procesada en JSON.

Escritura atómica (fichero temporal + os.replace) para que un corte a mitad de
escritura no corrompa el estado y provoque alertas duplicadas.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import timedelta
from pathlib import Path

import pandas as pd

from detector.models import Setup, SetupState

log = logging.getLogger(__name__)

VERSION = 1


class StateStore:
    def __init__(self, path: str | Path | None):
        """`path=None` = estado solo en memoria (backtest y tests)."""
        self.path = Path(path) if path is not None else None
        self.setups: dict[str, Setup] = {}
        self.last_processed: pd.Timestamp | None = None

    def load(self) -> "StateStore":
        if self.path is None or not self.path.exists():
            return self
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            backup = self.path.with_suffix(".corrupt.json")
            self.path.replace(backup)
            log.error("Estado corrupto, movido a %s; se empieza de cero", backup)
            return self
        lp = raw.get("last_processed")
        self.last_processed = pd.Timestamp(lp) if lp else None
        self.setups = {k: Setup.from_dict(v) for k, v in raw.get("setups", {}).items()}
        return self

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": VERSION,
            "last_processed": None if self.last_processed is None else self.last_processed.isoformat(),
            "setups": {k: s.to_dict() for k, s in self.setups.items()},
        }
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def pending_alerts(self) -> list[Setup]:
        return [s for s in self.setups.values() if s.state is SetupState.FVG]

    def prune(self, now: pd.Timestamp, keep_days: int) -> int:
        """Elimina setups finalizados más antiguos que `keep_days`."""
        cutoff = now - timedelta(days=keep_days)
        old = [
            k
            for k, s in self.setups.items()
            if s.state in (SetupState.ALERTED, SetupState.INVALIDATED)
            and s.updated_at is not None
            and s.updated_at < cutoff
        ]
        for k in old:
            del self.setups[k]
        return len(old)

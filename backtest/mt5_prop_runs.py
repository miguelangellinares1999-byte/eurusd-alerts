"""Pruebas de fondeo con el bot real (mt5/IndexPropBot.mq5) en el Probador de Estrategias de MT5.

Lanza una prueba por cada fecha de inicio (cada trimestre por defecto) con la configuración
IndexPropBot_fondeo_100k.set: 100.000 USD, +10% / DD 10% / diario 5%. El bot cierra todo y deja de
operar al llegar al objetivo o al DD máximo; aquí se lee del diario del probador cuándo y por qué
paró, y se guardan sus operaciones en backtest/mt5_runs/<inicio>.csv.

Requisitos: MT5 cerrado (el probador se lanza con /config y cierra el terminal al acabar) y el puerto
3000 libre (lo usa el agente local del probador).
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from pathlib import Path

import pandas as pd

TERMINAL = Path(r"C:\Program Files\BlackBull Markets MT5\terminal64.exe")
DATA = Path.home() / "AppData/Roaming/MetaQuotes/Terminal/16D9C17040576AD13C62C316983027D5"
CSV = Path.home() / "AppData/Roaming/MetaQuotes/Terminal/Common/Files/IndexPropBot_trades.csv"
OUT = Path("backtest/mt5_runs")
INI = Path("mt5/tester_fondeo.ini")
DEPOSIT = 100_000

INI_TEMPLATE = """; Generado por backtest/mt5_prop_runs.py: prueba de fondeo con IndexPropBot.
[Tester]
Expert=IndexPropBot
ExpertParameters=IndexPropBot_fondeo_100k.set
Symbol=NAS100
Period=M5
Model=1
FromDate={start:%Y.%m.%d}
ToDate={end:%Y.%m.%d}
ForwardMode=0
Deposit={deposit}
Currency=USD
Leverage=100
Optimization=0
Visual=0
ShutdownTerminal=1
"""


def journal() -> str:
    """Diario del probador del día (UTF-16), todo el texto."""
    logs = sorted((DATA / "Tester/logs").glob("*.log"))
    return logs[-1].read_text(encoding="utf-16", errors="ignore") if logs else ""


def run_one(start: pd.Timestamp, end: pd.Timestamp) -> dict:
    INI.write_text(INI_TEMPLATE.format(start=start, end=end, deposit=DEPOSIT).replace("\n", "\r\n"), encoding="utf-8")
    before = len(journal())
    subprocess.run([str(TERMINAL), f"/config:{INI.resolve()}"], check=False)
    log = journal()[before:]
    final = re.search(r"final balance ([\d.]+)", log)
    stop = re.search(r"(\d{4}\.\d\d\.\d\d \d\d:\d\d:\d\d)\s+Cierre total: (objetivo alcanzado|drawdown máximo)", log)
    trades = pd.DataFrame()
    if CSV.exists():
        OUT.mkdir(parents=True, exist_ok=True)
        dest = OUT / f"{start:%Y-%m-%d}.csv"
        shutil.copy(CSV, dest)
        trades = pd.read_csv(dest, sep=";")
    if stop:
        result = "aprobada" if stop.group(2) == "objetivo alcanzado" else "DD máximo"
        when = pd.Timestamp(stop.group(1).replace(".", "-"))
    else:
        result, when = "sin resolver", None
    eq = DEPOSIT + trades.beneficio.cumsum() if len(trades) else pd.Series([DEPOSIT])
    return {
        "inicio": start.date(),
        "resultado": result,
        "fin": when.date() if when is not None else None,
        "días de trading": len(pd.bdate_range(start, when)) if when is not None else None,
        "operaciones": len(trades),
        "saldo final": round(float(final.group(1)), 2) if final else None,
        "peor saldo": round(float(eq.min()), 2),
        "error": None if final else "el probador no terminó la prueba (revisa Tester/logs)",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--first", default="2020-07-01")
    ap.add_argument("--last", default="2025-07-01")
    ap.add_argument("--every", default="QS", help="frecuencia de inicios (pandas): QS = trimestral, MS = mensual")
    ap.add_argument("--months", type=int, default=15, help="duración máxima de cada prueba")
    args = ap.parse_args()
    today = pd.Timestamp.today().normalize() - pd.Timedelta(days=1)
    rows = []
    for start in pd.date_range(args.first, args.last, freq=args.every):
        end = min(start + pd.DateOffset(months=args.months), today)
        row = run_one(start, end)
        print(row, flush=True)
        rows.append(row)
    res = pd.DataFrame(rows)
    res.to_csv(OUT / "resumen.csv", index=False)
    pd.set_option("display.width", 200)
    print("\n" + res.to_string(index=False))
    done = res[res.resultado != "sin resolver"]
    passed = res[res.resultado == "aprobada"]
    print(f"\nAprobadas {len(passed)}/{len(res)} ({100 * len(passed) / max(len(res), 1):.0f}%), "
          f"DD máximo {int((res.resultado == 'DD máximo').sum())}, sin resolver {len(res) - len(done)}; "
          f"días hasta aprobar: mediana {passed['días de trading'].median():.0f}")


if __name__ == "__main__":
    main()

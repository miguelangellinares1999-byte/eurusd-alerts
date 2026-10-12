@echo off
rem Informe diario de IndexPropBot en la demo (lo lanza la tarea programada "IndexPropBot informe demo").
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0.."
if not exist logs mkdir logs
echo ==== %date% %time% >> logs\demo_report.log
"C:\Program Files\Python314\python.exe" -W ignore -m backtest.demo_report --email --since 2026-10-09 >> logs\demo_report.log 2>&1

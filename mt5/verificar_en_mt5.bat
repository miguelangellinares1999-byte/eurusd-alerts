@echo off
chcp 65001 >nul
rem Pasa IndexPropBot por el Probador de Estrategias de MT5 y compara sus operaciones con el backtest en Python.
echo.
echo  1. Cierra MetaTrader 5 (el probador abre su propia ventana y la cierra al acabar).
echo  2. Puede tardar unos minutos la primera vez: MT5 descarga el historial M1 desde 2020.
echo.
pause
rem El agente local del probador escucha en 127.0.0.1:3000; si otro programa ocupa ese puerto
rem (p. ej. un servidor de desarrollo web), la prueba falla con "tester agent authorization error".
netstat -ano | findstr /R /C:":3000 .*LISTENING" >nul
if not errorlevel 1 (
  echo.
  echo  ERROR: el puerto 3000 esta ocupado por otro programa ^(el probador de MT5 lo necesita^).
  echo  Cierralo ^(por ejemplo el servidor de backtesting_app^) y vuelve a ejecutar este archivo.
  netstat -ano | findstr /R /C:":3000 .*LISTENING"
  pause
  exit /b 1
)
tasklist | findstr /I terminal64.exe >nul
if not errorlevel 1 (
  echo  ERROR: MetaTrader 5 sigue abierto. Cierralo y vuelve a ejecutar este archivo.
  pause
  exit /b 1
)
start "" /wait "C:\Program Files\BlackBull Markets MT5\terminal64.exe" /config:"%~dp0tester_verificacion.ini"
cd /d "%~dp0.."
python -m backtest.verify_mt5
echo.
echo  Informe del probador: carpeta de datos de MT5\reports\IndexPropBot_verificacion.htm
pause

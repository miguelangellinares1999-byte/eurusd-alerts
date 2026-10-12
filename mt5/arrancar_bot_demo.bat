@echo off
chcp 65001 >nul
rem Abre MT5 con IndexPropBot puesto en NAS100 M5 y el trading algorítmico activado.
rem Antes: MT5 conectado a la cuenta DEMO con "Guardar contraseña" marcado.
tasklist | findstr /I terminal64.exe >nul
if not errorlevel 1 (
  echo  Cierra MetaTrader 5 y vuelve a ejecutar este archivo ^(MT5 solo carga el bot al arrancar^).
  pause
  exit /b 1
)
start "" "C:\Program Files\BlackBull Markets MT5\terminal64.exe" /config:"%~dp0arrancar_bot_demo.ini"
echo  MT5 abierto con el bot. Comprueba arriba a la derecha del grafico NAS100,M5 el gorro azul de IndexPropBot.
timeout /t 10 >nul

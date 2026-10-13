@echo off
chcp 65001 >nul
rem Abre MT5 con CRTPropBot puesto en NAS100 M15 y el trading algorítmico activado.
rem Antes: MT5 conectado a una cuenta DEMO de BlackBull de 5.000 USD con "Guardar contraseña" marcado.
rem Si el bot no arranca porque el saldo no cuadra, la cuenta no es la de 5.000 USD.
set "MT5=%~1"
if "%MT5%"=="" set "MT5=C:\Program Files\BlackBull Markets MT5"
tasklist | findstr /I terminal64.exe >nul
if not errorlevel 1 (
  echo  Cierra MetaTrader 5 y vuelve a ejecutar este archivo ^(MT5 solo carga el bot al arrancar^).
  pause
  exit /b 1
)
start "" "%MT5%\terminal64.exe" /config:"%~dp0arrancar_crt_demo.ini"
echo  MT5 abierto con el bot. Comprueba arriba a la derecha del grafico NAS100,M15 el gorro azul de CRTPropBot.
timeout /t 10 >nul

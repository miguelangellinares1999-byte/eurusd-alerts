# eurusd-alerts

Alertas por email cuando EURUSD (15m) completa esta secuencia SMC:

1. **SWEEP**: una vela de 15m supera con la mecha el máximo (PDH) o mínimo (PDL) del día anterior y **cierra de vuelta** dentro del rango.
2. **CHoCH**: tras barrer el PDH, una vela cierra (cuerpo) por debajo del último swing low; tras barrer el PDL, por encima del último swing high.
3. **FVG**: hueco de 3 velas en la pierna que provoca el CHoCH.
4. **ALERTA**: email con dirección, nivel barrido, nivel del CHoCH, zona del FVG (entrada) y extremo del sweep (SL).

```
eurusd-alerts/
├── detector/      lógica pura: niveles, sweep, swings, CHoCH, FVG, sesiones y máquina de estados
├── data/          fuentes de velas (MetaTrader5, CSV) y validación de velas
├── state/         persistencia JSON de setups (state.json se crea aquí)
├── notifier/      email SMTP (Gmail) y modo dry-run
├── backtest/      recorrido de un histórico y exportación a CSV
├── tests/         pytest
├── main.py        CLI: run | once | backtest | test-email
├── check_mt5.py   comprobación de la conexión con MT5 y del histórico
├── settings.py    carga de config.yaml + .env
└── config.yaml
```

## Instalación

Requiere **Windows**, Python 3.10 o superior y el terminal **MetaTrader 5** de tu bróker (la librería `MetaTrader5` solo existe para Windows y se comunica con el terminal instalado).

```powershell
cd eurusd-alerts
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt   # incluye MetaTrader5
copy .env.example .env
```

### MetaTrader 5

1. Instala el terminal MT5 de tu bróker (cuenta demo o real).
2. **Ábrelo e inicia sesión antes de ejecutar** cualquier comando del proyecto. Si el terminal no está abierto, `mt5.initialize()` falla con `IPC initialize failed, MetaTrader 5 x64 not found`. Si quieres que el script inicie sesión por su cuenta, pon `MT5_LOGIN`, `MT5_PASSWORD` y `MT5_SERVER` en `.env` (y `MT5_PATH` si tienes varios terminales).
3. Para backtests largos, en *Herramientas > Opciones > Gráficos* sube **Máx. barras en el gráfico** (p.ej. 100000 o "Unlimited"); 12 meses de M15 son unas 25.000 velas.
4. Comprueba la conexión:

```powershell
python check_mt5.py
```

Muestra la versión de MT5, la cuenta, el servidor, el símbolo, el desfase horario del servidor, el rango de fechas disponible, el nº de velas M15 descargadas, los huecos y las últimas 5 velas. Avisa si hay menos de 12 meses de histórico M15 y si la conversión horaria no es coherente a lo largo del año. Sale con código 1 si algo falla.

### Símbolo y zona horaria (`config.yaml` → `data_source.mt5`)

- **`symbol`**: el nombre exacto en tu bróker (`EURUSD`, `EURUSD.r`, `EURUSDm`...). Si no existe, el error lista los símbolos parecidos que sí tiene el bróker.
- **Hora del servidor**: MT5 da las velas en la hora del servidor del bróker, no en UTC. Opciones, de mayor a menor prioridad:
  - **`server_timezone`** (recomendado): zona con horario de verano. `"NY+7"` = hora de Nueva York + 7, que es lo que usan la mayoría de brókers (GMT+2 en invierno / GMT+3 en verano, la vela diaria cierra a las 00:00 del servidor). También acepta un nombre IANA (`"Europe/Athens"`, `"UTC"`).
  - **`auto_detect_offset: true`**: al conectar estima el desfase comparando la hora del último tick (`mt5.symbol_info_tick`) con la hora UTC actual. Si el mercado está cerrado o no hay ticks recientes, usa `server_utc_offset_hours`. Solo sirve para el desfase *actual*: no tiene en cuenta los cambios de hora del pasado.
  - **`server_utc_offset_hours`**: desfase fijo en horas (p.ej. `2`).

  Un desfase fijo solo es correcto la mitad del año si tu bróker cambia de hora. En un backtest de 12 meses eso desplaza una hora el día de trading (y por tanto el PDH/PDL) durante medio año. `check_mt5.py` lo detecta: si la apertura semanal (domingo 17:00 NY) no cae siempre a la misma hora, te recomienda `server_timezone: "NY+7"`.

### Contraseña de aplicación de Gmail

1. Activa la verificación en dos pasos en tu cuenta de Google.
2. Ve a <https://myaccount.google.com/apppasswords>, crea una contraseña (p.ej. "eurusd-alerts").
3. Pon en `.env`: `SMTP_USER`, `SMTP_APP_PASSWORD` (los 16 caracteres; los espacios dan igual) y `ALERT_TO` (uno o varios destinatarios separados por comas).
4. Comprueba que funciona: `python main.py test-email`

## Uso

```powershell
python main.py run          # bucle continuo: se despierta cada minuto, procesa solo al cerrar una vela de 15m
python main.py once         # un único ciclo y sale (para cron / Task Scheduler)
python main.py test-email   # email de prueba
python main.py backtest     # 12 meses de M15 desde MT5 (ver Backtest)
python check_mt5.py         # comprobación de la conexión con MT5
python -m pytest            # tests
```

Para probar sin enviar emails pon `alerts.dry_run: true` en `config.yaml`: las alertas se escriben en el log.

Los logs van a `logs/eurusd_alerts.log` (con rotación) y a la consola.

## Cómo funciona

### Ciclo en vivo

Cada minuto (`run`) o en cada ejecución de `once`:

1. Calcula cuál es la última vela de 15m que debería estar cerrada (con un margen de `candle_close_delay_seconds`). Si ya está procesada según `state/state.json`, termina sin descargar nada. De viernes 17:00 a domingo 17:00 (NY) no hace nada.
2. Descarga de MT5 las velas de los últimos `lookback_days` días (`copy_rates_from_pos`), las convierte a UTC, las valida (`validate_candles`), **descarta la vela en formación** y procesa en orden las velas nuevas posteriores a la última procesada. Cada descarga abre y cierra la conexión con el terminal (`mt5.initialize()` / `mt5.shutdown()`).
3. Envía los setups que han llegado a `FVG`, los marca `ALERTED` y guarda el estado.

El mismo motor (`detector/engine.py`) se usa en vivo y en el backtest: al procesar la vela *i* solo mira velas ≤ *i*.

### Máquina de estados (por setup)

```
SWEEP ──CHoCH──▶ CHOCH ──FVG──▶ FVG ──email OK──▶ ALERTED
  │                │              └─ email falla: se reintenta en el siguiente ciclo
  └──────┬─────────┘
         ▼
    INVALIDATED  (extremo del sweep roto, timeout, sin FVG, alerta caducada)
```

Cada setup se guarda en `state/state.json` con su historial de transiciones. Eso evita alertas duplicadas aunque el proceso se reinicie, y aunque las mismas velas se procesen dos veces.

### Reglas y decisiones concretas

| Tema | Regla |
|---|---|
| Día de trading | Cierra a las 17:00 de Nueva York (`trading_day.close_hour` / `timezone`), con horario de verano. Una vela que abre a las 17:00 NY ya es del día siguiente. |
| PDH/PDL | Máximo/mínimo del día de trading **válido** anterior. Los "días" de sábado/domingo se ignoran (el lunes usa el viernes); si el primer día del histórico está cortado tampoco se usa. |
| Sweep | `high > PDH` y `close < PDH` → setup **short**, SL = high de esa vela. `low < PDL` y `close > PDL` → setup **long**, SL = low. |
| Swings | Pivote estricto con `swing_n` velas a cada lado (por defecto 2). Un pivote en *i* solo existe al cerrar la vela *i+N* (sin lookahead). Máximos/mínimos iguales no forman pivote. |
| Nivel del CHoCH | Último swing low (short) / swing high (long) **confirmado al cerrar la vela del sweep**. Queda fijo para ese setup. Si no hay ninguno, el sweep se ignora. |
| CHoCH | Una vela posterior al sweep **cierra** más allá del nivel. Una mecha no basta. |
| FVG | Patrón de 3 velas con la vela 1 ≥ vela del sweep y la vela 3 ≤ vela del CHoCH. Si hay varios se toma el más reciente. La vela 3 también puede ser hasta `fvg_max_bars_after_choch` velas después del CHoCH (por defecto 1: el desplazamiento suele continuar en la vela siguiente). `min_fvg_pips` filtra huecos diminutos. |
| Invalidación | Antes del FVG: si una vela rompe el extremo del sweep, o pasan `max_setup_hours` horas desde el cierre de la vela del sweep. Tras el CHoCH, si no aparece el FVG en el plazo indicado. Si el mismo nivel se vuelve a barrer después de una invalidación, se abre un setup nuevo con su nuevo extremo. |
| Duplicados | Un setup activo por nivel y día. Con `one_alert_per_level_per_day: true`, tras una alerta ese nivel no vuelve a alertar ese día. |
| Filtro de sesión | `session_filter.enabled: true` → solo se aceptan sweeps cuya vela abre dentro de alguna de las sesiones listadas (cada una en su zona horaria, por lo que el DST se gestiona solo). |
| Alertas antiguas | No se envían alertas completadas hace más de `max_alert_age_minutes` (p.ej. al arrancar con un estado vacío y encontrar un setup de hace horas). |

## Backtest

```powershell
python main.py backtest                                       # últimos 12 meses de M15 desde MT5
python main.py backtest --months 6
python main.py backtest --from 2025-01-01 --to 2025-12-31
python main.py backtest --export-csv backtest/velas_mt5.csv   # guarda también las velas
python main.py backtest --strict-gaps                         # falla si hay huecos lunes-viernes
python main.py backtest --csv EURUSD_M15.csv --tz UTC         # desde un CSV en vez de MT5
```

1. Pide las velas M15 a MT5 con `copy_rates_range` y las convierte a UTC.
2. Las valida con `validate_candles` (ver abajo) e imprime el informe: velas recibidas, duplicados, vela en formación descartada y huecos.
3. Recorre las velas en orden con el mismo motor que el modo en vivo. El PDH/PDL de cada vela sale del **día de trading anterior completo** (17:00–17:00 NY), nunca del día en curso.
4. Muestra cada alerta y un resumen de descartes, y exporta las alertas a `--out` (por defecto `backtest/alerts.csv`): hora de la alerta, dirección, nivel barrido (PDH/PDL y precio), nivel del CHoCH, zona FVG (`fvg_low`–`fvg_high`), entrada en el punto medio, SL y riesgo en pips.

12 meses tardan unos 15–30 s.

**Comparar con TradingView**: `--export-csv` guarda las velas usadas con `timestamp` (UTC), `unix_time` (segundos, como el export de TradingView), OHLC y volumen (ticks). En TradingView pon la zona horaria del gráfico en UTC para comparar vela a vela. Pequeñas diferencias de precio entre brókers son normales; si las velas salen desplazadas una o más horas, revisa la zona horaria del servidor.

### Validación de velas (`data/validation.py`)

| Comprobación | Si falla |
|---|---|
| Ordenadas por tiempo | Se ordenan |
| Sin timestamps duplicados | Se queda la última y se informa |
| Sin NaN | Error |
| `timestamp` con zona horaria y alineado a 15 min | Error (suele indicar un desfase horario mal configurado) |
| `high >= max(open, close)` y `low <= min(open, close)` | Error con las velas afectadas |
| Solo velas cerradas | La última se descarta si sigue en formación; velas "del futuro" antes de la última dan error |
| Sin huecos lunes-viernes (el fin de semana, de viernes 17:00 a domingo 17:00 NY, se ignora) | Se informan en el log y en el informe; con `--strict-gaps`, error. Los festivos (25 de diciembre, 1 de enero) aparecen como huecos esperables |

### CSV

Formatos aceptados con `--csv`: columnas `time` (o `datetime`, `date`, `timestamp`) + `open, high, low, close`; el export de MetaTrader (`<DATE> <TIME> <OPEN> ...`); y el CSV que genera `--export-csv`. Los datos de menor temporalidad (1m, 5m) se agregan a 15m. `--tz` indica la zona horaria de las fechas si no la incluyen (un export manual de MT5 está en hora del servidor: p.ej. `--tz Europe/Athens`).

## Despliegue

Elige **una** de las dos formas: un proceso permanente (`run`) o una ejecución por minuto (`once`). `once` sale enseguida si no ha cerrado una vela nueva, así que ejecutarlo cada minuto es barato.

### Windows – Task Scheduler

Opción A, proceso permanente al iniciar sesión (con `pythonw.exe` no se abre ventana):

```powershell
schtasks /Create /TN "EURUSD Alerts" /SC ONLOGON /RL LIMITED /TR "\"C:\ruta\eurusd-alerts\.venv\Scripts\pythonw.exe\" \"C:\ruta\eurusd-alerts\main.py\" run"
```

Opción B, una ejecución cada minuto:

```powershell
schtasks /Create /TN "EURUSD Alerts" /SC MINUTE /MO 1 /TR "\"C:\ruta\eurusd-alerts\.venv\Scripts\pythonw.exe\" \"C:\ruta\eurusd-alerts\main.py\" once"
```

Desde la interfaz gráfica (`taskschd.msc`) equivale a: *Crear tarea* → Desencadenador "Al iniciar sesión" (A) o "Diariamente, repetir cada 1 minuto indefinidamente" (B) → Acción "Iniciar un programa" con el `pythonw.exe` del venv como programa, `main.py run` / `main.py once` como argumentos y la carpeta del proyecto en "Iniciar en". En *Configuración* deja "Si la tarea ya se está ejecutando: No iniciar una instancia nueva" para evitar ejecuciones solapadas. El terminal MT5 tiene que estar abierto y en la misma sesión de usuario, así que no marques "Ejecutar tanto si el usuario inició sesión como si no".

Comandos útiles: `schtasks /Run /TN "EURUSD Alerts"`, `schtasks /End /TN "EURUSD Alerts"`, `schtasks /Delete /TN "EURUSD Alerts" /F`.

## Tests

```powershell
python -m pytest -v
```

Cubren cada fase con casos alcistas y bajistas (los escenarios long son el reflejo exacto de los short): día de trading y PDH/PDL (DST, fin de semana, día cortado), sweep, swings (N configurable, sin lookahead), CHoCH (cierre frente a mecha), FVG (límites de la pierna, tamaño mínimo), la secuencia completa con sus invalidaciones, el filtro de sesión, el ciclo en vivo (sin duplicados tras reiniciar, reintento si falla el email, velas en formación ignoradas) el backtest y la lectura de CSV. Con un mock del módulo `MetaTrader5` (`tests/fake_mt5.py`, no hace falta el terminal) cubren `MT5Source` (initialize/shutdown, símbolo inexistente, constantes de timeframe, errores con `last_error()`), la conversión de hora del servidor a UTC (desfase fijo, autodetectado y `NY+7`), el día de trading alrededor de los cambios de hora de marzo y noviembre, la validación de velas (orden, duplicados, NaN, OHLC, vela en formación, huecos y fin de semana), un backtest de 12 meses con exportación a CSV, el PDH/PDL de cada vela frente a un cálculo independiente y `check_mt5.py`.

> Herramienta de alertas, no de ejecución. No es asesoramiento financiero.

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

## Estrategia Chartz (curso chart.wzrd) y comparativa

`detector/chartz.py` implementa, como estrategia aparte, el modelo del curso: estructura HTF con BOS/CDC y Trading Range, POI (order block con barrido de liquidez, BOS, ineficiencia y sin mitigar) en descuento/premium del TR y a favor de la tendencia, IRL formada y barrida antes del toque, y entrada con confirmación (barrido + CDC en LTF) dentro de las killzones de Londres y Nueva York. Se configura en la sección `chartz` de `config.yaml` (combo `scalping` M15/M5/M1 o `intradia` 1H/M15/M5). Las alertas en vivo siguen siendo las de PDH/PDL.

```powershell
python main.py compare                                  # 2024-01-01 -> 2026-01-01, velas M1 de MT5
python main.py compare --from 2025-01-01 --to 2025-07-01 --spread 0.5
```

Descarga M1 de MT5, saca las alertas de PDH/PDL (M15) y las señales Chartz, y simula las dos con las mismas reglas (`backtest/trades.py`): orden límite que se cancela si el precio toca el TP antes o al cerrar el día de trading, TP a 1R/2R/3R (y al weak high/low del TR para Chartz), y si SL y TP caen en la misma vela M1 cuenta como pérdida. Escribe `backtest/compare.csv` y las operaciones a 2R en `backtest/trades_2R_*.csv`.

## Daily Cycle en GBPUSD (dry run)

Con `daily_cycle.enabled: true` en `config.yaml`, cada pasada de `run`/`once` también busca la estrategia Daily Cycle (`detector/daily_cycle.py`) en GBPUSD: sesgo del TR de 4H, sweep + CHoCH de una sola vela con FVG en M15 durante la killzone de Londres (02:00–05:00 NY), entrada límite al 50% del FVG, SL en el extremo del sweep y TP a `tp_r`. Va en paralelo a EURUSD con su propia fuente y su propio estado; si falla, no afecta a las alertas de EURUSD.

Con `dry_run: true` no se envía email: la señal se escribe en el log (`ALERTA (dry run)`) y en el diario `state/daily_cycle_gbpusd.json` (todas las señales detectadas, avisadas o no). En GitHub Actions ese fichero se guarda en la caché junto a `state/state.json`. Para recibir emails, `dry_run: false`.

El backtest se hizo con velas de MT5; con Yahoo (GitHub Actions) las velas difieren algo y pueden salir señales distintas.

## Alerta diaria de SPY y QQQ (RSI(2))

`python main.py etf-alert` comprueba la estrategia de `backtest/etf_meanrev.py` en SPY y QQQ con la vela del día aún abierta (Yahoo), para poder entrar al cierre como en el backtest:

- **Comprar al cierre** si el precio está por encima de la MM200 y el RSI(2) baja de 10. Stop a 3 ATR(14) y tamaño de `alloc_pct` % del capital por ETF.
- **Vender al cierre** con el primer cierre por encima de la MM5 o a las 10 sesiones. Si el mínimo toca el stop, avisa de que la operación se ha cerrado.

Solo actúa entre las 15:00 y las 16:00 de Nueva York (`window`), una vez por día y solo si hay vela de hoy (los festivos no hace nada). Manda email únicamente cuando hay algo que hacer, salvo con `daily_summary: true`. Las posiciones avisadas se guardan en `state/etf_alerts.json`: la alerta da por hecho que ejecutas cada aviso. `--dry-run` escribe el aviso en el log y `--force` ignora la ventana horaria y los días ya procesados.

En GitHub Actions (`.github/workflows/etf-alert.yml`) se lanza de lunes a viernes a las 15:30 de Nueva York (dos crons en UTC, uno para el horario de verano y otro para el de invierno; el que cae fuera de la ventana no hace nada) y guarda el estado en la caché. El capital puede venir de la variable `ETF_CAPITAL` del repositorio.

## Alerta semanal de rotación sectorial (S&P 500 y Nasdaq 100)

`python main.py sector-alert` aplica la estrategia de `backtest/sector_rotation.py` a las acciones del S&P 500 y del Nasdaq 100 (`universe`) con el cierre del viernes:

- **Sectores líderes**: los `top` ETFs sectoriales SPDR con más fuerza relativa frente a SPY (13 y 26 semanas) que estén sobre su media de 40 semanas. Solo se opera si SPY también está sobre la suya.
- **Entrada**: en acciones de esos sectores, dos semanas alcistas con FVG, y después una semana que barre el mínimo de la anterior y cierra por encima. Se compra al cierre del viernes, con el SL en el mínimo de esa semana y el TP a `tp_r` R (3R).
- **Tamaño**: `risk_pct` % de riesgo por operación, como mucho `max_alloc_pct` % del capital por posición y `max_positions` abiertas. Si hay más señales que huecos libres, se eligen primero las de los sectores más fuertes.

Manda un email cada sábado (con `weekly_summary: true`, aunque no haya señal) y sigue las operaciones avisadas hasta el SL o el TP en `state/sector_alerts.json`: la alerta da por hecho que ejecutas cada aviso. Las señales de semanas anteriores, cuya entrada ya pasó, salen solo como aviso. En la primera pasada se dan por avisadas sin email.

En GitHub Actions (`.github/workflows/sector-alert.yml`) se lanza los sábados a las 08:30 UTC. El capital puede venir de la variable `SECTOR_CAPITAL` del repositorio.

## Bot MT5 para pruebas de fondeo en índices (IndexPropBot)

`mt5/IndexPropBot.mq5` es un Expert Advisor con el perfil "equilibrado" de `backtest/index_prop_optimize.py` (pruebas de +10% / DD 10% / diario 5% sin límite de tiempo):

- **ORB 30 en NAS100 (1.5%)**: a las 10:00 NY, buy stop en el máximo y sell stop en el mínimo del rango 09:30–10:00 (OCO), SL en el otro extremo y TP 2R. Las órdenes se borran a las 12:00 NY y la posición se cierra a las 16:00 NY.
- **RSI(2) diario en SPX500 y NAS100 (3%)**, solo compras: a las 16:50 NY, si el cierre está por encima de la SMA200 y el RSI(2) baja de 10, compra con SL a 2.5 ATR(14). Sale al primer cierre por encima de la SMA5 o a los 10 días.
- **Reglas de la prueba**: el riesgo se calcula sobre el saldo inicial, no se abre nada que pueda superar el 80% del límite diario, cerca del objetivo arriesga solo lo necesario y, al llegar a +10% o al DD máximo, cierra todo y deja de operar. Si las pérdidas del día (con flotante) llegan al 95% del límite diario, cierra todo hasta el día siguiente.
- No arranca en una cuenta real salvo con `InpAllowReal = true`. La cuenta debe ser *hedging* (en *netting* se mezclarían las posiciones ORB y RSI2 de NAS100). Cuenta con el servidor a hora NY + 7 (`InpServerMinusNY`).

Instalación: copia `mt5/IndexPropBot.mq5` a `MQL5\Experts` (MT5: Archivo → Abrir carpeta de datos), compílalo (F7 en MetaEditor) y copia `mt5/presets/*.set` a `MQL5\Presets` y `MQL5\Profiles\Tester`. Para operarlo: Algo Trading activado, arrastrar el bot a un gráfico de NAS100 (M5), pestaña Común → "Permitir trading algorítmico" y en Parámetros → Cargar `IndexPropBot_fondeo_100k.set`, cambiando `InpInitialBalance` al tamaño de la cuenta. El estado (saldo inicial, saldo al empezar el día, prueba terminada) se guarda en las variables globales del terminal (F3, prefijo `IPB_`); para empezar una prueba nueva hay que borrarlas.

**Verificación independiente**: `mt5/verificar_en_mt5.bat` (con MT5 cerrado) pasa el bot por el Probador de Estrategias (OHLC de M1 desde 2020: BlackBull solo guarda una semana de ticks reales) y `InpVerifyMode = true` (riesgo fijo, sin reglas de fondeo), y después `python -m backtest.verify_mt5` empareja sus operaciones (`Common\Files\IndexPropBot_trades.csv`) con las del backtest en Python por día y estrategia. El agente local del probador usa el puerto 127.0.0.1:3000; si otro programa lo ocupa (un servidor de desarrollo web, por ejemplo) la prueba falla con "tester agent authorization error". Diferencias conocidas: el RSI(2) del bot entra a las 16:50 NY y el backtest al cierre de las 17:00; si las dos órdenes del ORB saltan en la misma vela el backtest descarta el día y el bot opera la primera.

## Bot MT5 CRT para cuentas fondeadas (CRTPropBot)

`mt5/CRTPropBot.mq5` opera la cartera CRT H4 → M15 elegida con `backtest/crt_funded.py` (configuraciones positivas dentro y fuera de muestra, 2017-22 / 2023-26) en una sola cuenta, con 0,5% de riesgo por operación sobre el saldo inicial:

| Estrategia | Vela C | Sesgo | Entrada M15 | TP |
|---|---|---|---|---|
| NAS100 | todas (ventana 2 velas H4) | vela diaria anterior | límite en FVG | 1R |
| SPX500 | Londres (01:00 NY) | EMA50 H4 | ruptura de estructura (MSS) | 2R + break-even |
| US30 | Londres | vela diaria anterior | MSS | 2R |
| GBPUSD | Londres y NY (09:00 NY) | EMA50 H4 | límite en FVG | 2R + break-even |
| XAUUSD | NY | premium/discount del día | MSS | extremo opuesto de A |

En el backtest de la cartera (2023-26) salen ~111 operaciones/año, ~17 R/año y un DD máximo de 14,6 R (7,3% al 0,5%), sin cuentas perdidas; al 0,75% ya se pierde alguna cuenta. Protecciones del bot (todas desactivadas con `InpVerifyMode`):

- Lote redondeado hacia abajo (nunca más del 0,5%) y limitado por el margen libre; si no llega al lote mínimo no opera (en una cuenta de 5k pasa en ~14% de los setups de XAUUSD).
- Pérdida diaria medida como The5%ers: desde max(saldo, equity) a medianoche del servidor, contando flotante y el riesgo de lo abierto y pendiente. No abre nada que pueda superar el 80% del límite y cierra todo al 90% hasta el día siguiente.
- DD estático: no abre nada que pueda dejar la equity a menos de un 1% del suelo; al tocar suelo + 0,2% cierra todo y se detiene.
- Noticias de alto impacto (calendario de MT5, USD y las divisas del símbolo): sin entradas y sin límites pendientes 5 min antes y después (The5%ers prohíbe ejecutar órdenes 2 min antes y después). Las posiciones abiertas mantienen su SL/TP, y uno de ellos podría saltar dentro de esa ventana.
- Viernes: sin entradas desde las 12:00 NY y cierre total a las 16:30 NY (afecta a ~2,5% de las operaciones del backtest).
- No arranca si el saldo no cuadra con `InpInitialBalance`, si falta un símbolo o (aviso) si el servidor no va a GMT+2/+3 (= NY + 7). En una cuenta real necesita `InpAllowReal = true`.
- Al arrancar borra sus órdenes pendientes antiguas; un setup a medio formar se pierde si se reinicia el terminal.

Presets (`mt5/presets/`): `CRTPropBot_The5ers_5k_fondeada.set` (sin objetivo), `CRTPropBot_The5ers_5k_fase1.set` (objetivo 10%; 8% en el High Stakes clásico), `CRTPropBot_The5ers_5k_fase2.set` (5%), `CRTPropBot_BlackBull_demo_5k.set` (mismas reglas, símbolos de BlackBull) y `CRTPropBot_verificacion.set`. En The5%ers los nombres de los símbolos (`GBPUSD`, `XAUUSD`, índices) pueden ser distintos: el bot avisa si alguno no existe, y se cambian en Parámetros.

Instalación (igual que IndexPropBot): copia `mt5/CRTPropBot.mq5` a `MQL5\Experts` y compílalo, copia `mt5/presets/CRTPropBot_*.set` a `MQL5\Presets` y `MQL5\Profiles\Tester`, arrastra el bot a **un** gráfico cualquiera (p. ej. NAS100 M15): opera los 5 símbolos desde ahí. El estado va en variables globales del terminal (F3, prefijo `CRT_`).

- **Probarlo en BlackBull**: abre una demo nueva de **5.000 USD** (Archivo → Abrir una cuenta), aparte de la de IndexPropBot: los límites diario y de DD de cada bot cuentan toda la equity de la cuenta. Con esa cuenta conectada, `mt5/arrancar_crt_demo.bat` abre MT5 con el bot y `CRTPropBot_BlackBull_demo_5k.set`.
- **Verificación**: `mt5/verificar_crt_en_mt5.bat` (con MT5 cerrado) pasa el bot por el probador desde 2023 y `python -m backtest.verify_crt` empareja sus operaciones con las del backtest. `mt5/tester_crt_fondeo.ini` hace la misma prueba con las reglas de la cuenta.
- **The5%ers**: instala su terminal MT5, copia los mismos archivos, carga el preset de la fase que toque (al pasar de fase, borra las variables `CRT_` y carga el siguiente) y, ya fondeado, `CRTPropBot_The5ers_5k_fondeada.set`. Los retiros se piden a mano; tras un retiro el saldo vuelve al inicial y el bot sigue igual.

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

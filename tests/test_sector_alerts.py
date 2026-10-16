import pandas as pd

from backtest.sector_rotation import SECTOR_ETF
from sector_alerts import SectorAlerts, SectorData, SectorStore, format_report, run_sector_alerts, size


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, subject, body, html_body=None):
        self.sent.append((subject, body, html_body))


WEEKS = 60


def weekly(rows, start="2024-01-01"):
    """rows = [(open, high, low, close), ...] por semana; 5 días hábiles iguales cada una."""
    days = [r for r in rows for _ in range(5)]
    idx = pd.date_range(start, periods=len(days), freq="B", name="date")
    return pd.DataFrame(days, columns=["open", "high", "low", "close"], index=idx, dtype=float)


def trend(growth):
    return weekly([(p, p * 1.001, p * 0.999, p) for p in (100 * growth ** k for k in range(WEEKS))])


# Impulso de dos semanas alcistas con FVG sobre 102, retroceso y barrida de 106 con cierre en 109.
SETUP = [(100, 102, 98, 100), (100, 108, 99, 107), (107, 115, 104, 114), (114, 116, 106, 108), (108, 110, 105, 109)]
STOCK = weekly([(100, 101, 99, 100)] * (WEEKS - len(SETUP)) + SETUP)


def market(spy_growth=1.002):
    # Tecnología sube más que SPY (líder); el resto cae y queda bajo su MM40.
    etfs = {t: trend(1.01 if t == "XLK" else 0.995) for t in SECTOR_ETF.values()}
    return SectorData(spy=trend(spy_growth), etfs=etfs, stocks={"AAA": STOCK, "BBB": STOCK},
                      sectors={"AAA": "Information Technology", "BBB": "Energy"})


def cfg(tmp_path, **kw):
    return SectorAlerts(enabled=True, dry_run=False, capital=10_000, risk_pct=1, state_path=tmp_path / "s.json", **kw)


def now_after(data):
    return pd.Timestamp(data.spy.index[-1] + pd.Timedelta(days=1), tz="UTC")


def test_size_caps_notional():
    out = size(SectorAlerts(capital=10_000, risk_pct=1, max_alloc_pct=20), entry=109, stop=105)
    # 100 de riesgo / 4 = 25 acciones, pero el 20 % del capital solo da para 18.
    assert out["units"] == 18 and out["capped"] and out["risk_money"] == 72


def test_signal_only_in_leading_sector_and_tracked(tmp_path):
    data, c = market(), cfg(tmp_path)
    store = SectorStore(c.state_path).load()
    note = FakeNotifier()
    rep = run_sector_alerts(c, store, note, now_after(data), data)
    assert rep["market_bull"]
    assert [s["sector"] for s in rep["sectors"] if s["leader"]] == ["Information Technology"]
    assert [s["ticker"] for s in rep["signals"]] == ["AAA"]  # BBB es de Energía, no líder
    s = rep["signals"][0]
    assert s["chosen"] and (s["entry"], s["stop_loss"], s["take_profit"]) == (109, 105, 121)
    subject, body, page = note.sent[0]
    assert subject.startswith("[Sectores] COMPRA AAA") and "Take profit 3R: 121,00" in body
    assert "<html>" in page and "Líder" in page

    # Segunda pasada con los mismos datos: no repite la señal y sigue la operación.
    store = SectorStore(c.state_path).load()
    rep = run_sector_alerts(c, store, FakeNotifier(), now_after(data), data)
    assert rep["signals"] == [] and [p["status"] for p in rep["positions"]] == ["open"]


def test_no_signal_when_market_not_bull_or_no_free_slot(tmp_path):
    data = market(spy_growth=0.99)  # SPY bajo su MM40
    rep = run_sector_alerts(cfg(tmp_path), SectorStore(tmp_path / "a.json"), FakeNotifier(), now_after(data), data)
    assert not rep["market_bull"] and rep["signals"] == []
    data = market()
    rep = run_sector_alerts(cfg(tmp_path, max_positions=0), SectorStore(tmp_path / "b.json"), FakeNotifier(), now_after(data), data)
    assert [s["chosen"] for s in rep["signals"]] == [False]
    assert "sin hueco libre" in format_report(rep)[1]


def test_previous_week_signal_is_only_informative(tmp_path):
    # Una semana más sin barrida tras la señal: ya no es la última semana cerrada.
    data = market()
    data.stocks = {"AAA": weekly([(100, 101, 99, 100)] * (WEEKS - len(SETUP) - 1) + SETUP + [(109, 111, 108, 110)])}
    store = SectorStore(tmp_path / "s.json")
    store.alerted = ["OTRO:2020-01-03"]  # no es la primera pasada
    rep = run_sector_alerts(cfg(tmp_path), store, FakeNotifier(), now_after(data), data)
    s = rep["signals"][0]
    assert s["stale"] and not s["chosen"] and s["last"] == 110
    assert "la entrada ya pasó" in format_report(rep)[1]
    # En la primera pasada esa señal antigua se da por avisada sin mostrarla.
    rep = run_sector_alerts(cfg(tmp_path), SectorStore(tmp_path / "t.json"), FakeNotifier(), now_after(data), data)
    assert rep["signals"] == []

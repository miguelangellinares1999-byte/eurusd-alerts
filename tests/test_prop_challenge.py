import pandas as pd

from backtest.prop_challenge import Rules, Sizing, events, prepare, run_challenge, simulate


def trades(rs, strategy="X", tp_r=2.0, start="2024-01-02", per_day=1):
    """Una operación por hora de 10:00 a 16:00 NY, `per_day` al día, en días laborables."""
    rows, days = [], pd.bdate_range(start, periods=len(rs) // per_day + 2)
    for k, r in enumerate(rs):
        day = days[k // per_day]
        t = pd.Timestamp(f"{day.date()} 10:00", tz="America/New_York") + pd.Timedelta(minutes=10 * (k % per_day))
        rows.append({"fill_time": t.tz_convert("UTC"), "exit_time": (t + pd.Timedelta(hours=1)).tz_convert("UTC"),
                     "r": r, "tp_r": tp_r, "estrategia": strategy})
    return pd.DataFrame(rows)


def first_result(t, rules, sizing):
    """Resultado de la prueba que empieza el primer día."""
    t = prepare(t)
    days = pd.bdate_range(t.fill_day.min(), t.exit_day.max())
    ev = events(t, days)
    base = [sizing.risk_for(s) for s in t.estrategia]
    return run_challenge(ev, [e[0] for e in ev], t.r.tolist(), t.tp_r.tolist(), base, 0, rules, sizing)


def test_passes_after_enough_wins():
    # 1% de riesgo y +2R por operación: +10% al 5º acierto
    assert first_result(trades([2.0] * 10), Rules(), Sizing(0.01)) == ("aprobada", 5)


def test_fails_max_drawdown():
    assert first_result(trades([-1.0] * 20), Rules(), Sizing(0.01)) == ("DD máximo", 10)


def test_daily_limit_breach_and_guard():
    t = trades([-1.0] * 6, per_day=3)  # 3 pérdidas de 2% el mismo día = -6%
    assert first_result(t, Rules(daily=0.05), Sizing(0.02))[0] == "límite diario"
    # con la guarda (80% de 5% = 4%) solo se abren 2 operaciones ese día y no se rompe la regla
    res, _ = first_result(t, Rules(daily=0.05), Sizing(0.02, daily_guard=0.8))
    assert res != "límite diario"


def test_risk_per_strategy():
    t = pd.concat([trades([2.0], "A"), trades([2.0], "B", start="2024-01-03")], ignore_index=True)
    sz = Sizing(0.01, by_strategy=(("A", 0.01), ("B", 0.04)))
    # A: +2%, B: +8% -> +10%
    assert first_result(t, Rules(), sz) == ("aprobada", 2)


def test_to_target_does_not_stall_below_goal():
    # aciertos de 1.9R con TP teórico 2R: sin margen la cuenta se acercaría al objetivo sin llegar
    res = first_result(trades([1.9] * 200), Rules(), Sizing(0.02, to_target=True))
    assert res[0] == "aprobada"


def test_unlimited_time_reports_unresolved():
    t = trades([0.0] * 300)  # nunca gana ni pierde
    out = simulate(t, Rules(min_horizon=10), Sizing(0.01))
    assert out["sin resolver %"] == 100.0 and out["aprobadas %"] == 0.0

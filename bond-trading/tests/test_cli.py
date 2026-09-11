import os

import pytest

from bondtrader.cli import main

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(f"""
execution:
  broker: paper
  state_path: {tmp_path / 'pf.json'}
  journal_path: {tmp_path / 'orders.jsonl'}
backtest:
  initial_cash: 2000000
risk:
  max_weight_per_bond: 0.3
""", encoding="utf-8")
    return str(p)


def run(capsys, *argv):
    assert main(list(argv)) == 0
    return capsys.readouterr().out


def test_screen_and_info_commands(capsys, cfg):
    out = run(capsys, "--fixtures", FIX, "-c", cfg, "screen", "--top", "5")
    assert "SU26" in out and "g_spread" in out
    out = run(capsys, "screen", "--fixtures", FIX, "-c", cfg, "--ofz-only", "-v")
    assert "RU000A" not in out
    out = run(capsys, "--fixtures", FIX, "-c", cfg, "curve")
    assert "Наклон" in out
    out = run(capsys, "--fixtures", FIX, "-c", cfg, "keyrate")
    assert "Ключевая ставка: 21.00%" in out
    out = run(capsys, "--fixtures", FIX, "-c", cfg, "bond", "SU26238RMFS4")
    assert "ОФЗ 26238" in out and "DV01" in out
    out = run(capsys, "strategies")
    assert "ladder" in out and "rate_cycle" in out


def test_signals_trade_portfolio_cycle(capsys, cfg, tmp_path):
    out = run(capsys, "--fixtures", FIX, "-c", cfg, "signals", "-s", "ladder", "-p", "per_bucket=1", "-p", "edges=[1,2,3]")
    assert "Ордера относительно текущего портфеля" in out and "BUY" in out
    out = run(capsys, "--fixtures", FIX, "-c", cfg, "trade", "-s", "ladder", "-p", "per_bucket=1")
    assert "DRY-RUN" in out
    assert not (tmp_path / "pf.json").exists()
    out = run(capsys, "--fixtures", FIX, "-c", cfg, "trade", "-s", "ladder", "-p", "per_bucket=1", "--confirm")
    assert "filled" in out and (tmp_path / "pf.json").exists()
    out = run(capsys, "--fixtures", FIX, "-c", cfg, "portfolio")
    assert "NAV" in out and "DV01" in out and "Экспозиция по эмитентам" in out
    # повторный запуск — ребалансировка не нужна
    out = run(capsys, "--fixtures", FIX, "-c", cfg, "trade", "-s", "ladder", "-p", "per_bucket=1")
    assert "Ребалансировка не требуется" in out
    # смена стратегии -> продажи идут первыми
    out = run(capsys, "--fixtures", FIX, "-c", cfg, "signals", "-s", "rate_cycle")
    assert "SELL" in out


def test_bad_param(capsys, cfg):
    with pytest.raises(SystemExit):
        main(["--fixtures", FIX, "-c", cfg, "signals", "-p", "oops"])


def test_config_params_apply_only_to_same_strategy(capsys, cfg, tmp_path):
    p = tmp_path / "carry.yaml"
    p.write_text("strategy:\n  name: carry\n  params:\n    top_n: 3\n", encoding="utf-8")
    out = run(capsys, "--fixtures", FIX, "-c", str(p), "signals", "-s", "ladder")
    assert "Стратегия: ladder" in out
    from bondtrader.strategies import make_strategy
    st = make_strategy("ladder", {"top_n": 3, "per_bucket": 2})
    assert st.per_bucket == 2


def test_ratings_commands(capsys, cfg, tmp_path):
    csv = tmp_path / "ratings.csv"
    csv.write_text("subject,agency,rating,date,kind,isin,emitter_id,alias\nСбер,АКРА,AAA(RU),2025-01-01,issuer,,,Сбер\n", encoding="utf-8")
    c = tmp_path / "c.yaml"
    c.write_text(f"data:\n  ratings_csv: {csv}\n", encoding="utf-8")
    out = run(capsys, "--fixtures", FIX, "-c", str(c), "ratings", "list")
    assert "1 записей" in out and "Сбер" in out
    out = run(capsys, "--fixtures", FIX, "-c", str(c), "ratings", "show", "RU000A106K43")
    assert "AAA (АКРА" in out
    out = run(capsys, "--fixtures", FIX, "-c", str(c), "ratings", "coverage")
    assert "без рейтинга" in out and "RU000A107RZ0" in out
    out = run(capsys, "--fixtures", FIX, "-c", str(c), "screen", "--min-rating", "AA")
    assert "RU000A106K43" in out and "RU000A107RZ0" in out  # без рейтинга не отсекаются
    assert "Рейтинги: 1 записей" in out


def test_why_command(capsys):
    from bondtrader.cli import main
    assert main(["--fixtures", FIX, "why", "ВИС"]) == 0
    out = capsys.readouterr().out
    assert "ВИС Ф БП04" in out and "status" in out and "найдено" in out
    try:
        main(["--fixtures", FIX, "why", "НЕТТАКОГО"])
    except SystemExit as e:
        assert "ничего не найдено" in str(e)
    else:
        raise AssertionError("ожидался SystemExit")

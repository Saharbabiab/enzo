import os
import sys
import time
import types

import pytest


@pytest.fixture
def tb(monkeypatch):
    # telegram_bot imports the whole pipeline; stub only if the import fails.
    try:
        import telegram_bot
    except Exception:
        pytest.skip("telegram_bot not importable here")
    return telegram_bot


def test_beat_writes_fresh_file(tb, tmp_path):
    p = str(tmp_path / "sub" / "telegram_alive")
    tb.beat(p)
    assert os.path.exists(p)
    assert time.time() - os.path.getmtime(p) < 5


def test_compose_has_telegram_healthcheck():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(root, "docker-compose.yml"), encoding="utf-8").read()
    tg = text.split("enzo-telegram:")[1]
    assert "healthcheck:" in tg and "telegram_alive" in tg

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ytauto import retry  # noqa: E402
from ytauto.config import load_config  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(retry, "SLEEP", lambda s: sleeps.append(s))
    return sleeps


@pytest.fixture
def cfg(tmp_path):
    """Config asli proyek, tetapi semua path relatif diarahkan ke tmp_path."""
    c = load_config(ROOT / "config.yaml")
    c.base_dir = tmp_path
    (tmp_path / "assets" / "music").mkdir(parents=True)
    (tmp_path / "assets" / "fonts").mkdir(parents=True)
    (tmp_path / "history.json").write_text('{"version": 1, "videos": []}', encoding="utf-8")
    return c


def fixture_json(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeLLM:
    """LLM palsu: mengembalikan jawaban dari antrean, atau dari fungsi(system, prompt)."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def complete_json(self, system, prompt, temperature=None):
        self.calls.append({"system": system, "prompt": prompt})
        if not self.responses:
            raise AssertionError("FakeLLM kehabisan jawaban")
        r = self.responses.pop(0)
        return r(system, prompt) if callable(r) else r


@pytest.fixture
def fake_llm():
    return FakeLLM

"""The scheduled strategy review records outcomes without Telegram messages."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def review(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/strategy_review.py"
    spec = importlib.util.spec_from_file_location("strategy_review", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "DIRECTORY", tmp_path / "reviews")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Read-only review")
    monkeypatch.setattr(module, "PROMPT", prompt)

    messages = []

    class Scheduler:
        def __init__(self, root):
            self.root = root

        def notify(self, message):
            messages.append(message)

    monkeypatch.setitem(sys.modules, "paper_schedule", SimpleNamespace(Scheduler=Scheduler))
    return module, messages


def test_attention_review_saves_report_without_telegram(review, monkeypatch):
    module, messages = review
    report = {"status": "attention", "summary": "Needs inspection", "findings": []}

    def fake_run(args, **kwargs):
        output = Path(args[args.index("--output-last-message") + 1])
        output.write_text(json.dumps(report))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert module.main() == 0
    assert json.loads((module.DIRECTORY / "server-review-latest.json").read_text()) == report
    assert messages == []


def test_failed_review_records_failure_without_telegram(review, monkeypatch):
    module, messages = review

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="codex", timeout=1500)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert module.main() == 1
    assert len(list(module.DIRECTORY.glob("*/failure.json"))) == 1
    assert messages == []

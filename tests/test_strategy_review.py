"""The scheduled strategy review records outcomes without Telegram messages."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_review():
    path = Path(__file__).resolve().parents[1] / "scripts/strategy_review.py"
    spec = importlib.util.spec_from_file_location("strategy_review", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def review(tmp_path, monkeypatch):
    module = load_review()

    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "DIRECTORY", tmp_path / "reviews")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Read-only review")
    monkeypatch.setattr(module, "PROMPT", prompt)
    monkeypatch.setattr(module, "service_evidence", lambda: {"available": False, "units": {}})

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
        assert "包装器只读采集" in kwargs["input"]
        output = Path(args[args.index("--output-last-message") + 1])
        output.write_text(json.dumps(report))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert module.main() == 0
    assert json.loads((module.DIRECTORY / "server-review-latest.json").read_text()) == report
    assert messages == []


def test_service_evidence_excludes_environment_and_command_arguments(monkeypatch):
    module = load_review()
    monkeypatch.setattr(module.sys, "platform", "linux")
    output = "\n\n".join(
        f"Id={unit}\nActiveState=active\nSubState=waiting\nEnvironment=private-test-value\nExecStart=private-test-command"
        for unit in module.SERVICE_UNITS
    )

    def fake_run(args, **kwargs):
        assert kwargs["timeout"] == 5
        assert "Environment" not in args[-1]
        assert "ExecStart=" not in args[-1]
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    evidence = module.service_evidence()
    assert evidence["available"] is True
    assert set(evidence["units"]) == set(module.SERVICE_UNITS)
    assert "private-test" not in json.dumps(evidence)


def test_service_evidence_unavailable_is_explicit_and_does_not_prevent_review(monkeypatch):
    module = load_review()
    monkeypatch.setattr(module.sys, "platform", "linux")

    def unavailable(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="systemctl", timeout=5)

    monkeypatch.setattr(module.subprocess, "run", unavailable)
    assert module.service_evidence()["available"] is False


def test_failed_review_records_failure_without_telegram(review, monkeypatch):
    module, messages = review

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="codex", timeout=1500)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert module.main() == 1
    assert len(list(module.DIRECTORY.glob("*/failure.json"))) == 1
    assert messages == []

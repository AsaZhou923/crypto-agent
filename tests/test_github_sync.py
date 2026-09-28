import importlib.util
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def sync_module(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/github_sync.py"
    spec = importlib.util.spec_from_file_location("github_sync", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "repo"
    root.mkdir()
    (root / "src").mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module, "PATHS", ("src",))
    return module, root


def test_staged_source_rejects_private_env_value(sync_module):
    module, root = sync_module
    secret = "private-paper-credential-12345"
    (root / ".env").write_text("ALPACA_SECRET_KEY=" + secret + "\n")
    (root / "src/config.py").write_text("KEY = " + repr(secret) + "\n")
    subprocess.run(["git", "-C", str(root), "add", "src/config.py"], check=True)
    with pytest.raises(module.SyncError, match="credential"):
        module.validate_staged()


def test_staged_safe_source_and_binary_rejection(sync_module):
    module, root = sync_module
    source = root / "src/config.py"
    source.write_text("LIMIT = 500\n")
    subprocess.run(["git", "-C", str(root), "add", "src/config.py"], check=True)
    assert module.validate_staged() == ["src/config.py"]
    source.write_bytes(b"secret\x00binary")
    subprocess.run(["git", "-C", str(root), "add", "src/config.py"], check=True)
    with pytest.raises(module.SyncError, match="Binary"):
        module.validate_staged()

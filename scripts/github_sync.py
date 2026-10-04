"""Publish reviewed code and validated Paper config snapshots to the fixed GitHub repo."""

import fcntl
import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values

from crypto_agent.config import load_settings

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "runtime/github-sync"
REMOTE = "git@github.com:AsaZhou923/crypto-agent.git"
CONFIG = ROOT / "runtime/paper-session"
SNAPSHOT = ROOT / "config/deployment/paper-session"
CONFIG_FILES = ("paper.yaml", "strategy.yaml", "risk.yaml")
PATHS = (
    ".gitignore",
    ".env.example",
    "README.md",
    "pyproject.toml",
    "uv.lock",
    "config",
    "deploy",
    "docs",
    "frontend",
    "research",
    "scripts",
    "src",
    "tests",
)
SUSPECT = re.compile(
    rb"(?i)(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)


class SyncError(Exception):
    pass


def git(*args, timeout=60, check=True):
    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes"
    result = subprocess.run(
        ["git", *args], cwd=ROOT, env=env, capture_output=True, timeout=timeout, check=False
    )
    if check and result.returncode:
        raise SyncError("Git command failed: " + args[0])
    return result


def paths(blob: bytes) -> list[str]:
    return [p.decode("utf-8") for p in blob.split(b"\0") if p]


def private_values() -> list[bytes]:
    values = []
    for path in (ROOT / ".env", ROOT / "runtime/local-scheduler/notification.env"):
        if not path.exists():
            continue
        for key, value in dotenv_values(path).items():
            if (
                value
                and len(value) >= 8
                and any(part in key.upper() for part in ("KEY", "TOKEN", "SECRET", "PASSWORD", "WEBHOOK"))
            ):
                values.append(value.encode())
    return values


def validate_staged() -> list[str]:
    changed = paths(git("diff", "--cached", "--name-only", "-z").stdout)
    secrets = private_values()
    for path in changed:
        if not any(path == allowed or path.startswith(allowed + "/") for allowed in PATHS):
            raise SyncError("Unexpected staged path")
        content = git("show", ":" + path, check=False)
        if content.returncode:  # Deleted files have no stage-zero blob.
            continue
        data = content.stdout
        if len(data) > 1_000_000 or b"\0" in data:
            raise SyncError("Binary or oversized staged content")
        if SUSPECT.search(data) or any(value in data for value in secrets):
            raise SyncError("Potential credential in staged content")
    git("diff", "--cached", "--check")
    return changed


def notify(message: str):
    from paper_schedule import Scheduler

    Scheduler(ROOT).notify(message)


def save_status(status: str, **details):
    DIRECTORY.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {"at": datetime.now(UTC).isoformat(), "status": status, **details}
    temporary = DIRECTORY / "latest.json.tmp"
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(DIRECTORY / "latest.json")


def sync():
    if git("branch", "--show-current").stdout.strip() != b"main":
        raise SyncError("Expected main branch")
    if git("remote", "get-url", "origin").stdout.strip().decode() != REMOTE:
        raise SyncError("Unexpected GitHub remote")
    # SSH authorizes the push; Git commit headers come from this repository's
    # configured name and email. Keep automation aligned with the original
    # author identity that GitHub already associates with the owner account.
    original_author = git("log", "--max-parents=0", "--format=%an%x00%ae").stdout.strip().split(b"\0")
    configured_author = [
        git("config", "--get", "user.name").stdout.strip(),
        git("config", "--get", "user.email").stdout.strip(),
    ]
    if configured_author != original_author:
        raise SyncError("Git commit identity differs from the repository's original author")
    if git("diff", "--cached", "--name-only").stdout.strip():
        raise SyncError("Existing staged changes require manual review")
    git("fetch", "origin", "main", timeout=120)
    if git("merge-base", "--is-ancestor", "origin/main", "HEAD", check=False).returncode:
        raise SyncError("Remote main has new commits; manual integration required")

    # These three validated YAML files are private runtime inputs; only their
    # credential-free configuration is mirrored into the tracked snapshot.
    load_settings(CONFIG, "paper", root=ROOT)
    SNAPSHOT.mkdir(parents=True, exist_ok=True)
    for name in CONFIG_FILES:
        source = CONFIG / name
        target = SNAPSHOT / name
        data = source.read_bytes()
        if len(data) > 100_000 or b"\0" in data:
            raise SyncError("Invalid Paper config snapshot")
        if SUSPECT.search(data) or any(value in data for value in private_values()):
            raise SyncError("Potential credential in Paper config")
        if not target.exists() or target.read_bytes() != data:
            target.write_bytes(data)

    git("add", "-A", "--", *PATHS)
    changed = validate_staged()
    if changed:
        git("commit", "-m", "chore: sync server strategy and code")
    ahead = git("rev-list", "--count", "origin/main..HEAD").stdout.strip()
    if ahead != b"0":
        git("push", "origin", "HEAD:refs/heads/main", timeout=120)
        sha = git("rev-parse", "--short", "HEAD").stdout.strip().decode()
        save_status("pushed", commit=sha, changed_paths=changed)
        notify("Crypto Agent 服务器代码/策略已同步到 GitHub，提交 " + sha + "。")
    else:
        save_status("unchanged")


def main() -> int:
    os.umask(0o077)
    DIRECTORY.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (
        (DIRECTORY / "sync.lock").open("a") as lock,
        (ROOT / "runtime/deployment.lock").open("a") as deployment,
    ):
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(deployment, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        try:
            sync()
            (DIRECTORY / "last-failure.json").unlink(missing_ok=True)
            return 0
        except (OSError, ValueError, SyncError, subprocess.TimeoutExpired) as error:
            category = (
                type(error).__name__ + ": " + (str(error) if isinstance(error, SyncError) else "sync failed")
            )
            digest = hashlib.sha256(category.encode()).hexdigest()
            previous = DIRECTORY / "last-failure.json"
            last = json.loads(previous.read_text()) if previous.exists() else {}
            save_status("failed", reason=category)
            previous.write_text(json.dumps({"digest": digest}))
            if last.get("digest") != digest:
                notify("Crypto Agent GitHub 自动同步失败：" + category[:200])
            return 1


if __name__ == "__main__":
    raise SystemExit(main())

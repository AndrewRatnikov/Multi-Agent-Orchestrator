"""Shared fixtures for the verdict-script tests.

Every test runs against a throwaway orchestrator tree and throwaway git repos under
tmp_path. The scripts locate runs/ relative to their own file (ORCH_ROOT = two levels
up), so `orch` copies them into tmp_path/orch/.claude/scripts/ and runs the copies:
the real runs/ folder is never read or written.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
SCRIPTS = PROJECT / ".claude" / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """No user/global git config, no real HOME (verify.py reads ~/.config/...neon.env),
    no database URLs, and a private TMPDIR for verify.py's worktrees."""
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "tmp").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "Test")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "test@example.invalid")
    for k in list(os.environ):
        if k.startswith(("VERIFY_DATABASE_URL", "DATABASE_URL", "NEON_", "CLAUDE_PROJECT_DIR")):
            monkeypatch.delenv(k, raising=False)


# ── git repos ────────────────────────────────────────────────────────────────

def git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo)] + list(args), capture_output=True, text=True)
    assert r.returncode == 0, "git {} failed: {}".format(" ".join(args), r.stderr)
    return r.stdout.strip()


def write_files(root, files):
    for rel, content in files.items():
        p = Path(root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            if p.exists():
                p.unlink()
        else:
            p.write_text(content)


def commit(repo, files, message="change"):
    write_files(repo, files)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def make_repo(tmp_path):
    """make_repo(files, name="repo") -> Path of a git repo on `main` with one commit."""
    def make(files, name="repo"):
        repo = tmp_path / name
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        commit(repo, files, "initial")
        return repo
    return make


# ── orchestrator tree ────────────────────────────────────────────────────────

class Orch:
    def __init__(self, root):
        self.root = root
        self.scripts = root / ".claude" / "scripts"

    def run_dir(self, run_id="run_test"):
        d = self.root / "runs" / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def run(self, script, *args, input=None, env=None, timeout=60):
        path = self.scripts / script
        cmd = ["bash", str(path)] if script.endswith(".sh") else [sys.executable, str(path)]
        full_env = dict(os.environ)
        full_env.update(env or {})
        return subprocess.run(cmd + [str(a) for a in args], input=input, capture_output=True,
                              text=True, env=full_env, timeout=timeout, cwd=str(self.root))


@pytest.fixture
def orch(tmp_path):
    root = tmp_path / "orch"
    dest = root / ".claude" / "scripts"
    dest.mkdir(parents=True)
    for f in SCRIPTS.iterdir():
        if f.is_file() and f.suffix in (".py", ".sh"):
            shutil.copy2(f, dest / f.name)
    (root / "runs").mkdir()
    return Orch(root)


def load_script(name):
    """Import a hyphenated script (e.g. verify.py, check-contract.py) as a module."""
    path = SCRIPTS / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_").rsplit(".", 1)[0], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_fixture_json(name):
    return json.loads((FIXTURES / name).read_text())

"""size-run.py: small vs standard, from contract.json and the repo's sizing config."""
import json

from conftest import commit, git, load_fixture_json

RUN = "run_sz"


def make_run(orch, make_repo, contract=None, state="original_branch: main\n", repo_files=None):
    repo = make_repo(repo_files or {"README.md": "x\n"})
    run_dir = orch.run_dir(RUN)
    (run_dir / "contract.json").write_text(json.dumps(contract or load_fixture_json("contract.json")))
    (run_dir / "state.md").write_text(state)
    return run_dir, repo


def test_small_run(orch, make_repo):
    _, repo = make_run(orch, make_repo)
    r = orch.run("size-run.py", RUN, repo)
    assert r.returncode == 0
    assert r.stdout.splitlines() == ["size: small", "- 3 code file(s); no .claude/verify.json on main; built-in defaults"]


def test_too_many_files_and_sensitive_path(orch, make_repo):
    c = load_fixture_json("contract.json")
    c["files"].append({"path": "apps/api/src/auth/session.ts", "action": "modify", "owner": "coder"})
    _, repo = make_run(orch, make_repo, contract=c)
    r = orch.run("size-run.py", RUN, repo)
    assert r.returncode == 1
    lines = r.stdout.splitlines()
    assert lines[0] == "size: standard"
    assert "- 4 code files (small allows 3)" in lines
    assert "- sensitive path apps/api/src/auth/session.ts (matches `**/auth/**`)" in lines


def test_repo_sizing_read_from_original_branch_only(orch, make_repo):
    cfg = {"sizing": {"sensitive_paths": ["apps/web/src/components/layout/**"]}}
    _, repo = make_run(orch, make_repo, repo_files={".claude/verify.json": json.dumps(cfg)})
    # The run's own branch loosens the config; it must not count.
    git(repo, "checkout", "-q", "-b", "orch/x")
    commit(repo, {".claude/verify.json": json.dumps({"sizing": {"sensitive_paths": []}})})
    r = orch.run("size-run.py", RUN, repo)
    assert r.returncode == 1
    assert "- sensitive path apps/web/src/components/layout/site-nav.tsx (matches " \
           "`apps/web/src/components/layout/**`)" in r.stdout.splitlines()


def test_size_override_wins(orch, make_repo):
    c = load_fixture_json("contract.json")
    c["packages"]["added"] = ["zod"]
    _, repo = make_run(orch, make_repo, contract=c, state="original_branch: main\nsize_override: small\n")
    r = orch.run("size-run.py", RUN, repo)
    assert r.returncode == 0
    assert r.stdout.splitlines()[0] == "size: small"
    assert "- overridden by --size small (rules said standard)" in r.stdout


def test_missing_state_is_usage_error(orch, make_repo):
    run_dir, repo = make_run(orch, make_repo)
    (run_dir / "state.md").unlink()
    assert orch.run("size-run.py", RUN, repo).returncode == 3

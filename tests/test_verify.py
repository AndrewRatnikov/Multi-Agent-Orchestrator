"""verify.py: path normalisation, the error-line baseline diff, and end-to-end verdicts.

End-to-end tests run a copy of verify.py against a throwaway repo whose .claude/verify.json
steps are small shell commands. A stub `docker` that always fails is first on PATH, so no
test can ever start a container.
"""
import json
import os
import re
import stat

import pytest

from conftest import commit, git, load_script

verify = load_script("verify.py")


# ── _norm_path (macOS /var vs /private/var) ──────────────────────────────────

def test_norm_path_private_prefix_from_realpath():
    wt = "/var/folders/ab/xyz/T/orchestrator-verify-run-1"
    line = "/private/var/folders/ab/xyz/T/orchestrator-verify-run-1/src/a.ts"
    assert verify._norm_path(line, wt) == "<WT>/src/a.ts"
    assert verify._norm_path(line.replace("/private", ""), wt) == "<WT>/src/a.ts"


def test_norm_path_symlinked_worktree_without_orchestrator_name(tmp_path):
    real = tmp_path / "real-wt"
    real.mkdir()
    link = tmp_path / "link-wt"
    link.symlink_to(real)
    # Tool printed the realpath; verify.py only knows the symlinked path.
    assert verify._norm_path("{}/src/a.ts: error".format(real), str(link)) == "<WT>/src/a.ts: error"
    assert verify._norm_path("{}/src/a.ts: error".format(link), str(link)) == "<WT>/src/a.ts: error"


# ── new_error_lines: same failure vs new errors ──────────────────────────────

ESLINT_STYLISH = """\
/private/var/folders/ab/T/orchestrator-verify-{wt}/src/a.ts
  {line}:1  error  'x' is assigned a value but never used  no-unused-vars

✖ 1 problem (1 error, 0 warnings)
"""
PNPM_RECURSIVE = """\
apps/web lint$ eslint .
apps/web lint: /private/var/folders/ab/T/orchestrator-verify-{wt}/apps/web/src/a.ts
apps/web lint:   {line}:1  error  'x' is assigned a value but never used  no-unused-vars
apps/web lint: ✖ 1 problem (1 error, 0 warnings)
apps/web lint: Failed
"""
ESLINT_COMPACT = "/private/var/folders/ab/T/orchestrator-verify-{wt}/src/a.ts: line {line}, col 1, Error - " \
                 "'x' is assigned a value but never used (no-unused-vars)\n\n1 problem\n"
TSC = "src/a.ts({line},5): error TS2304: Cannot find name 'x'.\n"


def with_extra_error(fmt, wt):
    """The same output with one more error ('y', at line 20) right after the 'x' one."""
    out = []
    for raw in fmt.splitlines(keepends=True):
        out.append(raw.format(wt=wt, line=7))
        if "'x'" in raw:
            out.append(raw.format(wt=wt, line=20).replace("'x'", "'y'"))
    return "".join(out)


@pytest.mark.parametrize("fmt", [ESLINT_STYLISH, TSC, PNPM_RECURSIVE, ESLINT_COMPACT],
                         ids=["eslint-stylish", "tsc", "pnpm-recursive", "eslint-compact"])
def test_moved_preexisting_error_is_not_new(fmt):
    """The run added lines above an error that already existed on the original branch."""
    base = fmt.format(wt="run-1-baseline", line=6)
    branch = fmt.format(wt="run-1", line=7)
    assert verify.new_error_lines(branch, base, "/var/folders/ab/T/orchestrator-verify-run-1-baseline") == []


@pytest.mark.parametrize("fmt", [ESLINT_STYLISH, TSC, PNPM_RECURSIVE, ESLINT_COMPACT],
                         ids=["eslint-stylish", "tsc", "pnpm-recursive", "eslint-compact"])
def test_genuinely_new_error_is_reported(fmt):
    base = fmt.format(wt="run-1-baseline", line=6)
    branch = with_extra_error(fmt, "run-1")
    new =verify.new_error_lines(branch, base, "/var/folders/ab/T/orchestrator-verify-run-1-baseline")
    assert len(new) == 1 and "'y'" in new[0] and "'x'" not in new[0]


def test_same_message_in_a_second_file_is_new():
    base = ESLINT_STYLISH.format(wt="r-baseline", line=6)
    branch = base.replace("r-baseline", "r") + ESLINT_STYLISH.format(wt="r", line=6).replace("src/a.ts", "src/b.ts")
    new = verify.new_error_lines(branch, base, "")
    assert len(new) == 1 and new[0].startswith("<WT>/src/b.ts :: error")
    assert verify.files_in(new) == ["src/b.ts"]


# ── end-to-end ───────────────────────────────────────────────────────────────

RUN = "run_v"
# Prints lint.out with WT replaced by the worktree's realpath (like ESLint does), fails if non-empty.
LINT = 'sed "s#^WT#$(pwd -P)#" lint.out; test ! -s lint.out'
TEST = 'test -f ok.txt || { echo "FAIL src/b.test.ts > totals > expected 2 got 3"; exit 1; }'
OLD_ERROR = "WT/src/a.ts\n  {}:1  error  'x' is assigned a value but never used  no-unused-vars\n"
NEW_ERROR = "WT/src/b.ts\n  2:1  error  'y' is assigned a value but never used  no-unused-vars\n"


@pytest.fixture
def no_docker(tmp_path):
    """PATH entry with a `docker` that is installed but never running."""
    d = tmp_path / "stub-bin"
    d.mkdir()
    docker = d / "docker"
    docker.write_text("#!/bin/sh\necho 'Cannot connect to the Docker daemon' >&2\nexit 1\n")
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    return {"PATH": "{}:{}".format(d, os.environ["PATH"])}


@pytest.fixture
def verify_run(orch, make_repo, no_docker):
    """verify_run(main_files, branch_files, steps, plan=None, env=None) -> (exit, summary, run_dir)."""
    def go(main_files, branch_files, steps, plan=None, env=None):
        config = {"steps": steps}
        repo = make_repo(dict(main_files, **{".claude/verify.json": json.dumps(config)}))
        git(repo, "checkout", "-q", "-b", "orch/test")
        commit(repo, branch_files, "run change")
        git(repo, "checkout", "-q", "main")
        run_dir = orch.run_dir(RUN)
        (run_dir / "state.md").write_text("target_branch: orch/test\noriginal_branch: main\n")
        if plan:
            (run_dir / "plan.md").write_text(plan)
        r = orch.run("verify.py", repo, RUN, env=dict(no_docker, **(env or {})))
        md = (run_dir / "verify.md").read_text()
        summary = json.loads(re.search(r"```json\n(.*?)\n```", md, re.S).group(1))
        assert summary["exit_code"] == r.returncode, r.stdout + r.stderr
        return r.returncode, summary, run_dir
    return go


def test_e2e_pass(verify_run):
    code, s, _ = verify_run({"lint.out": "", "ok.txt": ""}, {"src/new.ts": "export {}\n"},
                            [{"name": "lint", "run": LINT}, {"name": "test", "run": TEST}])
    assert code == 0 and s["verdict"] == "PASS"
    assert s["failed"] == s["pre_existing"] == s["not_verified"] == []


def test_e2e_preexisting_failure_moved_down(verify_run, tmp_path):
    """Lint fails on main too; the run only pushed the error down a line. Worktrees live
    under a symlinked TMPDIR and the step prints realpaths (the macOS /private/var case)."""
    real = tmp_path / "tmp-real"
    real.mkdir()
    (tmp_path / "tmp-link").symlink_to(real)
    code, s, run_dir = verify_run(
        {"lint.out": OLD_ERROR.format(3), "ok.txt": ""}, {"lint.out": OLD_ERROR.format(4)},
        [{"name": "lint", "run": LINT}, {"name": "test", "run": TEST}],
        env={"TMPDIR": str(tmp_path / "tmp-link")})
    assert (code, s["verdict"]) == (0, "PASS")
    assert s["pre_existing"] == ["lint"] and s["failed"] == []
    assert "| 1 | lint | PRE-EXISTING |" in (run_dir / "verify.md").read_text()


def test_e2e_new_failure(verify_run):
    code, s, run_dir = verify_run({"lint.out": "", "ok.txt": ""}, {"ok.txt": None},
                                  [{"name": "lint", "run": LINT}, {"name": "test", "run": TEST}])
    assert (code, s["verdict"]) == (1, "FAIL")
    assert s["failed"] == ["test"] and s["pre_existing"] == []
    assert s["files_with_new_errors"] == ["src/b.test.ts"]
    assert (run_dir / "verify-logs" / "baseline-01-lint.log").exists()  # steps up to the failure are re-run


def test_e2e_new_errors_on_top_of_preexisting(verify_run):
    code, s, run_dir = verify_run(
        {"lint.out": OLD_ERROR.format(3), "ok.txt": ""},
        {"lint.out": OLD_ERROR.format(5) + NEW_ERROR},
        [{"name": "lint", "run": LINT}])
    assert (code, s["verdict"]) == (1, "FAIL")
    assert s["failed"] == ["lint"]
    assert s["files_with_new_errors"] == ["src/b.ts"]          # not src/a.ts: that one only moved
    assert "adds 1 new error line(s)" in (run_dir / "verify.md").read_text()


def test_e2e_plan_steps_are_not_baselined(verify_run):
    """A plan-authored step that fails on main too is still this run's failure; a plan line
    identical to a standard step is not run twice."""
    plan = "# Plan\n\n```verify\n# scope check\ntest -f never.txt\n{}\n```\n".format(LINT)
    code, s, run_dir = verify_run({"lint.out": "", "ok.txt": ""}, {"src/new.ts": "export {}\n"},
                                  [{"name": "lint", "run": LINT}], plan=plan)
    assert (code, s["verdict"]) == (1, "FAIL")
    assert s["failed"] == ["plan: test -f never.txt"] and s["pre_existing"] == []
    logs = sorted(os.listdir(run_dir / "verify-logs"))
    assert logs == ["01-lint.log", "02-plan-test-f-never-txt.log", "baseline-01-lint.log"]


def test_e2e_incomplete_without_database(verify_run):
    code, s, run_dir = verify_run(
        {"lint.out": "", "ok.txt": ""}, {"src/new.ts": "export {}\n"},
        [{"name": "lint", "run": LINT}, {"name": "migrate", "run": "echo migrated", "needs": "database"}])
    assert (code, s["verdict"]) == (4, "INCOMPLETE")
    assert s["not_verified"] == ["migrate"] and s["failed"] == []
    md = (run_dir / "verify.md").read_text()
    assert "Docker is installed but not running" in md and "**not** verified" in md


def test_e2e_provided_database_url_replaces_real_one(verify_run):
    url = "postgresql://verify:verify@127.0.0.1:1/throwaway"
    code, s, _ = verify_run(
        {"lint.out": ""}, {"src/new.ts": "export {}\n"},
        [{"name": "migrate", "run": 'test "$DATABASE_URL" = "{}" && test -z "$PGPASSWORD"'.format(url),
          "needs": "database"}],
        env={"VERIFY_DATABASE_URL": url, "DATABASE_URL": "postgresql://prod.example.invalid/real",
             "PGPASSWORD": "real"})
    assert (code, s["verdict"]) == (0, "PASS")

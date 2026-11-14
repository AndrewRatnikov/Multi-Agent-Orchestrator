"""Transition tests for orch.py.

No real pipeline runs: the existing scripts are replaced by a fake (`Fake`) that returns
scripted exit codes and writes the files the real ones would, the subagents are played
by `Agents` (it writes their artifacts and returns a RESULT block), and the target repo
is a throwaway git repo under tmp_path.

Run: python3 -m pytest .claude/scripts/tests -q
"""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import orch  # noqa: E402


# ── fakes ─────────────────────────────────────────────────────────────────────

def _sandbox(root):
    def run(args):
        run_id = args[1]
        with open(os.path.join(root, "runs", run_id, "report.md"), "a") as f:
            f.write("\n---\n## Test sandbox run — now\n\n- Command: `npm test`\n- Timeout: 150s (default)\n\n"
                    "### Result: {}\n\n```\n1 failing: expected 2 got 3\n```\nFull output: "
                    "`runs/{}/sandbox-logs/x.log`\n".format(run.verdict, run_id))
        return run.code, run.verdict
    run.code, run.verdict = 0, "PASS"
    return run


def _verify(root, code=0, summary=None, extra=""):
    def run(args):
        run_id = args[1]
        s = {"verdict": "PASS" if code == 0 else "FAIL", "exit_code": code, "failed": [], "not_verified": [],
             "pre_existing": [], "files_with_new_errors": [], "new_errors_by_file": {},
             "failed_steps_without_file_info": [], "new_migrations": []}
        s.update(summary or {})
        md = "# Verify\n\n| # | Step | Result |\n|---|---|---|\n| 0 | lint | {} |\n\n{}\n---\nMachine-readable " \
             "summary:\n\n```json\n{}\n```\n".format(s["verdict"], extra, json.dumps(s, indent=2))
        with open(os.path.join(root, "runs", run_id, "verify.md"), "w") as f:
            f.write(md)
        return code, md
    return run


def _render(root, code=2, reason="repo has no render config"):
    def run(args):
        d = os.path.join(root, "runs", args[1], "render")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "summary.json"), "w") as f:
            json.dump({"status": {0: "CAPTURED", 1: "INCOMPLETE", 2: "SKIPPED"}.get(code, "ERROR"),
                       "reason": reason, "results": []}, f)
        return code, reason
    return run


def _digest(root):
    def run(args):
        p = os.path.join(root, "runs", args[1], "repo-digest.md")
        with open(p, "w") as f:
            f.write("# Repo Digest\n\n## Test command\n```\nnpm test\n```\n\n## Files\n")
        return 0, "repo-digest: generated"
    return run


class Fake:
    """Stands in for orch.call. Rules are queues per key; the last entry repeats."""

    def __init__(self, root):
        self.calls = []
        self.sandbox = _sandbox(root)
        self.rules = {
            "repo-digest.sh": [_digest(root)],
            "size-run.py": [(1, "size: standard\n- 5 code files (small allows 3)")],
            "render-config.py:check": [(2, "not-needed: no render_targets")],
            "coder-slices.py:plan": [(1, "single pass")],
            "check-adr.py:trivial": [(0, "trivial")],
            "review-delta.py:diff": [(1, "full review: first review of the run")],
            "run-tests.sh": [self.sandbox],
            "verify.py": [_verify(root)],
            "render-capture.mjs": [_render(root)],
            "commit-code.py": [(0, "abc1234 feat: the thing")],
            "run-usage.py": [(0, "## By agent\n| a | b |\n\ntotal_tokens: 100 | weighted: 40")],
        }

    def on(self, key, *results):
        self.rules[key] = list(results)

    def key(self, script, args):
        mode = {"check-contract.py": lambda: args[2], "review-delta.py": lambda: args[2],
                "render-config.py": lambda: args[0], "coder-slices.py": lambda: args[0],
                "check-adr.py": lambda: args[-1], "check-paths.py": lambda: args[1]}.get(script)
        return "{}:{}".format(script, mode()) if mode else script

    def __call__(self, script, *args):
        args = [str(a) for a in args]
        self.calls.append((script,) + tuple(args))
        q = self.rules.get(self.key(script, args))
        res = (q.pop(0) if len(q) > 1 else q[0]) if q else (0, "")
        return res(args) if callable(res) else res

    def called(self, script, *prefix):
        return [c for c in self.calls if c[0] == script and c[1:1 + len(prefix)] == prefix]


class Agents:
    """Plays the subagents: writes their artifacts, returns their final report."""

    def __init__(self, root):
        self.root = root
        self.n = 0

    def run_dir(self, inst):
        return os.path.join(self.root, "runs", inst["run_id"])

    def product(self, inst):
        open(os.path.join(self.run_dir(inst), "prd.md"), "w").write("# PRD\n")
        return "PRD done.\n\n## RESULT\nstatus: DONE\ncriteria_count: 3\nsummary: three criteria\n"

    def architect(self, inst):
        d = self.run_dir(inst)
        open(os.path.join(d, "plan.md"), "w").write("# Plan\n\n## Verification\n\nManual:\n- check the page\n")
        open(os.path.join(d, "contract.json"), "w").write("{}")
        return ("## RESULT\nstatus: DONE\ncomponents: 1\ntest_selectors: 2\ntest_command: npm test\n"
                "summary: one component\n\n## Interface Contract\n- `thing()` in src/thing.js\n")

    def tester(self, inst):
        self.n += 1
        p = os.path.join(self.run_dir(inst), "tests", "tests", "thing.test.js")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write("test('thing v{}', () => {{}})\n".format(self.n))
        return ("## RESULT\nstatus: DONE\ntest_files: 1\ntest_cases: 1\ncriteria_covered: 3/3\n"
                "contract_gaps: 0\nfiles:\n- tests/thing.test.js\ngaps:\n- none\n")

    def reviewer(self, verdict="PASS", failures=("none",)):
        def run(inst):
            return ("## RESULT\nverdict: {}\nchecklist: B1✓\ntest_cases_reviewed: 1\nfailures:\n{}\n"
                    "contract_gaps:\n- none\n").format(verdict, "\n".join("- " + f for f in failures))
        return run

    def coder(self, inst):
        p = os.path.join(self.run_dir(inst), "code", "src", "thing.js")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write("module.exports = 1\n")
        return ("## RESULT\nstatus: DONE\nfiles:\n- src/thing.js (CREATE)\nnotes: none\ndeviations: none\n")

    def mismatch(self, inst):
        return "## RESULT\nstatus: CONTRACT_MISMATCH\ndetail: testid x\nrecommendation: resume from architect\n"

    def render_reviewer(self, verdict="PASS"):
        def run(inst):
            open(os.path.join(self.run_dir(inst), "render-review.md"), "w").write("# review\n")
            blocking = "- B1 home: totals shown twice (after/home.png)" if verdict == "FAIL" else "- none"
            return "## RESULT\nverdict: {}\ntargets_reviewed: 1\nscreenshots_viewed: 2\nblocking:\n{}\nnotes:\n- none\n" \
                .format(verdict, blocking)
        return run

    def adr(self, inst):
        return "## RESULT\nstatus: NONE\nadrs:\n- none\nskipped:\n- D1: trivial\nsummary: nothing to record\n"


def sh(*cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


@pytest.fixture
def env(tmp_path, monkeypatch, capsys):
    root = tmp_path / "orch"
    (root / "runs").mkdir(parents=True)
    (root / "memory.md").write_text("# Memory\n\n## Run history\n- old | x\n")
    monkeypatch.setattr(orch, "ROOT", str(root))
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@example.invalid"),
                 ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@example.invalid")):
        monkeypatch.setenv(k, v)
    repo = tmp_path / "repo"
    repo.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=repo)
    (repo / "package.json").write_text("{}\n")
    sh("git", "add", "-A", cwd=repo)
    sh("git", "commit", "-q", "-m", "init", cwd=repo)
    fake = Fake(str(root))
    monkeypatch.setattr(orch, "call", fake)
    agents = Agents(str(root))
    return Pipe(root, repo, fake, agents, capsys)


class Pipe:
    def __init__(self, root, repo, fake, agents, capsys):
        self.root, self.repo, self.fake, self.agents, self.capsys = root, repo, fake, agents, capsys
        self.id = None
        self.responders = {"product": agents.product, "architect": agents.architect, "tester": agents.tester,
                           "test-reviewer": agents.reviewer(), "coder": agents.coder,
                           "render-reviewer": agents.render_reviewer(), "adr": agents.adr}
        self.spawned = []

    def cmd(self, *argv):
        rc = orch.main([str(a) for a in argv])
        cap = self.capsys.readouterr()
        out = cap.out
        assert rc == 0, out + cap.err
        inst = json.loads(out)
        if inst["action"] == "continue":  # the main session shows `say` and calls next
            nxt = self.cmd("next", inst["run_id"])
            nxt["say"] = inst.get("say", []) + nxt.get("say", [])
            return nxt
        return inst

    def start(self, *extra):
        inst = self.cmd("start", "--repo", self.repo, "--task", "add a thing", *extra)
        self.id = inst["run_id"]
        return inst

    def next(self):
        return self.cmd("next", self.id)

    def feed(self, inst, text):
        f = inst.get("result_file") or inst["answer_file"]
        p = os.path.join(str(self.root), f)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write(text)
        stage = inst["stage"] if inst["action"] == "spawn" else "answer:" + inst["id"]
        return self.cmd("result", self.id, stage, f)

    def drive(self, inst, until=None, max_steps=60):
        """Answer spawns with the responders until `until(inst)` or a non-spawn instruction."""
        for _ in range(max_steps):
            if until and until(inst):
                return inst
            if inst["action"] != "spawn":
                return inst
            self.spawned.append(inst)
            inst = self.feed(inst, self.responders[inst["stage"]](inst))
        raise AssertionError("drive did not finish")

    def state(self):
        return json.loads((self.root / "runs" / self.id / "state.json").read_text())

    def report(self):
        return (self.root / "runs" / self.id / "report.md").read_text()

    def git_log(self):
        return subprocess.run(["git", "-C", str(self.repo), "log", "--format=%s"], stdout=subprocess.PIPE,
                              text=True).stdout.splitlines()


def spawn_of(stage):
    return lambda inst: inst["action"] == "spawn" and inst["stage"] == stage


# ── RESULT parser ─────────────────────────────────────────────────────────────

def test_parse_result_lists_continuations_and_tail():
    res = orch.parse_result("prose\n\n## RESULT\n```\nstatus: DONE\nfiles:\n- a.ts (CREATE)\nnotes: none\n"
                            "deviations:\n- order changed: because\n  it is safer\n```\n\n## Interface Contract\nx\n")
    assert res["status"] == "DONE"
    assert res["files"] == ["a.ts (CREATE)"]
    assert res["deviations"] == ["order changed: because it is safer"]
    assert orch.as_list(res["notes"]) == []
    assert res["_tail"].startswith("## Interface Contract")


def test_parse_result_missing_block():
    assert orch.parse_result("I did the thing.") is None


def test_parse_result_numbered_questions():
    res = orch.parse_result("## RESULT\nstatus: NEEDS_INPUT\nquestions:\n1. Which page?\n2. Which locale?\n")
    assert res["questions"] == ["Which page?", "Which locale?"]


# ── happy path ────────────────────────────────────────────────────────────────

def test_happy_path_standard_run(env):
    inst = env.start()
    assert inst["action"] == "spawn" and inst["agent"] == "orch-product"
    assert "STAGE: orch-product" in inst["prompt"] and "RUN_ID: " + env.id in inst["prompt"]
    assert any("started on branch" in s for s in inst["say"])
    inst = env.drive(inst)
    assert inst["action"] == "stop" and inst["status"] == "done", inst
    st = env.state()
    assert (st["step"], st["status"], st["size"]) == ("done", "done", "standard")
    log = env.git_log()
    assert log[0] == "test: add a thing"  # commit-code is faked, so only the test commit is real
    rep = env.report()
    for line in ("[product] DONE — three criteria", "[size] standard — 5 code files (small allows 3)",
                 "[render-config] not-needed", "[tester] DONE", "[test-reviewer] PASS", "[coder] DONE",
                 "[verify] PASS", "[render] SKIPPED — repo has no render config",
                 "[adr] NONE — trivial run, writer not run", "## Result: PASS ✓", "### Commits on orchestrator/"):
        assert line in rep, line
    assert env.fake.called("commit-code.py", str(env.repo), env.id) == [("commit-code.py", str(env.repo), env.id)]
    mem = (env.root / "memory.md").read_text()
    assert "## Run history\n- {} |".format(env.id) in mem and "size: standard | tokens: 100 (weighted 40)" in mem
    state_md = (env.root / "runs" / env.id / "state.md").read_text()
    assert "step: done\nstatus: done\n" in state_md and "retry_count: 0\n" in state_md
    assert "followup" in inst


def test_ui_run_without_targets_says_skipped_and_why(env):
    """An Architect opt-out (render_targets_none_reason) is shown at 2b and in report.md and the
    final summary, next to SKIPPED, never as a pass."""
    reason = "pure refactor: the page's markup is unchanged"

    def architect(inst):
        out = env.agents.architect(inst)
        p = os.path.join(env.agents.run_dir(inst), "contract.json")
        open(p, "w").write(json.dumps({"render_targets": [], "render_targets_none_reason": reason}))
        return out
    env.responders["architect"] = architect
    env.fake.on("render-config.py:check", (2, "no-targets: the run changes UI files (page.tsx) but the Architect "
                                              "declared no render targets; the render review will be SKIPPED. "
                                              "render_targets_none_reason: " + reason))
    env.fake.on("render-capture.mjs", _render(str(env.root), 2, "Architect declared no render targets: " + reason))
    says = []
    inst = env.drive(env.start(), until=lambda i: says.extend(i.get("say") or []))
    assert inst["action"] == "stop" and inst["status"] == "done", inst
    assert any(s.startswith("Render review will be **SKIPPED**") and reason in s for s in says)
    rep = env.report()
    assert "[render-config] no-targets:" in rep and "Render targets: none ({})".format(reason) in rep
    line = next(l for l in inst["message"].splitlines() if l.startswith("**Render review:**"))
    assert "SKIPPED" in line and reason in line and "not a pass" in line


def test_start_refuses_dirty_tree(env):
    (env.repo / "x.txt").write_text("dirty")
    inst = env.cmd("start", "--repo", env.repo, "--task", "t")
    assert inst["action"] == "stop" and "uncommitted changes" in inst["message"]
    assert os.listdir(env.root / "runs") == []


def test_start_asks_about_unmerged_branch(env):
    sh("git", "checkout", "-q", "-b", "feature", cwd=env.repo)
    (env.repo / "f.txt").write_text("f")
    sh("git", "add", "-A", cwd=env.repo)
    sh("git", "commit", "-q", "-m", "feature work", cwd=env.repo)
    inst = env.cmd("start", "--repo", env.repo, "--task", "t")
    assert inst["action"] == "ask" and "1 unmerged commits" in inst["question"]
    inst = env.cmd("start", "--repo", env.repo, "--task", "t", "--base", "switch")
    assert inst["action"] == "spawn"
    st = json.loads((env.root / "runs" / inst["run_id"] / "state.json").read_text())
    assert st["original_branch"] == "main"


# ── retry caps ────────────────────────────────────────────────────────────────

def test_contract_check_retry_cap_trips_after_two_retries(env):
    env.fake.on("check-contract.py:tests", (1, "UNDECLARED_TESTID foo {not-a-format-field}"))
    inst = env.drive(env.start())
    assert inst["action"] == "stop" and "retry cap reached (2/2)" in inst["message"]
    assert "--from tester" in inst["message"]
    st = env.state()
    assert (st["retry_count"], st["status"], st["pause_reason"]) == (2, "failed", "retry-cap")
    tester_spawns = [i for i in env.spawned if i["stage"] == "tester"]
    assert len(tester_spawns) == 3
    assert "FEEDBACK: The mechanical contract check failed" in tester_spawns[1]["prompt"]
    assert not any(i["stage"] == "test-reviewer" for i in env.spawned)  # never on a dirty contract check
    archive = os.listdir(env.root / "runs" / env.id / "archive")
    assert "tests_v1" in archive and "tests_v2" in archive
    assert env.git_log()[:3] == ["fix tests: add a thing (retry 2/2)", "fix tests: add a thing (retry 1/2)",
                                 "test: add a thing"]
    rep = env.report()
    assert "[check-contract] FAIL — 1 violation(s), returning to Tester (retry 1/2)" in rep
    assert "(retry cap reached)" in rep
    # next re-emits the stop, it doesn't run anything
    n = len(env.fake.calls)
    assert env.next()["action"] == "stop" and len(env.fake.calls) == n


def test_sandbox_fail_retry_cap(env):
    env.fake.sandbox.code, env.fake.sandbox.verdict = 1, "FAIL"
    inst = env.drive(env.start())
    assert inst["action"] == "stop" and "Coder retry cap reached (2/2)" in inst["message"]
    coder = [i for i in env.spawned if i["stage"] == "coder"]
    assert len(coder) == 3
    assert "expected 2 got 3" in coder[1]["prompt"] and "Full test output: runs/" in coder[1]["prompt"]
    fixes = env.fake.called("commit-code.py", str(env.repo), env.id, "--fix")
    assert [c[4] for c in fixes] == ["address sandbox failures (retry 1/2)", "address sandbox failures (retry 2/2)"]
    assert env.state()["retry_count"] == 2


# ── small runs and escalation ─────────────────────────────────────────────────

def test_small_run_skips_review_and_uses_sonnet(env):
    env.fake.on("size-run.py", (0, "size: small\n- 1 code file"))
    inst = env.drive(env.start())
    assert inst["status"] == "done"
    assert not any(i["stage"] == "test-reviewer" for i in env.spawned)
    assert all(i["model"] == "sonnet" for i in env.spawned if i["stage"] in ("tester", "coder"))
    assert "[test-reviewer] SKIPPED — small run (mechanical check 4a passed)" in env.report()
    assert (env.root / "runs" / env.id / ".test_reviewer_passed_at").exists()


def test_small_run_escalates_on_code_contract_failure_and_runs_skipped_review(env):
    env.fake.on("size-run.py", (0, "size: small\n- 1 code file"))
    env.fake.on("check-contract.py:code", (1, "MISSING_TESTID_IN_CODE thing-total"), (0, ""))
    inst = env.drive(env.start(), until=spawn_of("test-reviewer"))
    st = env.state()
    assert st["size"] == "standard"
    assert st["size_reasons"][-1].startswith("escalated: coder:")
    assert "[size] ESCALATED to standard — the code contract check sent the code back" in env.report()
    assert "sized small and skipped this review" in inst["prompt"]
    assert st["retry_count"] == 0  # the retry hasn't started yet
    inst = env.feed(inst, env.agents.reviewer()(inst))
    assert spawn_of("coder")(inst) and inst["model"] is None  # Opus from now on
    assert "MISSING_TESTID_IN_CODE" in inst["prompt"]
    assert env.state()["retry_count"] == 1
    inst = env.drive(inst)
    assert inst["status"] == "done"
    assert "size: small→standard" in (env.root / "memory.md").read_text()


def test_small_run_escalates_on_contract_check_and_never_goes_back(env):
    env.fake.on("size-run.py", (0, "size: small\n- 1 code file"))
    env.fake.on("check-contract.py:tests", (1, "UNDECLARED_TESTID x"), (0, ""))
    inst = env.drive(env.start(), until=lambda i: spawn_of("tester")(i) and "FEEDBACK" in i["prompt"])
    assert inst["model"] is None and env.state()["size"] == "standard"
    inst = env.drive(inst, until=spawn_of("test-reviewer"))  # 4b now runs: the run is standard
    assert spawn_of("test-reviewer")(inst)


# ── sandbox timeout ───────────────────────────────────────────────────────────

def test_timeout_routes_to_test_reviewer_then_coder(env):
    env.fake.sandbox.code, env.fake.sandbox.verdict = 2, "TIMEOUT"
    inst = env.drive(env.start(), until=lambda i: spawn_of("test-reviewer")(i) and len(
        [s for s in env.spawned if s["stage"] == "test-reviewer"]) == 1)
    assert inst["stage"] == "test-reviewer"
    assert "timed out after 150s" in inst["prompt"]
    assert "Tests timed out. Routing back to Test-Reviewer." in inst["say"]
    assert len(env.fake.called("review-delta.py", env.id, str(env.repo), "diff")) == 1  # not for the timeout review
    assert env.state()["step"] == "test-reviewer"
    env.fake.sandbox.code, env.fake.sandbox.verdict = 0, "PASS"
    inst = env.feed(inst, env.agents.reviewer()(inst))
    assert spawn_of("coder")(inst) and "timed out" in inst["prompt"]
    assert env.state()["retry_count"] == 1
    inst = env.drive(inst)
    assert inst["status"] == "done"
    assert env.fake.called("commit-code.py", str(env.repo), env.id, "--fix")[0][4] == \
        "address sandbox timeout (retry 1/2)"


def test_timeout_review_fail_goes_to_tester(env):
    env.fake.sandbox.code, env.fake.sandbox.verdict = 2, "TIMEOUT"
    inst = env.drive(env.start(), until=lambda i: spawn_of("test-reviewer")(i) and len(
        [s for s in env.spawned if s["stage"] == "test-reviewer"]) == 1)
    inst = env.feed(inst, env.agents.reviewer("FAIL", ["B2: tests/thing.test.js:thing - real fetch, mock it"])(inst))
    assert spawn_of("tester")(inst) and "real fetch, mock it" in inst["prompt"]


def test_run_without_test_files_skips_test_gates(env):
    env.responders["tester"] = lambda inst: "## RESULT\nstatus: DONE\ntest_files: 0\ntest_cases: 0\ngaps:\n- none\n"
    inst = env.drive(env.start())
    assert inst["action"] == "stop" and env.state().get("pause_reason") != "contract-check-error"
    assert (env.root / "runs" / env.id / "tests").is_dir()
    assert "SKIPPED — no test files" in env.report()
    assert not [i for i in env.spawned if i["stage"] == "test-reviewer"]
    assert [i for i in env.spawned if i["stage"] == "coder"]


def test_coder_notes_as_bullet_list_does_not_crash(env):
    inst = env.drive(env.start(), until=spawn_of("coder"))
    env.responders["coder"] = lambda i: "## RESULT\nstatus: DONE\nfiles:\n- src/thing.js (CREATE)\nnotes: fine\n- a bullet\n"
    inst = env.drive(inst)
    assert inst["action"] in ("stop", "spawn", "do", "continue")


# ── hard stops ────────────────────────────────────────────────────────────────

def test_tests_modified_after_review_stops_without_retry(env):
    env.fake.on("check-contract.py:code", (1, "TESTS_MODIFIED_AFTER_REVIEW tests/thing.test.js\n"
                                              "MISSING_TESTID_IN_CODE x"))
    inst = env.drive(env.start())
    assert inst["action"] == "stop" and "modified after the test-reviewer approved" in inst["message"]
    assert "--from tester" in inst["message"]
    st = env.state()
    assert (st["status"], st["pause_reason"], st["retry_count"]) == ("failed", "tests-modified-after-review", 0)
    assert len([i for i in env.spawned if i["stage"] == "coder"]) == 1
    assert "TESTS_MODIFIED_AFTER_REVIEW" in env.report()


def test_sandbox_error_stops(env):
    env.fake.sandbox.code, env.fake.sandbox.verdict = 3, "ERROR"
    inst = env.drive(env.start())
    assert inst["action"] == "stop" and env.state()["pause_reason"] == "sandbox-infrastructure-error"


def test_contract_mismatch_pauses(env):
    env.responders["coder"] = env.agents.mismatch
    inst = env.drive(env.start())
    assert "CONTRACT_MISMATCH" in inst["message"] and "--from architect" in inst["message"]
    assert env.state()["pause_reason"] == "contract-mismatch"
    assert env.fake.called("commit-code.py") == []


def test_missing_result_block_stops(env):
    env.responders["tester"] = lambda inst: "I wrote the tests."
    inst = env.drive(env.start())
    assert inst["action"] == "stop" and env.state()["pause_reason"] == "result-malformed"


def test_missing_adr_result_block_is_not_written_and_run_completes(env):
    env.fake.on("check-adr.py:trivial", (1, "plan has an ADR decision"))
    env.responders["adr"] = lambda inst: "Wrote some ADRs."
    inst = env.drive(env.start())
    assert inst["status"] == "done"
    assert "[adr] NOT WRITTEN — no valid RESULT block" in env.report()


def test_result_for_wrong_stage_is_rejected(env, capsys):
    env.start()
    p = env.root / "x.md"
    p.write_text("## RESULT\nstatus: DONE\n")
    assert orch.main(["result", env.id, "coder", str(p)]) == 2
    assert "waiting for stage product" in capsys.readouterr().err


# ── questions ─────────────────────────────────────────────────────────────────

NEEDS = "## RESULT\nstatus: NEEDS_INPUT\nquestions:\n1. Which page?\n2. Which currency?\n"


def test_needs_input_pauses_and_answer_in_session_resumes(env):
    inst = env.start()
    inst = env.feed(inst, NEEDS)
    assert inst["action"] == "ask" and inst["id"] == "questions:product" and "Which currency?" in inst["question"]
    st = env.state()
    assert (st["status"], st["pause_reason"]) == ("paused", "awaiting_human_input")
    assert (env.root / "runs" / env.id / "questions-product.md").exists()
    inst = env.feed(inst, "Home page; EUR")
    assert spawn_of("product")(inst)
    assert "ANSWERS: runs/{}/answers-product.md".format(env.id) in inst["prompt"]
    assert (env.root / "runs" / env.id / "answers-product.md").read_text().startswith("Home page; EUR")
    assert env.state()["status"] == "running"


def test_needs_input_survives_session_end_and_resume_asks_again(env):
    inst = env.start()
    env.feed(inst, NEEDS)
    inst = env.cmd("resume", env.id)  # new session, no --from: resumes at the paused step
    assert inst["action"] == "ask" and "Which page?" in inst["question"]
    assert any("Paused:  awaiting_human_input" in s for s in inst["say"])
    inst = env.feed(inst, "Home; EUR")
    assert spawn_of("product")(inst) and "ANSWERS:" in inst["prompt"]
    assert "[RESUMED] step: product" in env.report()


def test_resume_uses_answers_file_written_by_hand(env):
    inst = env.start()
    inst = env.drive(inst, until=spawn_of("architect"))
    env.feed(inst, NEEDS)
    (env.root / "runs" / env.id / "answers-architect.md").write_text("1. Home 2. EUR\n")
    inst = env.cmd("resume", env.id, "--from", "architect")
    assert spawn_of("architect")(inst) and "answers-architect.md" in inst["prompt"]


def test_unattended_render_decision_skips(env):
    env.fake.on("render-config.py:check", (1, "missing: no render section"))
    inst = env.drive(env.start("--unattended"))
    assert inst["status"] == "done"
    assert "[render-config] SKIPPED: no one to ask (unattended)" in env.report()


def test_render_decision_ask_and_skip(env):
    env.fake.on("render-config.py:check", (1, "missing: no render section"))
    inst = env.drive(env.start())
    assert inst["action"] == "ask" and inst["id"] == "render-decision"
    assert env.state()["pause_reason"] == "awaiting_render_decision"
    inst = env.feed(inst, "Skip this time")
    assert spawn_of("tester")(inst)
    assert "[render-config] SKIPPED by user for this run" in env.report()


# ── routing ───────────────────────────────────────────────────────────────────

def test_verify_routes_each_file_to_its_owner(env):
    root = str(env.root)
    env.fake.on("verify.py", _verify(root, 1, {"new_errors_by_file": {
        "tests/thing.test.js": ["tests/thing.test.js:1 no-unused-vars"],
        "src/thing.js": ["src/thing.js:1 TS2322"]}}), _verify(root, 0))
    inst = env.drive(env.start(), until=lambda i: spawn_of("tester")(i) and "Verify" in i["prompt"])
    assert "no-unused-vars" in inst["prompt"] and "TS2322" not in inst["prompt"]
    assert env.state()["verify_retries"] == 1
    inst = env.feed(inst, env.agents.tester(inst))
    assert spawn_of("test-reviewer")(inst) and "only fixes lint/type errors from Verify" in inst["prompt"]
    inst = env.feed(inst, env.agents.reviewer()(inst))
    assert spawn_of("coder")(inst) and "TS2322" in inst["prompt"] and "no-unused-vars" not in inst["prompt"]
    inst = env.drive(inst)
    assert inst["status"] == "done"
    assert env.git_log()[0] == "fix tests: add a thing (retry 1/2)"
    assert env.fake.called("commit-code.py", str(env.repo), env.id, "--fix")[0][4] == \
        "address Verify errors (retry 1/2)"


def test_verify_lockfile_fix_is_not_a_retry(env):
    root = str(env.root)
    env.fake.on("verify.py", _verify(root, 1, {"failed_steps_without_file_info": ["install"]},
                                     "**Hint:** Lockfile is out of date with package.json."), _verify(root, 0))
    inst = env.drive(env.start())
    assert inst["status"] == "done"
    assert env.fake.called("commit-code.py", str(env.repo), env.id, "--lockfile")
    assert env.state()["verify_retries"] == 0


def test_verify_incomplete_accept(env):
    env.fake.on("verify.py", _verify(str(env.root), 4, {"not_verified": ["migrations"]},
                                     "## Not verified\n\n- migrations: no Docker\n"))
    inst = env.drive(env.start())
    assert inst["action"] == "ask" and inst["id"] == "verify-accept"
    assert env.state()["pause_reason"] == "verify-incomplete"
    inst = env.drive(env.feed(inst, "Accept the gap and continue"))
    assert inst["status"] == "done"
    assert "[verify] ACCEPTED UNVERIFIED by user: migrations" in env.report()
    assert "Not verified (accepted by you): migrations" in inst["message"]


def test_render_fail_sends_findings_to_coder_with_retry_count_one(env):
    env.fake.on("render-capture.mjs", _render(str(env.root), 0, ""))
    env.responders["render-reviewer"] = env.agents.render_reviewer("FAIL")
    inst = env.drive(env.start(), until=lambda i: spawn_of("coder")(i) and "rendered page" in i["prompt"])
    st = env.state()
    assert (st["render_retries"], st["retry_count"]) == (1, 1)
    assert "totals shown twice" in inst["prompt"]
    env.responders["render-reviewer"] = env.agents.render_reviewer("PASS")
    inst = env.drive(inst)
    assert inst["status"] == "done"
    assert env.fake.called("commit-code.py", str(env.repo), env.id, "--fix")[0][4] == \
        "address render review findings (retry 1/2)"
    assert "archive/render-review_v1.md" in "".join(
        "archive/" + f for f in os.listdir(env.root / "runs" / env.id / "archive"))


# ── state files and old runs ──────────────────────────────────────────────────

def test_manual_render_config_ref_edit_is_picked_up(env):
    env.start()
    md = env.root / "runs" / env.id / "state.md"
    md.write_text(md.read_text() + "render_config_ref: chore/render-config\n")
    env.next()
    assert env.state()["render_config_ref"] == "chore/render-config"
    assert "render_config_ref: chore/render-config (set by user in state.md)" in env.report()


def test_old_run_without_state_json_is_imported_and_resumable(env):
    rid = "run_20260101_000000"
    d = env.root / "runs" / rid
    d.mkdir()
    sh("git", "checkout", "-q", "-b", "orchestrator/" + rid, cwd=env.repo)
    (d / "state.md").write_text(
        "run_id: {}\ntask: old task\nrepo: {}\noriginal_branch: main\ntarget_branch: orchestrator/{}\n"
        "step: verify\nstatus: paused\ntimestamp: x\nlast_artifact:\npause_reason: verify-incomplete\n"
        "retry_count: 1\nrender_retries: 0\nsize: standard\nsize_reasons: 5 code files\n".format(rid, env.repo, rid))
    (d / "report.md").write_text("# Run Report\n\n## Stage log\n")
    (d / "repo-digest.md").write_text("## Test command\n```\nnpm test\n```\n")
    env.id = rid
    inst = env.cmd("resume", rid)
    st = env.state()
    assert st["retry_count"] == 1 and st["verify_retries"] == 0 and st["step"] in ("render", "adr", "done")
    assert inst["status"] == "done"
    assert "coder_slices_done: 0" in (d / "state.md").read_text()


def test_resume_coder_after_cap_is_a_fix_pass_with_last_failure(env):
    env.fake.sandbox.code, env.fake.sandbox.verdict = 1, "FAIL"
    env.drive(env.start())
    env.fake.sandbox.code, env.fake.sandbox.verdict = 0, "PASS"
    inst = env.cmd("resume", env.id, "--from", "coder")
    assert spawn_of("coder")(inst) and "expected 2 got 3" in inst["prompt"]
    assert any(f.startswith("code_before_resume_") for f in os.listdir(env.root / "runs" / env.id / "archive"))
    inst = env.drive(inst)
    assert inst["status"] == "done"
    assert env.fake.called("commit-code.py", str(env.repo), env.id, "--fix")[-1][4] == "resume fix pass (retry 2/2)"


def test_sliced_first_pass(env, monkeypatch):
    def plan(args):
        d = env.root / "runs" / args[1]
        (d / "coder-slices.json").write_text(json.dumps({"slices": [{"label": "data & core"}, {"label": "app & UI"}]}))
        return 0, "2 slices"
    env.fake.on("coder-slices.py:plan", plan)
    inst = env.drive(env.start(), until=spawn_of("coder"))
    assert "NOTE: SLICE 1/2 (data & core)" in inst["prompt"]
    inst = env.feed(inst, env.agents.coder(inst))
    assert "NOTE: SLICE 2/2 (app & UI)" in inst["prompt"]
    assert env.state()["coder_slices_done"] == 1
    inst = env.drive(inst)
    assert inst["status"] == "done"
    sets = [c[3] for c in env.fake.called("coder-slices.py", "set")]
    assert sets[:3] == ["1", "2", "0"]
    assert "[coder] slice 2/2 DONE — 1 files" in env.report()


# ── multi-line task: one-line title everywhere a single line is expected ──────

LONG_TASK = ("Add a proper \"not found\" experience to the web app: a custom 404 page for unknown URLs, "
             "and the same message on detail pages.\n\n## Shared content\n\n- Props: a `variant`\n"
             "repo: /somewhere/else\nstep: done\n\n## Tests\n\n- each variant")


def test_task_title():
    t = orch.task_title(LONG_TASK)
    assert "\n" not in t and len(t) <= orch.TITLE_MAX + 1 and t.endswith("…")
    assert t.startswith('Add a proper "not found" experience')
    assert orch.task_title("## Fix the login bug.\n\nmore") == "Fix the login bug"
    assert orch.task_title("a | b") == "a / b"          # memory.md's history line is pipe-separated
    assert orch.task_title("") == "untitled task"


def test_multiline_task_stays_one_line_in_state_commits_report_and_memory(env):
    inst = env.cmd("start", "--repo", env.repo, "--task", LONG_TASK)
    env.id = inst["run_id"]
    title = orch.task_title(LONG_TASK)
    assert "TASK: " + LONG_TASK in inst["prompt"]       # agents still get the full text
    run = env.root / "runs" / env.id
    assert (run / "task.md").read_text() == LONG_TASK + "\n"
    inst = env.drive(inst)
    assert inst["status"] == "done", inst
    st = env.state()
    assert st["task"] == LONG_TASK and st["title"] == title
    state_md = (run / "state.md").read_text()
    assert "task: {}\nrepo: {}\n".format(title, env.repo) in state_md
    assert "## Shared content" not in state_md and "repo: /somewhere/else" not in state_md
    assert state_md.count("\nstep: ") == 1
    assert env.git_log()[0] == "test: " + title
    rep = env.report()
    assert "**Task:** {} (full text: `runs/{}/task.md`)".format(title, env.id) in rep
    assert "## Shared content" not in rep
    mem = (env.root / "memory.md").read_text()
    assert "- {} | ".format(env.id) in mem and "task: {} | result: PASS".format(title) in mem
    assert "## Shared content" not in mem and "## Tests" not in mem
    # a later load must not mistake the title in state.md for a hand edit of the task
    env.cmd("next", env.id)
    assert env.state()["task"] == LONG_TASK
    assert "task changed by hand" not in env.report()


def test_task_edited_by_hand_in_state_md_replaces_task(env):
    inst = env.cmd("start", "--repo", env.repo, "--task", LONG_TASK)
    env.id = inst["run_id"]
    p = env.root / "runs" / env.id / "state.md"
    p.write_text(p.read_text().replace("task: " + orch.task_title(LONG_TASK), "task: do something else"))
    env.next()
    st = env.state()
    assert st["task"] == "do something else" and st["title"] == "do something else"
    assert "[state] task changed by hand in state.md" in env.report()

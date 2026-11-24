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

REAL_PR_PREFLIGHT = orch.pr_preflight


# ── fakes ─────────────────────────────────────────────────────────────────────

PRD = ("# PRD: add a thing\n\n## Goal\n\nShow the monthly total on the dashboard.\n\n## Acceptance criteria\n\n"
       "1. The dashboard shows the total of recent expenses in the header card.\n"
       "2. An empty month shows **zero**, not a blank card.\n3. The total updates after an expense is added.\n\n"
       "## Out of scope\n\n- budgets\n")

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
        open(os.path.join(self.run_dir(inst), "prd.md"), "w").write(PRD)
        return "PRD done.\n\n## RESULT\nstatus: DONE\ncriteria_count: 3\nsummary: three criteria\n"

    def prd_reviewer(self, verdict="CLEAR", findings=("none",)):
        def run(inst):
            return "## RESULT\nverdict: {}\nfindings:\n{}\n".format(verdict, "\n".join("- " + f for f in findings))
        return run

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
            shot = os.path.join(self.run_dir(inst), "render", "after", "home.desktop.png")
            os.makedirs(os.path.dirname(shot), exist_ok=True)
            open(shot, "wb").close()
            blocking = "- B1 home: totals shown twice (after/home.desktop.png)" if verdict == "FAIL" else "- none"
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
    monkeypatch.setattr(orch, "pr_preflight", lambda repo: [])     # tests that need it restore the real one
    agents = Agents(str(root))
    return Pipe(root, repo, fake, agents, capsys)


class Pipe:
    def __init__(self, root, repo, fake, agents, capsys):
        self.root, self.repo, self.fake, self.agents, self.capsys = root, repo, fake, agents, capsys
        self.id = None
        self.gotchas = []
        self.responders = {"product": agents.product, "prd-review": agents.prd_reviewer(),
                           "architect": agents.architect, "tester": agents.tester,
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
            if inst["action"] == "do" and inst.get("kind") == "gotchas":
                self.gotchas.append(inst)          # the main session records gotchas, then runs `then`
                inst = self.cmd("next", inst["run_id"])
                continue
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
    assert len(env.gotchas) == 1 and "Stage 7 gotchas" in env.gotchas[0]["message"]
    assert "**Pull request:** not created: the repo has no `origin` remote." in inst["message"]
    assert "{COMMITS}" not in inst["message"] and "commit(s) ahead of `main`" in inst["message"]


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
    """Unattended: no one to triage, so the cap stops the run as it always did."""
    env.fake.on("check-contract.py:tests", (1, "UNDECLARED_TESTID foo {not-a-format-field}"))
    inst = env.drive(env.start("--unattended"))
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
    # Escalation ladder: the cap asks a person instead of stopping.
    assert inst["action"] == "ask" and inst["id"] == "triage:coder", inst
    assert "Coder retry cap reached (2/2)" in inst["question"] and "expected 2 got 3" in inst["question"]
    assert inst["options"] == orch.triage_options("coder")
    assert env.state()["pause_reason"] == "awaiting_triage"
    coder = [i for i in env.spawned if i["stage"] == "coder"]
    assert len(coder) == 3
    assert "expected 2 got 3" in coder[1]["prompt"] and "Full test output: runs/" in coder[1]["prompt"]
    # Only the last automatic attempt is boosted.
    assert [(i["model"], i["effort"]) for i in coder] == [(None, None), (None, None), ("opus", "xhigh")]
    assert "[ladder] coder: last automatic attempt runs on opus at effort xhigh" in env.report()
    fixes = env.fake.called("commit-code.py", str(env.repo), env.id, "--fix")
    assert [c[4] for c in fixes] == ["address sandbox failures (retry 1/2)", "address sandbox failures (retry 2/2)"]
    assert env.state()["retry_count"] == 2
    inst = env.feed(inst, orch.T_STOP)
    assert inst["action"] == "stop" and "Coder retry cap reached (2/2)" in inst["message"]
    assert (env.state()["status"], env.state()["pause_reason"]) == ("failed", "retry-cap")


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


def test_contract_mismatch_feeds_tester_on_resume(env):
    env.responders["coder"] = env.agents.mismatch
    env.drive(env.start())
    inst = env.cmd("resume", env.id, "--from", "tester")
    assert inst["action"] == "spawn" and inst["agent"] == "orch-tester"
    assert "CONTRACT_MISMATCH" in inst["prompt"] and "FEEDBACK" in inst["prompt"]


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
    inst = env.drive(env.cmd("resume", rid))
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


# ── Tester not run when the contract gives it nothing to write ────────────────

def _contract(env, inst, files):
    p = os.path.join(env.agents.run_dir(inst), "contract.json")
    open(p, "w").write(json.dumps({"files": files}))


def test_tester_not_spawned_when_contract_has_no_tester_files(env):
    inst = env.drive(env.start(), until=spawn_of("architect"))
    out = env.agents.architect(inst)
    _contract(env, inst, [{"path": ".github/workflows/ci.yml", "owner": "coder", "action": "modify"},
                          {"path": "README.md", "owner": "coder", "action": "modify"}])
    inst = env.drive(env.feed(inst, out))
    assert inst["action"] == "stop" and inst["status"] == "done", inst
    stages = [i["stage"] for i in env.spawned]
    assert "tester" not in stages and "test-reviewer" not in stages and "coder" in stages
    rep = env.report()
    assert "[tester] SKIPPED — contract.json declares no Tester-owned files" in rep
    assert "SKIPPED — no test files" in rep
    assert (env.root / "runs" / env.id / "tests").is_dir()


def test_tester_spawned_when_contract_has_tester_files(env):
    inst = env.drive(env.start(), until=spawn_of("architect"))
    out = env.agents.architect(inst)
    _contract(env, inst, [{"path": "src/thing.js", "owner": "coder", "action": "create"},
                          {"path": "tests/thing.test.js", "owner": "tester", "action": "create"}])
    inst = env.drive(env.feed(inst, out), until=spawn_of("tester"))
    assert spawn_of("tester")(inst)


# ── database preflight at start ───────────────────────────────────────────────

def _verify_json(env, steps):
    d = env.repo / ".claude"
    d.mkdir(exist_ok=True)
    (d / "verify.json").write_text(json.dumps({"steps": steps}))
    sh("git", "add", "-A", cwd=env.repo)
    sh("git", "commit", "-q", "-m", "verify config", cwd=env.repo)


DB_STEPS = [{"name": "lint", "run": "true"}, {"name": "api e2e", "run": "true", "needs": "database"}]


def test_start_asks_when_database_steps_cannot_run(env, monkeypatch):
    _verify_json(env, DB_STEPS)
    monkeypatch.setattr(orch, "database_unavailable", lambda: "Docker is installed but not running")
    inst = env.cmd("start", "--repo", env.repo, "--task", "t")
    assert inst["action"] == "ask" and inst["id"] == "database"
    assert "api e2e" in inst["question"] and "not running" in inst["question"]
    assert "--skip-db-check" in inst["then"]
    assert not list((env.root / "runs").iterdir())          # nothing created, no branch claimed
    assert subprocess.run(["git", "-C", str(env.repo), "branch", "--show-current"], stdout=subprocess.PIPE,
                          text=True).stdout.strip() == "main"
    # Docker started: the same command goes through
    monkeypatch.setattr(orch, "database_unavailable", lambda: None)
    inst = env.cmd("start", "--repo", env.repo, "--task", "t")
    assert inst["action"] == "spawn"


def test_start_anyway_records_that_the_check_was_skipped(env, monkeypatch):
    _verify_json(env, DB_STEPS)
    monkeypatch.setattr(orch, "database_unavailable", lambda: "Docker is installed but not running")
    inst = env.cmd("start", "--repo", env.repo, "--task", "t", "--skip-db-check")
    env.id = inst["run_id"]
    assert inst["action"] == "spawn" and "[setup] database check skipped by user" in env.report()


def test_unattended_start_records_a_database_warning(env, monkeypatch):
    _verify_json(env, DB_STEPS)
    monkeypatch.setattr(orch, "database_unavailable", lambda: "Docker is installed but not running")
    inst = env.cmd("start", "--repo", env.repo, "--task", "t2", "--unattended")
    env.id = inst["run_id"]
    assert inst["action"] == "spawn" and any("WARNING" in s for s in inst["say"])
    assert "[setup] WARNING: Verify in" in env.report()


def test_start_does_not_check_docker_without_database_steps(env, monkeypatch):
    _verify_json(env, [{"name": "lint", "run": "true"}])

    def boom():
        raise AssertionError("must not be called")
    monkeypatch.setattr(orch, "database_unavailable", boom)
    assert env.cmd("start", "--repo", env.repo, "--task", "t")["action"] == "spawn"


# ── RESULT parser: multi-paragraph fields (run_20261117_063301) ───────────────

CODER_MISMATCH_MULTILINE = (
    "I stopped.\n\n## RESULT\nstatus: CONTRACT_MISMATCH\n"
    "detail: In lincoln-sample.test.ts the ID list puts 1909-S before 1909-S-VDB. But two assertions expect:\n"
    "- \"owns 16 of 24 and misses exactly the documented 8\"\n"
    "- \"owns 8 of 12 and misses four\"\n\n"
    "A filter keeps the array's order, so one group of these tests fails.\n"
    "recommendation: resume from tester. Put 'sample-1909-S' first in EXPECTED_MISSING.\n")


def test_parse_result_keeps_paragraphs_and_later_keys():
    res = orch.parse_result(CODER_MISMATCH_MULTILINE)
    assert res["status"] == "CONTRACT_MISMATCH"
    assert res["recommendation"].startswith("resume from tester")
    detail = orch.as_text(res["detail"])
    assert "misses four" in detail and "A filter keeps the array's order" in detail
    assert res["_tail"] == ""


def test_parse_result_prose_after_last_key_is_still_tail():
    res = orch.parse_result("## RESULT\nstatus: DONE\nsummary: done\n\nSome closing prose.\nMore prose.\n")
    assert res["summary"] == "done" and res["_tail"] == "Some closing prose.\nMore prose."


def test_mismatch_fix_in():
    f = orch.mismatch_fix_in
    assert f({"fix_in": "tests"}) == "tests" and f({"fix_in": "contract"}) == "contract"
    assert f({"recommendation": "resume from tester and fix the order"}) == "tests"
    assert f({"recommendation": "resume from architect to correct the contract"}) == "contract"
    assert f({}) == "contract"


# ── CONTRACT_MISMATCH blamed on the tests goes back to the Tester ─────────────

def _mismatch_tests(inst):
    return ("## RESULT\nstatus: CONTRACT_MISMATCH\nfix_in: tests\n"
            "detail: tests/thing.test.js expects 'S' but its fixture sets variety VDB:\n- \"ignores owned slots\"\n\n"
            "The plan's label rule gives 'S VDB'.\nrecommendation: fix the fixture\n")


def test_mismatch_in_tests_routes_to_tester_and_completes(env):
    first = {"done": False}

    def coder(inst):
        if not first["done"]:
            first["done"] = True
            return _mismatch_tests(inst)
        return env.agents.coder(inst)
    env.responders["coder"] = coder
    inst = env.drive(env.start(), until=lambda i: spawn_of("tester")(i) and "CONTRACT_MISMATCH" in i["prompt"])
    assert "fixture sets variety VDB" in inst["prompt"] and "The plan's label rule" in inst["prompt"]
    assert "[coder] CONTRACT_MISMATCH (fix in: tests)" in env.report()
    inst = env.drive(inst, until=spawn_of("test-reviewer"))
    assert "contradict the plan" in inst["prompt"]
    inst = env.drive(inst)
    assert inst["action"] == "stop" and inst["status"] == "done", inst
    assert "fix tests: add a thing" in env.git_log()[0] or any(l.startswith("fix tests:") for l in env.git_log())


def test_mismatch_in_tests_stops_after_two_tester_fixes(env):
    env.responders["coder"] = _mismatch_tests
    inst = env.drive(env.start())
    assert inst["action"] == "stop" and "--from tester" in inst["message"]
    assert "after 2 automatic Tester" in inst["message"]
    assert env.state()["pause_reason"] == "contract-mismatch"
    assert len([i for i in env.spawned if i["stage"] == "tester"]) == 3


# ── a missing result file leaves the run waiting ──────────────────────────────

def test_missing_result_file_is_a_usage_error_and_run_keeps_waiting(env):
    inst = env.start()
    rc = orch.main(["result", env.id, "product", inst["result_file"]])
    err = env.capsys.readouterr().err
    assert rc == 2 and "missing" in err and "still waiting" in err
    assert env.state()["pending"]["stage"] == "product"
    assert "FAILED" not in env.report()
    inst = env.feed(inst, env.agents.product(inst))
    assert spawn_of("prd-review")(inst)
    inst = env.feed(inst, env.agents.prd_reviewer()(inst))
    assert spawn_of("architect")(inst)


# ── the summary names what was not verified ───────────────────────────────────

def test_pre_existing_failure_is_reported_as_not_verified(env):
    env.fake.on("verify.py", _verify(str(env.root), 0, {"pre_existing": ["web build"]},
                                     "## Pre-existing failures (also fail on `main`; not blocking)\n\n- web build: `x`\n"))
    inst = env.drive(env.start())
    assert inst["status"] == "done"
    assert "not everything was verified" in inst["message"]
    assert "`web build` fails on `main` too" in inst["message"]
    assert "## Result: PASS ✓ (with gaps)" in env.report()


def test_render_error_then_resume_past_render_is_not_a_skip(env):
    inst = env.drive(env.start(), until=spawn_of("architect"))
    out = env.agents.architect(inst)
    open(os.path.join(env.agents.run_dir(inst), "contract.json"), "w").write(json.dumps(
        {"render_targets": [{"id": "landing", "route": "/", "state": "signed out", "fixture": "signed-out"}]}))
    env.fake.on("render-config.py:check", (0, "configured: main"))
    env.fake.on("render-capture.mjs", _render(str(env.root), 3, "dev server exited before it was ready"))
    inst = env.drive(env.feed(inst, out))
    assert inst["action"] == "stop" and env.state()["pause_reason"] == "render-infrastructure-error"
    assert "--from adr" in inst["message"]
    inst = env.drive(env.cmd("resume", env.id, "--from", "adr"))
    assert inst["status"] == "done"
    assert "ERROR (dev server exited before it was ready)" in inst["message"]
    assert "not everything was verified" in inst["message"]
    assert "SKIPPED (not run)" not in inst["message"]


def test_clean_run_headline_unchanged(env):
    inst = env.drive(env.start())
    assert inst["message"].startswith("✓ Pipeline complete! Tests passed and the branch is verified.")


# ── Stage 8: pull request into the default branch ───────────────────────────

class FakeGh:
    def __init__(self, existing=None, missing=False):
        self.calls, self.existing, self.missing = [], existing or [], missing
        self.view = (0, json.dumps({"nameWithOwner": "o/r", "viewerPermission": "WRITE"}))

    def __call__(self, args, cwd):
        self.calls.append(list(args))
        if self.missing:
            return 127, "gh (GitHub CLI) is not installed"
        if args[:2] == ["repo", "view"]:
            return self.view
        if args[:2] == ["pr", "list"]:
            return 0, json.dumps(self.existing)
        if args[:2] == ["pr", "create"]:
            self.body = open(args[args.index("--body-file") + 1]).read()
            return 0, "Creating pull request\nhttps://github.com/o/r/pull/9"
        return 0, ""

    def made(self, verb):
        return [c for c in self.calls if c[:2] == ["pr", verb]]


def _origin(env, tmp_path):
    bare = tmp_path / "origin.git"
    sh("git", "init", "-q", "--bare", str(bare))
    sh("git", "remote", "add", "origin", str(bare), cwd=env.repo)
    sh("git", "push", "-q", "origin", "main", cwd=env.repo)
    return bare


def _remote_branches(bare):
    return subprocess.run(["git", "-C", str(bare), "branch", "--format=%(refname:short)"], stdout=subprocess.PIPE,
                          text=True).stdout.split()


def test_pr_opened_into_main_after_gotchas(env, tmp_path, monkeypatch):
    bare = _origin(env, tmp_path)
    fake = FakeGh()
    monkeypatch.setattr(orch, "gh", fake)
    inst = env.drive(env.start())
    assert inst["status"] == "done", inst
    assert "orchestrator/" + env.id in _remote_branches(bare)
    create = fake.made("create")[0]
    assert create[create.index("--base") + 1] == "main" and create[create.index("--head") + 1] == "orchestrator/" + env.id
    assert "--draft" not in create
    assert "**Pull request:** https://github.com/o/r/pull/9" in inst["message"]
    assert "[pr] OPENED https://github.com/o/r/pull/9" in env.report()
    body = fake.body
    assert "## Verify" in body and "## Commits" in body and "Nothing was merged" in body
    for local in (str(env.root), str(env.repo), "runs/" + env.id + "/"):
        assert local not in body
    assert env.gotchas, "the gotcha step must come before the PR"


def test_pr_is_draft_when_something_was_not_verified(env, tmp_path, monkeypatch):
    _origin(env, tmp_path)
    fake = FakeGh()
    monkeypatch.setattr(orch, "gh", fake)
    env.fake.on("verify.py", _verify(str(env.root), 0, {"pre_existing": ["web build"]},
                                     "## Pre-existing failures (also fail on `main`; not blocking)\n\n- web build: `x`\n"))
    inst = env.drive(env.start())
    assert "--draft" in fake.made("create")[0]
    assert "## Why this is a draft" in fake.body and "web build" in fake.body
    assert "(draft:" in inst["message"]


def test_no_pr_flag_skips_push_and_pr(env, tmp_path, monkeypatch):
    bare = _origin(env, tmp_path)
    fake = FakeGh()
    monkeypatch.setattr(orch, "gh", fake)
    inst = env.cmd("start", "--repo", env.repo, "--task", "add a thing", "--no-pr")
    env.id = inst["run_id"]
    inst = env.drive(inst)
    assert inst["status"] == "done"
    assert fake.calls == [] and _remote_branches(bare) == ["main"]
    assert "not created (you asked for no PR)" in inst["message"]
    assert "[pr] SKIPPED" in env.report()


def test_pr_turned_off_mid_run(env, tmp_path, monkeypatch):
    _origin(env, tmp_path)
    fake = FakeGh()
    monkeypatch.setattr(orch, "gh", fake)
    inst = env.drive(env.start(), until=spawn_of("coder"))
    again = env.cmd("pr", env.id, "off")
    assert again["action"] == "spawn" and again["stage"] == "coder"     # the run carries on where it was
    inst = env.drive(inst)
    assert inst["status"] == "done" and fake.calls == []
    assert "[pr] turned off by user" in env.report()


def test_existing_pr_is_updated_not_duplicated(env, tmp_path, monkeypatch):
    _origin(env, tmp_path)
    fake = FakeGh(existing=[{"number": 7, "url": "https://github.com/o/r/pull/7", "isDraft": False}])
    monkeypatch.setattr(orch, "gh", fake)
    inst = env.drive(env.start())
    assert fake.made("create") == [] and fake.made("edit")[0][2] == "7"
    assert "https://github.com/o/r/pull/7 (updated)" in inst["message"]


def test_pr_failures_never_fail_the_run(env, tmp_path, monkeypatch):
    _origin(env, tmp_path)
    monkeypatch.setattr(orch, "gh", FakeGh(missing=True))
    inst = env.drive(env.start())
    assert inst["status"] == "done"
    assert "branch pushed, but the PR was not created (gh (GitHub CLI) is not installed)" in inst["message"]
    # then `pr now` retries on the finished run
    fake = FakeGh()
    monkeypatch.setattr(orch, "gh", fake)
    again = env.cmd("pr", env.id, "now")
    assert again["status"] == "done" and fake.made("create")


def test_push_failure_is_reported(env, tmp_path, monkeypatch):
    sh("git", "remote", "add", "origin", str(tmp_path / "nowhere.git"), cwd=env.repo)
    monkeypatch.setattr(orch, "gh", FakeGh())
    inst = env.drive(env.start())
    assert inst["status"] == "done" and "not created: `git push` failed" in inst["message"]


def test_compare_url():
    assert orch.compare_url("git@github.com:o/r.git", "main", "b") == "https://github.com/o/r/compare/main...b?expand=1"
    assert orch.compare_url("https://github.com/o/r", "main", "b").startswith("https://github.com/o/r/compare/")
    assert orch.compare_url("/tmp/x.git", "main", "b") is None


# ── pull-request preflight at start ───────────────────────────────────────────

def test_pr_preflight_checks(env, tmp_path, monkeypatch):
    check = lambda: REAL_PR_PREFLIGHT(str(env.repo))
    fake = FakeGh()
    monkeypatch.setattr(orch, "gh", fake)
    assert "no `origin` remote" in check()[0]
    _origin(env, tmp_path)
    assert check() == []
    fake.view = (0, json.dumps({"nameWithOwner": "o/r", "viewerPermission": "READ"}))
    assert "read access to o/r" in check()[0]
    fake.view = (1, "HTTP 401: Bad credentials")
    assert "Bad credentials" in check()[0] and "gh auth login" in check()[0]
    monkeypatch.setattr(orch, "gh", FakeGh(missing=True))
    assert "`gh` is not installed" in check()[0]


def test_pr_preflight_push_refused(env, tmp_path, monkeypatch):
    sh("git", "remote", "add", "origin", str(tmp_path / "nowhere.git"), cwd=env.repo)
    monkeypatch.setattr(orch, "gh", FakeGh())
    assert any("git can't push to `origin`" in p for p in REAL_PR_PREFLIGHT(str(env.repo)))


def test_start_asks_when_pr_cannot_be_created(env, monkeypatch):
    monkeypatch.setattr(orch, "pr_preflight", lambda repo: ["the GitHub CLI `gh` is not installed"])
    inst = env.cmd("start", "--repo", env.repo, "--task", "t", "--size", "full")
    assert inst["action"] == "ask" and inst["id"] == "pull-request"
    assert "`gh` is not installed" in inst["question"] and "--size full" in inst["then"]
    assert not list((env.root / "runs").iterdir())               # nothing created, no branch claimed
    # "Start without a pull request"
    inst = env.cmd("start", "--repo", env.repo, "--task", "t", "--no-pr")
    env.id = inst["run_id"]
    assert inst["action"] == "spawn" and env.state()["pr"] is False
    assert "[setup] pull request turned off by user" in env.report()


def test_start_anyway_and_unattended_keep_the_pr_and_warn(env, monkeypatch):
    monkeypatch.setattr(orch, "pr_preflight", lambda repo: ["the repo has no `origin` remote"])
    inst = env.cmd("start", "--repo", env.repo, "--task", "t", "--skip-pr-check")
    env.id = inst["run_id"]
    assert inst["action"] == "spawn" and env.state()["pr"] is True
    assert "check skipped by user" in env.report()


def test_unattended_start_warns_about_the_pr(env, monkeypatch):
    monkeypatch.setattr(orch, "pr_preflight", lambda repo: ["the repo has no `origin` remote"])
    inst = env.cmd("start", "--repo", env.repo, "--task", "t", "--unattended")
    env.id = inst["run_id"]
    assert inst["action"] == "spawn" and any("pull request" in s and "WARNING" in s for s in inst["say"])


def test_no_pr_skips_the_check(env, monkeypatch):
    def boom(repo):
        raise AssertionError("must not be called")
    monkeypatch.setattr(orch, "pr_preflight", boom)
    assert env.cmd("start", "--repo", env.repo, "--task", "t", "--no-pr")["action"] == "spawn"


def test_resume_notes_a_broken_pr_setup(env, monkeypatch):
    env.fake.sandbox.code, env.fake.sandbox.verdict = 3, "ERROR"
    env.drive(env.start())
    monkeypatch.setattr(orch, "pr_preflight", lambda repo: ["the GitHub CLI `gh` is not installed"])
    env.fake.sandbox.code, env.fake.sandbox.verdict = 0, "PASS"
    inst = env.cmd("resume", env.id, "--from", "sandbox")
    assert any("pull request at the end won't work" in s for s in inst["say"])


def test_pr_now_checks_first(env, monkeypatch):
    inst = env.drive(env.start())
    assert inst["status"] == "done"
    monkeypatch.setattr(orch, "pr_preflight", lambda repo: ["the repo has no `origin` remote"])
    again = env.cmd("pr", env.id, "now")
    assert "Can't open the pull request yet" in again["message"]


# ── PRD review (ambiguity hunt, stage 1b) ─────────────────────────────────────

CITED = '[vague-term] "the total of recent expenses" — A: last 30 days; B: this month; settle: name the period'
UNCITED = '[vague-term] "the sum of all old expenses" — A: x; B: y; settle: z'


def test_prd_review_findings_go_back_to_product_once(env):
    env.responders["prd-review"] = env.agents.prd_reviewer("AMBIGUOUS", [CITED, UNCITED])
    inst = env.drive(env.start(), until=lambda i: spawn_of("product")(i) and "FEEDBACK:" in i["prompt"])
    assert "the total of recent expenses" in inst["prompt"] and "sum of all old" not in inst["prompt"]
    assert "NEEDS_INPUT" in inst["prompt"] and "unattended" not in inst["prompt"]
    d = env.root / "runs" / env.id
    assert "recent expenses" in (d / "prd-review.md").read_text()
    assert (d / "archive" / "prd_before_review.md").exists()
    rep = env.report()
    assert "[prd-review] dropped 1 finding(s) that cite nothing" in rep and "sum of all old" in rep
    assert "[prd-review] AMBIGUOUS — 1 finding(s), back to the Product agent" in rep
    inst = env.drive(inst)
    assert inst["status"] == "done"
    assert [i["stage"] for i in env.spawned][:4] == ["product", "prd-review", "product", "architect"]


def test_prd_review_clear_goes_to_architect(env):
    inst = env.drive(env.start(), until=spawn_of("architect"))
    assert "[prd-review] CLEAR" in env.report()
    assert [i["stage"] for i in env.spawned] == ["product", "prd-review"]
    assert env.spawned[1]["agent"] == "orch-prd-reviewer"


def test_prd_review_uncited_only_asks_once_then_counts_as_clear(env):
    env.responders["prd-review"] = env.agents.prd_reviewer("AMBIGUOUS", [UNCITED])
    inst = env.drive(env.start(), until=lambda i: spawn_of("prd-review")(i) and "CITATION_RETRY" in i["prompt"])
    assert "sum of all old" in inst["prompt"]
    inst = env.drive(inst, until=spawn_of("architect"))
    assert sum(1 for i in env.spawned if i["stage"] == "prd-review") == 2
    assert "[prd-review] CLEAR (no cited findings left)" in env.report()
    assert not any(i["stage"] == "product" and "FEEDBACK:" in i["prompt"] for i in env.spawned)


def test_prd_review_caps_findings(env):
    many = ['[vague-term] "the total of recent expenses" — finding {}'.format(n) for n in range(9)]
    env.responders["prd-review"] = env.agents.prd_reviewer("AMBIGUOUS", many)
    inst = env.drive(env.start(), until=lambda i: spawn_of("product")(i) and "FEEDBACK:" in i["prompt"])
    assert "finding 6" in inst["prompt"] and "finding 7" not in inst["prompt"]
    assert "[prd-review] kept the first 7 of 9 findings" in env.report()


def test_prd_review_unattended_asks_product_for_assumptions(env):
    env.responders["prd-review"] = env.agents.prd_reviewer("AMBIGUOUS", [CITED])
    inst = env.drive(env.start("--unattended"), until=lambda i: spawn_of("product")(i) and "FEEDBACK:" in i["prompt"])
    assert "This run is unattended" in inst["prompt"]


def test_prd_review_blocked_stops_and_resumes(env):
    env.responders["prd-review"] = lambda inst: "## RESULT\nverdict: BLOCKED\nfindings:\n- none\nblocked_reason: prd.md is empty\n"
    inst = env.drive(env.start())
    assert inst["action"] == "stop" and "prd.md is empty" in inst["message"] and "--from prd-review" in inst["message"]
    assert (env.state()["status"], env.state()["pause_reason"]) == ("failed", "reviewer-blocked")
    env.responders["prd-review"] = env.agents.prd_reviewer()
    inst = env.cmd("resume", env.id, "--from", "prd-review")
    assert spawn_of("prd-review")(inst)
    assert env.drive(inst)["status"] == "done"


def test_prd_review_skipped_when_forced_small(env):
    inst = env.drive(env.start("--size", "small"), until=spawn_of("architect"))
    assert "[prd-review] SKIPPED — run forced small (--size small)" in env.report()
    assert not any(i["stage"] == "prd-review" for i in env.spawned)


def test_quotes_source_ignores_emphasis_and_line_breaks():
    prd = "1. An empty month shows **zero**, not a\nblank card."
    assert orch.quotes_source('[x] "shows zero, not a blank card" — A', prd)
    assert orch.quotes_source('[x] “empty month shows zero” — A', prd)
    assert not orch.quotes_source('[x] "shows zero" — too short to be a citation', prd)
    assert not orch.quotes_source('[x] "shows nothing, not a blank card" — not in the PRD', prd)
    assert not orch.quotes_source("[x] no quote at all", prd)


def test_prd_criteria_count():
    assert orch.prd_criteria_count(PRD) == 3
    assert orch.prd_criteria_count("# PRD\n\nno criteria section\n") == 0


# ── reviewer output contract: test-reviewer and render-reviewer ───────────────

def test_test_review_uncited_failures_are_dropped_cited_ones_kept(env):
    env.responders["test-reviewer"] = env.agents.reviewer("FAIL", [
        "B1: tests/thing.test.js:thing - only checks the card renders", "B3: some test somewhere - magic value",
        "C1: criterion 2 has no test", "C1: criterion 9 has no test"])
    inst = env.drive(env.start(), until=lambda i: spawn_of("tester")(i) and "FEEDBACK:" in i["prompt"])
    assert "only checks the card renders" in inst["prompt"] and "criterion 2 has no test" in inst["prompt"]
    assert "magic value" not in inst["prompt"] and "criterion 9" not in inst["prompt"]
    assert "[test-reviewer] dropped 2 finding(s)" in env.report()


def test_test_review_fail_without_citations_retries_once_then_stops(env):
    env.responders["test-reviewer"] = env.agents.reviewer("FAIL", ["B2: the tests look weak"])
    inst = env.drive(env.start(), until=lambda i: spawn_of("test-reviewer")(i) and "CITATION_RETRY" in i["prompt"])
    assert "the tests look weak" in inst["prompt"]
    inst = env.drive(inst)
    assert inst["action"] == "stop" and "--from test-reviewer" in inst["message"]
    assert (env.state()["status"], env.state()["pause_reason"]) == ("failed", "result-malformed")
    assert not any(c for c in env.fake.called("review-delta.py") if c[3] == "mark")  # nothing was recorded as reviewed


def test_test_review_blocked_stops(env):
    env.responders["test-reviewer"] = lambda inst: ("## RESULT\nverdict: BLOCKED\nchecklist: none\n"
                                                    "blocked_reason: runs/x/tests is empty\n")
    inst = env.drive(env.start())
    assert inst["action"] == "stop" and "orch-test-reviewer couldn't do its review" in inst["message"]
    assert env.state()["pause_reason"] == "reviewer-blocked"


def test_render_review_uncited_blocking_becomes_a_note(env):
    env.fake.on("render-capture.mjs", _render(str(env.root), 0, ""))

    def reviewer(inst):
        open(os.path.join(env.agents.run_dir(inst), "render-review.md"), "w").write("# review\n")
        return ("## RESULT\nverdict: FAIL\ntargets_reviewed: 1\nscreenshots_viewed: 2\nblocking:\n"
                "- B1 home: totals shown twice (after/missing.png)\nnotes:\n- none\n")
    env.responders["render-reviewer"] = reviewer
    inst = env.drive(env.start(), until=lambda i: spawn_of("render-reviewer")(i) and "CITATION_RETRY" in i["prompt"])
    inst = env.drive(inst)
    assert inst["status"] == "done"
    assert not any(i["stage"] == "coder" and "rendered page" in i["prompt"] for i in env.spawned)
    rep = env.report()
    assert "[render-reviewer] PASS (no cited BLOCKING finding left)" in rep
    assert "dropped 1 finding(s)" in rep and "after/missing.png" in rep


def test_render_review_last_retry_is_boosted(env):
    env.fake.on("render-capture.mjs", _render(str(env.root), 0, ""))
    env.responders["render-reviewer"] = env.agents.render_reviewer("FAIL")
    inst = env.drive(env.start())
    assert inst["action"] == "stop" and "Render-review retry cap reached" in inst["message"]
    render_fixes = [i for i in env.spawned if i["stage"] == "coder" and "rendered page" in i["prompt"]]
    assert [(i["model"], i["effort"]) for i in render_fixes] == [(None, None), ("opus", "xhigh")]


# ── escalation ladder: triage at the cap ──────────────────────────────────────

def test_triage_choice_parsing():
    c = orch.T_MORE.format("Coder")
    assert orch.triage_choice("coder", c) == ("more", "")
    assert orch.triage_choice("coder", "One more Coder attempt: the fixture date is UTC") == \
        ("more", "the fixture date is UTC")
    assert orch.triage_choice("coder", orch.T_TESTS + " — test 2 expects cents") == ("tests", "test 2 expects cents")
    assert orch.triage_choice("coder", orch.T_CRITERION)[0] == "criterion"
    assert orch.triage_choice("coder", "stop, I'll look myself") == ("stop", "")
    assert orch.triage_choice("coder", "check the rounding in total()") == ("more", "check the rounding in total()")
    assert orch.triage_choice("tester", orch.T_PLAN)[0] == "plan"
    assert orch.triage_choice("tester", orch.T_TESTS)[0] == "more"   # not an option for the Tester: it's a hint


def _to_coder_triage(env):
    env.fake.sandbox.code, env.fake.sandbox.verdict = 1, "FAIL"
    inst = env.drive(env.start())
    assert inst["action"] == "ask" and inst["id"] == "triage:coder", inst
    return inst


def test_triage_one_more_attempt_with_hint(env):
    inst = _to_coder_triage(env)
    inst = env.feed(inst, "One more Coder attempt: the fixture date is UTC")
    assert spawn_of("coder")(inst) and (inst["model"], inst["effort"]) == ("opus", "xhigh")
    assert "the fixture date is UTC" in inst["prompt"] and "expected 2 got 3" in inst["prompt"]
    env.fake.sandbox.code, env.fake.sandbox.verdict = 0, "PASS"
    inst = env.drive(inst)
    assert inst["status"] == "done"
    assert "[triage] coder: more — hint: the fixture date is UTC" in env.report()


def test_triage_one_more_attempt_fails_again_asks_again(env):
    inst = _to_coder_triage(env)
    inst = env.drive(env.feed(inst, "check total()"))
    assert inst["action"] == "ask" and inst["id"] == "triage:coder"
    assert sum(1 for i in env.spawned if i["stage"] == "coder") == 4


def test_triage_tests_are_wrong_goes_through_tester_and_review(env):
    inst = _to_coder_triage(env)
    inst = env.feed(inst, orch.T_TESTS + " — the total should be in cents")
    assert spawn_of("tester")(inst)
    assert "decided the tests are wrong" in inst["prompt"] and "in cents" in inst["prompt"]
    assert "expected 2 got 3" in inst["prompt"]
    inst = env.drive(inst, until=spawn_of("test-reviewer"))
    assert "A person triaged the Coder's repeated failures" in inst["prompt"]
    env.fake.sandbox.code, env.fake.sandbox.verdict = 0, "PASS"
    inst = env.drive(inst)
    assert inst["status"] == "done"


def test_triage_criterion_is_wrong_stops_for_the_prd(env):
    inst = _to_coder_triage(env)
    inst = env.feed(inst, orch.T_CRITERION)
    assert inst["action"] == "stop" and "prd.md" in inst["message"] and "--from architect" in inst["message"]
    assert (env.state()["status"], env.state()["pause_reason"]) == ("paused", "criterion-disputed")


def test_tester_cap_triage_plan_is_wrong(env):
    env.fake.on("check-contract.py:tests", (1, "UNDECLARED_TESTID foo"))
    inst = env.drive(env.start())
    assert inst["action"] == "ask" and inst["id"] == "triage:tester"
    assert inst["options"] == orch.triage_options("tester")
    testers = [i for i in env.spawned if i["stage"] == "tester"]
    assert (testers[-1]["model"], testers[-1]["effort"]) == ("opus", "xhigh")
    inst = env.feed(inst, orch.T_PLAN)
    assert inst["action"] == "stop" and "--from architect" in inst["message"]
    assert env.state()["pause_reason"] == "contract-disputed"


def test_tester_cap_triage_stop(env):
    env.fake.on("check-contract.py:tests", (1, "UNDECLARED_TESTID foo"))
    inst = env.feed(env.drive(env.start()), "Stop here")
    assert inst["action"] == "stop" and "Contract check retry cap reached (2/2)" in inst["message"]
    assert "for the feedback" in inst["message"] and "--from tester" in inst["message"]


def test_cites_test_accepts_the_usual_spellings(env):
    env.drive(env.start(), until=spawn_of("test-reviewer"))
    r = orch.Run(env.id).load()
    run_rel = "runs/{}/tests/tests/thing.test.js".format(env.id)
    for f in ("B1: tests/thing.test.js:thing - x", "B1: thing.test.js - x", "B1: {}:thing - x".format(run_rel),
              "B1: {} - x".format(env.root / run_rel), "C1: criterion 3 - no test", "C1: AC-1 has no test",
              "B1: package.json is not a test but exists in the repo"):
        assert orch.cites_test(r, f, 3), f
    for f in ("B1: tests/other.test.js - x", "C1: criterion 4 - no test", "B2: the tests look weak"):
        assert not orch.cites_test(r, f, 3), f

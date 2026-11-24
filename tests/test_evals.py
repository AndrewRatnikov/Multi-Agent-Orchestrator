"""Tests for evals/run.py: the runner's own logic, with a fake `claude` binary (no model calls).

The fake reads its replies from files named in the environment: FAKE_AGENT_REPLY for a call with
`--agent`, FAKE_JUDGE_REPLY otherwise. FAKE_AGENT_ERROR makes the agent call fail.
"""
import importlib.util
import json
import os
import stat
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("evals_run", os.path.join(ROOT, "evals", "run.py"))
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)
orch = ev.orch

FAKE = """#!{python}
import json, os, sys
argv = sys.argv[1:]
agent = "--agent" in argv
log = os.environ.get("FAKE_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps(argv) + "\\n")
if agent and os.environ.get("FAKE_AGENT_ERROR"):
    print(json.dumps({{"type": "result", "subtype": "error_max_turns", "is_error": True,
                      "result": os.environ["FAKE_AGENT_ERROR"]}}))
    sys.exit(1)
reply = open(os.environ["FAKE_AGENT_REPLY" if agent else "FAKE_JUDGE_REPLY"]).read()
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": reply, "total_cost_usd": 0.01}}))
"""

AMBIGUOUS = """Findings below.

## RESULT
verdict: AMBIGUOUS
findings:
- [vague-term] "the total of recent expenses" — A: 30 days; B: this month; settle: name it
- [unmeasured-quality] "The card loads quickly, even for users" — A: 100 ms; B: no slower; settle: a number
- [vague-term] "Large expenses are highlighted in the card" — A: > €100; B: top 3; settle: the rule
- [conflict] "The total is shown in the user's currency" — A: per-user; B: EUR only; settle: EUR
"""
JUDGE_PASS = 'Looked at it.\n```json\n{"verdict": "PASS", "checks": [{"check": "recent", "pass": true, "evidence": "x"}]}\n```\n'
JUDGE_FAIL = '{"verdict": "FAIL", "checks": [{"check": "currency", "pass": false, "evidence": "not flagged"}]}'


@pytest.fixture
def fake(tmp_path, monkeypatch):
    root = tmp_path / "orch"
    (root / "runs").mkdir(parents=True)
    monkeypatch.setattr(ev, "ROOT", str(root))
    monkeypatch.setattr(orch, "ROOT", str(root))
    binp = tmp_path / "claude"
    binp.write_text(FAKE.format(python=sys.executable))
    binp.chmod(binp.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("CLAUDE_BIN", str(binp))
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "calls.log"))

    class F:
        def replies(self, agent, judge=JUDGE_PASS):
            (tmp_path / "agent.txt").write_text(agent)
            (tmp_path / "judge.txt").write_text(judge)
            monkeypatch.setenv("FAKE_AGENT_REPLY", str(tmp_path / "agent.txt"))
            monkeypatch.setenv("FAKE_JUDGE_REPLY", str(tmp_path / "judge.txt"))

        def calls(self):
            p = tmp_path / "calls.log"
            return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

        def result(self):
            runs = sorted((root / "runs").iterdir())
            assert len(runs) == 1
            return json.loads((runs[0] / "eval" / "result.json").read_text()), runs[0]
    f = F()
    f.root, f.tmp = root, tmp_path
    return f


def test_every_shipped_scenario_loads():
    names = ev.all_scenarios()
    assert len(names) >= 7
    for n in names:
        sc = ev.load_scenario(n)
        assert sc["rubric"], n
        assert sc.get("expect"), n


def test_passing_scenario(fake, capsys):
    fake.replies(AMBIGUOUS)
    assert ev.main(["prd-review-vague"]) == 0
    res, run_dir = fake.result()
    assert res["verdict"] == "PASS" and res["mechanical"] == "PASS" and res["judge"] == "PASS"
    assert run_dir.name.startswith("run_eval_prd-review-vague_")       # guard-writes.sh needs the run_ prefix
    assert (run_dir / "prd.md").exists() and (run_dir / "eval" / "report.md").read_text() == AMBIGUOUS
    agent_call, judge_call = fake.calls()
    assert agent_call[agent_call.index("--agent") + 1] == "orch-prd-reviewer"
    prompt = agent_call[agent_call.index("-p") + 1]
    assert prompt.startswith("ORCHESTRATOR_ROOT: " + str(fake.root)) and "STAGE: orch-prd-reviewer" in prompt
    assert "RUN_ID: " + run_dir.name in prompt
    assert judge_call[judge_call.index("--tools") + 1] == "Read,Grep,Glob" and "--agent" not in judge_call
    repo = agent_call[agent_call.index("--add-dir") + 1]
    assert prompt.count("REPO: " + repo) == 1 and judge_call[judge_call.index("--add-dir") + 1] == repo
    assert "1/1 passed" in capsys.readouterr().out


def test_uncited_finding_fails_mechanically(fake):
    fake.replies(AMBIGUOUS.replace('"the total of recent expenses"', '"the sum of old expenses"'))
    assert ev.main(["prd-review-vague"]) == 1
    res, _ = fake.result()
    assert res["mechanical"] == "FAIL" and res["verdict"] == "FAIL"
    bad = [c for c in res["checks"] if not c["pass"]]
    assert len(bad) == 1 and "sum of old expenses" in bad[0]["detail"]


def test_wrong_verdict_fails(fake):
    fake.replies("## RESULT\nverdict: CLEAR\nfindings:\n- none\n")
    assert ev.main(["prd-review-vague", "--no-judge"]) == 1
    res, _ = fake.result()
    failed = {c["check"] for c in res["checks"] if not c["pass"]}
    assert failed == {"verdict in ['AMBIGUOUS']", "at least 3 findings"}


def test_judge_fail_fails_the_scenario(fake):
    fake.replies(AMBIGUOUS, JUDGE_FAIL)
    assert ev.main(["prd-review-vague"]) == 1
    res, _ = fake.result()
    assert (res["mechanical"], res["judge"], res["verdict"]) == ("PASS", "FAIL", "FAIL")


def test_unparseable_judge_fails(fake):
    fake.replies(AMBIGUOUS, "I think it's fine.")
    assert ev.main(["prd-review-vague"]) == 1
    assert fake.result()[0]["judge"] == "UNPARSEABLE"


def test_no_judge_skips_the_judge_call(fake):
    fake.replies(AMBIGUOUS)
    assert ev.main(["prd-review-vague", "--no-judge"]) == 0
    assert fake.result()[0]["judge"] == "SKIPPED" and len(fake.calls()) == 1


def test_agent_error_is_a_failure(fake, monkeypatch):
    fake.replies(AMBIGUOUS)
    monkeypatch.setenv("FAKE_AGENT_ERROR", "ran out")
    assert ev.main(["prd-review-vague"]) == 1
    res, _ = fake.result()
    assert res["verdict"] == "FAIL" and "error_max_turns" in res["error"]


def test_unchanged_file_check(fake):
    """prd-review-vague expects prd.md untouched; the reviewer has no Write tool, but the check would catch it."""
    fake.replies(AMBIGUOUS)
    orig = ev.mechanical

    def tamper(sc, r, report, before):
        open(r.path("prd.md"), "a").write("edited\n")
        return orig(sc, r, report, before)
    ev.mechanical = tamper
    try:
        assert ev.main(["prd-review-vague", "--no-judge"]) == 1
    finally:
        ev.mechanical = orig
    assert any(c["check"] == "prd.md left as it was" and not c["pass"] for c in fake.result()[0]["checks"])


def test_product_feedback_is_built_from_orch_text():
    sc = ev.load_scenario("product-settles-review-unattended")
    fb = ev.feedback_for(sc)
    assert fb.startswith(orch.PRD_FEEDBACK.split("{findings}")[0])
    assert fb.endswith(orch.PRD_FEEDBACK_UNATTENDED) and '"the total of recent expenses"' in fb


def test_scenario_findings_quote_their_prd():
    """The findings a scenario hands to Product must themselves pass orch.py's citation check."""
    sc = ev.load_scenario("product-settles-review-unattended")
    prd = open(os.path.join(sc["dir"], "run", "prd.md")).read()
    assert all(orch.quotes_source(f, prd) for f in sc["prd_review_findings"])


def test_unknown_scenario_is_a_usage_error(capsys):
    assert ev.main(["no-such-scenario"]) == 2
    assert "no scenario" in capsys.readouterr().err


def test_parse_judge():
    assert ev.parse_judge(JUDGE_PASS)["verdict"] == "PASS"
    assert ev.parse_judge('{"a": 1}\n{"verdict": "FAIL", "checks": []}\ntrailing words')["verdict"] == "FAIL"
    assert ev.parse_judge('{"verdict": "MAYBE"}') is None
    assert ev.parse_judge("") is None


def test_signed_out_claude_stops_at_the_first_scenario(fake, monkeypatch, capsys):
    fake.replies(AMBIGUOUS)
    monkeypatch.setenv("FAKE_AGENT_ERROR", "Failed to authenticate: OAuth session expired and could not be refreshed")
    assert ev.main(["prd-review-vague", "prd-review-clear"]) == 2
    assert len(fake.calls()) == 1
    assert "/login" in capsys.readouterr().err


def test_unreadable_inputs_fail_mechanically(fake):
    fake.replies("The repo could not be read because permission was denied.\n\n" + AMBIGUOUS)
    assert ev.main(["prd-review-vague", "--no-judge"]) == 1
    bad = [c for c in fake.result()[0]["checks"] if not c["pass"]]
    assert [c["check"] for c in bad] == ["agent could read its inputs"]

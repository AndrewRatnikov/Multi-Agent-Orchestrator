#!/usr/bin/env python3
"""evals/run.py: behaviour evals for the pipeline's agents. On demand, not CI.

The unit tests (`pytest`) prove orch.py and the scripts route correctly. They can't tell whether an
agent still *behaves*: whether the PRD reviewer finds the ambiguity, whether the test-reviewer still
rejects a test that asserts nothing, whether Product asks instead of guessing. This runs a real agent
on a fixture and checks what it did, twice over:

  1. Mechanical checks (deterministic): the RESULT block parses and its fields have the expected
     values, findings cite what orch.py would accept, the right files exist or don't, the target repo
     is untouched.
  2. A judge (`claude -p`, read-only) checks the scenario's rubric.md against the agent's report and
     the files it left. Skip with --no-judge.

A scenario passes only when both pass.

Each run costs one agent session plus one judge call, takes minutes and isn't deterministic, so run it
when you change an agent's prompt or a contract it depends on, not on every commit.

Usage:
  python3 evals/run.py                       all scenarios
  python3 evals/run.py prd-review-vague ...  only these
  python3 evals/run.py --list                list the scenarios
  Options: --no-judge, --model M (agent model override), --judge-model M (default sonnet),
           --timeout SECONDS (per claude call, default 900)

Environment: CLAUDE_BIN (default `claude`), ORCH_ROOT (default: this checkout).

A scenario is a folder under evals/scenarios/:
  scenario.json   what to run and what to expect (see evals/README.md)
  rubric.md       what the judge checks
  run/            copied into runs/<run id>/ before the agent starts (prd.md, plan.md, tests/, ...)

The run folder (runs/run_eval_<scenario>_<timestamp>/) is kept for inspection; the agent's report,
the checks and the judge's verdict are in its eval/ folder.

Exit: 0 all passed, 1 a scenario failed, 2 usage error or claude not signed in (stops at the first).
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("ORCH_ROOT") or os.path.dirname(HERE)
SCENARIOS = os.path.join(HERE, "scenarios")
FIXTURES = os.path.join(HERE, "fixtures")
sys.path.insert(0, os.path.join(ROOT, ".claude", "scripts"))
import orch  # noqa: E402

STAGE_OF = {agent: stage for stage, agent in orch.AGENTS.items()}
CITE_CHECKS = {
    "prd-review": lambda r, f: orch.quotes_source(f, orch.read(r.path("prd.md"))),
    "test-reviewer": lambda r, f: orch.cites_test(r, f, orch.prd_criteria_count(orch.read(r.path("prd.md")))),
    "render-reviewer": lambda r, f: orch.cites_screenshot(r, f),
}
CITE_FIELD = {"prd-review": "findings", "test-reviewer": "failures", "render-reviewer": "blocking"}


AUTH_RE = re.compile(r"failed to authenticate|not (logged|signed) in|invalid api key|please run /login", re.I)


class UsageError(Exception):
    pass


class EvalRun:
    """What orch.py's citation checks need from a run: its id, folder and target repo."""

    def __init__(self, run_id, repo):
        self.id, self.repo = run_id, repo
        self.dir = os.path.join(ROOT, "runs", run_id)

    def path(self, *p):
        return os.path.join(self.dir, *p)


def sh(args, cwd=None, timeout=60):
    p = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
    return p.returncode, p.stdout


def claude(args, prompt, cwd, timeout):
    """Run `claude -p` and return (final text, raw json dict). Raises RuntimeError on a failed call."""
    cmd = [os.environ.get("CLAUDE_BIN", "claude"), "-p", prompt, "--output-format", "json",
           "--no-session-persistence"] + args
    try:
        p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError("claude timed out after {}s".format(timeout))
    try:
        data = json.loads(p.stdout)
    except ValueError:
        raise RuntimeError("claude exit {}, no JSON output:\n{}{}".format(p.returncode, p.stdout[-2000:], p.stderr[-2000:]))
    if data.get("is_error") or p.returncode != 0:
        raise RuntimeError("claude exit {} ({}): {}".format(p.returncode, data.get("subtype"), str(data.get("result"))[-2000:]))
    return data.get("result") or "", data


def load_scenario(name):
    d = os.path.join(SCENARIOS, name)
    sj = os.path.join(d, "scenario.json")
    if not os.path.isfile(sj):
        raise UsageError("no scenario {!r} (expected {})".format(name, sj))
    sc = json.load(open(sj))
    for key in ("agent", "repo", "task"):
        if not sc.get(key):
            raise UsageError("{}: scenario.json needs {!r}".format(name, key))
    if sc["agent"] not in STAGE_OF:
        raise UsageError("{}: unknown agent {!r} (one of {})".format(name, sc["agent"], ", ".join(sorted(STAGE_OF))))
    if not os.path.isdir(os.path.join(FIXTURES, sc["repo"])):
        raise UsageError("{}: no fixture repo {!r} under evals/fixtures/".format(name, sc["repo"]))
    sc["name"], sc["dir"] = name, d
    sc["rubric"] = orch.read(os.path.join(d, "rubric.md")).strip()
    return sc


def all_scenarios():
    return sorted(n for n in os.listdir(SCENARIOS) if os.path.isfile(os.path.join(SCENARIOS, n, "scenario.json")))


def setup(sc, tmp):
    """Copy the fixture repo into a throwaway git repo and the scenario's run/ into runs/<run id>/."""
    repo = os.path.join(tmp, "repo")
    shutil.copytree(os.path.join(FIXTURES, sc["repo"]), repo)
    env = dict(os.environ, GIT_AUTHOR_NAME="eval", GIT_AUTHOR_EMAIL="eval@example.invalid",
               GIT_COMMITTER_NAME="eval", GIT_COMMITTER_EMAIL="eval@example.invalid")
    for args in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "fixture"]):
        subprocess.run(args, cwd=repo, check=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    run_id = "run_eval_{}_{}".format(sc["name"], datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    r = EvalRun(run_id, repo)
    src = os.path.join(sc["dir"], "run")
    if os.path.isdir(src):
        shutil.copytree(src, r.dir)
    else:
        os.makedirs(r.dir)
    os.makedirs(r.path("eval"))
    return r


def feedback_for(sc):
    """FEEDBACK as orch.py would write it. `prd_review_findings` builds the PRD-review feedback from orch.py's own
    text, so a scenario can't drift from what the pipeline really sends."""
    if sc.get("prd_review_findings"):
        fb = orch.PRD_FEEDBACK.format(findings="\n".join("- " + f for f in sc["prd_review_findings"]))
        return fb + (orch.PRD_FEEDBACK_UNATTENDED if sc.get("unattended") else "")
    return sc.get("feedback")


def prompt_for(sc, r):
    return orch.spawn_prompt(sc["agent"], r.id, r.repo, sc["task"], answers=sc.get("answers"),
                             feedback=feedback_for(sc), note=sc.get("note"),
                             extra=[tuple(kv) for kv in sc.get("extra", [])])


def snapshot(r, names):
    return {n: orch.read(r.path(n), None) for n in names}


def mechanical(sc, r, report, before):
    """Deterministic checks. Returns [(check, ok, detail)]."""
    checks = []
    res = orch.parse_result(report)
    checks.append(("RESULT block parses", res is not None, "" if res else "no `## RESULT` block in the final report"))
    res = res or {}
    for key, allowed in (sc.get("expect") or {}).items():
        got = orch.as_text(res.get(key)).strip()
        checks.append(("{} in {}".format(key, allowed), got in allowed, "got {!r}".format(got)))
    for key, n in (sc.get("min_items") or {}).items():
        got = len(orch.as_list(res.get(key)))
        checks.append(("at least {} {}".format(n, key), got >= n, "got {}".format(got)))
    for key, n in (sc.get("max_items") or {}).items():
        got = len(orch.as_list(res.get(key)))
        checks.append(("at most {} {}".format(n, key), got <= n, "got {}".format(got)))
    if sc.get("cited"):
        stage = STAGE_OF[sc["agent"]]
        items = orch.as_list(res.get(CITE_FIELD[stage]))
        bad = [i for i in items if not CITE_CHECKS[stage](r, i)]
        checks.append(("every {} cites what orch.py checks".format(CITE_FIELD[stage]), not bad,
                       "uncited: " + "; ".join(bad) if bad else "{} cited".format(len(items))))
    for f in sc.get("files_present", []):
        checks.append(("{} exists".format(f), os.path.exists(r.path(f)), ""))
    for f in sc.get("files_absent", []):
        checks.append(("{} does not exist".format(f), not os.path.exists(r.path(f)), ""))
    for f in sc.get("changed", []):
        checks.append(("{} was rewritten".format(f), orch.read(r.path(f), None) not in (None, before.get(f)), ""))
    for f in sc.get("unchanged", []):
        checks.append(("{} left as it was".format(f), orch.read(r.path(f), None) == before.get(f), ""))
    denied = re.search(r"permission (was )?denied|could not be read|couldn't read|cannot read|can't read", report, re.I)
    if denied and not sc.get("expect_unreadable"):
        checks.append(("agent could read its inputs", False,
                       "the report says: ...{}...".format(report[max(0, denied.start() - 80):denied.end() + 40])))
    if sc.get("repo_unchanged", True):
        _, out = sh(["git", "status", "--porcelain"], cwd=r.repo)
        checks.append(("target repo untouched", not out.strip(), out.strip()))
    return checks


def parse_judge(text):
    """The judge's verdict: the last JSON object in its reply with a PASS/FAIL `verdict`."""
    text = re.sub(r"```(?:json)?", "", text or "")
    dec = json.JSONDecoder()
    for m in reversed(list(re.finditer(r"\{", text))):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("verdict") in ("PASS", "FAIL"):
            return obj
    return None


def judge(sc, r, report, checks, model, timeout):
    template = orch.read(os.path.join(HERE, "judge-prompt.md"))
    prompt = (template.replace("{{SCENARIO}}", sc["name"]).replace("{{AGENT}}", sc["agent"])
              .replace("{{RUN_DIR}}", r.dir).replace("{{REPO}}", r.repo).replace("{{RUBRIC}}", sc["rubric"])
              .replace("{{REPORT}}", report)
              .replace("{{CHECKS}}", "\n".join("- [{}] {}{}".format("x" if ok else " ", c, " ({})".format(d) if d else "")
                                              for c, ok, d in checks)))
    text, _ = claude(["--model", model, "--tools", "Read,Grep,Glob", "--max-turns", "20", "--add-dir", r.repo],
                     prompt, ROOT, timeout)
    return text, parse_judge(text)


def run_scenario(sc, opts):
    tmp = tempfile.mkdtemp(prefix="orch-eval-")
    try:
        r = setup(sc, tmp)
        before = snapshot(r, sc.get("changed", []) + sc.get("unchanged", []))
        # The fixture repo lives in a temp folder outside the project; without --add-dir a headless session can't
        # read it (in real runs settings.local.json grants Read on the target repos).
        args = ["--agent", sc["agent"], "--max-turns", str(sc.get("max_turns", 40)), "--permission-mode", "acceptEdits",
                "--add-dir", r.repo]
        if opts.model:
            args += ["--model", opts.model]
        out = {"scenario": sc["name"], "run_id": r.id, "agent": sc["agent"]}
        try:
            report, raw = claude(args, prompt_for(sc, r), ROOT, opts.timeout)
            out["cost_usd"] = raw.get("total_cost_usd")
        except RuntimeError as e:
            out.update(verdict="FAIL", error=str(e))
            orch.write(r.path("eval", "result.json"), json.dumps(out, indent=2) + "\n")
            return out
        orch.write(r.path("eval", "report.md"), report)
        checks = mechanical(sc, r, report, before)
        out["checks"] = [{"check": c, "pass": ok, "detail": d} for c, ok, d in checks]
        mech_ok = all(ok for _, ok, _ in checks)
        out["mechanical"] = "PASS" if mech_ok else "FAIL"
        if opts.no_judge:
            out["judge"] = "SKIPPED"
        else:
            try:
                text, verdict = judge(sc, r, report, checks, opts.judge_model, opts.timeout)
                orch.write(r.path("eval", "judge.md"), text)
                out["judge"] = verdict["verdict"] if verdict else "UNPARSEABLE"
                out["judge_checks"] = (verdict or {}).get("checks", [])
            except RuntimeError as e:
                out["judge"], out["judge_error"] = "ERROR", str(e)
        out["verdict"] = "PASS" if mech_ok and out["judge"] in ("PASS", "SKIPPED") else "FAIL"
        orch.write(r.path("eval", "result.json"), json.dumps(out, indent=2) + "\n")
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def summary_line(o):
    bits = ["mechanical {}".format(o.get("mechanical", "-")), "judge {}".format(o.get("judge", "-"))]
    if o.get("cost_usd") is not None:
        bits.append("${:.2f}".format(o["cost_usd"]))
    return "{:<5} {:<36} {}  runs/{}/eval/".format(o["verdict"], o["scenario"], ", ".join(bits), o["run_id"])


def main(argv=None):
    ap = argparse.ArgumentParser(description="Behaviour evals for the pipeline's agents.")
    ap.add_argument("scenarios", nargs="*")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--model")
    ap.add_argument("--judge-model", default="sonnet")
    ap.add_argument("--timeout", type=int, default=900)
    opts = ap.parse_args(argv)
    orch.ROOT = ROOT          # orch.py's prompt and citation checks resolve runs/ against the same root
    try:
        names = opts.scenarios or all_scenarios()
        scenarios = [load_scenario(n) for n in names]
    except UsageError as e:
        print("evals: " + str(e), file=sys.stderr)
        return 2
    if opts.list:
        for sc in scenarios:
            print("{:<36} {}".format(sc["name"], sc.get("about", sc["agent"])))
        return 0
    results = []
    for sc in scenarios:
        print("… {} ({})".format(sc["name"], sc["agent"]), flush=True)
        o = run_scenario(sc, opts)
        results.append(o)
        print(summary_line(o), flush=True)
        for c in o.get("checks", []):
            if not c["pass"]:
                print("      ✗ {}{}".format(c["check"], ": " + c["detail"] if c["detail"] else ""))
        for c in o.get("judge_checks", []):
            if isinstance(c, dict) and not c.get("pass", True):
                print("      ✗ judge: {} ({})".format(c.get("check", "?"), c.get("evidence", "")))
        if o.get("error") or o.get("judge_error"):
            print("      ! " + (o.get("error") or o.get("judge_error"))[:500])
        if AUTH_RE.search(o.get("error") or o.get("judge_error") or ""):
            print("\nclaude isn't signed in, so no scenario can run. Run `claude`, then /login, check that "
                  "`claude -p \"say ok\"` answers, and run the evals again.", file=sys.stderr)
            return 2
    failed = [o for o in results if o["verdict"] != "PASS"]
    print("\n{}/{} passed".format(len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

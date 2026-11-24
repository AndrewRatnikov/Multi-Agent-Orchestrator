# Agent behaviour evals (on demand, not CI)

`pytest` proves that `orch.py` and the scripts route correctly, with every agent played by a fake. It
can't tell whether a real agent still **behaves**: whether the PRD reviewer finds the ambiguity, whether
the test-reviewer still rejects a test that asserts nothing, whether Product asks instead of guessing.
These evals run a real agent on a fixture and check what it did.

Each run costs one agent session plus one judge call, takes a few minutes and isn't deterministic.
Run the evals when you change an agent's prompt, a RESULT contract, or a model; not on every commit.

```bash
python3 evals/run.py                          # all scenarios
python3 evals/run.py test-reviewer-weak       # one (or several) by name
python3 evals/run.py --list
python3 evals/run.py --no-judge               # mechanical checks only (cheaper)
python3 evals/run.py --model sonnet           # try an agent on another model
```

Exit code 1 when any scenario fails. Run from the orchestrator root, with `claude` on PATH and signed in
(`CLAUDE_BIN` points elsewhere).

## How a scenario runs

1. The fixture repo (`fixtures/<repo>/`) is copied to a temp folder and committed: the target repo.
2. The scenario's `run/` folder is copied to `runs/run_eval_<scenario>_<timestamp>/`.
3. The agent runs as the main session (`claude -p --agent <agent>`), so its tools, model and write-guard
   hook are the ones the pipeline uses. Its prompt comes from `orch.spawn_prompt`, the same function
   that builds every pipeline spawn.
4. **Mechanical checks**: the RESULT block parses; fields have the expected values; findings cite what
   `orch.py` would accept (the same `quotes_source` / `cites_test` / `cites_screenshot` functions); files
   exist, don't exist, changed or stayed the same; the target repo is untouched.
5. **Judge**: a read-only `claude -p` (Sonnet by default) checks `rubric.md` against the report and the
   files, and answers `{"verdict": "PASS" | "FAIL", "checks": [...]}`.

A scenario passes only when both pass. The run folder is kept; `eval/` in it holds the agent's report,
`result.json` (every check) and the judge's reply.

## Scenarios

| Scenario | What it proves |
|---|---|
| `prd-review-vague` | The PRD reviewer finds the build-changing ambiguities (period, speed, threshold, a currency that conflicts with CLAUDE.md), each quoted verbatim, ≤ 7 |
| `prd-review-clear` | It doesn't invent ambiguity in a precise PRD (false-positive guard) |
| `prd-review-blocked` | It returns BLOCKED, not a guess, when prd.md is missing |
| `test-reviewer-weak` | The test-reviewer fails tests that assert nothing and miss criteria, citing file and criterion |
| `test-reviewer-solid` | It passes good tests (false-positive guard) |
| `product-settles-review-unattended` | Product resolves PRD-review findings with stated assumptions when no one can answer, without adding scope |
| `product-vague-task-asks` | Product asks (NEEDS_INPUT) instead of writing a PRD for a vague task |

## Adding a scenario

`scenarios/<name>/scenario.json`:

```json
{
  "about": "one line for --list",
  "agent": "orch-test-reviewer",
  "repo": "repo-budget",
  "task": "the TASK line the agent gets",
  "note": "optional NOTE line",
  "answers": "optional ANSWERS line",
  "feedback": "optional FEEDBACK line",
  "prd_review_findings": ["optional: builds FEEDBACK from orch.PRD_FEEDBACK"],
  "unattended": false,
  "expect": {"verdict": ["FAIL"]},
  "min_items": {"failures": 2},
  "max_items": {"findings": 7},
  "cited": true,
  "files_present": [], "files_absent": [], "changed": [], "unchanged": [],
  "repo_unchanged": true,
  "max_turns": 40
}
```

plus `rubric.md` (numbered items the judge checks one by one) and `run/` (what's already in the run
folder when the agent starts). Every scenario should have a twin that guards the other direction: a
"should fail" next to a "should pass", so a prompt change can't buy recall with false alarms.

`tests/test_evals.py` covers the runner itself with a fake `claude` binary, at no cost.

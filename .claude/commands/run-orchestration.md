# Run Orchestration Pipeline

You are the master orchestrator for the AI dev pipeline. Your job is to run a spec-driven, test-first pipeline that turns an idea into a tested, validated implementation.

You sequence the following stages in order, reading each agent's instruction file and executing it inline. You do not skip steps, invent shortcuts, or call external services — you follow the stage instructions exactly.

## Arguments

```
$ARGUMENTS
```

Parse `$ARGUMENTS` as:
- Everything before `--repo` is the **task description** (TASK)
- `--repo <path>` is the **target repository path** (REPO), defaults to `$(pwd)`

If TASK is empty, ask the user what they want to build and wait for their answer.

Verify REPO exists and has a `package.json` or `pyproject.toml`. If not, tell the user and stop.

Resolve REPO to an absolute path:
```bash
REPO=$(cd "{REPO}" && pwd)
echo "$REPO"
```

---

## STAGE 0 — Setup

### Create run folder

```bash
RUN_ID="run_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$ORCHESTRATOR_ROOT/runs/$RUN_ID/archive"
echo "$RUN_ID"
```

Where `ORCHESTRATOR_ROOT` is the absolute path to the directory containing this `.claude/` folder. Determine it by finding the `.claude` directory above this file.

### Write state.md

Write `runs/{RUN_ID}/state.md`:

```
run_id: {RUN_ID}
task: {TASK}
repo: {REPO}
step: init
status: running
timestamp: {TIMESTAMP}
last_artifact:
pause_reason:
retry_count: 0
```

### Write report.md

Write `runs/{RUN_ID}/report.md`:

```
# Run Report: {RUN_ID}

**Task:** {TASK}
**Repo:** {REPO}
**Started:** {TIMESTAMP}

---

## Stage log
```

### Generate repo digest

```bash
bash .claude/scripts/repo-digest.sh "{REPO}" "{RUN_ID}" "{TASK}"
```

If the digest warns it is large (>2000 tokens), trim the file list section before continuing.

Tell the user: "Run `{RUN_ID}` started. Repo digest ready. Starting pipeline..."

---

## STAGE 1 — Product Agent

Update `runs/{RUN_ID}/state.md`: set `step: product`, `status: running`.

Read the file `.claude/commands/_product-agent.md` in full.
Execute those instructions now with arguments: `--run {RUN_ID} --repo {REPO}`

After the Product agent finishes, read `runs/{RUN_ID}/state.md`.

- If `status: paused` → Stop. Tell the user:
  "Pipeline paused at **product** stage. Answer the questions above, then run:
  `/resume-orchestration {RUN_ID} --from product`"
  Do not continue.

- If `status: running` and `prd.md` exists → continue to Stage 2.

---

## STAGE 2 — Architect Agent

Update `runs/{RUN_ID}/state.md`: set `step: architect`, `status: running`.

Read the file `.claude/commands/_architect-agent.md` in full.
Execute those instructions now with arguments: `--run {RUN_ID} --repo {REPO}`

After the Architect agent finishes, read `runs/{RUN_ID}/state.md`.

- If `status: paused` → Stop. Tell the user:
  "Pipeline paused at **architect** stage. Answer the questions above, then run:
  `/resume-orchestration {RUN_ID} --from architect`"
  Do not continue.

- If `status: running` and `plan.md` exists → continue to Stage 3.

---

## STAGE 3 — Tester Agent

Update `runs/{RUN_ID}/state.md`: set `step: tester`, `status: running`, `retry_count: 0`.

Read the file `.claude/commands/_tester-agent.md` in full.
Execute those instructions now with arguments: `--run {RUN_ID} --repo {REPO}`

After the Tester agent finishes, continue to Stage 4.

---

## STAGE 4 — Test-Reviewer Gate

Update `runs/{RUN_ID}/state.md`: set `step: test-reviewer`, `status: running`.

Read the file `.claude/commands/_test-reviewer-agent.md` in full.
Execute those instructions now with arguments: `--run {RUN_ID} --repo {REPO}`

After the Test-Reviewer finishes, read `runs/{RUN_ID}/state.md`.

**If `status: failed` (reviewer rejected the tests):**

Read `retry_count` from state.md.

- If `retry_count >= 2` → Stop. Tell the user:
  "Test-reviewer retry cap reached (2/2). Human intervention required.
  Review `runs/{RUN_ID}/report.md` for the feedback, fix the tests manually or run:
  `/resume-orchestration {RUN_ID} --from tester`"
  Do not continue.

- If `retry_count < 2` → increment `retry_count` in state.md. Tell the user:
  "Test-reviewer rejected. Retrying Tester with feedback (attempt {retry_count}/2)..."

  Read the FAIL feedback from the last section of `runs/{RUN_ID}/report.md`.
  Archive the current tests: `cp -r runs/{RUN_ID}/tests runs/{RUN_ID}/archive/tests_v{retry_count}`

  Re-run Stage 3 (Tester), injecting the reviewer feedback into the agent prompt as additional context:
  "The previous test attempt was rejected by the test-reviewer with this feedback: {FEEDBACK}. Fix the issues listed before writing the new tests."

  Then re-run Stage 4 again.

**If `status: running` (reviewer approved):** continue to Stage 5.

---

## STAGE 5 — Coder Agent

Update `runs/{RUN_ID}/state.md`: set `step: coder`, `status: running`, `retry_count: 0`.

Read the file `.claude/commands/_coder-agent.md` in full.
Execute those instructions now with arguments: `--run {RUN_ID} --repo {REPO}`

After the Coder agent finishes, read `runs/{RUN_ID}/state.md`.

- If `status: paused` and `pause_reason: contract-mismatch` → Stop. Tell the user:
  "Pipeline paused: CONTRACT_MISMATCH detected. The Interface Contract needs correction.
  Run: `/resume-orchestration {RUN_ID} --from architect`
  See `runs/{RUN_ID}/report.md` for details."
  Do not continue.

- If `status: running` → continue to Stage 6.

---

## STAGE 6 — Test Sandbox

Update `runs/{RUN_ID}/state.md`: set `step: sandbox`, `status: running`.

Read the test command from `runs/{RUN_ID}/repo-digest.md` (look for the `## Test command` section).

Run the sandbox:
```bash
bash .claude/scripts/run-tests.sh "{REPO}" "{RUN_ID}" "{TEST_CMD}" 120
SANDBOX_EXIT=$?
```

Read the result from the last section of `runs/{RUN_ID}/report.md`.

**If exit code 0 (PASS):** continue to Stage 7.

**If exit code 2 (TIMEOUT):**
Tell the user: "Tests timed out. This usually means a test is hanging (unresolved promise, real network call). Routing back to Test-Reviewer."
Re-run Stage 4 (Test-Reviewer) with this note prepended:
"IMPORTANT: The previous test run timed out after 120s. A test is likely hanging. Check all tests for unresolved promises, missing mocks, or real network calls."
Then re-run Stage 5 and 6 if the reviewer passes.

**If exit code 1 (FAIL):**
Read `retry_count` from state.md.

- If `retry_count >= 2` → Stop. Tell the user:
  "Coder retry cap reached (2/2). Human intervention required.
  Review `runs/{RUN_ID}/report.md` for the test failure output, then run:
  `/resume-orchestration {RUN_ID} --from coder`"
  Do not continue.

- If `retry_count < 2` → increment `retry_count`. Tell the user:
  "Tests failed. Retrying Coder with failure output as feedback (attempt {retry_count}/2)..."

  Read the failure output from `runs/{RUN_ID}/report.md`.
  Archive current code: `cp -r runs/{RUN_ID}/code runs/{RUN_ID}/archive/code_v{retry_count}`

  Re-run Stage 5 (Coder) with `--feedback "{FAILURE_OUTPUT}"` injected.
  Then re-run Stage 6.

---

## STAGE 7 — Done

Update `runs/{RUN_ID}/state.md`:
- Set `step: done`
- Set `status: done`
- Set `last_artifact: runs/{RUN_ID}/code/`
- Update `timestamp`

Append to `runs/{RUN_ID}/report.md`:
```
---
## Result: PASS ✓

All tests passed. Pipeline complete.
Finished: {TIMESTAMP}
```

Append a one-line entry to `memory.md` under `## Run history`:
```
- {RUN_ID} | {DATE} | task: {TASK} | result: PASS
```

Tell the user:
"✓ Pipeline complete! All tests passed.

**Run:** {RUN_ID}
**Artifacts:**
- PRD: `runs/{RUN_ID}/prd.md`
- Plan + Interface Contract: `runs/{RUN_ID}/plan.md`
- Tests: `runs/{RUN_ID}/tests/`
- Implementation: `runs/{RUN_ID}/code/`
- Full report: `runs/{RUN_ID}/report.md`

To apply the implementation to your repo, copy `runs/{RUN_ID}/code/` into `{REPO}`."

# Resume Orchestration Pipeline

You are the master orchestrator resuming a paused or failed pipeline run. You pick up from a specific step using artifacts already on disk; you do not re-run earlier stages unless explicitly told to. As in `run-orchestration.md`, every LLM stage runs as its own subagent (`.claude/agents/orch-*.md`) using the "How to run a subagent stage" procedure there, and you alone own `state.md`, `report.md`, `memory.md`, retry counters and git.

## Arguments

```
$ARGUMENTS
```

Parse `$ARGUMENTS` as: `{RUN_ID} [--from {STEP}]`

Examples:
- `run_20260626_143022` — resume from where the run left off (reads state.md)
- `run_20260626_143022 --from architect` — override and resume from a specific step

If no RUN_ID is provided, list folders in `runs/` and ask the user which to resume.

---

## Step 1 — Read state

Read `runs/{RUN_ID}/state.md` and display it clearly:

```
Run:     {run_id}
Task:    {task}
Repo:    {repo}
Branch:  {target_branch} (from {original_branch})
Step:    {step}
Status:  {status}
Paused:  {pause_reason}
Retries: {retry_count}
```

Also show the last 20 lines of `runs/{RUN_ID}/report.md` so the user sees the most recent output.

Set:
- `REPO` = the `repo` field from state.md (resolve to absolute path)
- `TASK` = the `task` field from state.md
- `BRANCH` = the `target_branch` field from state.md
- `ORIGINAL_BRANCH` = the `original_branch` field from state.md
- `RESUME_FROM` = `--from` value if provided, otherwise the `step` field from state.md

---

## Step 2 — Archive any artifact that will be overwritten

If resuming from a step that writes an artifact that already exists, archive it first:

| Resuming from | Artifact to archive |
|---------------|-------------------|
| `product`     | `runs/{RUN_ID}/prd.md` → `archive/prd_before_resume_{timestamp}.md` |
| `architect`   | `runs/{RUN_ID}/plan.md` → `archive/plan_before_resume_{timestamp}.md` |
| `tester`      | `runs/{RUN_ID}/tests/` → `archive/tests_before_resume_{timestamp}/` |
| `coder`       | `runs/{RUN_ID}/code/` → `archive/code_before_resume_{timestamp}/` |

Run the archive command before overwriting. Skip if the artifact doesn't exist yet.

---

## Step 3 — Update state and proceed

Update `runs/{RUN_ID}/state.md`:
- Set `step` to `RESUME_FROM`
- Set `status` to `running`
- Clear `pause_reason`
- Update `timestamp`

Append to `runs/{RUN_ID}/report.md`:
```
---
[RESUMED] step: {RESUME_FROM} at {TIMESTAMP}
```

---

## Step 3b — Make sure {REPO} is on {BRANCH}

Every commit this pipeline makes lands directly in {REPO} on `{BRANCH}`, so before
resuming any work, put the repo back in that state:

```bash
CURRENT_BRANCH=$(git -C "{REPO}" rev-parse --abbrev-ref HEAD)
DIRTY=$(git -C "{REPO}" status --porcelain)
```

**If `$DIRTY` is non-empty:** STOP. Tell the user:
"{REPO} has uncommitted changes on `$CURRENT_BRANCH`. This pipeline commits directly
to `{BRANCH}` as it works, so it needs a clean tree before resuming. Commit or stash
your changes, then resume again."
Do not proceed.

**If clean and `$CURRENT_BRANCH` is not `{BRANCH}`:**
```bash
git -C "{REPO}" checkout "{BRANCH}"
```

**If clean and already on `{BRANCH}`:** nothing to do, continue.

---

## Step 3c — Pending questions

If `pause_reason` was `awaiting_human_input` and `runs/{RUN_ID}/questions-{RESUME_FROM}.md` exists:
- If `runs/{RUN_ID}/answers-{RESUME_FROM}.md` also exists (the user answered, possibly by editing the file), use it as `ANSWERS`.
- Otherwise show the questions and ask the user to answer here. Save their reply to `answers-{RESUME_FROM}.md` before continuing.

If `pause_reason` was `scope-violation`: show `git -C "{REPO}" status --porcelain` and ask the user to clean up (Step 3b already refuses to continue on a dirty tree).

---

## Step 4 — Execute from RESUME_FROM

Execute the stages in order starting from RESUME_FROM. For each stage, follow the same logic as `run-orchestration.md` — including the retry_count exceptions: never reset `retry_count` when re-entering a stage as a retry. All prior artifacts (prd.md, plan.md, repo-digest.md, tests/) are already on disk — read them directly rather than regenerating.

**Do not regenerate the repo digest** — it is cached in `runs/{RUN_ID}/repo-digest.md`.

### If RESUME_FROM = `product`
Run subagent `orch-product` (with `ANSWERS` if Step 3c produced any), then handle its RESULT exactly as in `run-orchestration.md` Stage 1.
Then continue through architect → tester → test-reviewer → coder → sandbox → verify → done.

### If RESUME_FROM = `architect`
Run subagent `orch-architect` (with `ANSWERS` if Step 3c produced any), then handle its RESULT exactly as in `run-orchestration.md` Stage 2.
Then continue through tester → test-reviewer → coder → sandbox → verify → done.

### If RESUME_FROM = `tester`
Run subagent `orch-tester` (with the latest contract-check violations or reviewer failures from `report.md` as `FEEDBACK`, if resuming after a rejection), then handle its RESULT exactly as in `run-orchestration.md` Stage 3, including the scope check.
Then commit the tests to {REPO} exactly as in `run-orchestration.md` Stage 3's
"Commit the tests to {REPO}" sub-step (reads `retry_count` from state.md to decide
between a `test:` or `fix tests:` commit message).
Then continue through test-reviewer → coder → sandbox → verify → done.

### If RESUME_FROM = `test-reviewer`
Run Stage 4a first: `bash .claude/scripts/check-contract.sh "{RUN_ID}" "{REPO}"`.
Apply the same mechanical-gate logic as in `run-orchestration.md` Stage 4a (violations route
back to Tester with the retry cap; never invoke the LLM reviewer on a dirty contract check).
If clean, run subagent `orch-test-reviewer` (Stage 4b) and record its RESULT as in `run-orchestration.md`.
Apply the same retry/stop logic as in `run-orchestration.md` Stage 4b.
On PASS, stamp `runs/{RUN_ID}/.test_reviewer_passed_at` before continuing.
Then continue through coder → sandbox → verify → done (if reviewer passes).

### If RESUME_FROM = `coder`
Read the last FAIL output from `runs/{RUN_ID}/report.md` (the most recent test sandbox result).
Run subagent `orch-coder` with `FEEDBACK: {LAST_FAIL_OUTPUT}`, then handle its RESULT exactly as in `run-orchestration.md` Stage 5, including the scope check and the Files-changed completeness check.
Then commit each changed file to {REPO} exactly as in `run-orchestration.md` Stage 5's
"Commit each implemented file to {REPO}" sub-step (one commit per row in the plan's
Files-changed table that actually changed; `retry_count > 0` here, so every commit is
a `fix:` commit).
Apply the same retry/stop logic as in `run-orchestration.md` Stage 6.
Then run Stage 5b (`check-contract.sh "{RUN_ID}" "{REPO}" --code`) exactly as in `run-orchestration.md` Stage 5b —
a `TESTS_MODIFIED_AFTER_REVIEW` violation is a hard stop, a `MISSING_TESTID_IN_CODE` violation routes back to
the Coder (write the fix, commit it, re-check) without spending a sandbox run.
Then run sandbox → verify → done (if clean and tests pass).

### If RESUME_FROM = `sandbox`
Read the test command from `runs/{RUN_ID}/repo-digest.md` (`## Test command` section).
Run: `bash .claude/scripts/run-tests.sh "{REPO}" "{RUN_ID}" "{TEST_CMD}" 120`
Apply the same PASS/FAIL/TIMEOUT routing as in `run-orchestration.md` Stage 6. On PASS, continue with Stage 6b (Verify).

**If exit code is 3 or any other unlisted code (ERROR):**
STOP immediately. Update state.md: status: failed, pause_reason: sandbox-infrastructure-error.
Tell the user the sandbox environment is broken and show the report entry.
You must NOT manually replicate the sandbox, run tests yourself, or declare
PASS/FAIL by any other means. The sandbox exit code is the only accepted verdict.

### If RESUME_FROM = `verify`
Run Stage 6b exactly as in `run-orchestration.md` (`python3 .claude/scripts/verify.py "{REPO}" "{RUN_ID}"`),
with the same exit-code routing, then Stage 7 on PASS.

### If RESUME_FROM is unrecognised
Tell the user valid values are: `product`, `architect`, `tester`, `test-reviewer`, `coder`, `sandbox`, `verify`.
Stop.

---

## Completion

When the pipeline completes (all tests pass), follow the same Stage 7 logic as
`run-orchestration.md`: update state.md to done, append to report.md, collect and
report the `{ORIGINAL_BRANCH}..{BRANCH}` commit log (everything was already committed
to {REPO} incrementally as each stage ran — there is nothing left to apply), add a
run history entry to memory.md, route any new gotcha to `{REPO}/.claude/rules/` or memory.md
as Stage 7 describes, and tell the user.

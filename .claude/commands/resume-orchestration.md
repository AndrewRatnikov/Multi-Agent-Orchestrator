# Resume Orchestration Pipeline

You are the master orchestrator resuming a paused or failed pipeline run. You pick up from a specific step using artifacts already on disk — you do not re-run earlier stages unless explicitly told to.

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
Step:    {step}
Status:  {status}
Paused:  {pause_reason}
Retries: {retry_count}
```

Also show the last 20 lines of `runs/{RUN_ID}/report.md` so the user sees the most recent output.

Set:
- `REPO` = the `repo` field from state.md (resolve to absolute path)
- `TASK` = the `task` field from state.md
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

## Step 4 — Execute from RESUME_FROM

Execute the stages in order starting from RESUME_FROM. For each stage, follow the same logic as `run-orchestration.md` — including the retry_count exceptions: never reset `retry_count` when re-entering a stage as a retry. All prior artifacts (prd.md, plan.md, repo-digest.md, tests/) are already on disk — read them directly rather than regenerating.

**Do not regenerate the repo digest** — it is cached in `runs/{RUN_ID}/repo-digest.md`.

### If RESUME_FROM = `product`
Read `.claude/commands/_product-agent.md` and execute with `--run {RUN_ID} --repo {REPO}`.
Then continue through architect → tester → test-reviewer → coder → sandbox → done.

### If RESUME_FROM = `architect`
Read `.claude/commands/_architect-agent.md` and execute with `--run {RUN_ID} --repo {REPO}`.
Then continue through tester → test-reviewer → coder → sandbox → done.

### If RESUME_FROM = `tester`
Read `.claude/commands/_tester-agent.md` and execute with `--run {RUN_ID} --repo {REPO}`.
Then continue through test-reviewer → coder → sandbox → done.

### If RESUME_FROM = `test-reviewer`
Run Stage 4a first: `bash .claude/scripts/check-contract.sh "{RUN_ID}" "{REPO}"`.
Apply the same mechanical-gate logic as in `run-orchestration.md` Stage 4a (violations route
back to Tester with the retry cap; never invoke the LLM reviewer on a dirty contract check).
If clean, read `.claude/commands/_test-reviewer-agent.md` and execute with `--run {RUN_ID} --repo {REPO}` (Stage 4b).
Apply the same retry/stop logic as in `run-orchestration.md` Stage 4b.
On PASS, stamp `runs/{RUN_ID}/.test_reviewer_passed_at` before continuing.
Then continue through coder → sandbox → done (if reviewer passes).

### If RESUME_FROM = `coder`
Read the last FAIL output from `runs/{RUN_ID}/report.md` (the most recent test sandbox result).
Read `.claude/commands/_coder-agent.md` and execute with `--run {RUN_ID} --repo {REPO} --feedback "{LAST_FAIL_OUTPUT}"`.
Apply the same retry/stop logic as in `run-orchestration.md` Stage 6.
Then run Stage 5b (`check-contract.sh "{RUN_ID}" "{REPO}" --code`) exactly as in `run-orchestration.md` Stage 5b —
a `TESTS_MODIFIED_AFTER_REVIEW` violation is a hard stop, a `MISSING_TESTID_IN_CODE` violation routes back to
the Coder without spending a sandbox run.
Then run sandbox → done (if clean and tests pass).

### If RESUME_FROM = `sandbox`
Read the test command from `runs/{RUN_ID}/repo-digest.md` (`## Test command` section).
Run: `bash .claude/scripts/run-tests.sh "{REPO}" "{RUN_ID}" "{TEST_CMD}" 120`
Apply the same PASS/FAIL/TIMEOUT routing as in `run-orchestration.md` Stage 6.

**If exit code is 3 or any other unlisted code (ERROR):**
STOP immediately. Update state.md: status: failed, pause_reason: sandbox-infrastructure-error.
Tell the user the sandbox environment is broken and show the report entry.
You must NOT manually replicate the sandbox, run tests yourself, or declare
PASS/FAIL by any other means. The sandbox exit code is the only accepted verdict.

### If RESUME_FROM is unrecognised
Tell the user valid values are: `product`, `architect`, `tester`, `test-reviewer`, `coder`, `sandbox`.
Stop.

---

## Completion

When the pipeline completes (all tests pass), follow the same Stage 7 logic as `run-orchestration.md`:
update state.md to done, append to report.md, run Stage 7a to apply the result onto a new
`orchestrator/{RUN_ID}` branch in {REPO} (skipping if {REPO} has uncommitted changes),
add a run history entry to memory.md, and tell the user.

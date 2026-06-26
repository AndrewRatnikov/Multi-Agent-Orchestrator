# Run Orchestration Pipeline

You are the master orchestrator for the AI dev pipeline. Your job is to run a spec-driven, test-first pipeline that turns an idea into a tested, validated implementation.

## Arguments

The user has provided the following task/idea:

```
$ARGUMENTS
```

If `$ARGUMENTS` is empty, ask the user to describe the feature, bug fix, or task they want to implement, then wait for their response before proceeding.

## Step 1 — Create the run folder

Generate a run ID using the current timestamp in the format `run_YYYYMMDD_HHMMSS`.

Run this bash command to create the run folder structure:

```bash
RUN_ID="run_$(date +%Y%m%d_%H%M%S)"
mkdir -p "runs/$RUN_ID/archive"
echo "$RUN_ID"
```

## Step 2 — Write state.md

Write the following file to `runs/{RUN_ID}/state.md`, replacing placeholders with actual values:

```
run_id: {RUN_ID}
task: {TASK}
step: init
status: running
timestamp: {TIMESTAMP}
last_artifact:
pause_reason:
retry_count: 0
```

Valid `step` values (in order): `init` → `product` → `architect` → `tester` → `test-reviewer` → `coder` → `done`
Valid `status` values: `running` | `paused` | `failed` | `done`

## Step 3 — Write report.md

Write `runs/{RUN_ID}/report.md` with this header:

```
# Run Report: {RUN_ID}

**Task:** {TASK}
**Started:** {TIMESTAMP}

---

## Stage log
```

## Step 4 — Confirm and proceed

Tell the user:
- The run ID (e.g. `run_20260626_143022`)
- That the run folder has been created at `runs/{RUN_ID}/`
- That you are now starting the pipeline

Then update `state.md` to set `step: product` and proceed to the Product agent stage.

> **Note (Phase 1 skeleton):** Pipeline stages are not yet wired. For now, stop here and confirm the run folder and state.md were created. Print the contents of state.md so the user can verify.

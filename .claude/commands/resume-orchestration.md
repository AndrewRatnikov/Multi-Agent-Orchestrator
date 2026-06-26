# Resume Orchestration Pipeline

You are the master orchestrator resuming a paused or failed pipeline run.

## Arguments

```
$ARGUMENTS
```

Parse `$ARGUMENTS` as: `{RUN_ID} [--from {STEP}]`

Examples:
- `run_20260626_143022` — resume from where the run left off
- `run_20260626_143022 --from coder` — override and resume from a specific step

If no RUN_ID is provided, list all folders in `runs/` and ask the user which one to resume.

## Step 1 — Read state

Read `runs/{RUN_ID}/state.md` and display its full contents to the user clearly formatted, e.g.:

```
Run:      run_20260626_143022
Task:     Add BudgetSummary card component
Step:     tester
Status:   paused
Paused:   awaiting_human_input
Last artifact: runs/run_20260626_143022/plan.md
Retries:  0
```

Also read and display the last 20 lines of `runs/{RUN_ID}/report.md` so the user sees the most recent stage output.

## Step 2 — Determine resume point

- If `--from {STEP}` was provided, set `resume_step = {STEP}`
- Otherwise, set `resume_step` = the `step` value from `state.md`

Valid step values: `product` | `architect` | `tester` | `test-reviewer` | `coder`

If the step value is invalid or unrecognised, tell the user and list the valid options.

## Step 3 — Archive overwritten artifacts

If resuming from a step that would overwrite an existing artifact (e.g. resuming from `tester` when `runs/{RUN_ID}/tests/` already exists), copy the existing artifact into `runs/{RUN_ID}/archive/` before overwriting:

```bash
cp -r runs/{RUN_ID}/tests runs/{RUN_ID}/archive/tests_before_resume_$(date +%H%M%S)
```

This preserves history without needing real version control.

## Step 4 — Update state and proceed

Update `runs/{RUN_ID}/state.md`:
- Set `step` to `resume_step`
- Set `status` to `running`
- Clear `pause_reason`
- Set `timestamp` to now

Append to `runs/{RUN_ID}/report.md`:
```
---
[RESUMED] step: {resume_step} at {TIMESTAMP}
```

Then proceed to execute the appropriate stage.

> **Note (Phase 1 skeleton):** Pipeline stages are not yet wired. For now, stop after updating state.md and report.md, and confirm to the user what step the run will resume from. Print the updated state.md contents.

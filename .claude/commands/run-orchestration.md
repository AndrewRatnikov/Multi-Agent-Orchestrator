# Run Orchestration Pipeline

You are the master orchestrator for the AI dev pipeline. Your job is to run a spec-driven, test-first pipeline that turns an idea into a tested, validated implementation.

## Arguments

```
$ARGUMENTS
```

Parse `$ARGUMENTS` as follows:
- Everything before `--repo` is the **task description**
- `--repo <path>` is the **target repository path** (optional, defaults to current directory)

Examples:
```
/run-orchestration add a BudgetSummary card component --repo ~/code/personal-finance-tracker
/run-orchestration fix the login redirect bug --repo /Users/andrew/projects/my-app
/run-orchestration add dark mode toggle
```

If the task description is empty, ask the user to describe the feature, bug fix, or task, then wait for their response.

If `--repo` is not provided, use `$(pwd)` as the repo path and inform the user.

Store both values — you will use them throughout all subsequent steps:
- `TASK` = the task description
- `REPO` = the resolved absolute path to the target repository

Verify the repo path exists and contains a recognisable project (has `package.json`, `pyproject.toml`, or similar). If not, tell the user and stop.

## Step 1 — Create the run folder

Run:

```bash
RUN_ID="run_$(date +%Y%m%d_%H%M%S)"
mkdir -p "/path/to/AI-Orchestrator/runs/$RUN_ID/archive"
echo "$RUN_ID"
```

Replace `/path/to/AI-Orchestrator` with the actual absolute path of the orchestrator project (the directory containing this `.claude/` folder). Runs are always stored here, not inside the target repo.

## Step 2 — Write state.md

Write `runs/{RUN_ID}/state.md` inside the orchestrator project:

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

Valid `step` values (in order): `init` → `product` → `architect` → `tester` → `test-reviewer` → `coder` → `done`
Valid `status` values: `running` | `paused` | `failed` | `done`

## Step 3 — Write report.md

Write `runs/{RUN_ID}/report.md`:

```
# Run Report: {RUN_ID}

**Task:** {TASK}
**Repo:** {REPO}
**Started:** {TIMESTAMP}

---

## Stage log
```

## Step 4 — Generate repo digest

```bash
bash .claude/scripts/repo-digest.sh "{REPO}" "{RUN_ID}" "{TASK}"
```

The script writes `runs/{RUN_ID}/repo-digest.md` and prints a token estimate.
If it warns the digest is large (>2000 tokens), read the file and trim the largest section before continuing.
The script skips generation if the digest already exists (cache hit on resume).

## Step 5 — Confirm and proceed

Tell the user:
- Run ID
- Target repo path
- Repo digest token estimate
- That you are now starting the pipeline with the Product agent

Update `state.md` to set `step: product`, then proceed to the Product agent stage.

> **Note (Phase 1/2 skeleton):** Pipeline agent stages are not yet wired. For now, stop here and confirm to the user that the run folder, state.md, and repo-digest.md were created successfully. Print the contents of state.md and the first 30 lines of repo-digest.md so they can verify.

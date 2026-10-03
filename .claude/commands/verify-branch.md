# Verify a branch

Run the pipeline's Verify stage on any branch, without a pipeline run: for changes made by
hand or in a plain Claude session (dependency updates, config changes, quick fixes).
Verify checks the **committed** branch the way CI and the deploy will (frozen-lockfile
install, then every step in the repo's `.claude/verify.json`: lint, typecheck, builds, all
test suites, migrations and e2e on a throwaway database). Failing steps are re-run on the
base branch, so problems that already exist there are reported as pre-existing.

You only run the script and report what it says. Never fix anything, and never run checks
yourself instead of the script: its exit code is the verdict.

## Arguments

```
$ARGUMENTS
```

Parse as: `[BRANCH] [--base BASE] [--repo PATH]`
- `--repo`: the target repo (absolute path, `~` allowed). If missing, ask the user which repo.
- `BRANCH`: the branch to verify. Default: the repo's currently checked-out branch.
- `--base`: what to compare against. Default: the repo's default branch:
  ```bash
  BASE=$(git -C "$REPO" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')
  [ -z "$BASE" ] && BASE=$(git -C "$REPO" rev-parse --verify --quiet main >/dev/null && echo main || echo master)
  ```

## Steps

1. **Check the inputs.**
   ```bash
   REPO=$(cd "{REPO}" && pwd)
   git -C "$REPO" rev-parse --verify --quiet "{BRANCH}" && git -C "$REPO" rev-parse --verify --quiet "{BASE}"
   git -C "$REPO" --no-optional-locks status --porcelain
   ```
   - If either ref doesn't exist, stop and say which.
   - If `BRANCH` equals `BASE`, stop: there's nothing to compare.
   - If the status output is non-empty **and** `BRANCH` is the checked-out branch, tell the
     user Verify only sees committed work, list the uncommitted files, and ask whether to
     continue anyway or commit first.

2. **Create a verify-only run folder.**
   ```bash
   RUN_ID="verify_$(date +%Y%m%d_%H%M%S)"
   mkdir -p "runs/$RUN_ID"
   printf 'run_id: %s\ntask: verify-branch %s vs %s\nrepo: %s\noriginal_branch: %s\ntarget_branch: %s\nstep: verify\nstatus: running\n' \
     "$RUN_ID" "{BRANCH}" "{BASE}" "$REPO" "{BASE}" "{BRANCH}" > "runs/$RUN_ID/state.md"
   printf '# Verify-only run: %s\n\n**Branch:** %s (vs %s)\n**Repo:** %s\n' "$RUN_ID" "{BRANCH}" "{BASE}" "$REPO" > "runs/$RUN_ID/report.md"
   ```

3. **Run Verify.** It can take several minutes (install, builds, test suites).
   ```bash
   python3 .claude/scripts/verify.py "$REPO" "$RUN_ID"
   echo "exit=$?"
   ```
   Then set `status:` in `state.md` to `done` (exit 0), `failed` (1 or 3) or `incomplete` (4).

4. **Report to the user**, from `runs/$RUN_ID/verify.md`:
   - The verdict and the results table.
   - **0 PASS:** say it's verified. Include "Pre-existing failures" (they also fail on the base
     branch) and "Production follow-up" (new migrations to apply at deploy) if present.
   - **1 FAIL:** for each failure in "Failures caused by this run", give the step, the key
     error lines and the log file path. Offer to help fix them; don't start fixing unasked.
   - **3 ERROR:** a setup problem (worktree, install infrastructure, Docker failed to start),
     not a verdict on the code. Show the error.
   - **4 INCOMPLETE:** nothing failed, but required checks couldn't run (usually Docker isn't
     running, so the database and e2e steps were skipped). Tell the user to start Docker
     Desktop and run `/verify-branch` again.
   - End with where the details are: `runs/$RUN_ID/verify.md` and `runs/$RUN_ID/verify-logs/`.

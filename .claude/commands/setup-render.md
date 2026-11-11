# Set up render review for a repo

Give a target repo the config the pipeline's render review (Stage 6c) needs, without a pipeline
run: the `render` section of `.claude/verify.json`, `.claude/launch.json` and a sign-in/seed hook
under `.claude/render/`. A subagent writes them, they're committed on a **separate branch** cut
from the base (in a throwaway worktree: your checkout is never touched), and a smoke test opens
real pages with them. You merge the branch as its own PR; after that, every run's render review
uses it.

Pipeline runs offer the same thing at Stage 2b when a run changes pages and the repo has no
render config. This command is for setting a repo up ahead of time, or retrying after a failed setup.

You orchestrate only: the subagent writes the files, `render-config.py` validates and commits
them, `render-capture.mjs` smoke-tests them. Their exit codes are the verdict.

## Arguments

```
$ARGUMENTS
```

Parse as: `[--repo PATH] [--base BASE] [--branch NAME]`
- `--repo`: the target repo (absolute path, `~` allowed). If missing, ask the user which repo.
- `--base`: the branch to build the config on. Default: the repo's default branch:
  ```bash
  BASE=$(git -C "$REPO" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')
  [ -z "$BASE" ] && BASE=$(git -C "$REPO" rev-parse --verify --quiet main >/dev/null && echo main || echo master)
  ```
- `--branch`: the branch to commit on. Default: `chore/render-config`.

## Steps

1. **Check the inputs.**
   ```bash
   REPO=$(cd "{REPO}" && pwd)
   git -C "$REPO" rev-parse --verify --quiet "{BASE}"
   git -C "$REPO" show "{BASE}:.claude/verify.json" 2>/dev/null | python3 -c 'import json,sys; print("has render" if isinstance(json.load(sys.stdin).get("render"), dict) else "no render")' 2>/dev/null || echo "no verify.json"
   git -C "$REPO" for-each-ref --format='%(refname:short)' 'refs/heads/chore/render*'
   ```
   - BASE doesn't exist: stop and say so.
   - BASE **already has** a render section: tell the user it's set up already and ask whether to
     replace it (continue) or stop.
   - A `chore/render*` branch exists: tell the user (it may be an unmerged earlier setup; a run
     can use it via `render_config_ref`) and ask whether to continue with a new setup.

2. **Clear any "don't ask again"** for this repo (asking for a setup means they want it):
   ```bash
   python3 .claude/scripts/render-config.py optin "$REPO"
   ```

3. **Create a setup folder.**
   ```bash
   SETUP_ID="rsetup_$(date +%Y%m%d_%H%M%S)"
   mkdir -p "runs/$SETUP_ID/render-setup" "runs/$SETUP_ID/archive"
   printf 'run_id: %s\ntask: setup-render\nrepo: %s\noriginal_branch: %s\nstep: render-setup\nstatus: running\nrender_setup_retries: 0\n' \
     "$SETUP_ID" "$REPO" "{BASE}" > "runs/$SETUP_ID/state.md"
   printf '# Render setup: %s\n\n**Repo:** %s\n**Base:** %s\n**Branch:** %s\n\n## Log\n' "$SETUP_ID" "$REPO" "{BASE}" "{BRANCH}" > "runs/$SETUP_ID/report.md"
   ```

4. **Run the render setup procedure** exactly as written in `.claude/procedures/render-setup.md`, with `RUN_ID` = `$SETUP_ID`, `SETUP_DIR` =
   `runs/$SETUP_ID/render-setup`, `BASE` and `SETUP_BRANCH` from the arguments, and **no
   `CONTRACT`** (the subagent picks a small fixture set for the repo's main pages). Record each
   step in `runs/$SETUP_ID/report.md` and keep `render_setup_retries` in its `state.md`. Its
   "Success" step has no run to set `render_config_ref` on: skip that part.
   Then set `status:` in `state.md` to `done` (success) or `failed`.

5. **Report to the user.**
   - **Success:** the branch and commit, the files it adds, how sign-in works and the
     prerequisites (from the subagent's RESULT), the fixtures it defined (name and description:
     the Architect will pick from these), and the smoke test result. Then: "Merge `{BRANCH}` into
     `{BASE}` as its own PR. Until it's merged, a run can use it by setting
     `render_config_ref: {BRANCH}` in its `state.md` (Stage 2b offers this automatically)."
     Point to `runs/$SETUP_ID/render-setup/notes.md` for the details.
   - **Needs input:** the subagent's questions (it may need a decision about sign-in); answer them
     here and the procedure continues.
   - **Blocked or failed:** the reason, the smoke test's screenshots and dev-server log
     (`runs/$SETUP_ID/render-setup/smoke/render/`), and whether a branch was left behind.

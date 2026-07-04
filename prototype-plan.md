# Prototype Plan: Multi-Agent Dev Orchestrator

## Scope

**Platform:** Claude Code custom slash commands (`.claude/commands/`)  
**Pipeline:** Product → Architect → Tester → Test-Reviewer → Coder (5 stages, 1 reviewer gate)  
**Target repo:** Personal Finance Tracker (React/TypeScript)  
**Task type:** "add a tested React component"  
**Model:** Single model throughout, no tiering  
**Explicitly out of scope:** Plan-reviewer, Code-reviewer, memory curator, cost optimization, provider abstraction

---

## File Structure

```
.claude/
  commands/
    run-orchestration.md       ← master entry point, sequences all stages
    resume-orchestration.md    ← resumes from a named step using saved artifacts
    _product-agent.md          ← PRD writer (underscore = internal, not user-facing)
    _architect-agent.md        ← plan + Interface Contract
    _tester-agent.md           ← writes tests from plan
    _test-reviewer-agent.md    ← reviews test quality
    _coder-agent.md            ← implements against tests + contract
  scripts/
    repo-digest.sh             ← generates directory tree, deps, test command
    run-tests.sh               ← sets up worktree, runs tests, captures exit code
    log-cost.sh                ← appends token count + estimated $ to run report

runs/
  run_20260626_143022/         ← one folder per run (timestamp)
    prd.md
    plan.md                    ← includes Interface Contract section
    tests/                     ← actual test files written by Tester
    code/                      ← actual implementation files written by Coder
    state.md                   ← current step, status (running/paused/failed/done)
    report.md                  ← failure details, reviewer feedback, cost per stage
    repo-digest.md             ← cached at run start, not regenerated on resume
    archive/                   ← previous versions on overwrite (plan_v1.md, etc.)

memory.md                      ← structured memory bank (manually edited for MVP)
```

---

## Build Phases

### Phase 0 — Scaffolding ✅

**Goal:** Skeleton in place, folder conventions established, nothing runs yet.

- [x] `git init` in the project root
- [ ] Create a GitHub repo and add it as remote: `git remote add origin git@github.com:<you>/ai-orchestrator.git`
- [x] Create initial `.gitignore` (excludes `runs/`, `.env`, `node_modules`, `.DS_Store`)
- [x] Create `.claude/commands/` and `.claude/scripts/` directories
- [x] Create `runs/` directory with a `.gitkeep`
- [x] Write `memory.md` with named sections (Testing conventions, Architecture decisions, Known gotchas, Run history)
- [x] Add `runs/` to `.gitignore`
- [x] Add `README.md` and `LICENSE` (MIT)
- [x] Initial commit + push: `rm .git/index.lock && git add . && git commit -m "chore: project scaffold" && git push -u origin main`

**Done when:** Repo is on GitHub, directory structure in place, `runs/` excluded from tracking.

---

### Phase 1 — Disk Persistence + Pause/Resume ✅

**Goal:** The single most critical mechanism. Everything else depends on it.

- [x] Write `run-orchestration.md` — skeleton only, creates the run folder + `state.md`, then stops. No agent calls yet.
- [x] Write `resume-orchestration.md` — reads `state.md` from a given run folder, prints current step and status, supports `--from {STEP}` override.
- [x] Define the `state.md` schema (run_id, task, step, status, timestamp, last_artifact, pause_reason, retry_count)
- [x] Test: run `/run-orchestration "some task"` in Claude Code, verify folder + state.md created. Run `/resume-orchestration {run_id}`, verify it reads and prints state correctly.

**Done when:** Create → pause → resume cycle works end-to-end on disk, no agents involved.

---

### Phase 2 — Repo Digest Generator ✅

**Goal:** Architect has real grounding before writing a single path.

`repo-digest.sh` generates:

- Directory tree (depth 3, excludes node_modules/dist/runs/.git)
- `package.json` scripts + dependencies only (no devDependencies)
- Test command extracted from scripts
- All `.tsx` / `.ts` / `.py` source files
- Similar files matched against task hint keywords
- Existing test files
- `tsconfig.json` path aliases + Jest config

Output: `runs/{run_id}/repo-digest.md`. Cached — script skips if file already exists.

- [x] Write `.claude/scripts/repo-digest.sh`
- [x] Call it from `run-orchestration.md` after folder creation (Step 4), with task hint passed through
- [x] Verify output is readable and under ~2000 tokens (run `/run-orchestration` against your target repo and check)

**Done when:** `/run-orchestration` creates a run folder containing a populated `repo-digest.md`.

---

### Phase 3 — Agent Definitions ✅

**Goal:** Each agent has a real prompt with a concrete checklist. This is the most design-intensive phase.

- [x] **3a. `_product-agent.md`** — asks clarifying questions (pauses + waits), writes `prd.md` with Goal / User stories / Acceptance criteria / Out of scope. Self-review checklist ensures every criterion is independently testable.
- [x] **3b. `_architect-agent.md`** — reads PRD + repo digest + memory, may ask questions (same pause mechanism), writes `plan.md` with Interface Contract (file path, export, props, data-testids, dependencies). Self-review checklist verifies all paths match repo conventions.
- [x] **3c. `_tester-agent.md`** — reads Interface Contract as primary input, writes tests only against named contract items, outputs `CONTRACT_GAP` comments instead of inventing missing names. Self-review checklist enforces no invented selectors.
- [x] **3d. `_test-reviewer-agent.md`** — explicit 4-section checklist (A: contract compliance, B: test quality, C: coverage, D: CONTRACT_GAPs). FAIL routes back to Tester with line-level feedback + retry cap at 2.
- [x] **3e. `_coder-agent.md`** — reads tests as ground truth, writes implementation to `runs/{run_id}/code/`, detects CONTRACT_MISMATCHes (pauses cleanly rather than guessing), must not modify test files.

**Done when:** Each agent prompt is written and has been manually reviewed for completeness against its checklist.

---

### Phase 4 — Test Sandbox (Day 5–6)

**Goal:** Ground truth from exit code, not agent self-report. Clean worktree, two-layer timeout.

Write `.claude/scripts/run-tests.sh`:

```bash
#!/bin/bash
# Usage: run-tests.sh <repo_path> <run_id> <test_command> <timeout_seconds>

REPO=$1
RUN_ID=$2
TEST_CMD=$3
TIMEOUT=${4:-120}

# 1. Create disposable worktree
WORKTREE="/tmp/orchestrator-sandbox-$RUN_ID"
git -C "$REPO" worktree add "$WORKTREE" HEAD --detach

# 2. Copy generated code + tests into worktree
cp -r "runs/$RUN_ID/code/." "$WORKTREE/src/"
cp -r "runs/$RUN_ID/tests/." "$WORKTREE/src/"

# 3. Run tests with hard timeout
cd "$WORKTREE"
timeout "$TIMEOUT" bash -c "$TEST_CMD" > "/tmp/test-output-$RUN_ID.txt" 2>&1
EXIT_CODE=$?

# 4. Classify result
if [ $EXIT_CODE -eq 124 ]; then
  echo "TIMEOUT" >> "runs/$RUN_ID/report.md"
elif [ $EXIT_CODE -eq 0 ]; then
  echo "PASS" >> "runs/$RUN_ID/report.md"
else
  echo "FAIL" >> "runs/$RUN_ID/report.md"
  cat "/tmp/test-output-$RUN_ID.txt" >> "runs/$RUN_ID/report.md"
fi

# 5. Cleanup
git -C "$REPO" worktree remove "$WORKTREE" --force

exit $EXIT_CODE
```

Timeout routing (in `run-orchestration.md`):

- `TIMEOUT` → route back to test-reviewer (likely a broken test, not bad code)
- `FAIL` → retry Coder (max 2 retries), then stop-and-report
- `PASS` → continue

- [x] Write `run-tests.sh` — git worktree sandbox, TypeScript pre-flight, `npm install` if needed, two-layer timeout (runner + hard subprocess cap), classifies PASS/FAIL/TIMEOUT/ERROR, always cleans up via `trap EXIT`, writes full output to `report.md`
- [x] Write `log-cost.sh` — appends per-stage token counts + estimated cost to `report.md` and a running total to `cost.md`
- [ ] Test manually: run a passing test → verify PASS. Run a failing test → verify FAIL. Inject `while(true){}` → verify TIMEOUT + worktree cleaned up.

**Done when:** All three exit conditions produce the correct classification and the worktree is always cleaned up.

---

### Phase 5 — Wire the Orchestrator ✅

**Goal:** `/run-orchestration <idea>` runs the full pipeline end-to-end.

- [x] `run-orchestration.md` fully wired — 7 stages in sequence:
  - Stage 0: setup (run folder, state.md, report.md, repo digest)
  - Stage 1: Product agent → pause if questions
  - Stage 2: Architect agent → pause if questions
  - Stage 3: Tester agent
  - Stage 4: Test-Reviewer gate → retry Tester up to 2x with injected feedback, then stop
  - Stage 5: Coder agent → pause on CONTRACT_MISMATCH
  - Stage 6: Test sandbox → TIMEOUT routes to Test-Reviewer, FAIL retries Coder up to 2x, PASS continues
  - Stage 7: Done — updates state, appends run history to memory.md, tells user where artifacts are
- [x] `resume-orchestration.md` fully wired — reads state.md, archives overwritten artifacts, executes from any named step (`product` / `architect` / `tester` / `test-reviewer` / `coder` / `sandbox`), carries retry and feedback logic identical to run-orchestration
- [x] Test pause: answer a Product agent question, verify PRD written, verify resume from Architect works
- [x] Test failure: force a test failure, verify report written, verify `--from coder` resumes correctly

**Done when:** Full end-to-end run completes including one deliberate pause + resume cycle.

---

### Phase 6 — Cost Tracking ✅

**Goal:** Visibility into where spend concentrates, not optimization.

Write `.claude/scripts/log-cost.sh` — appends to `report.md` after each stage:

```
[product-agent]  input: 1,240 tokens  output: 890 tokens  est: $0.012
[architect-agent] input: 4,100 tokens  output: 2,300 tokens  est: $0.048
...
```

- [x] Write `log-cost.sh` (written in Phase 4) — estimates tokens from artifact file sizes (chars/4), writes per-stage line to `report.md` and running total table to `cost.md`
- [x] Wire into `run-orchestration.md` after every agent stage (product, architect, tester, test-reviewer, coder) with bash estimates of input/output token counts
- [x] Stage 7 reads `cost.md` and displays the full table + includes total in the `memory.md` run history entry
- [x] Verify a completed run's `report.md` and `cost.md` have a full cost breakdown

**Done when:** Every run produces a cost line per stage.

---

### Phase 7 — Validation Run (Day 8)

**Goal:** Answer the doc's key open questions empirically, not theoretically.

Run the pipeline against the Personal Finance Tracker repo with the task: **"Add a Budget Summary card component that displays total income, total expenses, and net balance."**

Checklist:

- [ ] Does the Interface Contract prevent the Tester/Coder name-mismatch loop? (Verify no invented selectors in tests)
- [ ] Does the repo digest keep the Architect grounded? (Verify generated paths exist in the actual repo)
- [ ] Does pause/resume feel usable? (Run one deliberate pause at the Architect question step)
- [ ] Does the sandbox catch a hang cleanly? (Inject `while(true){}` into a test, verify TIMEOUT + clean worktree)
- [ ] Where does cost concentrate? (Read the report.md cost breakdown)
- [ ] Does the same agent config work in a fresh Claude Code session? (Open a new session, run `/run-orchestration` from scratch)

Record findings in `runs/validation-run/report.md`. These answers drive what to build next.

---

## What Gets Built Next (Post-Prototype)

Only after validation run completes:

1. **Plan-reviewer** persona (checks PRD ↔ plan completeness + Interface Contract coverage)
2. **Code-reviewer** persona (correctness, security, style)
3. **Memory curator** (structured writes to `memory.md` after successful runs)
4. **Model tiering** (based on actual cost data from Phase 6, not guesswork)
5. **Thin CLI wrapper** (only if prompt-based sequencing in Claude Code feels fragile)

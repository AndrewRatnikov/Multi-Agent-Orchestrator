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
- [ ] Initial commit + push: `rm .git/index.lock && git add . && git commit -m "chore: project scaffold" && git push -u origin main`

**Done when:** Repo is on GitHub, directory structure in place, `runs/` excluded from tracking.

---

### Phase 1 — Disk Persistence + Pause/Resume ✅
**Goal:** The single most critical mechanism. Everything else depends on it.

- [x] Write `run-orchestration.md` — skeleton only, creates the run folder + `state.md`, then stops. No agent calls yet.
- [x] Write `resume-orchestration.md` — reads `state.md` from a given run folder, prints current step and status, supports `--from {STEP}` override.
- [x] Define the `state.md` schema (run_id, task, step, status, timestamp, last_artifact, pause_reason, retry_count)
- [ ] Test: run `/run-orchestration "some task"` in Claude Code, verify folder + state.md created. Run `/resume-orchestration {run_id}`, verify it reads and prints state correctly.

**Done when:** Create → pause → resume cycle works end-to-end on disk, no agents involved.

---

### Phase 2 — Repo Digest Generator (Day 2)
**Goal:** Architect has real grounding before writing a single path.

Write `.claude/scripts/repo-digest.sh`:
- Directory tree, depth 3 (`find . -maxdepth 3 -type f`)
- Contents of `package.json` (deps + scripts section only — strip devDependencies if large)
- List of existing component files similar to the task (e.g., `find src/components -name "*.tsx"`)
- Test command extracted from `package.json` scripts

Output: `runs/{run_id}/repo-digest.md`. Cached at run start — `resume-orchestration.md` must **not** regenerate it.

- [ ] Write the script
- [ ] Call it from `run-orchestration.md` as the first step after folder creation
- [ ] Verify output is readable and under ~2000 tokens (trim if not)

**Done when:** `/run-orchestration` creates a run folder containing a populated `repo-digest.md`.

---

### Phase 3 — Agent Definitions (Day 3–5)
**Goal:** Each agent has a real prompt with a concrete checklist. This is the most design-intensive phase.

Build in dependency order — each agent's output is the next agent's input.

#### 3a. Product Agent (`_product-agent.md`)
- Input: raw idea string from `$ARGUMENTS`
- Behavior: ask clarifying questions, block until answered, write `prd.md`
- Output format: sections for Goal, User stories, Acceptance criteria, Out of scope
- Pause mechanism: outputs questions, sets `state.md` status to `paused`, halts

#### 3b. Architect Agent (`_architect-agent.md`)
- Input: `prd.md` + `repo-digest.md` + relevant sections of `memory.md`
- Behavior: may ask clarifying questions (same pause mechanism as Product)
- Output format for `plan.md` must include a mandatory Interface Contract:
  ```markdown
  ## Interface Contract
  - Component: <name>
  - File: <exact path matching repo conventions>
  - Exports: <default export name>, props: <{ prop: type }>
  - Test selectors: data-testid="<value>", data-testid="<value>"
  - Dependencies: <what it imports>
  ```
- Checklist the architect must self-verify before outputting:
  - [ ] Every file path matches a pattern that exists in the repo digest
  - [ ] Every selector name is unique within the component
  - [ ] All acceptance criteria from the PRD are addressed

#### 3c. Tester Agent (`_tester-agent.md`)
- Input: `plan.md` (Interface Contract section is primary) + `memory.md` Testing conventions section
- Behavior: write test files **only against the Interface Contract** — no invented paths or selectors
- Output: actual test files in `runs/{run_id}/tests/`
- Constraint in prompt: "Do not invent any selector, path, or export name not stated in the Interface Contract. If the contract is missing something you need, output a CONTRACT_GAP note instead of inventing."

#### 3d. Test-Reviewer Agent (`_test-reviewer-agent.md`)
- Input: `plan.md` + test files
- Checklist (must be explicit in the prompt):
  - [ ] Every selector used in tests appears in the Interface Contract
  - [ ] Each test exercises behavior, not just asserts a hardcoded return value
  - [ ] No test awaits an unresolved promise or hits real network
  - [ ] Coverage: at least one test per acceptance criterion in the PRD
- Output: `PASS` or `FAIL` with specific line-level feedback. On `FAIL`, sets `state.md` to `failed` + writes feedback to `report.md`.

#### 3e. Coder Agent (`_coder-agent.md`)
- Input: `plan.md` + test files + `repo-digest.md` + `memory.md` (Conventions + Architecture sections)
- Behavior: implement code that makes the tests pass
- Constraint in prompt: "Do not modify the test files. Your only goal is to make them pass. Use the exact paths, exports, and selectors from the Interface Contract."
- Output: actual implementation files in `runs/{run_id}/code/`

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

- [ ] Write the script
- [ ] Test it manually: copy a trivial passing test into the sandbox, verify `PASS`. Copy a failing test, verify `FAIL`. Hang a test artificially, verify `TIMEOUT` and clean exit.

**Done when:** All three exit conditions produce the correct classification and the worktree is always cleaned up.

---

### Phase 5 — Wire the Orchestrator (Day 6–7)
**Goal:** `/run-orchestration <idea>` runs the full pipeline end-to-end.

Update `run-orchestration.md` to sequence all stages in order, with:

```
1. Create run folder + state.md
2. Generate repo digest → cache to runs/{id}/repo-digest.md
3. Call _product-agent → writes prd.md → if questions, pause
4. Call _architect-agent → writes plan.md → if questions, pause
5. Call _tester-agent → writes tests/
6. Call _test-reviewer-agent → PASS/FAIL
   - FAIL: inject feedback, retry Tester (max 2 retries), then stop
7. Run run-tests.sh (pre-flight: do tests even compile/parse?)
8. Call _coder-agent → writes code/
9. Run run-tests.sh (real ground truth)
   - TIMEOUT: route back to _test-reviewer-agent
   - FAIL: retry Coder (max 2 retries), then stop
   - PASS: continue
10. Update state.md to done
11. Append run summary to memory.md Run history section
```

Update `resume-orchestration.md` to accept `--from <step>` and jump to that step using saved artifacts from the run folder.

- [ ] Wire all stages
- [ ] Test pause: answer a Product agent question, verify PRD written, verify resume from Architect works
- [ ] Test failure: force a test failure, verify report written, verify `--from coder` resumes correctly

**Done when:** Full end-to-end run completes including one deliberate pause + resume cycle.

---

### Phase 6 — Cost Tracking (Day 7)
**Goal:** Visibility into where spend concentrates, not optimization.

Write `.claude/scripts/log-cost.sh` — appends to `report.md` after each stage:
```
[product-agent]  input: 1,240 tokens  output: 890 tokens  est: $0.012
[architect-agent] input: 4,100 tokens  output: 2,300 tokens  est: $0.048
...
```

- [ ] Write the script (uses Claude API response headers or SDK usage object)
- [ ] Call it from `run-orchestration.md` after each agent stage
- [ ] Verify a completed run's `report.md` has a full cost breakdown

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

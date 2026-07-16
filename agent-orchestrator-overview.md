# Project Overview: Multi-Agent Dev Orchestrator (v2)

## One-line pitch
A CLI tool where you hand the orchestrator an idea/feature/bug, and a pipeline of specialized agents — Product → Architect → Reviewer → Tester → Reviewer → Coder → Reviewer — turns it into a tested, validated branch. Agent definitions are portable enough to also drop directly into Claude Code.

## The workflow (your design)
1. **Product/doc-writer agent** — takes your raw idea, asks clarifying questions, produces a PRD.
2. **Architect agent** — turns the PRD into a concrete, doable technical plan, grounded in a repo digest (see Design decisions #9) so paths/conventions are real, not invented. Can also ask questions. The plan must include an explicit **Interface Contract** (see Design decisions #7) — exact file paths, exports, prop names, and test selectors — so Tester and Coder build against the same names instead of each inventing their own.
3. **Reviewer (plan)** — checks the plan actually satisfies the PRD.
4. **Tester agent** — writes tests based on the plan, before any code exists.
5. **Reviewer (tests)** — checks test quality/coverage against the plan.
6. **Coder agent** — implements code that must pass the tests.
7. **Reviewer (code)** — validates the final solution.
8. **Output** — a branch with a tested, validated feature/fix.
9. **Memory bank** — a persistent store of decisions, conventions, and useful context, fed by the pipeline over time.

This is a **spec-driven, test-first pipeline with a review gate after every stage** — a stronger design than a generic "planner delegates to workers" pattern, because each gate catches a bad output before it contaminates the next stage. Tests-before-code in particular is the single best decision in this design: it gives the coder agent an objective, machine-checkable target instead of "reviewer vibes."

## Design decisions (locked)

These were open questions; each is now decided. A few of them share mechanisms or build on each other — cross-references noted inline.

### 1. Backtracking — one step back (MVP)
On a failed gate, the pipeline retries at the **current stage only**, with the reviewer's feedback injected into the retry prompt. No automatic walk-back to earlier stages. Combined with decision #6, this is effectively **manual backtracking**: if a one-step retry doesn't resolve it, the run stops and a human decides where to resume — rather than the agent deciding how far back to go. Simpler to build, more trustworthy for an MVP, and avoids the "replan invalidates the tests" cascade for now.

### 2. Test-gaming — recommended approach
Coder sees the tests verbatim, but the **test-reviewer gate is mandatory** and specifically audits test quality before the coder runs — does this test actually exercise the behavior, or just assert a hardcoded return? This is the main defense against an agent satisfying the letter of a test without satisfying the feature.

### 3. Reviewer — three distinct personas
Not one Reviewer agent reused three times. Each checks something structurally different:
- **Plan-reviewer:** does this plan satisfy the PRD? (completeness/traceability) — and per decision #7, does the plan's Interface Contract name every selector/export/path the tests and code will need?
- **Test-reviewer:** do these tests meaningfully exercise the plan? (quality/coverage, not just count)
- **Code-reviewer:** correctness, security, style, tech debt

Each persona needs its own checklist/prompt — defining those checklists is real design work worth doing before implementation, not boilerplate to skip.

### 4. Questions — pipeline blocks and waits
Product and Architect agents pause the run and wait for your answer before continuing. This is the same underlying mechanism as decision #6's failure handling (see note below) — one pause/resume system, not two.

### 5. Memory bank — structured sections with status, not a flat append-only file
A single flat `memory.md`, injected in full and only ever appended to, has two distinct failure modes worth naming separately:
- **Context dilution** — every agent gets the whole file, including sections irrelevant to its job, diluting its context as the project grows.
- **Stale context persisting forever** — a past mistake that was later manually corrected has no way to be marked dead; append-only means the bad entry sits in the file alongside its correction, and an agent can pattern-match to either.

The fix is structure plus ownership, not just "let agents edit it":
- **Named sections** (`Conventions`, `Architecture decisions`, `Known gotchas`, brief `Run history`) — each agent is injected only the section(s) relevant to its role. The Tester reads `Testing conventions`; the Coder doesn't need to.
- **Status per entry** (`active`, `superseded`, `retired`) rather than a pure timestamp log — a correction explicitly marks the old entry dead instead of just appending a newer, contradicting one underneath it.
- **One curator step writes to it, not five agents freely.** At the end of each successful run, a single curator agent decides whether to add a new entry or mark an existing one superseded and write its replacement — avoiding the conflicting-edit problem that "every agent can edit memory" would otherwise introduce.
- **Human edits remain cheap** — since it's still one readable markdown file, you can hand-correct an entry's status at any time; the curator is the default writer, not the only one.

Example shape:
```markdown
## Testing conventions
- [active] Use data-testid, not class selectors, for Cypress queries. (added: run-014)
- [superseded by run-031] Use shallow render for all component tests.
- [active] Use full render with RTL for components with conditional logic. (added: run-031, supersedes above)
```
Still a flat file on disk for the MVP — no database, no retrieval — just one with internal structure and an edit discipline instead of pure append-only.

### 6. Failure handling — stop, report, resume from any step
A failed stage stops the run and writes a report: which step failed, why, and what the last good state was. The user reviews the report, can make fixes, and re-runs — optionally specifying which step to resume from.

**Implication worth naming:** this requires every stage's output (PRD, plan, tests, code) to be **persisted to disk**, not just held in memory during a single run — a resumed run starts from saved artifacts, not from re-asking the agents. It also means each stage must be **independently invocable**, taking the prior stage's saved artifact as input, since "start from step N" has to work. This is the same pause/resume machinery as decision #4's question-blocking: in both cases the pipeline halts, persists state, and waits for a human before continuing. Build one mechanism, use it for both.

### 7. Interface contract lives in the Plan, not invented later by the Tester
This addresses a specific trap: if the Tester invents mechanical details — import paths, `data-testid` values, prop names — independently of the Coder, the two agents are guaranteed to occasionally invent different names for the same thing. The Coder then gets stuck trying to satisfy a test that's structurally wrong (wrong selector, wrong path), not behaviorally wrong — an unwinnable loop that looks like a code bug but is actually a contract mismatch.

The fix: the **Architect's plan output must include an explicit Interface Contract** stating the concrete, checkable surface a component/function exposes, before Tester or Coder touch anything:

```
## Interface Contract
- Component: Header
- File: src/components/Header.tsx
- Exports: default export `Header`, props `{ title: string }`
- Test selectors: data-testid="header", data-testid="header-title"
```

Tester writes tests against this stated contract; Coder implements against the same stated contract. Neither agent invents names independently. The plan-reviewer's checklist (decision #3) gets a concrete, mechanical item: *does the Interface Contract name everything the tests will need to reference?*

**Worth being honest about the limit of this fix:** it doesn't eliminate failure mode B entirely — the Architect can still state the contract wrong, or the Coder can still typo a path independently of it. What it does is collapse "two agents independently inventing the same names" down to "one agent states the names once, two agents read them" — removing the structural cause of the trapped loop, not just patching one instance of it. It does nothing for failure mode A (a test that's wrong about *intent*, not mechanics) — that's still the test-reviewer's job per decision #2.

### 8. Test execution — sandboxed, shelled out, timeout-bound, exit-code is ground truth
"The coder runs tests" hides four separate decisions:

- **Where:** never the live working directory. Run tests inside a disposable copy of the repo for the run (a fresh clone, or a git worktree on a throwaway branch) — both because LLM-written code/tests are an untrusted execution boundary even without malicious intent (bad path assumptions, accidental destructive commands, hitting real network), and because it guarantees a failed run never leaves the actual checkout dirty. (Amended: the pipeline now checks out its own `orchestrator/{run_id}` branch in the target repo at the start of a run and commits tests/code to it incrementally as work happens — that's a dedicated non-main branch the user isn't actively working on, distinct from this decision, which is specifically about where the test *command* executes. Execution still happens in the disposable worktree, never directly in that checked-out branch.)
- **How:** shell out to the project's own real test runner (`npm test`, `pytest`, etc.) as a subprocess; capture stdout/stderr/exit code. The orchestrator never parses or reimplements test logic itself — language-agnosticism here just means each project declares one config string (its test command), nothing more.
- **Timeout — two layers, mandatory:** a per-suite timeout from the test runner itself (Jest's `--testTimeout`, pytest-timeout, etc.) so one bad test doesn't take the whole run down silently, *and* a hard subprocess-level timeout wrapping the entire test command as a backstop for when the runner itself hangs (e.g. an LLM-written test awaiting an unresolved promise or hitting a real network call). Without this, decision #6's "stop and report" can't even trigger — there's no exit code to catch.
- **Pass/fail ground truth:** the test command's own exit code, full stop. Never the agent's self-reported "tests pass" — that would reintroduce exactly the gameable, ungrounded signal the tests-before-code design (decision #2) was meant to eliminate.

**Worth distinguishing in the failure report:** a timeout and a normal test failure are different failure types with different likely causes. "Tests failed" (nonzero exit, ran to completion) usually means the Coder's logic is wrong — retry at Coder per decision #1. "Tests timed out" more often means the *Tester* wrote something broken (an unresolvable assertion, a hang) — which should route back to the test-reviewer gate rather than retrying the Coder against a possibly-broken test. Tag these distinctly rather than treating all test-stage failures the same.

### 9. Architect needs repo context — a targeted digest, not the raw codebase
Without grounding in the real repo, the Interface Contract from decision #7 is itself built on guesses — the Architect could write a clean-sounding path like `src/components/Header.tsx` while the project actually uses `src/widgets/Header/index.tsx`. The contract would still be internally consistent (Tester and Coder both read the same path), just wrong about reality. This is a distinct failure mode from the Tester/Coder mismatch decision #7 already addresses — it's the Architect being wrong about the world, not two agents disagreeing with each other.

The fix is a **repo digest**, not raw file contents dumped into context — generated once per run (or cached):
- Directory tree, depth-limited
- Dependency manifest (`package.json` / `pyproject.toml`, etc.) — so the Architect knows what's actually installed, not just what's idiomatic
- A short list of existing similar files when the task resembles something already in the codebase (e.g., sibling components, for a "add a component" task) — gives the Architect a concrete style reference instead of inventing conventions
- The real test command/runner — also feeds decision #8 directly

**Relationship to the memory bank (decision #5):** these are complementary, not duplicates. The repo digest answers "what does this codebase currently look like" (a snapshot); the memory file answers "what have we decided it *should* look like going forward, including corrections." Early on, before memory has much in it, the digest does most of the grounding work; over time, memory absorbs more of it as conventions get codified.

**Plan-reviewer checklist addition (decision #3):** does the Interface Contract's stated file path/naming actually match real conventions in the repo digest, or does it invent something inconsistent with the codebase? Cheap and mechanical to check — easier than judging plan quality in the abstract.

**Worth naming honestly:** the digest is a snapshot taken at the *start* of a run. If a run is paused and resumed across real wall-clock time (decision #6), the codebase may have changed underneath it by the time it resumes. Likely fine to ignore for a single-dev MVP with short-lived runs, but worth remembering if resumes ever span days rather than minutes.

### 10. Cost control — measure first, tier later
Several earlier decisions are cost multipliers by design, worth naming explicitly rather than discovering by surprise:
- **Decision #1** (one-step-back retries) — every retry is a full extra agent call with the prior failure injected.
- **Decision #3** (three reviewer personas) — extra calls beyond the four producer stages; one for the MVP's single test-reviewer gate, three at full scale.
- **Decision #5** (memory bank) — injected into every relevant prompt, every run, plus a curator call at the end of every *successful* run; grows the per-call token cost monotonically unless actively pruned.
- **Decision #9** (repo digest) — cheap if cached, wasted spend if regenerated on every resume by default.

**Concrete levers, in rough order of impact:**
- **Model tiering** — not every stage needs the same model. The Architect (grounding against the repo digest, deciding the Interface Contract) and code-reviewer (needs to catch subtle bugs) are good candidates for the strongest available model; the Tester scaffolding tests from an already-fully-specified plan, or a curator doing mechanical memory bookkeeping, are good candidates for a cheaper/faster one.
- **Hard retry cap** — decision #1 already bounds retries to one step back; add an explicit numeric ceiling (e.g., max 2 retries per stage before forcing stop-and-report) so a degenerate loop can't quietly burn spend unattended.
- **Repo digest caching** — generate once per run, invalidate only on an explicit "repo changed" flag rather than regenerating by default on every resume.
- **Memory pruning as a cost control, not just a correctness one** — decision #5's status-based pruning (archiving `superseded`/`retired` entries out of the actively-injected context) keeps per-call token cost from creeping up over the project's lifetime, on top of its original purpose of preventing stale context.
- **Track cost per run as a first-class artifact** — since decision #3 already gives every run its own folder, add a cost line (tokens in/out per stage, estimated $) to the run's report. For a single-user MVP this doesn't need a dashboard, just enough visibility to notice if one task type is unexpectedly expensive.

**Worth being honest about sequencing:** cost optimization is genuinely a post-MVP concern, not a day-one one. Model tiering especially is easy to over-engineer early — downgrading the wrong stage to a cheaper model can quietly reintroduce the exact failure modes (sloppy Interface Contracts, gamed tests) that decisions #2, #7, and #9 exist to prevent. Build the MVP single-model first, actually measure cost per run, then tier based on where spend really concentrates rather than guessing upfront.

## Honest competitive reality
The supervisor/pipeline pattern itself is now table stakes — CrewAI, LangGraph, Claude Agent SDK, and others all support some version of staged delegation. Your differentiator is the **specific pipeline shape** (PRD → plan → tests-before-code → multi-stage review) plus **dual portability** (same agent defs run via your CLI or drop into Claude Code), not the existence of multiple agents.

## Suggested MVP slice
Don't build all seven stages with full review-persona variety first. But unlike a typical MVP, you **can't defer persistence and pause/resume** — decision #6 made that core, not a nice-to-have for later. So the MVP should be small in *stage count* but already correct in *mechanism*:

**PRD (Product, blocks on questions) → Plan (Architect, blocks on questions, grounded in a repo digest per decision #9, must output an Interface Contract per decision #7) → Tests (Tester) → Code (Coder)** — one reviewer gate only (test-reviewer, since it's the most load-bearing one per decision #2), one task type (e.g., "add a tested React component"), against your own Personal Finance Tracker repo. Single model throughout per decision #10 — no tiering yet. Every stage writes its artifact to disk, with a basic cost line (tokens/estimated $) logged per stage; the run can be resumed or restarted from any stage via CLI flag from day one.

This validates the riskiest assumptions cheaply:
- Does tests-before-code actually produce better code, or just slower code?
- Does the Interface Contract actually prevent the Tester/Coder mismatch loop, or does it just move the disagreement earlier?
- Does the repo digest actually keep the Interface Contract grounded in reality, or does the Architect still invent paths/conventions despite having it?
- Does pause/resume on disk-persisted artifacts actually feel usable, or is it more friction than it's worth?
- Does the structured, status-tracked memory file (decision #5) actually get read selectively and corrected over time, or does the curator step just become another thing that silently bloats?
- Does sandboxed test execution with a timeout (decision #8) actually catch a hang cleanly, or does the disposable-checkout approach add more friction than it's worth for a one-person MVP?
- Where does cost actually concentrate (decision #10) — which stage is most expensive in practice, to inform tiering later?
- Does the same agent config really work both standalone and inside Claude Code?

Add the plan-reviewer and code-reviewer personas only after this slice runs end-to-end at least once, including one deliberate pause-and-resume cycle.

## Open questions — resolved
1. **Target user — you, or a senior dev like you.** This simplifies earlier decisions retroactively: no need for polished error messages or forgiving UX for an audience of one experienced engineer who understands the pipeline's internals. Resist over-building CLI UX before the core mechanism is proven.
2. **Provider strategy — Claude only for MVP.** Defer provider-agnosticism; build against one API first, abstract later if it's ever needed. Faster to ship, and the portability claim (CLI vs. Claude Code) is about agent *definitions*, not about supporting every LLM provider on day one.
3. **Artifact format — one folder per run.** Each run gets its own `run_{datetimestamp}/` directory containing `prd.md`, `plan.md`, `tests.md`, `code.md` (or actual code files), plus the failure report from decision #6 if applicable. Simple, human-browsable, greppable — no database needed for the MVP.

**Implication worth deciding now, while cheap:** resuming a run mutates the same folder it failed in, not a fresh one. If a human hand-edits `plan.md` and resumes from the Tests step, you'll later want to know what the plan looked like when the tests were originally generated against it — otherwise a stale-mismatch bug becomes guesswork. Recommend: on any retry/resume that overwrites a stage's artifact, copy the previous version into an `archive/` subfolder first (e.g. `archive/plan_v1.md`) before writing the new one. Cheap insurance, preserves history without needing real version control.

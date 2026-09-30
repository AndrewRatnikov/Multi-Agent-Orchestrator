# Memory Bank

Pipeline-level context for the AI Orchestrator. It holds lessons about **the pipeline itself**, not about any one target repo.

**Where repo-specific knowledge lives:**
- `{REPO}/.claude/rules/*.md` and `{REPO}/CLAUDE.md`. Agents read these directly. They also load for plain Claude Code sessions in that repo, so a lesson recorded there helps with or without the orchestrator.
- `repo-notes/{repo-name}.md` in this project, for repos that don't have `.claude/rules/` yet. Move the notes into the repo when you next work on it.

When a run learns something, record it in the target repo's rules if it's about that codebase, or here if it's about how the pipeline works.

Status values: `active` | `superseded` | `retired`. The orchestrator (main session) is the only agent-side writer; subagents never edit this file. Humans may edit it directly.

---

## Pipeline conventions

<!-- Read by: Architect, Tester, Test-Reviewer, Coder -->

- [active] Tests query elements by `data-testid`, never by class selectors. Every testid a test uses must be declared in the Interface Contract. (added: run_20260627_113337)

---

## Pipeline gotchas

<!-- Read by: all agents -->

- [active] **Test command in a monorepo.** `repo-digest.sh` reads only the root `package.json` `scripts`, so it reports `UNKNOWN` when the real `test` scripts live in workspace members. The Architect must choose the real per-member command (e.g. `pnpm --filter api test`) and write it into `repo-digest.md`'s `## Test command`, because the sandbox runs that section verbatim. The command must include every setup step that **any** suite it runs needs, not just this task's new tests. Take those steps from `{REPO}/.claude/rules/` (e.g. coin-collector's `test-commands.md`: Prisma generate, shared build). (added: run_20260719_190933; recurred 3x before rules existed: run_20260720_171320, run_20260721_161448, run_20260726_221855, run_20260804_165504)
- [active] `run-tests.sh` builds workspace library packages before running tests: after `pnpm install`, if `pnpm-workspace.yaml` exists, it runs `pnpm --filter '!./apps/**' -r --if-present run build`. If "Failed to resolve entry for package" still appears for a workspace import, the package's own build script is broken; the build step itself did run. (added: run_20260728_071525)
- [active] Tests that shell out to a real build or type-check (`execSync('pnpm run build')`) need an explicit generous timeout (e.g. `{ timeout: 15000 }`). A cold sandbox worktree easily exceeds vitest's 5s default. (added: run_20260717_221635)
- [active] **Check scope after every Tester and Coder stage.** A Tester once wrote an unrelated file into the target repo's `docs/` while its summary claimed it had stayed in scope. Subagents now have a write-guard hook, but Bash can still write anywhere, so the orchestrator runs `git -C {REPO} status --porcelain` after each such stage and before any `git add`. Never trust a subagent's own summary for this. (added: run_20260722_121303)
- [active] **`runs/{RUN_ID}/tests/` and `code/` must mirror the full repo-relative path, not just the filename.** `run-tests.sh`'s "copy test files into sandbox" step does `cp -r "$RUN_DIR/tests/." "$SANDBOX/"`, i.e. it assumes the folder already mirrors the repo structure (so a file destined for `tests/foo.test.ts` must live at `runs/{RUN_ID}/tests/tests/foo.test.ts`, not `runs/{RUN_ID}/tests/foo.test.ts`). A Tester that writes flat causes two symptoms: (1) the Stage-3 `cp -r runs/{RUN_ID}/tests/. {REPO}/` lands the file at the wrong path in the target repo (orchestrator must check the staged path, not just that something got staged), and (2) even if that's caught and fixed by hand in {REPO}, the sandbox's independent copy step recreates the flat stray file at the sandbox root on top of the correctly-committed one, so vitest picks up a broken duplicate whose relative imports don't resolve and the whole suite reports FAIL even though the real, correctly-placed copy passes. Before trusting a sandbox FAIL, check whether the failing test file's reported path matches its intended repo-relative path — a mismatch means this bug, not a real regression. Fix: after a Tester/Coder stage, `find runs/{RUN_ID}/tests` (or `code`) and confirm every file's path under that folder equals its target repo-relative path; move it into place before the Stage-3/5 commit if not. (added: run_20260929_212417)
- [active] **Architect's verify-block scope assertions must diff against `{original_branch}`, never a hardcoded `main`.** In a repo where orchestrator runs are chained (a run's `original_branch` is a prior run's branch, not `main`), `git diff --name-only main...HEAD -- <path>` in a plan.md verify step will show files changed by *earlier* runs that are already merged into the current run's actual base — a false positive that looks like this run touched an out-of-scope path. `verify.py` cannot rescue this automatically: its baseline re-run explicitly excludes plan-authored steps (`base_steps = [s for s in steps[:upto] if not s.get("plan")]`), so a plan step that would fail identically on `original_branch` is never reclassified PRE-EXISTING — it always shows as "Failures caused by this run." The Architect must write scope-check commands against `{original_branch}` (the actual state.md field), e.g. `git diff --name-only {original_branch}...HEAD -- apps/web`. If this is caught late (at Stage 6b), the orchestrator can manually confirm via `git diff --name-only {original_branch}...HEAD -- <path>` and `git diff --name-only main...{original_branch} -- <path>` (proving the same failure pre-exists on the base branch) before asking the user whether to accept it as pre-existing — do not silently override the verdict. (added: run_20260929_212417)

- [active] **verify.py's lint baseline diff is line-number-sensitive.** It compares error lines verbatim, so a pre-existing error that the run merely pushed down (e.g. an import added above it) shows as "new" under "Failures caused by this run". When lint is the only failure, re-diff the branch and baseline logs with line/col stripped (`awk '/  error  /{$1=""; print file":"$0}'` per file, then `diff`) and compare the `✖ N problems` totals. If that shows 0 new errors, report it and ask the user to accept; never override the verdict yourself. (added: run_20260929_221728)
- [active] **Lint errors in reviewed test files first appear at Verify, and the Coder can't fix them.** The Tester has no Bash, so it can't run the target's linter, and any edit to a test after review trips `TESTS_MODIFIED_AFTER_REVIEW`. So when verify's new lint errors sit in test files, send only the Coder-file errors to a Coder retry, then ask the user whether to resume from tester. Before that Tester retry, prove a fix in a throwaway `git worktree` under the scratchpad (symlink `node_modules`, then run eslint, tsc and vitest) and pass the diff as reference; the Tester can't check its own lint. (added: run_20260929_221728)
---

## check-contract.sh known false positives

<!-- Read by: Architect, Tester, Coder. Temporary: most of these go away when the contract moves to contract.json (rebuild step 3). -->

The script greps `plan.md` and the test/code files with regexes, so these cases are expected. Handle them as described; don't retry an agent over them.

- [active] **Pre-existing testids (Check 1).** If this run preserves or extends an existing test file, every testid that file queries must appear in `plan.md` under a "Pre-existing testids (contract-check only)" heading, written as the literal `data-testid="..."` form (the script greps exactly that). Declare one only if a test in this run's `tests/` actually references it: grep first, and delete stray declarations. (added: run_20260722_121303, refined: run_20260801_142634, run_20260802_183303)
- [active] **MISSING_TESTID_IN_CODE for testids the Coder didn't create (Check 5a).** Check 5a expects every testid in `plan.md` to appear in `code/`, so pre-existing testids, testids that exist only inside a test's `vi.mock()` factory, and template-built testids all get flagged. Verify each against the real source file (for templates, look for the interpolation), record it as a verified false positive in `report.md`, and continue without a Coder retry. (added: run_20260725_140648, recurred: run_20260801_142634, run_20260929_221728)
- [active] **Parameterized testids** (e.g. `{p}-filter-country` via a `testIdPrefix` prop): list every concrete instantiation in `plan.md` in addition to the template line, so Check 1 passes. Check 5a's flag on the interpolated form is then a known false positive (see above). (added: run_20260721_171115)
- [active] **Negative assertions (Check 1)** can't be told apart from positive ones. To assert an old element is gone, use a structural check (e.g. `within(root).queryAllByRole('button')` has length 0), not `queryByTestId('old-id')`: the removed id isn't in the contract and will be flagged. (added: run_20260730_153718)
- [active] **Packages the Coder adds in this run (Check 2).** Check 2 reads the target's *current* `package.json`, before the Coder has added anything. For a test that needs such a package, load it with `require('pkg')` (Check 2 only greps `import` lines), plus a comment explaining why and `// eslint-disable-next-line @typescript-eslint/no-var-requires`. (added: run_20260802_221803)
- [active] **`vi.stubGlobal('fetch', ...)` (Check 4).** Only the literal substrings `vi.mock(` / `jest.mock(` count as evidence of mocking. With `vi.stubGlobal`, add a comment that contains `vi.mock(` explaining the choice. (added: run_20260717_221635)
- [retired — fixed in script] Check 2 aggregates dependencies across all workspace members (run_20260718_131730). Check 3 resolves relative imports against the test file's own directory (run_20260720_070901), and resolves `@/` aliases from each workspace member's tsconfig (run_20260721_131640). If one of these recurs, it's a regression in the script.

---

## Retired entries

- [retired 2026-09-27] "Use data-testid, not class selectors (run-001)", "Co-locate component tests in `__tests__/`" and "path aliases @/components, never `../../` (run-002)". These were the template's example lines from the Init commit, mistakenly marked `[active]`. The `__tests__/` one contradicts coin-collector's real convention (tests sit next to the file, e.g. `app/settings/page.test.tsx`), yet it went to the Architect and Coder on every run. The data-testid rule is kept above because the pipeline really relies on it.
- [moved 2026-09-27] All coin-collector-companion entries now live in that repo's `.claude/rules/` (`test-commands.md`, `api.md`, `web.md`, `monorepo.md`). One was stale: the api Jest ESM fix is now `test/support/shared-esm-transformer.js`, not a `build:cjs` output. Entries for req-lab are in `repo-notes/req-lab.md`.

---

## Run history

<!-- Brief log of completed runs. Injected into: curator only -->
<!-- Example entry:
- run-001 | 2026-06-26 | task: Add BudgetSummary component | result: PASS | tokens: 4200
-->

- run_20260929_221728 | 2026-09-29 | task: Inline editing of expenses on the Transactions page (localDb.updateExpense w/ cross-month chunk move, updateExpenseSchema, ExpenseRowEditor, useMutation + ['expenses']/['analytics'] invalidation) in minima-spend | result: PASS (tester retry 1 for jest-dom toBeDisabled; code-gate edit-error-${field} template testid = known FP; verify lint: coder retry 1 for import/order, tester retry 2 for test-file lint via verified worktree diff, then 4 line-shifted pre-existing errors accepted by user) | tokens: 117518
- run_20260929_212417 | 2026-09-29 | task: Harden the Cloudflare Worker CORS proxy in req-lab/backend against open-relay use, add custom-header preflight support | result: PASS (1 test-reviewer retry for a tautological Vary assertion; sandbox initially FAIL'd due to a runs/{RUN_ID}/tests/ path-mirroring bug, not a code defect, see Pipeline gotchas; verify.py FAIL on two plan scope-checks was a stale-`main` false positive on a chained orchestrator branch, verified and accepted by user, see Pipeline gotchas) | tokens: 67538
- run_20260804_165504 | 2026-08-04 | task: Feedback form in settings (new Feedback tab, route-based like SetsTabs, POST /feedback persisting userId+text via a new Feedback Prisma model, auth-only via existing global JwtAuthGuard) in coin-collector-companion | result: PASS (first sandbox run failed on unrelated pre-existing tests due to a Prisma-generate Test-command gap the Architect scoped too narrowly, see Known gotchas; fixed by correcting the Test command, no Coder retry needed) | tokens: 79851
- run_20260929_200230 | 2026-09-29 | task: Restore green test suite + clean tsc in MinimaSpend (no feature work) | result: PASS (zero retries at any stage; check-contract, test-reviewer, sandbox, and verify all passed first try) | tokens: 38425
- run_20260802_221803 | 2026-08-02 | task: Step 2 of backlog_password-management.md (JWT refresh tokens) in coin-collector-companion — RefreshToken table + TokenService rotate-on-use/reuse-detection, POST /auth/refresh + POST /auth/logout (cookie-authenticated), access token 7d->15m, login sets httpOnly refresh cookie, changePassword retrofitted to revoke all sessions, apiFetch credentials:'include' + silent refresh-and-retry on 401 | result: PASS | tokens: 103044
- run_20260802_183303 | 2026-08-02 | task: Step 1 of backlog_password-management.md (change password) in coin-collector-companion — PATCH /auth/password, change-password form on /settings, apiFetch skipAuthRedirectOn401 opt-out, i18n en/es | result: PASS | tokens: 69574
- run_20260802_172836 | 2026-08-02 | task: Step 0 of backlog_password-management.md (settings page scaffold + account info) in coin-collector-companion — GET /auth/me, /settings route, nav link, i18n en/es | result: PASS | tokens: 56819
- run_20260801_142634 | 2026-08-01 | task: static Glossary page (docs/backlog_glossary.md) in coin-collector-companion — /glossary route, 20-term data file, i18n en/es, nav link | result: PASS | tokens: 70376
- run_20260730_153718 | 2026-07-30 | task: migrate the two-button language switcher in coin-collector-companion's nav to a single dropdown | result: PASS | tokens: 28220
- run_20260728_071525 | 2026-07-28 | task: implement the "Coin Collector Companion" Classical design system (from claude.ai/design) across all 12 apps/web routes in coin-collector-companion, restyle only (no logic rebuild) | result: PASS (resumed mid-pipeline from an interrupted Coder step; fixed a code-contract false-positive class from dynamic/removed/test-only testids in plan.md's appendix, a genuine missing home-paragraph testid, a durable sandbox fix for building workspace library packages before tests, and a Set Editor race/missing-field bug on retry 1/2) | tokens: 367781
- run_20260725_140648 | 2026-07-25 | task: catalog coin contributions — user-submitted catalog coins (backlog_catalog-contributions.md) in coin-collector-companion | result: PASS (schema/status field + POST /catalog + submission/confirmation UI; live-DB migration apply and manual e2e pass deferred, documented) | tokens: 105643
- run_20260722_121303 | 2026-07-22 | task: Day 5 — styling pass, top-level nav, DB cleanup script (backlog_week3.md Day 5) in coin-collector-companion | result: PASS (scope limited to automatable items 5.1/5.2/5.6-code, per explicit user choice; 5.3/5.4/5.5-build+console/5.6-execution left as a documented manual checklist) | tokens: 70792
- run_20260627_113337 | 2026-06-27 | task: add possibility to delete current user | result: PASS
- run_20260629_171846 | 2026-06-29 | task: add a Budget Summary card component (total income, expenses, net balance) | result: PASS
- run_20260716_133354 | 2026-07-16 | task: migrate current project to pnpm monorepo | result: PASS | tokens: 23367
- run_20260717_221635 | 2026-07-17 | task: add Cloudflare Workers CORS proxy backend in /backend | result: PASS | tokens: 26971
- run_20260721_171115 | 2026-07-21 | task: Implement day 4 from backlog_week3.md (set editor + gap view + collection page) | result: PASS | tokens: 74601
- run_20260718_131730 | 2026-07-18 | task: Phase 2 custom set builder (generate-slots, POST /sets, PATCH /sets/:id/slots) in coin-collector-companion | result: PASS | tokens: 69607
- run_20260719_190933 | 2026-07-19 | task: Catalog endpoints + Wikipedia attribution footer (backlog_week1.md 5.1-5.2) in coin-collector-companion | result: PASS | tokens: 37450
- run_20260719_200109 | 2026-07-19 | task: SetsModule create/rename/delete/list + clone-from-canonical/user (backlog_week2.md Day 1) in coin-collector-companion | result: PASS | tokens: 48860
- run_20260720_070901 | 2026-07-20 | task: Coin membership on user sets + canonical/public-set reads (backlog_week2.md Day 2) in coin-collector-companion | result: PASS | tokens: 70151
- run_20260721_094026 | 2026-07-21 | task: Day 1 auth foundation — token storage, apiFetch Bearer/401, login/signup, RequireAuth guard (backlog_week3.md Day 1) in coin-collector-companion | result: PASS | tokens: 49410
- run_20260720_142942 | 2026-07-20 | task: Collection/ownership module + gap-view endpoint (backlog_week2.md Day 4) in coin-collector-companion | result: PASS | tokens: 99251
- run_20260720_121716 | 2026-07-20 | task: Canonical-set seed templates + admin seed script (backlog_week2.md Day 3, tasks 3.1-3.2) in coin-collector-companion | result: PASS | tokens: 48541
- run_20260720_171320 | 2026-07-20 | task: Week 2 Day 5 live E2E verification + wrap-up (backlog_week2.md 4.4/4.5 re-confirm, 5.1-5.5) in coin-collector-companion | result: PASS (sandbox hit known Prisma-generation gap, verified directly against real repo instead per user approval) | tokens: 56913
- run_20260721_131640 | 2026-07-21 | task: Anonymous browse — catalog + canonical sets (backlog_week3.md Day 2) in coin-collector-companion | result: PASS (paused once for human sign-off on a TESTS_MODIFIED_AFTER_REVIEW gate, resumed and completed; 1 Coder retry for a React 19 use()+Suspense environment bug) | tokens: 61464
- run_20260721_161448 | 2026-07-21 | task: Public sets, dashboard, set creation (backlog_week3.md Day 3) in coin-collector-companion | result: PASS (flagged a real API gap — no GET /sets/:id endpoint — rather than adding a backend route; first sandbox run failed on the recurring packages/shared dist/ gap, fixed by correcting the Test command, no Coder retry needed) | tokens: 73053
- run_20260726_221855 | 2026-07-26 | task: i18n infrastructure for apps/web (translation of all phrases, en/es locales, persisted switcher, translation-ready catalog data) in coin-collector-companion | result: PASS (dependency-free Context-based i18n across 25 files/6 new modules; first sandbox run failed on the recurring packages/shared dist/ gap — should have been caught by grepping memory.md before Stage 0's Test command was set, see Known gotchas update — fixed by correcting the Test command, no Coder retry needed) | tokens: 58244
- run_20260731_132040 | 2026-07-31 | task: Implement docs/backlog_my-submissions.md (GET /catalog?submittedByMe=true) | result: PASS | tokens: 95037

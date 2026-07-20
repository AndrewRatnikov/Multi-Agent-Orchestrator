#!/usr/bin/env bash
# check-contract.sh — mechanical contract-compliance gate
#
# Usage:
#   check-contract.sh <run_id> <repo_path>          # test-compliance mode: run after
#                                                    # the Tester, before the LLM reviewer
#   check-contract.sh <run_id> <repo_path> --code    # code-compliance mode: run after
#                                                     # the Coder, before the sandbox
#
# Exit 0 = clean. Exit 1 = violations found (printed one per line to stdout,
# each prefixed with a machine-readable check id).
#
# This intentionally does NOT parse TypeScript properly — it's grep/sed/comm.
# False negatives are acceptable: the LLM reviewer still runs after this for
# judgment calls (assertion quality, coverage). What this script catches is
# meant to never need judgment: either the name exists somewhere or it doesn't.

set -uo pipefail

RUN_ID="${1:?Usage: check-contract.sh <run_id> <repo_path> [--code]}"
REPO_PATH="${2:?Usage: check-contract.sh <run_id> <repo_path> [--code]}"
MODE="${3:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORCHESTRATOR_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
RUN_DIR="$ORCHESTRATOR_ROOT/runs/$RUN_ID"
PLAN="$RUN_DIR/plan.md"
TESTS_DIR="$RUN_DIR/tests"
CODE_DIR="$RUN_DIR/code"

VIOLATIONS=()
add_violation() { VIOLATIONS+=("$1"); }

if [ ! -f "$PLAN" ]; then
  echo "CONTRACT_ERROR: plan.md not found at $PLAN"
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ════════════════════════════════════════════════════════════════════════
# MODE: --code  (post-Coder checks)
# ════════════════════════════════════════════════════════════════════════
if [ "$MODE" = "--code" ]; then

  # ── Check 5a: every contract testid appears somewhere in code/ ───────────
  grep -ohE 'data-testid="[^"]+"' "$PLAN" 2>/dev/null | sed 's/.*="//;s/"$//' | sort -u > "$TMP/contract_testids"
  if [ -s "$TMP/contract_testids" ] && [ -d "$CODE_DIR" ]; then
    while IFS= read -r tid; do
      [ -z "$tid" ] && continue
      if ! grep -rqF "$tid" "$CODE_DIR" 2>/dev/null; then
        add_violation "MISSING_TESTID_IN_CODE: data-testid=\"$tid\" is in the contract but not found anywhere in runs/$RUN_ID/code/"
      fi
    done < "$TMP/contract_testids"
  fi

  # ── Check 5b: no test file edited after the test-reviewer PASS gate ──────
  SENTINEL="$RUN_DIR/.test_reviewer_passed_at"
  if [ -f "$SENTINEL" ] && [ -d "$TESTS_DIR" ]; then
    NEWER=$(find "$TESTS_DIR" -type f -newer "$SENTINEL" 2>/dev/null)
    if [ -n "$NEWER" ]; then
      while IFS= read -r f; do
        [ -z "$f" ] && continue
        add_violation "TESTS_MODIFIED_AFTER_REVIEW: $f has an mtime newer than the test-reviewer PASS gate"
      done <<< "$NEWER"
    fi
  fi

  if [ ${#VIOLATIONS[@]} -eq 0 ]; then
    echo "check-contract --code: clean. ${RUN_ID}"
    exit 0
  else
    printf '%s\n' "${VIOLATIONS[@]}"
    exit 1
  fi
fi

# ════════════════════════════════════════════════════════════════════════
# MODE: default (pre-Coder checks, run after Tester)
# ════════════════════════════════════════════════════════════════════════

if [ ! -d "$TESTS_DIR" ]; then
  echo "CONTRACT_ERROR: runs/$RUN_ID/tests/ not found"
  exit 1
fi

# ── Check 1: data-testid parity (tests → contract) ──────────────────────────
grep -rhoE "(get|query|find)(All)?ByTestId\(['\"][^'\"]+" "$TESTS_DIR" 2>/dev/null \
  | sed "s/.*['\"]//" > "$TMP/used_testids_raw"
grep -rhoE 'data-testid="[^"]+"' "$TESTS_DIR" 2>/dev/null \
  | sed 's/.*="//;s/"$//' >> "$TMP/used_testids_raw"
sort -u "$TMP/used_testids_raw" -o "$TMP/used_testids"

grep -ohE 'data-testid="[^"]+"' "$PLAN" 2>/dev/null \
  | sed 's/.*="//;s/"$//' | sort -u > "$TMP/declared_testids"

comm -23 "$TMP/used_testids" "$TMP/declared_testids" > "$TMP/testid_violations" || true
if [ -s "$TMP/testid_violations" ]; then
  while IFS= read -r tid; do
    [ -z "$tid" ] && continue
    add_violation "TESTID_NOT_IN_CONTRACT: test references data-testid=\"$tid\" which is not declared in plan.md's Interface Contract"
  done < "$TMP/testid_violations"
fi

# ── Check 2: package imports exist in the TARGET repo's package.json ────────
# Read the repo's package.json directly — repo-digest.md strips devDependencies,
# which is exactly why the @testing-library/jest-dom regression slipped past
# the LLM gate in run 1.
#
# Monorepo-aware: a pnpm/npm/yarn workspace declares real dependencies in each
# workspace member's own package.json (e.g. apps/api/package.json), not just the
# root one — checking only the root produces false UNKNOWN_PACKAGE violations for
# every backend-only dependency in a workspace repo. When pnpm-workspace.yaml is
# present, aggregate dependencies across the root plus every package.json matched
# by its `packages:` glob entries (only simple `dir/*` entries are expanded; exact
# paths are used as-is).
if [ -f "$REPO_PATH/package.json" ]; then
  node -e "
    const fs = require('fs');
    const path = require('path');
    const repoPath = '$REPO_PATH';

    function depsOf(pkgJsonPath) {
      try {
        const p = require(pkgJsonPath);
        return Object.assign({}, p.dependencies||{}, p.devDependencies||{}, p.peerDependencies||{});
      } catch (e) {
        return {};
      }
    }

    let allDeps = depsOf(path.join(repoPath, 'package.json'));

    const workspaceFile = path.join(repoPath, 'pnpm-workspace.yaml');
    if (fs.existsSync(workspaceFile)) {
      const lines = fs.readFileSync(workspaceFile, 'utf-8').split('\n');
      let inPackages = false;
      const patterns = [];
      for (const line of lines) {
        if (/^packages:/.test(line)) { inPackages = true; continue; }
        if (inPackages) {
          const m = line.match(/^\s*-\s*['\"]?([^'\"#]+)['\"]?\s*$/);
          if (m) { patterns.push(m[1].trim()); continue; }
          if (/^\S/.test(line)) inPackages = false; // dedented out of the packages: block
        }
      }
      for (const pattern of patterns) {
        if (pattern.endsWith('/*')) {
          const dir = path.join(repoPath, pattern.slice(0, -2));
          if (fs.existsSync(dir)) {
            for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
              if (entry.isDirectory()) {
                allDeps = Object.assign(allDeps, depsOf(path.join(dir, entry.name, 'package.json')));
              }
            }
          }
        } else {
          allDeps = Object.assign(allDeps, depsOf(path.join(repoPath, pattern, 'package.json')));
        }
      }
    }

    console.log(Object.keys(allDeps).join('\n'));
  " > "$TMP/known_packages" 2>/dev/null || touch "$TMP/known_packages"

  # Bare-specifier imports: not starting with '.' (relative) and not the '@/' path alias.
  # Matches both `import x from 'pkg'` and side-effect `import 'pkg'` forms.
  grep -rhoE "^[[:space:]]*import[^;]*['\"][^'\"]+['\"]" "$TESTS_DIR" 2>/dev/null \
    | grep -oE "['\"][^'\"]+['\"]\$" | tr -d "'\"" \
    | grep -vE '^\.' | grep -vE '^@/' \
    | sort -u > "$TMP/imported_specifiers"

  while IFS= read -r spec; do
    [ -z "$spec" ] && continue
    # Node.js builtins (with or without the 'node:' prefix) are always available
    # and never appear in package.json — skip them rather than flag a false positive.
    bare="${spec#node:}"
    case "$bare" in
      assert|assert/strict|async_hooks|buffer|child_process|cluster|console|constants|crypto|dgram|diagnostics_channel|dns|dns/promises|domain|events|fs|fs/promises|http|http2|https|inspector|inspector/promises|module|net|os|path|path/posix|path/win32|perf_hooks|process|punycode|querystring|readline|readline/promises|repl|stream|string_decoder|sys|timers|timers/promises|tls|trace_events|tty|url|util|util/types|v8|vm|wasi|worker_threads|zlib)
        continue ;;
    esac
    if [[ "$spec" == @*/* ]]; then
      pkg="$(echo "$spec" | cut -d/ -f1-2)"
    else
      pkg="$(echo "$spec" | cut -d/ -f1)"
    fi
    if ! grep -qxF "$pkg" "$TMP/known_packages"; then
      add_violation "UNKNOWN_PACKAGE: test imports '$spec' (package '$pkg') which is not in $REPO_PATH/package.json dependencies or devDependencies"
    fi
  done < "$TMP/imported_specifiers"
fi

# ── Check 3: relative/alias imports resolve to a contract File: path ───────
grep -ohE '\*\*File:\*\*[[:space:]]*`[^`]+`' "$PLAN" 2>/dev/null \
  | sed -E 's/.*`([^`]+)`.*/\1/' | sed -E 's/\.[jt]sx?$//' | sort -u > "$TMP/contract_files"

# Resolve the @/ alias from the target repo's tsconfig.json if present (default: @/ -> src/).
ALIAS_TARGET="src"
if [ -f "$REPO_PATH/tsconfig.json" ]; then
  RESOLVED=$(node -e "
    try {
      const t = require('$REPO_PATH/tsconfig.json');
      const paths = (t.compilerOptions && t.compilerOptions.paths) || {};
      const key = Object.keys(paths).find(k => k.startsWith('@/'));
      if (key) console.log(paths[key][0].replace(/\/\*\$/, ''));
    } catch (e) {}
  " 2>/dev/null)
  [ -n "$RESOLVED" ] && ALIAS_TARGET="$RESOLVED"
fi

if [ -s "$TMP/contract_files" ]; then
  # Process file-by-file so relative ('./', '../') imports resolve against each
  # test file's OWN directory, not naively against the repo root. TESTS_DIR mirrors
  # the target repo's real directory structure (e.g.
  # tests/apps/api/src/sets/sets.controller.spec.ts), so a test file's path relative
  # to TESTS_DIR is exactly its future repo-relative directory — joining a relative
  # import against that directory (not against $REPO_PATH directly) is what makes
  # the existing-file fallback below actually correct for nested test files that
  # import untouched sibling files (e.g. a Day-2 test importing a Day-1 DTO that
  # isn't itself part of this run's Interface Contract).
  : > "$TMP/violations_check3"
  while IFS= read -r -d '' testfile; do
    rel_dir="$(dirname "${testfile#"$TESTS_DIR"/}")"

    grep -hoE "^[[:space:]]*import[^;]*['\"][^'\"]+['\"]" "$testfile" 2>/dev/null \
      | grep -oE "['\"][^'\"]+['\"]\$" | tr -d "'\"" \
      | grep -E '^(\./|\.\./|@/)' | sort -u | while IFS= read -r imp; do
        [ -z "$imp" ] && continue
        if [[ "$imp" == @/* ]]; then
          resolved="${ALIAS_TARGET}/${imp#@/}"
        else
          resolved=$(node -e "console.log(require('path').normalize(require('path').join('$rel_dir', '$imp')))" 2>/dev/null)
          [ -z "$resolved" ] && resolved="$imp"
        fi
        resolved="${resolved%.ts}"; resolved="${resolved%.tsx}"; resolved="${resolved%.js}"; resolved="${resolved%.jsx}"
        resolved="${resolved#./}"
        # An import is legitimate if it matches a contract File: path OR already
        # exists in the target repo (tests may import existing helpers/types/utils
        # that are correctly absent from the contract — only NEW names must come
        # from the contract).
        if grep -qF "$resolved" "$TMP/contract_files"; then
          continue
        fi
        exists_in_repo=0
        for ext in ts tsx js jsx; do
          if [ -f "$REPO_PATH/$resolved.$ext" ]; then exists_in_repo=1; break; fi
        done
        if [ "$exists_in_repo" -eq 1 ] || [ -d "$REPO_PATH/$resolved" ] \
           || [ -f "$REPO_PATH/$resolved/index.ts" ] || [ -f "$REPO_PATH/$resolved/index.tsx" ]; then
          continue
        fi
        echo "IMPORT_NOT_IN_CONTRACT: test imports '$imp' (resolved: $resolved) which matches neither an Interface Contract File: path nor an existing file in the target repo" >> "$TMP/violations_check3"
      done
  done < <(find "$TESTS_DIR" -type f \( -name '*.test.*' -o -name '*.spec.*' \) -print0)

  if [ -s "$TMP/violations_check3" ]; then
    while IFS= read -r v; do add_violation "$v"; done < "$TMP/violations_check3"
  fi
fi

# ── Check 4: banned patterns ─────────────────────────────────────────────────
while IFS= read -r -d '' f; do
  if grep -qE 'while[[:space:]]*\([[:space:]]*true' "$f"; then
    add_violation "BANNED_PATTERN: $f contains while(true)"
  fi
  if grep -qE '\bfetch\(|axios\.' "$f" && ! grep -qE '\b(vi|jest)\.mock\(' "$f"; then
    add_violation "BANNED_PATTERN: $f calls fetch()/axios without a vi.mock()/jest.mock() in the same file"
  fi
done < <(find "$TESTS_DIR" -type f \( -name '*.test.*' -o -name '*.spec.*' \) -print0)

# ── Verdict ──────────────────────────────────────────────────────────────────
if [ ${#VIOLATIONS[@]} -eq 0 ]; then
  echo "check-contract: clean. ${RUN_ID}"
  exit 0
else
  printf '%s\n' "${VIOLATIONS[@]}"
  exit 1
fi

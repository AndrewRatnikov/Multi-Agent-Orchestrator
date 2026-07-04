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
if [ -f "$REPO_PATH/package.json" ]; then
  node -e "
    const p = require('$REPO_PATH/package.json');
    const deps = Object.assign({}, p.dependencies||{}, p.devDependencies||{}, p.peerDependencies||{});
    console.log(Object.keys(deps).join('\n'));
  " > "$TMP/known_packages" 2>/dev/null || touch "$TMP/known_packages"

  # Bare-specifier imports: not starting with '.' (relative) and not the '@/' path alias.
  # Matches both `import x from 'pkg'` and side-effect `import 'pkg'` forms.
  grep -rhoE "^[[:space:]]*import[^;]*['\"][^'\"]+['\"]" "$TESTS_DIR" 2>/dev/null \
    | grep -oE "['\"][^'\"]+['\"]\$" | tr -d "'\"" \
    | grep -vE '^\.' | grep -vE '^@/' \
    | sort -u > "$TMP/imported_specifiers"

  while IFS= read -r spec; do
    [ -z "$spec" ] && continue
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
  grep -rhoE "^[[:space:]]*import[^;]*['\"][^'\"]+['\"]" "$TESTS_DIR" 2>/dev/null \
    | grep -oE "['\"][^'\"]+['\"]\$" | tr -d "'\"" \
    | grep -E '^(\.|@/)' | sort -u > "$TMP/code_imports"

  while IFS= read -r imp; do
    [ -z "$imp" ] && continue
    resolved="$imp"
    if [[ "$imp" == @/* ]]; then
      resolved="${ALIAS_TARGET}/${imp#@/}"
    fi
    resolved="${resolved%.ts}"; resolved="${resolved%.tsx}"; resolved="${resolved%.js}"; resolved="${resolved%.jsx}"
    resolved="${resolved#./}"
    if ! grep -qF "$resolved" "$TMP/contract_files"; then
      add_violation "IMPORT_NOT_IN_CONTRACT: test imports '$imp' (resolved: $resolved) which does not match any Interface Contract File: path"
    fi
  done < "$TMP/code_imports"
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

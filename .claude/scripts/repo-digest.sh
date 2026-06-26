#!/usr/bin/env bash
# repo-digest.sh — generates a repo digest for the Architect agent
#
# Usage: repo-digest.sh <repo_path> <run_id> [task_hint]
#
# Output: runs/{run_id}/repo-digest.md
# Idempotent: skips generation if repo-digest.md already exists (cached per run)

set -euo pipefail

REPO_PATH="${1:?Usage: repo-digest.sh <repo_path> <run_id> [task_hint]}"
RUN_ID="${2:?Usage: repo-digest.sh <repo_path> <run_id> [task_hint]}"
TASK_HINT="${3:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORCHESTRATOR_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
OUTPUT="$ORCHESTRATOR_ROOT/runs/$RUN_ID/repo-digest.md"

# Cache: skip if already generated for this run
if [ -f "$OUTPUT" ]; then
  echo "repo-digest: cache hit — skipping generation ($OUTPUT)"
  exit 0
fi

cd "$REPO_PATH"

echo "repo-digest: generating for run $RUN_ID..."

{
  echo "# Repo Digest"
  echo "_Generated: $(date -u +"%Y-%m-%dT%H:%M:%SZ") | Run: ${RUN_ID}_"
  echo ""

  # ── 1. Directory tree (depth 3, exclude noise) ──────────────────────────────
  echo "## Directory tree"
  echo '```'
  find . -maxdepth 3 \
    -not -path './.git/*' \
    -not -path './node_modules/*' \
    -not -path './runs/*' \
    -not -path './.next/*' \
    -not -path './dist/*' \
    -not -path './build/*' \
    -not -path './coverage/*' \
    -not -name '*.lock' \
    | sort \
    | sed 's|^\./||' \
    | sed 's|[^/]*/|  |g'
  echo '```'
  echo ""

  # ── 2. Package manifest (scripts + dependencies only) ───────────────────────
  if [ -f "package.json" ]; then
    echo "## package.json (scripts + dependencies)"
    echo '```json'
    # Extract only scripts and dependencies sections via node
    node -e "
      const p = require('./package.json');
      const out = {};
      if (p.scripts)          out.scripts = p.scripts;
      if (p.dependencies)     out.dependencies = p.dependencies;
      if (p.peerDependencies) out.peerDependencies = p.peerDependencies;
      console.log(JSON.stringify(out, null, 2));
    " 2>/dev/null || cat package.json
    echo '```'
    echo ""

    # ── 3. Test command ────────────────────────────────────────────────────────
    echo "## Test command"
    TEST_CMD=$(node -e "
      const p = require('./package.json');
      const s = p.scripts || {};
      // prefer 'test', then 'test:unit', then first key containing 'test'
      const key = Object.keys(s).find(k => k === 'test')
               || Object.keys(s).find(k => k === 'test:unit')
               || Object.keys(s).find(k => k.includes('test'));
      console.log(key ? \`npm run \${key}\` : 'UNKNOWN');
    " 2>/dev/null || echo "UNKNOWN")
    echo '```'
    echo "$TEST_CMD"
    echo '```'
    echo ""
  elif [ -f "pyproject.toml" ]; then
    echo "## pyproject.toml"
    echo '```toml'
    cat pyproject.toml
    echo '```'
    echo ""
    echo "## Test command"
    echo '```'
    echo "pytest"
    echo '```'
    echo ""
  fi

  # ── 4. Existing component / source files ────────────────────────────────────
  echo "## Existing source files"

  # TypeScript/React projects
  if find . -name "*.tsx" -not -path './node_modules/*' | grep -q .; then
    echo "### Components (.tsx)"
    echo '```'
    find . -name "*.tsx" \
      -not -path './node_modules/*' \
      -not -path './.next/*' \
      -not -path './dist/*' \
      | sort | sed 's|^\./||'
    echo '```'
    echo ""
  fi

  if find . -name "*.ts" -not -path './node_modules/*' -not -name "*.d.ts" | grep -q .; then
    echo "### TypeScript files (.ts)"
    echo '```'
    find . -name "*.ts" \
      -not -path './node_modules/*' \
      -not -name "*.d.ts" \
      -not -path './.next/*' \
      -not -path './dist/*' \
      | sort | sed 's|^\./||'
    echo '```'
    echo ""
  fi

  # Python projects
  if find . -name "*.py" -not -path './node_modules/*' | grep -q .; then
    echo "### Python files (.py)"
    echo '```'
    find . -name "*.py" \
      -not -path './.git/*' \
      -not -path './node_modules/*' \
      | sort | sed 's|^\./||'
    echo '```'
    echo ""
  fi

  # ── 5. Similar files (if task hint provided) ─────────────────────────────────
  if [ -n "$TASK_HINT" ]; then
    echo "## Similar existing files (task hint: \"$TASK_HINT\")"
    echo '```'
    # Extract meaningful words from the hint and search for similar filenames
    KEYWORDS=$(echo "$TASK_HINT" | tr '[:upper:]' '[:lower:]' | grep -oE '[a-z]{3,}' | head -5 | tr '\n' '|' | sed 's/|$//')
    if [ -n "$KEYWORDS" ]; then
      find . \
        -not -path './node_modules/*' \
        -not -path './.git/*' \
        -not -path './runs/*' \
        -type f \
        | grep -iE "$KEYWORDS" \
        | sort \
        | head -20 \
        | sed 's|^\./||' \
        || echo "(no similar files found)"
    fi
    echo '```'
    echo ""
  fi

  # ── 6. Existing test files ───────────────────────────────────────────────────
  echo "## Existing test files"
  echo '```'
  find . \
    -not -path './node_modules/*' \
    -not -path './.git/*' \
    -not -path './runs/*' \
    -type f \
    \( -name "*.test.ts" -o -name "*.test.tsx" -o -name "*.spec.ts" -o -name "*.spec.tsx" \
       -o -name "*.test.js" -o -name "*.test.jsx" -o -name "*.spec.js" \
       -o -name "test_*.py" -o -name "*_test.py" \) \
    | sort | sed 's|^\./||'
  echo '```'
  echo ""

  # ── 7. tsconfig / jest config (conventions) ──────────────────────────────────
  if [ -f "tsconfig.json" ]; then
    echo "## tsconfig.json (path aliases + target)"
    echo '```json'
    node -e "
      const t = require('./tsconfig.json');
      const out = { compilerOptions: {} };
      const co = t.compilerOptions || {};
      ['target','module','baseUrl','paths','jsx','strict'].forEach(k => {
        if (co[k] !== undefined) out.compilerOptions[k] = co[k];
      });
      console.log(JSON.stringify(out, null, 2));
    " 2>/dev/null || cat tsconfig.json
    echo '```'
    echo ""
  fi

  if [ -f "jest.config.js" ] || [ -f "jest.config.ts" ] || [ -f "jest.config.mjs" ]; then
    echo "## Jest config"
    echo '```'
    cat jest.config.* 2>/dev/null | head -40
    echo '```'
    echo ""
  fi

} > "$OUTPUT"

# Token estimate (rough: 1 token ≈ 4 chars)
CHAR_COUNT=$(wc -c < "$OUTPUT")
TOKEN_EST=$(( CHAR_COUNT / 4 ))
echo "repo-digest: written to $OUTPUT (~${TOKEN_EST} tokens)"

if [ "$TOKEN_EST" -gt 2000 ]; then
  echo "repo-digest: WARNING — digest is large (~${TOKEN_EST} tokens). Consider trimming devDependencies or file lists."
fi

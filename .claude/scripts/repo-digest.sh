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

# Directories that are never part of the repo's own source, pruned at ANY depth:
# node_modules (every workspace member has one) and .claude/worktrees (stale
# full copies of the repo left by Claude Code worktree sessions).
PRUNE_ANY=( -name node_modules -o -name .git -o -path '*/.claude/worktrees' )

# list_files <find tests...>: repo files matching the tests, minus noise, sorted, no ./ prefix
list_files() {
  find . \( "${PRUNE_ANY[@]}" -o -path ./runs -o -path ./.next -o -path ./dist \) -prune \
    -o -type f \( "$@" \) -print \
    | sort | sed 's|^\./||'
}

# fenced <text>: print text inside a ``` block, nothing if empty
fenced() {
  echo '```'
  [ -n "$1" ] && printf '%s\n' "$1"
  echo '```'
  echo ""
}

# resolve_test_cmd: prints "<source>\t<command>" for a package.json repo.
# 1. a test script in the root package.json
# 2. {REPO}/.claude/rules/test-commands.md (bash/sh fences; every command line, deduped)
# 3. workspace members that have a test script
# Only when all three fail: UNKNOWN.
resolve_test_cmd() {
  node -e '
    const fs = require("fs"), path = require("path");
    const readJSON = f => { try { return JSON.parse(fs.readFileSync(f, "utf8")); } catch { return null; } };
    const testKey = s => { const k = Object.keys(s || {});
      return k.find(x => x === "test") || k.find(x => x === "test:unit") || k.find(x => x.includes("test")); };
    const done = (src, cmd) => { console.log(src + "\t" + cmd); process.exit(0); };
    const pkg = readJSON("package.json") || {};

    const rootKey = testKey(pkg.scripts);
    if (rootKey) done("root package.json", "npm run " + rootKey);

    const rules = ".claude/rules/test-commands.md";
    if (fs.existsSync(rules)) {
      const cmds = []; let inFence = false, take = false;
      for (const line of fs.readFileSync(rules, "utf8").split("\n")) {
        const m = line.match(/^\s*(```|~~~)\s*([\w-]*)\s*$/);
        if (m) { if (inFence) { inFence = false; take = false; } else { inFence = true; take = /^(bash|sh|shell|zsh|console)$/i.test(m[2]); } continue; }
        if (!take) continue;
        const c = line.trim().replace(/^\$\s+/, "");
        if (c && !c.startsWith("#") && !cmds.includes(c)) cmds.push(c);
      }
      if (cmds.length) done(rules, cmds.join(" && "));
    }

    let globs = [], pm = "npm";
    if (fs.existsSync("pnpm-workspace.yaml")) {
      pm = "pnpm"; let inPk = false;
      for (const line of fs.readFileSync("pnpm-workspace.yaml", "utf8").split("\n")) {
        if (/^packages\s*:/.test(line)) { inPk = true; continue; }
        if (inPk && /^\S/.test(line)) break;
        const m = inPk && line.match(/^\s*-\s*["\x27]?([^"\x27#]+?)["\x27]?\s*(#.*)?$/);
        if (m) globs.push(m[1]);
      }
    } else {
      const w = pkg.workspaces; globs = Array.isArray(w) ? w : (w && w.packages) || [];
      if (fs.existsSync("yarn.lock")) pm = "yarn";
    }
    const dirs = [];
    for (const g of globs) {
      if (g.startsWith("!")) continue;
      const m = g.match(/^(.*?)\/\*\*?$/);
      if (m) { try { for (const d of fs.readdirSync(m[1] || ".").sort()) dirs.push(path.join(m[1], d)); } catch {} }
      else dirs.push(g.replace(/\/$/, ""));
    }
    const cmds = [];
    for (const d of dirs) {
      const p = readJSON(path.join(d, "package.json")); if (!p) continue;
      const k = testKey(p.scripts); if (!k) continue;
      const name = p.name || d, run = k === "test" ? "test" : "run " + k;
      cmds.push(pm === "pnpm" ? `pnpm --filter ${name} ${run}`
              : pm === "yarn" ? `yarn workspace ${name} ${run}`
              : `npm run ${k} --workspace=${d}`);
    }
    if (cmds.length) done("workspace members", cmds.join(" && "));
    done("none found", "UNKNOWN");
  ' 2>/dev/null || printf 'node failed\tUNKNOWN\n'
}

TEST_SRC=""
{
  echo "# Repo Digest"
  echo "_Generated: $(date -u +"%Y-%m-%dT%H:%M:%SZ") | Run: ${RUN_ID}_"
  echo ""

  # ── 1. Directory tree (depth 3, exclude noise) ──────────────────────────────
  echo "## Directory tree"
  echo '```'
  find . -maxdepth 3 \
    \( "${PRUNE_ANY[@]}" -o -path ./runs -o -path ./.next -o -path ./dist -o -path ./build -o -path ./coverage \) -prune \
    -o -not -name '*.lock' -print \
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
    # run-tests.sh joins every non-fence line of this section with " && ", so
    # the section holds the fenced command and nothing else (no notes, no source).
    RESOLVED=$(resolve_test_cmd)
    TEST_SRC="${RESOLVED%%$'\t'*}"
    TEST_CMD="${RESOLVED#*$'\t'}"
    echo "## Test command"
    fenced "${TEST_CMD:-UNKNOWN}"
  elif [ -f "pyproject.toml" ]; then
    echo "## pyproject.toml"
    echo '```toml'
    cat pyproject.toml
    echo '```'
    echo ""
    TEST_SRC="pyproject.toml"
    echo "## Test command"
    fenced "pytest"
  fi

  # ── 4. Existing component / source files ────────────────────────────────────
  echo "## Existing source files"

  TSX=$(list_files -name '*.tsx')
  if [ -n "$TSX" ]; then
    echo "### Components (.tsx)"
    fenced "$TSX"
  fi

  TS=$(list_files -name '*.ts' -not -name '*.d.ts')
  if [ -n "$TS" ]; then
    echo "### TypeScript files (.ts)"
    fenced "$TS"
  fi

  PY=$(list_files -name '*.py')
  if [ -n "$PY" ]; then
    echo "### Python files (.py)"
    fenced "$PY"
  fi

  # ── 5. Similar files (if task hint provided) ─────────────────────────────────
  if [ -n "$TASK_HINT" ]; then
    # The heading gets only the first non-blank line of the task, capped at 80
    # chars: the full task can be pages long.
    HINT_TITLE=$(printf '%s\n' "$TASK_HINT" | awk 'NF{print; exit}' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | tr -d '"')
    if [ "${#HINT_TITLE}" -gt 80 ]; then HINT_TITLE="${HINT_TITLE:0:77}..."; fi
    echo "## Similar existing files (task hint: \"$HINT_TITLE\")"
    # Extract meaningful words from the hint and search for similar filenames
    KEYWORDS=$(printf '%s\n' "$TASK_HINT" | tr '[:upper:]' '[:lower:]' | grep -oE '[a-z]{3,}' | head -5 | tr '\n' '|' | sed 's/|$//' || true)
    SIMILAR=""
    if [ -n "$KEYWORDS" ]; then
      SIMILAR=$(list_files -name '*' | grep -iE "$KEYWORDS" | head -20 || true)
    fi
    fenced "${SIMILAR:-(no similar files found)}"
  fi

  # ── 6. Existing test files ───────────────────────────────────────────────────
  echo "## Existing test files"
  fenced "$(list_files -name '*.test.ts' -o -name '*.test.tsx' -o -name '*.spec.ts' -o -name '*.spec.tsx' \
              -o -name '*.test.js' -o -name '*.test.jsx' -o -name '*.spec.js' \
              -o -name 'test_*.py' -o -name '*_test.py')"

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
[ -n "$TEST_SRC" ] && echo "repo-digest: test command from $TEST_SRC"

if [ "$TOKEN_EST" -gt 2000 ]; then
  echo "repo-digest: WARNING — digest is large (~${TOKEN_EST} tokens). Consider trimming devDependencies or file lists."
fi

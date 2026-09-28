#!/bin/bash
# PreToolUse hook for pipeline subagents: allows Write/Edit only inside the
# artifact area that the given role owns. Exit 2 blocks the call, and stderr is
# shown to the agent. Usage (from agent frontmatter):
#   guard-writes.sh <product|architect|tester|coder>
#
# This is a deterministic backstop for the "stay in your folder" rule that used
# to live only in the prompts. Bash can still write files, so the orchestrator
# also runs `git status --porcelain` on the target repo after each stage.

ROLE="$1"
ROOT="${CLAUDE_PROJECT_DIR:-$(pwd)}"

INPUT=$(cat)
TARGET=$(printf '%s' "$INPUT" | /usr/bin/python3 -c '
import json, os, sys
d = json.load(sys.stdin)
ti = d.get("tool_input") or {}
p = ti.get("file_path") or ti.get("notebook_path") or ""
cwd = d.get("cwd") or os.getcwd()
if p and not os.path.isabs(p):
    p = os.path.join(cwd, p)
print(os.path.normpath(p) if p else "")
')

if [ -z "$TARGET" ]; then
  exit 0   # not a file-writing call we understand; let permissions decide
fi

REAL_ROOT="$(cd "$ROOT" && pwd -P)"
# Regexes, anchored. [^/]+ keeps a run id from swallowing extra path segments.
case "$ROLE" in
  product)   PATTERNS=("^runs/run_[^/]+/prd\.md$") ;;
  architect) PATTERNS=("^runs/run_[^/]+/plan\.md$" "^runs/run_[^/]+/repo-digest\.md$") ;;
  tester)    PATTERNS=("^runs/run_[^/]+/tests/.+") ;;
  coder)     PATTERNS=("^runs/run_[^/]+/code/.+") ;;
  *) echo "guard-writes.sh: unknown role '$ROLE'" >&2; exit 2 ;;
esac

# Resolve symlinks in the directory part so /Users/... vs /private/... or a
# symlinked checkout can't slip past the pattern match.
DIR=$(dirname "$TARGET")
while [ ! -d "$DIR" ] && [ "$DIR" != "/" ]; do DIR=$(dirname "$DIR"); done
REAL_DIR=$(cd "$DIR" && pwd -P)
REAL_TARGET="$REAL_DIR${TARGET#"$DIR"}"

REL="${REAL_TARGET#"$REAL_ROOT"/}"
if [ "$REL" != "$REAL_TARGET" ]; then   # target is inside the orchestrator project
  for pat in "${PATTERNS[@]}"; do
    if [[ "$REL" =~ $pat ]]; then
      exit 0
    fi
  done
fi

echo "Blocked by pipeline write guard: the $ROLE agent may only write to paths matching: ${PATTERNS[*]} (relative to the orchestrator project). You tried to write $TARGET. Put your output in your own artifact folder; the orchestrator copies it into the target repo." >&2
exit 2

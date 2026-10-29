#!/bin/bash
# PreToolUse hook for pipeline subagents: allows Write/Edit only inside the
# artifact area that the given role owns. Exit 2 blocks the call, and stderr is
# shown to the agent. Usage (from agent frontmatter):
#   guard-writes.sh <product|architect|tester|coder|render-reviewer|adr-writer>
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

TOOL=$(printf '%s' "$INPUT" | /usr/bin/python3 -c 'import json, sys; print(json.load(sys.stdin).get("tool_name") or "")')
MAX_REWRITE_BYTES=8000

if [ -z "$TARGET" ]; then
  exit 0   # not a file-writing call we understand; let permissions decide
fi

REAL_ROOT="$(cd "$ROOT" && pwd -P)"
# Regexes, anchored. [^/]+ keeps a run id from swallowing extra path segments.
case "$ROLE" in
  product)   PATTERNS=("^runs/run_[^/]+/prd\.md$") ;;
  architect) PATTERNS=("^runs/run_[^/]+/plan\.md$" "^runs/run_[^/]+/repo-digest\.md$" "^runs/run_[^/]+/contract\.json$") ;;
  tester)    PATTERNS=("^runs/run_[^/]+/tests/.+") ;;
  coder)     PATTERNS=("^runs/run_[^/]+/code/.+") ;;
  render-reviewer) PATTERNS=("^runs/run_[^/]+/render-review\.md$") ;;
  adr-writer) PATTERNS=("^runs/run_[^/]+/adr/.+") ;;
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
      # The Coder edits MODIFY files in place (seed-code.py copied them into
      # code/). Re-typing a large existing file with Write costs its whole size
      # in output tokens, plus the same again in context on every later call.
      # Generated files (runs/<run>/generated.json, written by regen.py) are
      # rebuilt by their generator after the Coder's pass; the Coder never edits them.
      if [ "$ROLE" = "coder" ]; then
        GEN=$(REL="$REL" ROOT="$REAL_ROOT" /usr/bin/python3 -c '
import json, os, re
m = re.match(r"runs/(run_[^/]+)/code/(.+)$", os.environ["REL"])
if m:
    try:
        data = json.load(open(os.path.join(os.environ["ROOT"], "runs", m.group(1), "generated.json")))
        for e in data.get("files") or []:
            if e.get("path") == m.group(2):
                print(e.get("command") or "its generator")
                break
    except Exception:
        pass
')
        # Sliced first pass (coder-slices.py): a pass may not do a LATER slice's work,
        # or its context grows back to what slicing is meant to avoid.
        LATER=$(REL="$REL" ROOT="$REAL_ROOT" /usr/bin/python3 -c '
import json, os, re
m = re.match(r"runs/(run_[^/]+)/code/(.+)$", os.environ["REL"])
if m:
    run = os.path.join(os.environ["ROOT"], "runs", m.group(1))
    try:
        k = int(open(os.path.join(run, ".coder-slice")).read().strip() or 0)
        if k > 0:
            for s in json.load(open(os.path.join(run, "coder-slices.json")))["slices"]:
                if s["index"] > k and any(f["path"] == m.group(2) for f in s["files"]):
                    print("%d (%s)" % (s["index"], s["label"]))
                    break
    except Exception:
        pass
')
        if [ -n "$LATER" ]; then
          echo "Blocked by pipeline write guard: $REL belongs to a later Coder slice, $LATER. Implement only your own slice's files (runs/<run>/coder-slices.json); the next pass does that file." >&2
          exit 2
        fi
        if [ -n "$GEN" ]; then
          echo "Blocked by pipeline write guard: $REL is a generated file (\`$GEN\`). Don't edit it: change its sources, and the orchestrator re-runs the generator after your pass." >&2
          exit 2
        fi
      fi
      if [ "$ROLE" = "coder" ] && [ "$TOOL" = "Write" ] && [ -f "$REAL_TARGET" ]; then
        SIZE=$(wc -c < "$REAL_TARGET" | tr -d ' ')
        if [ "$SIZE" -gt "$MAX_REWRITE_BYTES" ]; then
          echo "Blocked by pipeline write guard: $REL already exists ($SIZE bytes). Don't rewrite it with Write; change it with Edit (targeted edits; Read only the lines around each change first). Write is allowed for new files and for existing files up to $MAX_REWRITE_BYTES bytes." >&2
          exit 2
        fi
      fi
      exit 0
    fi
  done
fi

echo "Blocked by pipeline write guard: the $ROLE agent may only write to paths matching: ${PATTERNS[*]} (relative to the orchestrator project). You tried to write $TARGET. Put your output in your own artifact folder; the orchestrator copies it into the target repo." >&2
exit 2

#!/usr/bin/env python3
"""
check-adr.py: mechanical checks for the pipeline's Architecture Decision Records.

Three modes:

  check-adr.py <run_id> plan
      plan.md must have a `## Decisions` section, written by the Architect. It is
      either a single line starting with `None` (plus a reason), or one or more
      entries:

          ### D1: <the decision, one sentence>
          - **Source:** ...
          - **Options:** ...
          - **Why:** ...
          - **Supersedes:** ... | none
          - **ADR:** yes | no: <reason>

  check-adr.py <run_id> <repo> adr
      Checks the ADR writer's output in runs/{RUN_ID}/adr/, which mirrors the repo
      root (so a new ADR is runs/{RUN_ID}/adr/docs/adr/2026-10-12-slug.md), against
      the target repo, before the orchestrator copies it in:
        - only files under docs/adr/                                  OUTSIDE_ADR_DIR
        - ADR names are YYYY-MM-DD-lowercase-slug.md                  BAD_NAME
        - a new ADR has a `# ` title, Status (Accepted|Proposed),
          Date equal to the name's date, Source naming the run, Code,
          and Context / Decision / Alternatives considered /
          Consequences sections                                       MISSING_FIELD / MISSING_SECTION / DATE_MISMATCH / BAD_STATUS
        - an existing ADR may only have its Status line changed,
          to "Superseded by [..](..)"                                 EXISTING_ADR_EDITED
        - an ADR named in a new ADR's Supersedes line is marked
          "Superseded by" that new ADR                                SUPERSEDE_NOT_MARKED
        - README.md links every ADR in docs/adr/                      MISSING_INDEX_ROW
        - every relative .md link in the staged files resolves        BROKEN_LINK

        - every repo path in a new ADR's Code and Supersedes lines
          exists on the branch (spans containing a "/"; routes and
          spans with spaces or {placeholders} are skipped)            MISSING_CODE_PATH

  check-adr.py <run_id> trivial
      Decides whether Stage 6d can skip the ADR writer. Exit 0 (trivial) only when
      all of these hold, else exit 1 with the reason:
        - plan.md's `## Decisions` is `None: …`, or every entry says `ADR: no`
        - there is no runs/{RUN_ID}/answers-*.md (the user made no decision)
        - runs/{RUN_ID}/deviations.md is absent or empty (the Coder kept to the plan)

Exit: 0 clean, 1 violations (one per line on stdout), 3 usage/setup error.
"""

import glob
import os
import re
import sys

ORCH_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ADR_DIR = "docs/adr"
NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
CODE_SPAN_RE = re.compile(r"`([^`]+)`")
REQUIRED_SECTIONS = ["Context", "Decision", "Alternatives considered", "Consequences"]
DECISION_FIELDS = ["Source", "Options", "Why", "Supersedes", "ADR"]


def usage(msg):
    print(f"usage error: {msg}")
    print(__doc__.strip().splitlines()[0])
    sys.exit(3)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def field(text, name):
    m = re.search(rf"^- \*\*{re.escape(name)}:\*\*[ \t]*(.*)$", text, re.M)
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------- plan mode

def check_plan(run_id):
    plan = os.path.join(ORCH_ROOT, "runs", run_id, "plan.md")
    if not os.path.isfile(plan):
        usage(f"{plan} not found")
    text = read(plan)
    m = re.search(r"^## Decisions[ \t]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not m:
        return ["MISSING_DECISIONS_SECTION plan.md has no `## Decisions` section"]
    body = m.group(1).strip()
    entries = re.split(r"^### ", body, flags=re.M)
    head, entries = entries[0].strip(), entries[1:]
    if not entries:
        if re.match(r"^None\b\S*\s*\S", head):
            return []
        return ["EMPTY_DECISIONS `## Decisions` must hold `### D<n>:` entries, or one line `None: <reason>`"]
    out = []
    for e in entries:
        title_line = e.splitlines()[0].strip()
        if not re.match(r"^D\d+:\s*\S", title_line):
            out.append(f"BAD_DECISION_TITLE `### {title_line}` (expected `### D<n>: <decision>`)")
            continue
        did = title_line.split(":")[0]
        for name in DECISION_FIELDS:
            v = field(e, name)
            if not v:
                out.append(f"MISSING_DECISION_FIELD {did}: no `- **{name}:**` line")
        adr = (field(e, "ADR") or "").lower()
        if adr and not (adr.startswith("yes") or re.match(r"^no\b\W*\w", adr)):
            out.append(f"BAD_ADR_FLAG {did}: `ADR:` must be `yes` or `no: <reason>`")
    return out


# ------------------------------------------------------------- trivial mode

def check_trivial(run_id):
    """Reasons the run is NOT trivial (empty list = trivial, the writer can be skipped)."""
    run_dir = os.path.join(ORCH_ROOT, "runs", run_id)
    plan = os.path.join(run_dir, "plan.md")
    if not os.path.isfile(plan):
        usage(f"{plan} not found")
    reasons = []
    m = re.search(r"^## Decisions[ \t]*\n(.*?)(?=^## |\Z)", read(plan), re.M | re.S)
    if not m:
        reasons.append("plan.md has no `## Decisions` section (planned before the ADR stage existed)")
    else:
        for e in re.split(r"^### ", m.group(1).strip(), flags=re.M)[1:]:
            did = e.splitlines()[0].split(":")[0].strip()
            if not (field(e, "ADR") or "").lower().startswith("no"):
                reasons.append(f"plan.md decision {did} is marked for an ADR")
    answers = sorted(glob.glob(os.path.join(run_dir, "answers-*.md")))
    if answers:
        reasons.append("the user answered questions: " + ", ".join(os.path.basename(a) for a in answers))
    dev = os.path.join(run_dir, "deviations.md")
    if os.path.isfile(dev) and read(dev).strip():
        reasons.append("the Coder recorded deviations from the plan (deviations.md)")
    return reasons


# ----------------------------------------------------------------- adr mode

def staged_files(root):
    found = []
    for d, _, files in os.walk(root):
        for f in files:
            if f == ".DS_Store":
                continue
            found.append(os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/"))
    return sorted(found)


def resolve_link(target, rel_file, final_names, repo):
    """True if a relative link from docs/adr/<file> resolves."""
    target = target.split("#", 1)[0]
    if not target or re.match(r"^[a-z]+:", target):
        return True  # anchor-only or absolute URL
    if "/" not in target:
        return target in final_names or os.path.exists(os.path.join(repo, ADR_DIR, target))
    base = os.path.dirname(rel_file)
    path = os.path.normpath(os.path.join(repo, base, target))
    return os.path.exists(path)


def check_adr(run_id, repo):
    root = os.path.join(ORCH_ROOT, "runs", run_id, "adr")
    if not os.path.isdir(root):
        usage(f"{root} not found")
    if not os.path.isdir(repo):
        usage(f"repo {repo} not found")
    files = staged_files(root)
    if not files:
        usage(f"{root} is empty")

    out = []
    staged = {}  # name -> text, only files directly in docs/adr/
    for rel in files:
        if not re.match(rf"^{ADR_DIR}/[^/]+\.md$", rel):
            out.append(f"OUTSIDE_ADR_DIR {rel} (only {ADR_DIR}/*.md may be written)")
            continue
        staged[os.path.basename(rel)] = read(os.path.join(root, rel))

    repo_adr_dir = os.path.join(repo, ADR_DIR)
    existing = {}
    if os.path.isdir(repo_adr_dir):
        for f in os.listdir(repo_adr_dir):
            if f.endswith(".md"):
                existing[f] = read(os.path.join(repo_adr_dir, f))

    final = dict(existing)
    final.update(staged)
    final_names = set(final)
    new_adrs = [n for n in staged if n != "README.md" and n not in existing]
    edited_adrs = [n for n in staged if n != "README.md" and n in existing]

    for name in new_adrs:
        text = staged[name]
        m = NAME_RE.match(name)
        if not m:
            out.append(f"BAD_NAME {name} (expected YYYY-MM-DD-lowercase-kebab-slug.md)")
        if not re.match(r"^# \S", text):
            out.append(f"MISSING_FIELD {name}: first line must be `# <the decision>`")
        for fname in ["Status", "Date", "Source", "Code"]:
            if not field(text, fname):
                out.append(f"MISSING_FIELD {name}: no `- **{fname}:**` line")
        status = field(text, "Status") or ""
        if status and not re.match(r"^(Accepted|Proposed)\b", status):
            out.append(f"BAD_STATUS {name}: a new ADR is `Accepted` or `Proposed`, not `{status}`")
        date = field(text, "Date") or ""
        if m and date and not date.startswith(m.group(1)):
            out.append(f"DATE_MISMATCH {name}: Date `{date}` doesn't start with the name's date {m.group(1)}")
        source = field(text, "Source") or ""
        if source and run_id not in source:
            out.append(f"MISSING_FIELD {name}: Source must name the run `{run_id}`")
        for sec in REQUIRED_SECTIONS:
            if not re.search(rf"^## {re.escape(sec)}[ \t]*$", text, re.M):
                out.append(f"MISSING_SECTION {name}: no `## {sec}`")
        # Repo paths named on the Code / Supersedes lines must exist on the branch.
        for fname in ["Code", "Supersedes"]:
            for span in CODE_SPAN_RE.findall(field(text, fname) or ""):
                p = span.strip()
                if "/" not in p or " " in p or "{" in p or p.startswith("/") or re.match(r"^[a-z]+:", p):
                    continue
                p = p[2:] if p.startswith("./") else p
                if "*" in p:
                    if not glob.glob(os.path.join(repo, p)):
                        out.append(f"MISSING_CODE_PATH {name}: {fname} names `{span}`, which matches nothing in the repo")
                elif not os.path.exists(os.path.join(repo, p.rstrip("/"))):
                    out.append(f"MISSING_CODE_PATH {name}: {fname} names `{span}`, which doesn't exist in the repo")
        # Supersession must be mirrored on the old ADR.
        sup = field(text, "Supersedes") or ""
        for target in LINK_RE.findall(sup):
            t = target.split("#", 1)[0]
            if "/" in t or not t.endswith(".md"):
                continue  # a design doc or spec section, not an ADR in this folder
            old = final.get(t)
            if old is None:
                continue  # reported as BROKEN_LINK below
            old_status = field(old, "Status") or ""
            if not (old_status.startswith("Superseded by") and f"]({name})" in old_status):
                out.append(f"SUPERSEDE_NOT_MARKED {t}: its Status must read `Superseded by [..]({name})`")

    for name in edited_adrs:
        before, after = existing[name], staged[name]
        if before == after:
            continue
        def without_status(s):
            return [l for l in s.splitlines() if not l.startswith("- **Status:**")]
        if without_status(before) != without_status(after):
            out.append(f"EXISTING_ADR_EDITED {name}: only its Status line may change")
        status = field(after, "Status") or ""
        if field(before, "Status") != status and not re.match(r"^Superseded by \[[^\]]+\]\([^)]+\.md\)", status):
            out.append(f"EXISTING_ADR_EDITED {name}: a changed Status must read `Superseded by [..](YYYY-MM-DD-slug.md)`")

    readme = final.get("README.md")
    adr_names = sorted(n for n in final if n != "README.md")
    if readme is None:
        out.append(f"MISSING_INDEX_ROW {ADR_DIR}/README.md doesn't exist; it must index every ADR")
    else:
        for n in adr_names:
            if f"]({n})" not in readme:
                out.append(f"MISSING_INDEX_ROW {n} isn't linked from {ADR_DIR}/README.md")

    for name, text in staged.items():
        rel = f"{ADR_DIR}/{name}"
        prose = re.sub(r"^```.*?^```[ \t]*$", "", text, flags=re.M | re.S)  # templates in code fences
        for target in sorted(set(LINK_RE.findall(prose))):
            if not resolve_link(target, rel, final_names, repo):
                out.append(f"BROKEN_LINK {rel}: `{target}` doesn't resolve")

    return out


def main(argv):
    if len(argv) == 3 and argv[2] == "plan":
        violations = check_plan(argv[1])
    elif len(argv) == 3 and argv[2] == "trivial":
        reasons = check_trivial(argv[1])
        print("trivial: no decisions, no user answers, no deviations" if not reasons
              else "not trivial:\n" + "\n".join(f"- {r}" for r in reasons))
        return 1 if reasons else 0
    elif len(argv) == 4 and argv[3] == "adr":
        violations = check_adr(argv[1], os.path.abspath(argv[2]))
    else:
        usage("expected `<run_id> plan`, `<run_id> trivial` or `<run_id> <repo> adr`")
    for v in violations:
        print(v)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

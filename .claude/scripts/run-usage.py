#!/usr/bin/env python3
"""run-usage.py: real token usage of one orchestrator run, read from Claude Code's session logs.

Usage:
  python3 .claude/scripts/run-usage.py <RUN_ID> [--projects-dir DIR] [--no-write]

Claude Code records every model call, with the exact `usage` the API returned, in
JSONL transcripts under ~/.claude/projects/<project>/ ($CLAUDE_CONFIG_DIR/projects
if that is set). This script finds the calls that belong to RUN_ID and adds them up:

  * Subagent stages: transcripts whose first prompt contains "RUN_ID: <RUN_ID>"
    (the orchestrator's spawn prompt). The stage name comes from the prompt's
    "STAGE:" line, else from the parent's Agent/Task call (subagent_type).
  * Orchestrator: the main-thread calls of every /run-orchestration or
    /resume-orchestration invocation that mentions RUN_ID, or that spawned one of
    its subagents. Other conversations that merely mention the run are ignored.

It works for past runs too, as long as their transcripts still exist (Claude Code
deletes old transcripts after `cleanupPeriodDays`, 30 days by default).

Writes runs/<RUN_ID>/usage.md and runs/<RUN_ID>/usage.json, prints usage.md, and
ends with one line: `total_tokens: N | weighted: W`.
Exit: 0 ok, 1 no transcript found for the run, 3 setup error.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ORCH_ROOT = Path(__file__).resolve().parents[2]
ORCH_COMMANDS = {"run-orchestration", "resume-orchestration"}
COMMAND_RE = re.compile(r"<command-name>\s*/?([\w:.-]+)\s*</command-name>")
AGENT_TOOLS = {"Task", "Agent"}

# Standard API price ratios, relative to one uncached input token.
W_INPUT, W_CW5, W_CW1H, W_READ, W_OUTPUT = 1.0, 1.25, 2.0, 0.1, 5.0


# ── transcript helpers ────────────────────────────────────────────────────────

def text_of(content):
    """Flatten a message's content (string or list of blocks) to searchable text."""
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                t = b.get("type")
                if t == "text":
                    parts.append(b.get("text") or "")
                elif t == "tool_result":
                    parts.append(text_of(b.get("content")))
                elif t == "tool_use":
                    parts.append(json.dumps(b.get("input") or {}, ensure_ascii=False))
    return "\n".join(parts)


def entry_text(e):
    msg = e.get("message")
    return text_of(msg.get("content")) if isinstance(msg, dict) else ""


def load(path):
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict):
                out.append(e)
    return out


def agent_spawns(e):
    """(tool_use_id, subagent_type, prompt) for each Agent/Task call in an assistant entry."""
    msg = e.get("message")
    if e.get("type") != "assistant" or not isinstance(msg, dict):
        return []
    found = []
    for b in msg.get("content") or []:
        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in AGENT_TOOLS:
            inp = b.get("input") or {}
            found.append((b.get("id"), inp.get("subagent_type") or "general-purpose", inp.get("prompt") or ""))
    return found


def norm(s):
    return re.sub(r"\s+", " ", s or "").strip()


# ── usage accounting ──────────────────────────────────────────────────────────

class Tally:
    def __init__(self, label):
        self.label = label
        self.calls = self.input = self.cw5 = self.cw1h = self.read = self.output = 0
        self.peak = 0
        self.models = []
        self.first = self.last = None
        self.sources = set()
        self.detail = None

    def add(self, e, src):
        msg = e["message"]
        u = msg.get("usage") or {}
        inp = int(u.get("input_tokens") or 0)
        read = int(u.get("cache_read_input_tokens") or 0)
        cw = int(u.get("cache_creation_input_tokens") or 0)
        split = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
        cw1h = min(cw, int(split.get("ephemeral_1h_input_tokens") or 0))
        out = int(u.get("output_tokens") or 0)
        self.calls += 1
        self.input += inp
        self.read += read
        self.cw5 += cw - cw1h
        self.cw1h += cw1h
        self.output += out
        self.peak = max(self.peak, inp + read + cw)
        model = msg.get("model")
        if model and model not in self.models:
            self.models.append(model)
        ts = e.get("timestamp")
        if ts:
            self.first = ts if self.first is None or ts < self.first else self.first
            self.last = ts if self.last is None or ts > self.last else self.last
        self.sources.add(src)

    def merge(self, other):
        for k in ("calls", "input", "cw5", "cw1h", "read", "output"):
            setattr(self, k, getattr(self, k) + getattr(other, k))
        self.peak = max(self.peak, other.peak)
        for m in other.models:
            if m not in self.models:
                self.models.append(m)
        for ts in (other.first, other.last):
            if ts:
                self.first = ts if self.first is None or ts < self.first else self.first
                self.last = ts if self.last is None or ts > self.last else self.last
        self.sources |= other.sources

    @property
    def cache_write(self):
        return self.cw5 + self.cw1h

    @property
    def total(self):
        return self.input + self.cache_write + self.read + self.output

    @property
    def weighted(self):
        return round(self.input * W_INPUT + self.cw5 * W_CW5 + self.cw1h * W_CW1H
                     + self.read * W_READ + self.output * W_OUTPUT)

    def as_dict(self):
        return {
            "label": self.label, "models": self.models, "calls": self.calls,
            "input": self.input, "cache_write": self.cache_write, "cache_write_1h": self.cw1h,
            "cache_read": self.read, "output": self.output, "total": self.total,
            "weighted": self.weighted, "peak_context": self.peak,
            "avg_context": round((self.input + self.cache_write + self.read) / self.calls) if self.calls else 0,
            "first": self.first, "last": self.last,
        }


def real_calls(entries):
    """Assistant entries with usage, one per API response.

    Claude Code writes one line per content block of a response, each repeating
    the same message id and usage; keep the line with the highest output count."""
    best, order = {}, []
    for i, e in enumerate(entries):
        msg = e.get("message")
        if e.get("type") != "assistant" or not isinstance(msg, dict) or not msg.get("usage"):
            continue
        if msg.get("model") == "<synthetic>":
            continue
        key = msg.get("id") or e.get("requestId") or e.get("uuid") or ("line", i)
        out = int((msg.get("usage") or {}).get("output_tokens") or 0)
        if key not in best:
            order.append(key)
            best[key] = (out, e)
        elif out >= best[key][0]:
            best[key] = (out, e)
    return [(k, best[k][1]) for k in order]


# ── finding the run's transcripts ─────────────────────────────────────────────

def projects_root(arg):
    if arg:
        return Path(arg).expanduser()
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(cfg).expanduser() if cfg else Path.home() / ".claude") / "projects"


def candidate_files(root, run_id):
    """JSONL files mentioning run_id: this project's folder first, every project as fallback."""
    needle = run_id.encode()
    own = root / re.sub(r"[^A-Za-z0-9]", "-", str(ORCH_ROOT))

    def scan(base):
        hits = []
        for p in sorted(base.rglob("*.jsonl")):
            try:
                if needle in p.read_bytes():
                    hits.append(p)
            except OSError:
                pass
        return hits

    files = scan(own) if own.is_dir() else []
    return (files or scan(root)), own


def is_agent_file(p):
    return "subagents" in p.parts or p.name.startswith("agent-")


def collect(run_id, files):
    run_re = re.compile(r"RUN_ID:\s*" + re.escape(run_id) + r"(?![\w])")
    loaded = {p: load(p) for p in files}

    # Agent/Task calls anywhere: prompt -> type, tool_use_id -> type, agentId -> type.
    by_prompt, by_tool_id, by_agent_id = {}, {}, {}
    for entries in loaded.values():
        for e in entries:
            for tid, stype, prompt in agent_spawns(e):
                by_prompt[norm(prompt)] = stype
                if tid:
                    by_tool_id[tid] = stype
        for e in entries:
            r = e.get("toolUseResult")
            if isinstance(r, dict) and r.get("agentId"):
                msg = e.get("message") or {}
                for b in msg.get("content") or [] if isinstance(msg, dict) else []:
                    if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in by_tool_id:
                        by_agent_id[str(r["agentId"])] = by_tool_id[b["tool_use_id"]]

    # Split every file into subagent groups and main-thread entries.
    sub_groups, mains = [], []
    for p, entries in loaded.items():
        if is_agent_file(p):
            sub_groups.append((p, p.stem.replace("agent-", "", 1), entries))
            continue
        side = [e for e in entries if e.get("isSidechain")]
        mains.append((p, [e for e in entries if not e.get("isSidechain")]))
        if side:  # older layout: subagent turns stored inline, linked by parentUuid
            by_uuid = {e.get("uuid"): e for e in side if e.get("uuid")}
            roots, groups = {}, {}

            def root(e):
                seen, cur = [], e
                while True:
                    u = cur.get("uuid")
                    if u in roots:
                        r = roots[u]
                        break
                    seen.append(u)
                    parent = by_uuid.get(cur.get("parentUuid"))
                    if parent is None:
                        r = u
                        break
                    cur = parent
                for s in seen:
                    roots[s] = r
                return r

            for e in side:
                key = e.get("agentId") or root(e)
                groups.setdefault(key, []).append(e)
            for key, g in groups.items():
                sub_groups.append((p, str(key), g))

    seen_calls = set()
    invocations, notes = [], []

    for p, agent_id, entries in sub_groups:
        first_user = next((e for e in entries if e.get("type") == "user"), None)
        prompt = entry_text(first_user) if first_user else ""
        if not run_re.search(prompt):
            continue
        m = re.search(r"^\s*STAGE:\s*(\S+)", prompt, re.M)
        stage = (m.group(1) if m else None) or by_agent_id.get(agent_id) or by_prompt.get(norm(prompt))
        if not stage:
            stage = "subagent (unknown type)"
        t = Tally(stage)
        sl = re.search(r"SLICE\s+(\d+/\d+)", prompt)
        if sl:
            t.detail = "slice " + sl.group(1)
        for key, e in real_calls(entries):
            if key in seen_calls:
                continue
            seen_calls.add(key)
            t.add(e, str(p))
        if t.calls:
            invocations.append(t)

    orch = Tally("orchestrator (main thread)")
    segments = 0
    for p, entries in mains:
        # Segments start at each slash-command invocation.
        segs, cur = [], {"cmd": None, "entries": []}
        for e in entries:
            if e.get("type") == "user":
                m = COMMAND_RE.search(entry_text(e))
                if m:
                    segs.append(cur)
                    cur = {"cmd": m.group(1).split(":")[-1], "entries": []}
            cur["entries"].append(e)
        segs.append(cur)
        for s in segs:
            text = "\n".join(entry_text(e) for e in s["entries"])
            spawned = any(run_re.search(pr) for e in s["entries"] for _, _, pr in agent_spawns(e))
            if spawned or (s["cmd"] in ORCH_COMMANDS and run_id in text):
                segments += 1
                for key, e in real_calls(s["entries"]):
                    if key in seen_calls:
                        continue
                    seen_calls.add(key)
                    orch.add(e, str(p))
            elif run_id in text and any(e.get("type") == "assistant" for e in s["entries"]):
                notes.append(f"Not counted: {p.name} mentions {run_id} outside an orchestration command"
                             f" ({'/' + s['cmd'] if s['cmd'] else 'no command'}).")

    invocations.sort(key=lambda t: t.first or "")
    return orch, segments, invocations, sorted(set(notes))


# ── output ────────────────────────────────────────────────────────────────────

def fmt(n):
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def short_model(models):
    return ", ".join(re.sub(r"^claude-", "", m) for m in models) or "?"


COLUMNS = ["Model", "Calls", "Input", "Cache write", "Cache read", "Output", "Weighted", "Share", "Peak context"]


def cells(t, total_w, label=None):
    share = f"{100 * t.weighted / total_w:.0f}%" if total_w else "-"
    return [label or t.label, short_model(t.models), str(t.calls), fmt(t.input), fmt(t.cache_write),
            fmt(t.read), fmt(t.output), fmt(t.weighted), share, fmt(t.peak)]


def table(first, rows):
    """Markdown table padded so the columns also line up as plain text.
    Text columns are left-aligned, number columns right-aligned."""
    head = [first] + COLUMNS
    widths = [max(len(r[i]) for r in [head] + rows) for i in range(len(head))]
    left = {0, 1}

    def line(r):
        return "| " + " | ".join(c.ljust(w) if i in left else c.rjust(w)
                                 for i, (c, w) in enumerate(zip(r, widths))) + " |"

    sep = "|" + "|".join(("-" * (w + 2)) if i in left else ("-" * (w + 1) + ":")
                         for i, w in enumerate(widths)) + "|"
    return [line(head), sep] + [line(r) for r in rows]


def render(run_id, orch, segments, invocations, notes):
    everything = Tally("TOTAL")
    by_agent = {}
    for t in invocations:
        by_agent.setdefault(t.label, Tally(t.label)).merge(t)
        everything.merge(t)
    everything.merge(orch)
    agents = list(by_agent.values())
    if orch.calls:
        agents.append(orch)
    agents.sort(key=lambda t: -t.weighted)
    tw = everything.weighted

    counts = {}
    numbered = []
    for t in invocations:
        counts[t.label] = counts.get(t.label, 0) + 1
        n = counts[t.label]
        c = Tally(f"{t.label}" + (f" #{n}" if n > 1 else ""))
        c.merge(t)
        c.detail = t.detail
        numbered.append(c)
    for label, k in counts.items():
        if k > 1:
            for c in numbered:
                if c.label == label:
                    c.label = f"{label} #1"
    for c in numbered:
        if c.detail:
            c.label = f"{c.label} ({c.detail})"

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    L = [f"# Token usage: {run_id}", "",
         f"Real counts from Claude Code's session logs ({now}). "
         f"Orchestrator: {segments} command invocation(s); subagents: {len(invocations)} invocation(s).", "",
         "## By agent", ""]
    L += table("Agent", [cells(t, tw) for t in agents] + [cells(everything, tw, "**TOTAL**")])
    L += ["", "## Every subagent invocation, in order", ""]
    L += table("Stage", [cells(t, tw) for t in numbered]) if numbered else ["(none)"]
    L += ["",
          f"Total tokens processed: {everything.total:,} (input {everything.input:,}, cache write "
          f"{everything.cache_write:,}, cache read {everything.read:,}, output {everything.output:,}).",
          "",
          "- **Cache read** is context re-sent on each call and served from the prompt cache. It is "
          "usually most of the volume: every call re-reads the whole conversation so far.",
          "- **Weighted** converts everything to uncached-input-token equivalents using the standard "
          "API price ratios (input x1, cache write x1.25 or x2 for the 1-hour cache, cache read x0.1, "
          "output x5), so stages can be compared fairly. It ignores the price gap between models: "
          "an Opus token costs more than a Sonnet token.",
          "- **Peak context** is the largest single call's prompt (input + cache read + cache write).",
          "- A run still in progress shows only the calls written so far."]
    if notes:
        L += ["", "## Notes", ""] + [f"- {n}" for n in notes]
    return "\n".join(L) + "\n", everything, agents, numbered


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_id")
    ap.add_argument("--projects-dir", help="Claude Code projects folder (default ~/.claude/projects)")
    ap.add_argument("--no-write", action="store_true", help="print only; don't write usage.md/usage.json")
    a = ap.parse_args()

    root = projects_root(a.projects_dir)
    if not root.is_dir():
        print(f"ERROR: Claude Code projects folder not found: {root} (use --projects-dir)", file=sys.stderr)
        return 3

    files, own = candidate_files(root, a.run_id)
    orch, segments, invocations, notes = collect(a.run_id, files)
    if not orch.calls and not invocations:
        print(f"No transcript calls found for {a.run_id} under {root} "
              f"(looked in {own.name} first, then every project). Transcripts older than "
              f"Claude Code's cleanupPeriodDays (default 30) are deleted.", file=sys.stderr)
        return 1

    md, total, agents, numbered = render(a.run_id, orch, segments, invocations, notes)
    if not a.no_write:
        run_dir = ORCH_ROOT / "runs" / a.run_id
        if run_dir.is_dir():
            (run_dir / "usage.md").write_text(md, encoding="utf-8")
            data = {
                "run_id": a.run_id,
                "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "total": total.as_dict(),
                "by_agent": [t.as_dict() for t in agents],
                "invocations": [t.as_dict() for t in numbered],
                "orchestrator_invocations": segments,
                "transcripts": sorted({s for t in invocations + [orch] for s in t.sources}),
                "notes": notes,
            }
            (run_dir / "usage.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        else:
            print(f"(runs/{a.run_id}/ not found; printing only)", file=sys.stderr)
    sys.stdout.write(md)
    print(f"\ntotal_tokens: {total.total} | weighted: {total.weighted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

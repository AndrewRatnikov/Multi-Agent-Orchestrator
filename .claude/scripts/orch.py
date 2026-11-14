#!/usr/bin/env python3
"""orch.py: the pipeline's control flow, as a deterministic driver.

The main session no longer reads 1,100 lines of stage rules and edits state.md by
hand. It loops: ask orch.py what to do, do it, hand back the result. orch.py runs every
mechanical step itself (check-contract, check-paths, size-run, seed-code, coder-slices,
commit-code, run-tests, verify, render-capture, review-delta, ...) by calling the
existing scripts, and owns all state, counters, retry caps, escalation and routing.

Usage:
  orch.py start --repo <path> --task <text> [--size small|full] [--unattended]
                [--base continue|switch]
      Stage 0 (clean-tree check, branch, run folder, state, digest), then prints the
      first instruction.
  orch.py next <run_id>
      Prints one JSON instruction (see below). Idempotent: while an instruction is
      outstanding it prints the same one again.
  orch.py result <run_id> <stage> <result-file>
      Hands back what the instruction produced: a subagent's final report (stage =
      the instruction's `stage`) or the user's answer (stage = `answer:<id>`).
      Parses the RESULT block, updates state, then prints the next instruction.
  orch.py resume <run_id> [--from <step>]
      /resume-orchestration: checks the repo, archives what will be overwritten, sets
      the run going again from <step> (default: where it stopped), prints the first
      instruction. Runs without state.json (older runs) are imported from state.md.
  orch.py status <run_id>
      Prints the state summary and the last 20 report lines. Changes nothing.

Instructions (one JSON object on stdout; `say` = lines to show the user first):
  {"action": "spawn", "agent", "model", "stage", "prompt", "result_file", "then"}
      Spawn the subagent with exactly this prompt (and model, when not null), save its
      final report verbatim to result_file, run `then`.
  {"action": "ask", "id", "question", "options", "answer_file", "then"}
      Ask the user (AskUserQuestion when options are given), save their answer
      verbatim to answer_file, run `then`.
  {"action": "do", "message", "then"}
      A small task for the main session itself (e.g. trim the digest), then `then`.
  {"action": "continue", "then"}
      A long mechanical step just finished; call `then` (keeps each call under the
      Bash timeout).
  {"action": "stop", "status", "message", ["followup"]}
      Show the message and end the turn. `followup` is main-session work for Stage 7.

State lives in runs/<id>/state.json, written only by this script. state.md is
re-rendered from it after every change (same fields as before, plus
architect_retries), because several scripts read it. A manual edit of
render_config_ref, size_override or task in state.md is picked up and logged.

Exit: 0 instruction printed, 2 usage error or a result that doesn't match the
outstanding instruction (message on stderr), 3 internal error.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("ORCH_ROOT") or os.path.dirname(os.path.dirname(SCRIPTS))

STEPS = ["product", "architect", "render-setup", "tester", "test-reviewer", "coder",
         "sandbox", "verify", "render", "adr"]
CAP = 2
LONG_SCRIPTS = {"run-tests.sh", "verify.py", "render-capture.mjs", "repo-digest.sh", "commit-code.py"}
MD_FIELDS = ["run_id", "task", "repo", "original_branch", "target_branch", "step", "status",
             "timestamp", "last_artifact", "pause_reason", "retry_count", "verify_retries",
             "render_retries", "render_setup_retries", "coder_slices_done", "architect_retries",
             "size", "size_override", "size_reasons"]
INT_FIELDS = {"retry_count", "verify_retries", "render_retries", "render_setup_retries",
              "coder_slices_done", "architect_retries"}
MANUAL_FIELDS = ("render_config_ref", "size_override", "task")
FEEDBACK_INLINE_MAX = 12000

AGENTS = {"product": "orch-product", "architect": "orch-architect",
          "render-setup": "orch-render-setup", "tester": "orch-tester",
          "test-reviewer": "orch-test-reviewer", "coder": "orch-coder",
          "render-reviewer": "orch-render-reviewer", "adr": "orch-adr-writer"}
VALID = {"product": ("status", {"DONE", "NEEDS_INPUT"}),
         "architect": ("status", {"DONE", "NEEDS_INPUT"}),
         "render-setup": ("status", {"DONE", "NEEDS_INPUT", "BLOCKED"}),
         "tester": ("status", {"DONE"}),
         "test-reviewer": ("verdict", {"PASS", "FAIL"}),
         "coder": ("status", {"DONE", "CONTRACT_MISMATCH"}),
         "render-reviewer": ("verdict", {"PASS", "FAIL"}),
         "adr": ("status", {"DONE", "NONE"})}

ESCALATION_NOTE = ("This run was sized small and skipped this review; review the committed "
                   "tests before the retry.")
VERIFY_TESTER_NOTE = ("This pass only fixes lint/type errors from Verify; confirm no assertion "
                      "was weakened, removed or loosened.")


class OrchError(Exception):
    pass


class UsageError(Exception):
    pass


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read(path, default=""):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return default


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


TITLE_MAX = 64


def task_title(task, limit=TITLE_MAX):
    """One line for commit subjects, memory.md's run history, report.md and state.md: the task's
    first non-empty line, without markdown markers, cut at a word boundary. The full task stays
    in state.json and runs/<id>/task.md; agents get the full text."""
    line = next((l.strip() for l in (task or "").splitlines() if l.strip()), "")
    line = re.sub(r"^(#+|[-*>]|\d+\.)\s+", "", line)
    line = re.sub(r"\s+", " ", line).replace("|", "/").strip()
    if len(line) <= limit:
        return line.rstrip(" .,;:") or "untitled task"
    cut = line[:limit]
    if " " in cut[limit // 2:]:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(" .,;:(-") + "…"


# ── subprocess helpers (tests replace `call`) ─────────────────────────────────

def call(script, *args):
    """Run one of the existing scripts from the orchestrator root. Returns (exit, output)."""
    path = os.path.join(SCRIPTS, script)
    runner = {"py": ["python3"], "sh": ["bash"], "mjs": ["node"]}[script.rsplit(".", 1)[1]]
    p = subprocess.run(runner + [path] + [str(a) for a in args], cwd=ROOT,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return p.returncode, p.stdout.rstrip("\n")


def git(repo, *args):
    p = subprocess.run(["git", "-C", repo] + list(args), stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True)
    return p.returncode, p.stdout.rstrip("\n")


# ── RESULT block parser ───────────────────────────────────────────────────────

KEY_RE = re.compile(r"^([a-z][a-z0-9_]*):\s*(.*)$")
ITEM_RE = re.compile(r"^\s*(?:-|\d+\.)\s+(.*)$")


def parse_result(text):
    """Parse the last `## RESULT` block. Returns dict with scalar keys, list keys,
    `_raw` (the block) and `_tail` (prose after it), or None if there is no block."""
    lines = text.splitlines()
    starts = [i for i, l in enumerate(lines) if l.strip() == "## RESULT"]
    if not starts:
        return None
    i = starts[-1] + 1
    if i < len(lines) and lines[i].strip().startswith("```"):
        i += 1
    res, key, block_end = {}, None, len(lines)
    raw = ["## RESULT"]
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        indented = line[:1] in (" ", "\t")
        if s.startswith("```") or s.startswith("## "):
            block_end = i + (1 if s.startswith("```") else 0)
            break
        m_item = ITEM_RE.match(line)
        m_key = KEY_RE.match(s) if not indented else None
        if not s:
            pass
        elif m_item and key is not None:
            if not isinstance(res.get(key), list):
                res[key] = [] if res.get(key) in ("", None) else [res[key]]
            res[key].append(m_item.group(1).strip())
        elif m_key:
            key = m_key.group(1)
            res[key] = m_key.group(2).strip()
        elif indented and key is not None and isinstance(res.get(key), list) and res[key]:
            res[key][-1] += " " + s
        else:
            block_end = i
            break
        raw.append(line)
        i += 1
    res["_raw"] = "\n".join(l for l in raw).rstrip()
    res["_tail"] = "\n".join(lines[block_end:]).strip()
    return res


def as_list(v):
    """A RESULT field as a list; `none` / empty means []."""
    if v is None:
        return []
    items = v if isinstance(v, list) else [v]
    items = [x for x in items if x and x.strip().lower() not in ("none", "\"none\"", "n/a")]
    return items


# ── the run ───────────────────────────────────────────────────────────────────

class Run:
    def __init__(self, run_id):
        self.id = run_id
        self.dir = os.path.join(ROOT, "runs", run_id)
        self.st = None
        self.says = []
        self.long_ran = False

    # paths
    def path(self, *p):
        return os.path.join(self.dir, *p)

    def rel(self, *p):
        return "/".join(("runs", self.id) + p)

    @property
    def ctx(self):
        return self.st.setdefault("ctx", {})

    @property
    def repo(self):
        return self.st["repo"]

    @property
    def title(self):
        return self.st.get("title") or task_title(self.st.get("task", ""))

    # persistence
    def load(self, write_import=True):
        if not os.path.isdir(self.dir):
            raise UsageError("no run folder runs/{}".format(self.id))
        jp = self.path("state.json")
        if os.path.exists(jp):
            self.st = json.loads(read(jp))
            self._merge_manual_edits()
        else:
            self.st = import_state_md(read(self.path("state.md")), self.id)
            if write_import:
                self.save()
        return self

    def _merge_manual_edits(self):
        md = read(self.path("state.md"))
        if not md or md == self.st.get("_md"):
            return
        fields = parse_md_fields(md)
        changed = False
        for k in MANUAL_FIELDS:
            new = fields.get(k, "")
            old = self.st.get(k) or ""
            if k == "task":
                # state.md holds the one-line title; only a real edit of that line replaces the task
                first = next((l.strip() for l in old.splitlines() if l.strip()), "")
                if new in ("", old, first, self.title):
                    continue
                self.st["task"] = new
                self.st["title"] = task_title(new)
                write(self.path("task.md"), new + "\n")
                self.report("[state] task changed by hand in state.md")
                changed = True
                continue
            if new != old:
                self.st[k] = (new or None) if k == "render_config_ref" else new
                if k == "render_config_ref":
                    self.report("[render-config] render_config_ref: {} (set by user in state.md)".format(new or "cleared"))
                else:
                    self.report("[state] {} changed by hand in state.md".format(k))
                changed = True
        if changed:
            self.save()

    def save(self):
        st = self.st
        lines = []
        for k in MD_FIELDS:
            v = st.get(k)
            if k == "size_reasons":
                v = "; ".join(v or [])
            if k == "task":
                v = self.title          # one line: a multi-line value would break this key: value file
            lines.append("{}: {}".format(k, "" if v is None else v))
        if st.get("render_config_ref"):
            lines.append("render_config_ref: {}".format(st["render_config_ref"]))
        md = "\n".join(lines) + "\n"
        st["_md"] = md
        write(self.path("state.md"), md)
        write(self.path("state.json"), json.dumps(st, indent=2, ensure_ascii=False) + "\n")

    # output
    def say(self, msg):
        self.says.append(msg)

    def report(self, text):
        p = self.path("report.md")
        cur = read(p)
        with open(p, "a", encoding="utf-8") as f:
            if cur and not cur.endswith("\n"):
                f.write("\n")
            f.write(text.rstrip("\n") + "\n")

    def set(self, **kw):
        self.st.update(kw)
        self.st["timestamp"] = now_iso()

    def cmd(self, *parts):
        return "python3 .claude/scripts/orch.py " + " ".join(parts)

    # scripts
    def call(self, script, *args):
        if script in LONG_SCRIPTS:
            self.long_ran = True
        return call(script, *args)

    # instructions
    def _pend(self, inst):
        self.st["pending"] = inst
        return inst

    def spawn(self, stage, feedback=None, note=None, answers=None, extra=None, model=None):
        n = self.ctx.setdefault("spawns", {}).get(stage, 0) + 1
        self.ctx["spawns"][stage] = n
        agent = AGENTS[stage]
        lines = ["ORCHESTRATOR_ROOT: " + ROOT, "RUN_ID: " + self.id, "STAGE: " + agent,
                 "REPO: " + self.repo, "TASK: " + self.st["task"]]
        if answers:
            lines.append("ANSWERS: " + answers)
        if feedback:
            lines.append("FEEDBACK: " + self._inline(feedback, stage, n))
        if note:
            lines.append("NOTE: " + note)
        for k, v in (extra or []):
            lines.append("{}: {}".format(k, v))
        lines.append("Follow your agent instructions. Finish with the RESULT block.")
        rf = self.rel("results", "{}-{}.md".format(stage, n))
        self.st["phase"] = stage + ".wait"
        return self._pend({"action": "spawn", "agent": agent, "model": model, "stage": stage,
                           "prompt": "\n".join(lines), "result_file": rf,
                           "then": self.cmd("result", self.id, stage, rf)})

    def _inline(self, text, stage, n):
        if len(text) <= FEEDBACK_INLINE_MAX:
            return text
        p = self.path("results", "{}-{}-feedback.md".format(stage, n))
        write(p, text)
        return text[:FEEDBACK_INLINE_MAX] + "\n[... truncated; full feedback: {}]".format(
            self.rel("results", os.path.basename(p)))

    def ask(self, qid, question, options=None, pause=None):
        n = self.ctx.setdefault("asks", {}).get(qid, 0) + 1
        self.ctx["asks"][qid] = n
        if pause:
            self.set(status="paused", pause_reason=pause)
        af = self.rel("results", "answer-{}-{}.md".format(qid.replace(":", "-"), n))
        self.st["phase"] = "ask"
        return self._pend({"action": "ask", "id": qid, "question": question,
                           "options": options or [], "answer_file": af,
                           "then": self.cmd("result", self.id, "answer:" + qid, af)})

    def stop(self, message, status, pause_reason=""):
        self.set(status=status, pause_reason=pause_reason)
        self.st["phase"] = "stopped"
        return self._pend({"action": "stop", "status": status, "message": message})

    def do(self, message, phase):
        self.st["phase"] = phase
        return {"action": "do", "message": message, "then": self.cmd("next", self.id)}

    def emit(self, inst):
        out = dict(inst)
        out["run_id"] = self.id
        if self.says:
            out["say"] = self.says
        self.says = []
        return out

    # the loop
    def advance(self):
        for _ in range(500):
            if self.st.get("pending"):
                return self.emit(self.st["pending"])
            ph = self.st.get("phase")
            handler = PHASES.get(ph)
            if handler is None:
                raise OrchError("run {} has no next step (phase {!r}); use resume".format(self.id, ph))
            inst = handler(self)
            self.save()
            if inst:
                return self.emit(inst)
            if self.long_ran:
                self.long_ran = False
                return self.emit({"action": "continue", "then": self.cmd("next", self.id)})
        raise OrchError("no instruction after 500 steps (loop?)")

    def handle_result(self, stage, file):
        p = self.st.get("pending")
        if not p or p["action"] not in ("spawn", "ask"):
            raise UsageError("run {} is not waiting for a result".format(self.id))
        text = read(file if os.path.isabs(file) else os.path.join(ROOT, file))
        if p["action"] == "spawn":
            if stage != p["stage"]:
                raise UsageError("waiting for stage {}, got {}".format(p["stage"], stage))
            self.st["pending"] = None
            res = parse_result(text)
            key, ok = VALID[stage]
            if res is None or res.get(key) not in ok:
                if stage == "adr":
                    self.report("[adr] NOT WRITTEN — no valid RESULT block")
                    self.ctx["adr_outcome"] = "NOT WRITTEN (no RESULT block)"
                    self.st["phase"] = "done"
                else:
                    self.report("[{}] FAILED — RESULT block missing or malformed".format(stage))
                    self.stop("Stage {} returned no valid RESULT block (see {}). Stopping; resume with "
                              "`/resume-orchestration {} --from {}` once it's sorted out."
                              .format(stage, file, self.id, stage_step(stage)), "failed", "result-malformed")
            else:
                RESULTS[stage](self, res)
        else:
            if stage != "answer:" + p["id"]:
                raise UsageError("waiting for answer:{}, got {}".format(p["id"], stage))
            self.st["pending"] = None
            self.set(status="running", pause_reason="")
            ANSWERS[p["id"].split(":")[0]](self, p["id"], text.strip())
        self.save()

    # shared steps
    def scope_check(self):
        _, out = git(self.repo, "status", "--porcelain")
        if out.strip():
            self.report("[scope] {} has changes no stage should have made:\n{}".format(self.repo, out))
            return self.stop("A subagent wrote outside its folder. {} now has uncommitted changes:\n\n{}\n\n"
                             "Clean them up (nothing was deleted), then resume."
                             .format(self.repo, out), "paused", "scope-violation")
        return None

    def archive(self, name, dest, move=False):
        src = self.path(name)
        if not os.path.exists(src):
            return None
        dst = self.path("archive", dest)
        base, n = dst, 2
        while os.path.exists(dst):
            root, ext = os.path.splitext(base) if os.path.isfile(src) else (base, "")
            dst = "{}_{}{}".format(root, n, ext)
            n += 1
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if move:
            shutil.move(src, dst)
        elif os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        return dst

    def has_contract(self):
        return os.path.exists(self.path("contract.json"))

    def small(self):
        return self.st.get("size") == "small"

    def model_for(self, stage):
        return "sonnet" if self.small() and stage in ("tester", "coder") else None

    def escalate(self, stage, why):
        """Small run -> standard on the first trouble. Returns True when the skipped
        test review must run before the retry."""
        if not self.small():
            return False
        self.st["size"] = "standard"
        self.st.setdefault("size_reasons", []).append("escalated: {}: {}".format(stage, why))
        self.ctx["escalated"] = True
        self.report("[size] ESCALATED to standard — {}".format(why))
        return bool(self.ctx.get("review_skipped"))

    def route(self, t, needs_review=False):
        """Apply a transition now, or after the escalation review."""
        if needs_review and t.get("phase") != "tester":
            self.ctx["review_skipped"] = False
            self.ctx["deferred"] = t
            self.ctx["review_note"] = ESCALATION_NOTE
            self.st["phase"] = "4b"
        else:
            if needs_review:
                self.ctx["review_skipped"] = False
            self.apply(t)

    def apply(self, t):
        for src, dst in t.get("archive", []):
            self.archive(src, dst)
        if "rc" in t:
            self.st["retry_count"] = t["rc"]
        for stage, fb in t.get("feedback", {}).items():
            self.ctx.setdefault("feedback", {})[stage] = fb
            self.ctx.setdefault("last_feedback", {})[stage] = fb
        self.ctx.update(t.get("ctx", {}))
        if t.get("say"):
            self.say(t["say"])
        self.st["phase"] = t["phase"]

    def take_feedback(self, stage):
        return self.ctx.setdefault("feedback", {}).pop(stage, None)

    def cap_stop(self, what, step, extra=""):
        return self.stop("{} retry cap reached (2/2). Human intervention required.\n"
                         "Review `runs/{}/report.md`{}, fix by hand or run:\n"
                         "`/resume-orchestration {} --from {}`\n"
                         "Partial progress is already committed on `{}` in {} if you want to inspect it directly."
                         .format(what, self.id, extra, self.id, step, self.st["target_branch"], self.repo),
                         "failed", "retry-cap")

    def to_tester(self, report_block, feedback, cap_what, say_fmt, escalate_why=None):
        if escalate_why:
            self.escalate("tester", escalate_why)
        self.ctx["review_skipped"] = False  # 4b runs on the rewritten tests anyway
        rc = self.st["retry_count"]
        if report_block:
            self.report(report_block.replace("{retry}", "retry {}/2".format(rc + 1) if rc < CAP else "retry cap reached"))
        self.ctx.setdefault("last_feedback", {})["tester"] = feedback
        if rc >= CAP:
            return self.cap_stop(cap_what, "tester", " for the feedback")
        self.apply({"rc": rc + 1, "archive": [("tests", "tests_v{}".format(rc + 1))],
                    "feedback": {"tester": feedback}, "say": say_fmt.format(n=rc + 1), "phase": "tester"})
        return None

    def to_coder(self, report_block, feedback, fix_reason, cap_what, say_fmt, escalate_why, extra_ctx=None):
        needs = self.escalate("coder", escalate_why) if escalate_why else False
        rc = self.st["retry_count"]
        if report_block:
            self.report(report_block.replace("{retry}", "retry {}/2".format(rc + 1) if rc < CAP else "retry cap reached"))
        self.ctx.setdefault("last_feedback", {})["coder"] = feedback
        if rc >= CAP:
            return self.cap_stop(cap_what, "coder", " for the failure output")
        ctx = {"fix_reason": fix_reason, "render_pass": False}
        ctx.update(extra_ctx or {})
        self.route({"rc": rc + 1, "archive": [("code", "code_v{}".format(rc + 1))],
                    "feedback": {"coder": feedback}, "ctx": ctx,
                    "say": say_fmt.format(n=rc + 1), "phase": "coder"}, needs)
        return None


def stage_step(stage):
    return {"render-reviewer": "render", "adr": "adr"}.get(stage, stage)


def parse_md_fields(md):
    out = {}
    for line in md.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            if re.match(r"^[a-z_]+$", k.strip()):
                out[k.strip()] = v.strip()
    return out


def import_state_md(md, run_id):
    """state.json for a run that only has state.md (runs from before orch.py)."""
    if not md:
        raise UsageError("runs/{}/state.md is missing".format(run_id))
    f = parse_md_fields(md)
    st = {k: f.get(k, "") for k in MD_FIELDS}
    st["run_id"] = st["run_id"] or run_id
    for k in INT_FIELDS:
        try:
            st[k] = int(f.get(k) or 0)
        except ValueError:
            st[k] = 0
    st["size"] = f.get("size") or "standard"
    st["size_reasons"] = [r.strip() for r in (f.get("size_reasons") or "").split(";") if r.strip()]
    st["render_config_ref"] = f.get("render_config_ref") or None
    st["unattended"] = False
    step = st.get("step") or ""
    later = STEPS[STEPS.index("sandbox"):] + ["done"]
    st["ctx"] = {"imported": True, "escalated": any(r.startswith("escalated") for r in st["size_reasons"]),
                 "code_committed": step in later or (step == "coder" and st["retry_count"] > 0)}
    st["phase"] = None
    st["pending"] = None
    return st


# ── Stage 1 / 2: product, architect ───────────────────────────────────────────

def ph_product(r):
    r.set(step="product", status="running")
    return r.spawn("product", answers=r.ctx.get("answers", {}).get("product"))


def needs_input(r, stage, res):
    qs = as_list(res.get("questions"))
    qf = r.path("questions-{}.md".format(stage))
    write(qf, "# Questions from {}\n\n".format(stage) + "\n".join(
        "{}. {}".format(i + 1, q) for i, q in enumerate(qs)) + "\n")
    r.report("[{}] NEEDS_INPUT — {} question(s), see questions-{}.md".format(stage, len(qs), stage))
    if r.st.get("unattended"):
        return r.stop("Pipeline paused at **{}**: it has questions (runs/{}/questions-{}.md) and no one "
                      "is here to answer. Answer them in runs/{}/answers-{}.md, then "
                      "`/resume-orchestration {} --from {}`.".format(stage, r.id, stage, r.id, stage, r.id,
                                                                    stage_step(stage)),
                      "paused", "awaiting_human_input")
    q = "The {} stage needs answers before it can continue:\n\n{}\n\n(Answer here, or later with " \
        "`/resume-orchestration {} --from {}`.)".format(stage, read(qf), r.id, stage_step(stage))
    return r.ask("questions:" + stage, q, pause="awaiting_human_input")


def ans_questions(r, qid, text):
    stage = qid.split(":", 1)[1]
    af = r.path("answers-{}.md".format(stage))
    write(af, text + "\n")
    r.ctx.setdefault("answers", {})[stage] = r.rel("answers-{}.md".format(stage))
    r.report("[{}] answers saved to answers-{}.md".format(stage, stage))
    r.st["phase"] = {"render-setup": "rs.agent"}.get(stage, stage)


def res_product(r, res):
    if res["status"] == "NEEDS_INPUT":
        return needs_input(r, "product", res)
    if not os.path.exists(r.path("prd.md")):
        r.report("[product] FAILED — DONE but prd.md is missing")
        return r.stop("The Product agent said DONE but runs/{}/prd.md doesn't exist.".format(r.id),
                      "failed", "missing-artifact")
    r.report("[product] DONE — {}".format(res.get("summary", "")))
    r.set(step="architect", last_artifact=r.rel("prd.md"))
    r.say("PRD written: {}".format(res.get("summary", "")))
    r.st["phase"] = "architect"


def ph_architect(r):
    r.set(step="architect", status="running")
    return r.spawn("architect", feedback=r.take_feedback("architect"),
                   answers=r.ctx.get("answers", {}).get("architect"))


def test_command(r):
    m = re.search(r"^## Test command\s*\n(.*?)(?=^## |\Z)", read(r.path("repo-digest.md")), re.S | re.M)
    body = (m.group(1) if m else "").replace("```", "").strip()
    return body


def res_architect(r, res):
    if res["status"] == "NEEDS_INPUT":
        return needs_input(r, "architect", res)
    tc = test_command(r)
    if not os.path.exists(r.path("plan.md")) or not tc or tc.upper() == "UNKNOWN":
        what = "plan.md is missing" if not os.path.exists(r.path("plan.md")) else "repo-digest.md's Test command is UNKNOWN"
        r.report("[architect] FAILED — DONE but {}".format(what))
        return r.stop("The Architect said DONE but {}. Fix it, then `/resume-orchestration {} --from architect`."
                      .format(what, r.id), "failed", "missing-artifact")
    r.report("[architect] DONE — {}".format(res.get("summary", "")))
    r.ctx["contract_tail"] = res.get("_tail", "")
    r.st["phase"] = "architect.check"


def architect_retry(r, violations, label):
    r.report("[{}] FAIL — returning to Architect\n\n{}".format(label, violations))
    if r.st["architect_retries"] >= CAP:
        return r.stop("Contract validation retry cap reached (2/2). Human intervention required. "
                      "Violations:\n\n{}\n\nFix plan.md/contract.json by hand or "
                      "`/resume-orchestration {} --from architect`.".format(violations, r.id),
                      "failed", "retry-cap")
    r.st["architect_retries"] += 1
    r.ctx.setdefault("feedback", {})["architect"] = (
        "Fix contract.json/plan.md so `check-contract.py validate` and `check-adr.py plan` pass: " + violations)
    r.say("Contract validation failed; re-running the Architect (attempt {}/2)...".format(r.st["architect_retries"]))
    r.st["phase"] = "architect"
    return None


def ph_architect_check(r):
    c1, o1 = r.call("check-contract.py", r.id, r.repo, "validate")
    c2, o2 = r.call("check-adr.py", r.id, "plan")
    if c1 == 0 and c2 == 0:
        r.st["phase"] = "sizing"
        return None
    v = "\n".join(o for c, o in ((c1, o1), (c2, o2)) if c != 0)
    return architect_retry(r, v, "check-contract validate / check-adr plan")


def ph_sizing(r):
    if r.ctx.get("escalated"):
        r.report("[size] standard — already escalated earlier in this run")
    else:
        code, out = r.call("size-run.py", r.id, r.repo)
        reasons = [l[2:].strip() for l in out.splitlines() if l.startswith("- ")]
        if code == 0:
            size = "small"
        elif code == 1:
            size = "standard"
        else:
            size, reasons = "standard", ["size-run error: " + out.strip().splitlines()[-1] if out.strip() else "size-run error"]
        r.st["size"], r.st["size_reasons"] = size, reasons
        r.report("[size] {} — {}".format(size, "; ".join(reasons)))
    tail = r.ctx.pop("contract_tail", "")
    r.say("Run size: **{}** ({})".format(r.st["size"], "; ".join(r.st["size_reasons"])))
    if tail:
        r.say(tail)
    r.say("Test command: `{}`".format(test_command(r)))
    r.set(step="render-setup", last_artifact=r.rel("plan.md"))
    r.st["phase"] = "render-setup"


# ── Stage 2b: render config ───────────────────────────────────────────────────

RS_USE, RS_SETUP, RS_SKIP, RS_OPTOUT = "Use {}", "Set it up now", "Skip this time", \
    "Skip, and don't ask again for this repo"


def ph_render_setup(r):
    r.set(step="render-setup", status="running")
    code, out = r.call("render-config.py", "check", r.id, r.repo)
    r.report("[render-config] {}".format(out.strip()))
    if code == 2 and out.strip().startswith("no-targets:"):
        r.say("Render review will be **SKIPPED**: " + out.strip()[len("no-targets:"):].strip())
    if code in (0, 2, 3, 4):
        return to_tester_stage(r)
    if r.st.get("unattended"):
        r.report("[render-config] SKIPPED: no one to ask (unattended)")
        return to_tester_stage(r)
    if code == 5:
        pend = [l.split(":", 1)[1].split()[0] for l in out.splitlines() if l.startswith("pending:")]
        x = pend[0] if pend else "?"
        r.ctx["rs_pending"] = x
        return r.ask("render-decision",
                     "{} has no render config on `{}`, but branch `{}` has one (it was never merged). "
                     "Use it for this run's render review?".format(r.repo, r.st["original_branch"], x),
                     [RS_USE.format(x), "Set up a new config", RS_SKIP], pause="awaiting_render_decision")
    targets = render_targets(r)
    return r.ask("render-decision",
                 "This run changes pages, but {repo} has no render config, so the render review would be "
                 "SKIPPED: no one would look at the composed pages.{t} I can set it up now: a subagent "
                 "writes the config (dev-server command, a sign-in and test-data hook), I commit it on a "
                 "separate branch `chore/render-config` cut from `{base}` (never this run's branch) and "
                 "smoke-test it by opening a real page. It adds a few minutes now and a review stage "
                 "later, and it may need Docker. You merge that branch as its own PR."
                 .format(repo=r.repo, base=r.st["original_branch"],
                         t=(" Targets: " + "; ".join(targets) + ".") if targets else ""),
                 [RS_SETUP, RS_SKIP, RS_OPTOUT], pause="awaiting_render_decision")


def contract_json(r):
    try:
        c = json.loads(read(r.path("contract.json")) or "{}")
    except ValueError:
        return {}
    return c if isinstance(c, dict) else {}


def render_none_reason(r):
    """The Architect's reason for declaring no render targets ('' when there are targets or no reason)."""
    c = contract_json(r)
    if c.get("render_targets"):
        return ""
    return (c.get("render_targets_none_reason") or c.get("render_skip_reason") or "").strip()


def render_targets(r):
    c = contract_json(r)
    out = []
    for t in c.get("render_targets") or []:
        out.append("{} {} ({})".format(t.get("id", "?"), t.get("route", "?"), t.get("state", "default")))
    return out


def ans_render_decision(r, qid, text):
    a = text.lower()
    if "don't ask" in a or "dont ask" in a or "do not ask" in a:
        _, out = r.call("render-config.py", "optout", r.repo, "--run", r.id, "--reason", text)
        r.report("[render-config] OPTED OUT by user for this repo")
        to_tester_stage(r)
    elif a.startswith("use"):
        x = r.ctx.get("rs_pending")
        r.st["render_config_ref"] = x
        r.report("[render-config] render_config_ref: {} (chosen by user)".format(x))
        r.st["phase"] = "render-setup.recheck"
    elif a.startswith("set"):
        r.st["phase"] = "rs.start"
    elif a.startswith("skip"):
        r.report("[render-config] SKIPPED by user for this run")
        to_tester_stage(r)
    else:
        r.say("I didn't understand that answer; asking again.")
        r.st["phase"] = "render-setup"


def ph_render_recheck(r):
    code, out = r.call("check-contract.py", r.id, r.repo, "validate")
    if code == 0:
        return to_tester_stage(r)
    return architect_retry(r, out, "check-contract validate (render config re-check)")


def to_tester_stage(r):
    r.set(step="tester")
    r.st["phase"] = "tester.enter"
    return None


def ph_rs_start(r):
    if os.path.exists(r.path("render-setup")):
        n = 1
        while os.path.exists(r.path("archive", "render-setup_v{}".format(n))):
            n += 1
        r.archive("render-setup", "render-setup_v{}".format(n), move=True)
    os.makedirs(r.path("render-setup"), exist_ok=True)
    r.st["render_setup_retries"] = 0
    r.ctx.update(rs_reuse=False, rs_branch="chore/render-config", rs_commit=None)
    r.st["phase"] = "rs.agent"


def ph_rs_agent(r):
    extra = [("SETUP_DIR", r.rel("render-setup")), ("BASE", r.st["original_branch"])]
    if r.has_contract():
        extra.append(("CONTRACT", r.rel("contract.json")))
    return r.spawn("render-setup", feedback=r.take_feedback("render-setup"),
                   answers=r.ctx.get("answers", {}).get("render-setup"), extra=extra)


def res_render_setup(r, res):
    st = res["status"]
    if st == "NEEDS_INPUT":
        if r.st.get("unattended"):
            return rs_fail(r, "the setup agent has questions and no one is here to answer")
        return needs_input(r, "render-setup", res)
    if st == "BLOCKED":
        r.report("[render-setup] BLOCKED — {}".format(res.get("blocked_reason", "")))
        if r.st.get("unattended"):
            return to_tester_stage(r)
        return r.ask("rs-blocked", "Render setup is blocked: {}. Stop asking about render config for "
                     "{}?".format(res.get("blocked_reason", ""), r.repo), [RS_OPTOUT, RS_SKIP])
    stop = r.scope_check()
    if stop:
        return stop
    r.ctx["rs_prereq"] = as_list(res.get("prerequisites"))
    r.report("[render-setup] DONE — {}\nsignin: {}\nprerequisites:\n{}\nfixtures:\n{}".format(
        res.get("summary", ""), res.get("signin", ""),
        "\n".join("- " + x for x in as_list(res.get("prerequisites"))) or "- none",
        "\n".join("- " + x for x in as_list(res.get("fixtures"))) or "- none"))
    r.st["phase"] = "rs.apply"


def ans_rs_blocked(r, qid, text):
    if "ask" in text.lower():
        r.call("render-config.py", "optout", r.repo, "--run", r.id, "--reason", "render setup blocked")
        r.report("[render-config] OPTED OUT by user for this repo")
    else:
        r.report("[render-config] SKIPPED by user for this run")
    to_tester_stage(r)


def ph_rs_apply(r):
    r.ctx.setdefault("rs_branch", "chore/render-config")
    args = ["apply", r.rel("render-setup"), r.repo, "--base", r.st["original_branch"],
            "--branch", r.ctx["rs_branch"]]
    if r.has_contract():
        args += ["--contract", r.rel("contract.json")]
    if r.ctx.get("rs_reuse"):
        args.append("--reuse")
    code, out = r.call("render-config.py", *args)
    if code == 0:
        m = re.search(r"^commit:\s*(\S+)", out, re.M)
        r.ctx["rs_commit"] = m.group(1) if m else None
        r.report("[render-setup] committed {} on {}".format(r.ctx["rs_commit"], r.ctx["rs_branch"]))
        r.st["phase"] = "rs.smoke"
    elif code == 1:
        return rs_retry(r, out)
    elif code == 6 and not r.ctx.get("rs_reuse"):
        alt = "chore/render-config-" + datetime.now().strftime("%Y%m%d")
        if r.st.get("unattended"):
            r.ctx["rs_branch"] = alt
            r.st["phase"] = "rs.apply"
            return None
        r.ctx["rs_alt"] = alt
        return r.ask("rs-branch", "Branch `{}` already exists in {}. Commit the config on top of it, "
                     "or use a new name?".format(r.ctx["rs_branch"], r.repo),
                     ["Commit on top of `{}`".format(r.ctx["rs_branch"]), "Use `{}`".format(alt)])
    else:
        return rs_fail(r, "render-config.py apply: " + out.strip())
    return None


def ans_rs_branch(r, qid, text):
    if text.lower().startswith("use"):
        r.ctx["rs_branch"] = r.ctx.get("rs_alt")
    else:
        r.ctx["rs_reuse"] = True
    r.st["phase"] = "rs.apply"


def ph_rs_smoke(r):
    r.ctx.setdefault("rs_branch", "chore/render-config")
    sd = r.rel("render-setup")
    code, out = r.call("render-config.py", "smoke-prep", sd, r.repo, "--branch", r.ctx["rs_branch"])
    if code == 3:
        return rs_fail(r, "smoke-prep: " + out.strip())
    if code != 0:
        return rs_retry(r, out)
    code, out = r.call("render-capture.mjs", r.repo, "render-smoke", "--run-dir", sd + "/smoke", "--only", "after")
    summary = {}
    try:
        summary = json.loads(read(r.path("render-setup", "smoke", "render", "summary.json")) or "{}")
    except ValueError:
        pass
    if code == 0:
        n = len({x.get("target") for x in summary.get("results", []) if x.get("reached")})
        r.report("[render-setup] PASS — smoke test reached {} page(s) on {}".format(n, r.ctx["rs_branch"]))
        r.st["render_config_ref"] = r.ctx["rs_branch"]
        r.st["phase"] = "render-setup.recheck"
        return None
    return rs_retry(r, "{}\nScreenshots: {sd}/smoke/render/after/, dev server log: "
                       "{sd}/smoke/render/logs/after-dev.log".format(summary.get("reason") or out.strip(), sd=sd))


def rs_retry(r, feedback):
    if r.st["render_setup_retries"] >= CAP:
        return rs_fail(r, feedback.strip().splitlines()[0] if feedback.strip() else "retry cap reached")
    r.st["render_setup_retries"] += 1
    r.ctx.setdefault("feedback", {})["render-setup"] = feedback
    r.ctx["rs_reuse"] = True
    r.st["phase"] = "rs.agent"
    return None


def rs_fail(r, reason):
    extra = ""
    if r.ctx.get("rs_commit"):
        extra = " (branch {} with commit {} is left for you to inspect or delete)".format(
            r.ctx["rs_branch"], r.ctx["rs_commit"])
    r.report("[render-setup] FAILED — {}{}".format(reason, extra))
    r.ctx["rs_failed"] = reason
    r.say("Render setup failed ({}); this run continues without render review.".format(reason))
    return to_tester_stage(r)


# ── Stage 3: tester ───────────────────────────────────────────────────────────

def ph_tester_enter(r):
    r.st["retry_count"] = 0
    r.st["phase"] = "tester"


def ph_tester(r):
    r.set(step="tester", status="running")
    return r.spawn("tester", feedback=r.take_feedback("tester"), model=r.model_for("tester"))


def res_tester(r, res):
    stop = r.scope_check()
    if stop:
        return stop
    gaps = as_list(res.get("gaps"))
    r.report("[tester] DONE — {} test files, {} cases, {} contract gaps\n{}".format(
        res.get("test_files", "?"), res.get("test_cases", "?"), res.get("contract_gaps", len(gaps)),
        "\n".join("- " + f for f in as_list(res.get("files")))).rstrip())
    if gaps:
        r.say("The Tester found contract gaps (you may want to resume from architect to fill them):\n" +
              "\n".join("- " + g for g in gaps))
    # A run whose plan declares no test files (config/docs only) legitimately leaves tests/ empty or absent;
    # downstream gates (check-contract, run-tests.sh) require the folder to exist.
    os.makedirs(r.path("tests"), exist_ok=True)
    r.set(step="test-reviewer", last_artifact=r.rel("tests") + "/")
    r.st["phase"] = "tester.paths"


def ph_tester_paths(r):
    code, out = r.call("check-paths.py", r.id, "tests", "--prune")
    if code == 0:
        r.st["phase"] = "tester.commit"
        return None
    if code == 3:
        r.report("[check-paths] ERROR — {}".format(out))
        return r.stop("plan.md has no Files changed table ({}). Resume from architect: "
                      "`/resume-orchestration {} --from architect`.".format(out.strip(), r.id),
                      "failed", "no-files-changed-table")
    return r.to_tester("[check-paths] FAIL — returning to Tester ({retry})\n\n" + out, out,
                       "Contract check", "Path check failed. Retrying Tester with the violations as feedback (attempt {n}/2)...",
                       escalate_why="the path check sent the tests back to the Tester")


def ph_tester_commit(r):
    repo = r.repo
    copy_tree(r.path("tests"), repo)
    _, out = r.call("autofix.py", repo, r.id, "tests")
    if out.strip():
        r.report(out.strip())
    git(repo, "add", "-A")
    staged, _ = git(repo, "diff", "--cached", "--quiet")
    if staged != 0:
        rc = r.st["retry_count"]
        task = r.title
        if rc == 0:
            msg = ("test: {}\n\nWritten test-first, before any implementation. Not yet reviewed — subject "
                   "to the mechanical contract gate and the test-reviewer.\n\nRun: {}").format(task, r.id)
        else:
            msg = "fix tests: {} (retry {}/2)\n\nRun: {}".format(task, rc, r.id)
        code, out = git(repo, "commit", "-m", msg)
        if code != 0:
            r.report("[tester] commit failed: {}".format(out))
            return r.stop("Committing the tests failed:\n{}".format(out), "failed", "commit-failed")
        _, sha = git(repo, "rev-parse", "--short", "HEAD")
        r.report("[tester] committed {} ({})".format(sha, msg.splitlines()[0]))
    else:
        r.report("[tester] tests unchanged, nothing to commit")
    r.st["phase"] = "4a"


def copy_tree(src, dst):
    if not os.path.isdir(src):
        return
    for root, _, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target = os.path.join(dst, rel) if rel != "." else dst
        os.makedirs(target, exist_ok=True)
        for f in files:
            shutil.copy2(os.path.join(root, f), os.path.join(target, f))


# ── Stage 4: contract gate + test reviewer ────────────────────────────────────

def ph_4a(r):
    r.set(step="test-reviewer", status="running")
    os.makedirs(r.path("tests"), exist_ok=True)  # config/docs-only runs have no test files
    if r.has_contract():
        code, out = r.call("check-contract.py", r.id, r.repo, "tests")
        if code == 3:
            r.report("[check-contract] ERROR — {}".format(out))
            return r.stop("check-contract.py tests failed with a setup error:\n{}\n\nFix it, then "
                          "`/resume-orchestration {} --from test-reviewer`.".format(out, r.id),
                          "failed", "contract-check-error")
    else:
        code, out = r.call("check-contract.sh", r.id, r.repo)
    if code == 0:
        r.st["phase"] = "4b"
        return None
    n = len([l for l in out.splitlines() if l.strip()])
    fb = ("The mechanical contract check failed with these violations: {}. Fix every one before writing the "
          "new tests — these are not style suggestions, they are exact name/path/package mismatches. Check "
          "memory.md's check-contract false-positive list first; if a violation is a known false positive, "
          "handle it as that list says.").format(out)
    return r.to_tester("[check-contract] FAIL — {} violation(s), returning to Tester ({{retry}})\n\n{}".format(n, out),
                       fb, "Contract check",
                       "Mechanical contract check failed. Retrying Tester with violations as feedback (attempt {n}/2)...",
                       escalate_why="the contract check sent the tests back to the Tester")


def touch(path):
    with open(path, "a"):
        os.utime(path, None)


def has_test_files(r):
    return any(files for _, _, files in os.walk(r.path("tests")))


def ph_4b(r):
    r.set(step="test-reviewer", status="running")
    if not has_test_files(r):
        r.report("[test-reviewer] SKIPPED — no test files in this run (nothing to review)")
        touch(r.path(".test_reviewer_passed_at"))
        r.ctx["review_skipped"] = True
        return after_review_pass(r)
    if r.small() and not r.ctx.get("deferred"):
        r.report("[test-reviewer] SKIPPED — small run (mechanical check 4a passed)")
        touch(r.path(".test_reviewer_passed_at"))
        r.ctx["review_skipped"] = True
        return after_review_pass(r)
    note_parts = []
    timeout_note = r.ctx.pop("timeout_review", None)
    if timeout_note:
        note_parts.append(timeout_note)
    else:
        code, out = r.call("review-delta.py", r.id, r.repo, "diff")
        if code == 2:
            r.report("[test-reviewer] NOT NEEDED — tests unchanged since the last PASS")
            touch(r.path(".test_reviewer_passed_at"))
            r.ctx.pop("review_note", None)
            return after_review_pass(r)
        if code == 0:
            delta = "REVIEW_MODE: delta\n" + out.strip()
            fails = r.ctx.get("last_review_failures") if r.ctx.get("last_review_verdict") == "FAIL" else None
            if fails:
                delta += "\nfailures (from the last review; check each is fixed):\n" + "\n".join("- " + f for f in fails)
            note_parts.append(delta)
        elif code == 3:
            r.report("[review-delta] ERROR — {} (running a full review)".format(out.strip()))
    extra = r.ctx.pop("review_note", None)
    if extra:
        note_parts.append(extra)
    return r.spawn("test-reviewer", note="\n".join(note_parts) or None)


def res_test_reviewer(r, res):
    verdict = res["verdict"]
    r.call("review-delta.py", r.id, r.repo, "mark", verdict)
    r.report("[test-reviewer] {}\n{}".format(verdict, res["_raw"]))
    r.ctx["last_review_verdict"] = verdict
    if verdict == "PASS":
        gaps = as_list(res.get("contract_gaps"))
        if gaps:
            r.say("Test-reviewer advisory contract gaps:\n" + "\n".join("- " + g for g in gaps))
        return after_review_pass(r)
    fails = as_list(res.get("failures"))
    r.ctx["last_review_failures"] = fails
    r.ctx.pop("deferred", None)
    fb = ("The previous test attempt was rejected by the test-reviewer with this feedback: {}. Fix the issues "
          "listed before writing the new tests.").format("\n".join("- " + f for f in fails))
    return r.to_tester(None, fb, "Test-reviewer", "Test-reviewer rejected. Retrying Tester with feedback (attempt {n}/2)...")


def after_review_pass(r):
    ctx = r.ctx
    if ctx.get("deferred"):
        r.apply(ctx.pop("deferred"))
        return None
    if ctx.get("timeout_feedback"):
        fb = ctx.pop("timeout_feedback")
        return r.to_coder(None, fb, "address sandbox timeout", "Coder",
                          "Test review passed after the timeout. Sending the timeout to the Coder (attempt {n}/2)...",
                          None)
    if "verify_coder" in ctx:
        vc = ctx.pop("verify_coder")
        if vc:
            r.set(step="coder")
            r.apply({"archive": [("code", "code_verify_v{}".format(r.st["verify_retries"]))],
                     "feedback": {"coder": vc}, "ctx": {"fix_reason": "address Verify errors", "render_pass": False},
                     "phase": "coder"})
        else:
            r.st["phase"] = "5b"
        return None
    r.set(step="coder", status="running")
    r.st["phase"] = "coder.enter"
    return None


# ── Stage 5: coder ────────────────────────────────────────────────────────────

def ph_coder_enter(r):
    # A fresh entry once code is committed is a fix pass (the render-review rule:
    # retry_count 1, so commits are `fix:` commits and 5b/6 keep one retry).
    r.st["retry_count"] = 1 if r.ctx.get("code_committed") else 0
    if r.ctx.get("code_committed"):
        r.ctx.setdefault("fix_reason", "address review findings")
    else:  # a new first pass (e.g. after a resume from architect): slice it afresh
        r.ctx.pop("slices", None)
        r.st["coder_slices_done"] = 0
    r.st["phase"] = "coder"


def mismatch_stop(r):
    return r.stop("Pipeline paused: CONTRACT_MISMATCH detected. The Interface Contract needs correction.\n"
                  "Run: `/resume-orchestration {} --from architect`\nSee `runs/{}/report.md` for details.\n"
                  "Whatever was already committed this pass is on `{}` in {}."
                  .format(r.id, r.id, r.st["target_branch"], r.repo), "paused", "contract-mismatch")


def ph_coder(r):
    r.set(step="coder", status="running")
    code, out = r.call("seed-code.py", r.id, r.repo)
    if code == 3:
        r.report("[seed-code] ERROR — {}".format(out))
        return mismatch_stop(r)
    first = r.st["retry_count"] == 0 and r.st["render_retries"] == 0 and not r.ctx.get("code_committed")
    if first and "slices" not in r.ctx:
        code, out = r.call("coder-slices.py", "plan", r.id, r.repo)
        r.ctx["slices"] = None
        if code == 0:
            data = json.loads(read(r.path("coder-slices.json")))
            r.ctx["slices"] = {"n": len(data["slices"]), "labels": [s.get("label", "") for s in data["slices"]]}
        elif code == 3:
            r.report("[coder-slices] ERROR — {} (running a single pass)".format(out.strip()))
    if first and r.ctx.get("slices") and r.st["coder_slices_done"] < r.ctx["slices"]["n"]:
        r.st["phase"] = "coder.slice"
        return None
    r.call("coder-slices.py", "set", r.id, "0")
    r.ctx["coder_mode"] = "single"
    return r.spawn("coder", feedback=r.take_feedback("coder"), model=r.model_for("coder"))


def ph_coder_slice(r):
    s = r.ctx["slices"]
    k = r.st["coder_slices_done"] + 1
    r.call("coder-slices.py", "set", r.id, str(k))
    r.ctx["coder_mode"] = "slice"
    return r.spawn("coder", model=r.model_for("coder"),
                   note="SLICE {}/{} ({}): implement only this slice's files; see runs/{}/coder-slices.json."
                   .format(k, s["n"], s["labels"][k - 1], r.id))


def append_deviations(r, devs, heading):
    if not devs:
        return
    p = r.path("deviations.md")
    cur = read(p)
    write(p, cur + ("\n" if cur and not cur.endswith("\n\n") else "") + heading + "\n\n" +
          "\n".join("- " + d for d in devs) + "\n")
    r.report("deviations ({}):\n{}".format(heading.lstrip("# "), "\n".join("- " + d for d in devs)))


def res_coder(r, res):
    mode = r.ctx.pop("coder_mode", "single")
    if res["status"] == "CONTRACT_MISMATCH":
        r.call("coder-slices.py", "set", r.id, "0")
        r.escalate("coder", "the Coder returned CONTRACT_MISMATCH")
        r.report("[coder] CONTRACT_MISMATCH — {}\nrecommendation: {}".format(res.get("detail", ""),
                                                                          res.get("recommendation", "")))
        return mismatch_stop(r)
    raw_notes = res.get("notes", "")
    # notes may be a one-line string or a list (bullets under `notes:`)
    notes = " ".join(raw_notes) if isinstance(raw_notes, list) else raw_notes
    if notes and notes.strip().lower() not in ("none", '"none"'):
        r.say("Coder notes (if this is an environment issue, the fix may belong in repo-digest.md's "
              "Test command, not the code): " + notes)
    devs = as_list(res.get("deviations"))
    files = as_list(res.get("files"))
    if mode == "slice":
        s = r.ctx["slices"]
        k = r.st["coder_slices_done"] + 1
        r.report("[coder] slice {}/{} DONE — {} files".format(k, s["n"], len(files)))
        append_deviations(r, devs, "## Coder pass 0, slice {}/{}".format(k, s["n"]))
        r.st["coder_slices_done"] = k
        if k < s["n"]:
            r.st["phase"] = "coder.slice"
            return None
        r.call("coder-slices.py", "set", r.id, "0")
    else:
        heading = ("## Coder pass render-{}".format(r.st["render_retries"]) if r.ctx.get("render_pass")
                   else "## Coder pass {}".format(r.st["retry_count"]))
        r.report("[coder] DONE — {} files; deviations: {}".format(len(files), len(devs) or "none"))
        append_deviations(r, devs, heading)
    r.st["phase"] = "coder.done"
    return None


def ph_coder_done(r):
    stop = r.scope_check()
    if stop:
        return stop
    code, out = r.call("check-paths.py", r.id, "code")
    if code == 3:
        r.report("[check-paths] ERROR — {}".format(out))
        return r.stop("check-paths.py code failed: {}".format(out), "failed", "no-files-changed-table")
    if code == 1:
        return r.to_coder("[check-paths code] FAIL — returning to Coder ({retry})\n\n" + out, out,
                          "address path-check violations", "Code path check",
                          "Coder's files don't match the plan. Retrying Coder with the violations as feedback (attempt {n}/2)...",
                          "the path check sent the code back to the Coder")
    rc = r.st["retry_count"]
    if rc == 0:
        code, out = r.call("commit-code.py", r.repo, r.id)
    else:
        code, out = r.call("commit-code.py", r.repo, r.id, "--fix",
                           "{} (retry {}/2)".format(r.ctx.get("fix_reason") or "address review findings", rc))
    if out.strip():
        r.report(out.strip())
    if code != 0:
        return r.stop("Committing the implementation failed:\n{}".format(out), "failed", "commit-failed")
    warns = [l for l in out.splitlines() if "WARNING" in l]
    if warns:
        r.say("\n".join(warns))
    r.ctx["code_committed"] = True
    r.ctx["render_pass"] = False
    r.st["phase"] = "5b"
    return None


# ── Stage 5b: code contract gate ──────────────────────────────────────────────

def ph_5b(r):
    if r.has_contract():
        code, out = r.call("check-contract.py", r.id, r.repo, "code")
        if code == 3:
            r.report("[check-contract --code] ERROR — {}".format(out))
            return r.stop("check-contract.py code failed with a setup error:\n{}".format(out),
                          "failed", "contract-check-error")
    else:
        code, out = r.call("check-contract.sh", r.id, r.repo, "--code")
    if code == 0:
        r.st["phase"] = "sandbox"
        return None
    if "TESTS_MODIFIED_AFTER_REVIEW" in out:
        r.report("[check-contract --code] FAIL — TESTS_MODIFIED_AFTER_REVIEW\n\n" + out)
        return r.stop("Tests were modified after the test-reviewer approved them. This requires human review — "
                      "run `/resume-orchestration {} --from tester` once you've decided how to proceed.\n"
                      "Whatever was already committed this pass is on `{}` in {}."
                      .format(r.id, r.st["target_branch"], r.repo), "failed", "tests-modified-after-review")
    n = len([l for l in out.splitlines() if l.strip()])
    return r.to_coder("[check-contract --code] FAIL — {} violation(s), returning to Coder ({{retry}})\n\n{}".format(n, out),
                      out, "address code contract violations", "Code contract check",
                      "Coder dropped a contract testid. Retrying Coder with the violation as feedback (attempt {n}/2)...",
                      "the code contract check sent the code back to the Coder")


# ── Stage 6: sandbox ──────────────────────────────────────────────────────────

def latest_section(r, title):
    rep = read(r.path("report.md"))
    i = rep.rfind(title)
    if i < 0:
        return ""
    rest = rep[i:]
    m = re.search(r"\n---\n", rest[1:])
    return rest[: m.start() + 1] if m else rest


def ph_sandbox(r):
    r.set(step="sandbox", status="running")
    code, out = r.call("run-tests.sh", r.repo, r.id, "-")
    r.st["last_artifact"] = r.rel("code") + "/"
    if code == 0:
        r.ctx.pop("last_feedback", None)
        r.set(step="verify")
        r.st["phase"] = "verify"
        return None
    entry = latest_section(r, "## Test sandbox run")
    if code == 2:
        m = re.search(r"- Timeout:\s*(\d+)s", entry)
        secs = m.group(1) if m else "?"
        note = ("IMPORTANT: The previous test run timed out after {}s (the `- Timeout:` line of the latest "
                "sandbox entry in report.md). Check all tests for unresolved promises, missing mocks, or real "
                "network calls.").format(secs)
        r.say("Tests timed out. Routing back to Test-Reviewer.")
        r.escalate("sandbox", "the sandbox timed out and routed work back to the Test-Reviewer")
        r.ctx["review_skipped"] = False
        r.ctx["timeout_review"] = note
        r.ctx["timeout_feedback"] = note + "\n\nThe test review found nothing to change in the tests; look for " \
                                           "what in the code can hang (an unresolved promise, a real network call)."
        r.set(step="test-reviewer")
        r.st["phase"] = "4b"
        return None
    if code == 1:
        m = re.search(r"Full output: `([^`]+)`", entry)
        fb = entry.strip() + ("\n\nFull test output: {}".format(m.group(1)) if m else "")
        return r.to_coder(None, fb, "address sandbox failures", "Coder",
                          "Tests failed. Retrying Coder with failure output as feedback (attempt {n}/2)...",
                          "the sandbox failed and routed work to the Coder")
    tc = test_command(r)
    hint = ""
    if not tc or tc.upper() == "UNKNOWN" or "```" in tc:
        hint = "\n\nThe Test command in runs/{}/repo-digest.md is invalid: fix that section, not the code.".format(r.id)
    return r.stop("The sandbox environment is broken (run-tests.sh exit {}). This is not a test verdict.\n\n{}{}\n\n"
                  "Whatever was already committed is on `{}` in {}, untouched by this failure — only the "
                  "disposable sandbox worktree is affected. Resume with `/resume-orchestration {} --from sandbox`."
                  .format(code, entry.strip() or out[-3000:], hint, r.st["target_branch"], r.repo, r.id),
                  "failed", "sandbox-infrastructure-error")


# ── Stage 6b: verify ──────────────────────────────────────────────────────────

def verify_summary(r):
    md = read(r.path("verify.md"))
    m = re.search(r"Machine-readable summary:\s*```json\s*\n(.*?)\n```", md, re.S)
    try:
        return md, json.loads(m.group(1)) if m else {}
    except ValueError:
        return md, {}


def md_section(md, heading_prefix):
    m = re.search(r"^" + re.escape(heading_prefix) + r".*?$(.*?)(?=^#{2,3} |\Z|^---$)", md, re.S | re.M)
    return m.group(0).strip() if m else ""


def ph_verify(r):
    r.set(step="verify", status="running")
    code, out = r.call("verify.py", r.repo, r.id)
    md, summ = verify_summary(r)
    table = "\n".join(l for l in md.splitlines() if l.startswith("|"))
    if code == 0:
        r.report("[verify] PASS")
        r.ctx.pop("lockfile_tried", None)
        r.ctx.pop("not_verified", None)
        r.set(step="render")
        r.st["phase"] = "render"
        return None
    if code == 1:
        r.say("Verify failed:\n" + table)
        if "**Hint:** Lockfile is out of date" in md and not r.ctx.get("lockfile_tried"):
            c2, o2 = r.call("commit-code.py", r.repo, r.id, "--lockfile")
            r.report("[verify] lockfile out of date; " + (o2.strip() or "commit-code --lockfile"))
            if c2 != 0:
                return r.stop("Updating the lockfile failed:\n{}".format(o2), "failed", "commit-failed")
            r.ctx["lockfile_tried"] = True
            return None
        r.ctx.pop("lockfile_tried", None)
        by_file = summ.get("new_errors_by_file") or {}
        nofile = summ.get("failed_steps_without_file_info") or []
        if r.st["verify_retries"] >= CAP:
            return r.stop("Verify retry cap reached (2/2). Human intervention required.\n\n{}\n\nSee `runs/{}/verify.md`. "
                          "Fix by hand and `/resume-orchestration {} --from verify`, or resume from tester/coder."
                          .format(table, r.id, r.id), "failed", "retry-cap")
        tester_files = {f: v for f, v in by_file.items() if os.path.isfile(r.path("tests", f))}
        coder_files = {f: v for f, v in by_file.items() if f not in tester_files}
        if not tester_files and not coder_files and not nofile:
            r.report("[verify] FAIL — no file or step to route the failure to")
            return r.stop("Verify failed, but its summary names no file and no failed step, so there's no one to "
                          "route it to. See `runs/{}/verify.md`; fix by hand and `/resume-orchestration {} --from verify`."
                          .format(r.id, r.id), "failed", "verify-unroutable")
        r.st["verify_retries"] += 1
        vr = r.st["verify_retries"]
        needs = r.escalate("verify", "Verify failed and routed work to the {}".format(
            "Tester" if tester_files else "Coder"))
        coder_fb = None
        if coder_files or nofile:
            parts = ["{}:\n{}".format(f, "\n".join(v)) for f, v in coder_files.items()]
            parts += [md_section(md, "### {} (".format(s)) or "Failed step: {}".format(s) for s in nofile]
            coder_fb = "Verify found these errors in the code (runs/{}/verify.md has the details):\n\n{}".format(
                r.id, "\n\n".join(parts))
        r.report("[verify] FAIL — routing: {} Tester-owned file(s), {} Coder-owned file(s), {} step(s) without "
                 "file info (verify retry {}/2)".format(len(tester_files), len(coder_files), len(nofile), vr))
        rc = max(r.st["retry_count"], 1)
        if tester_files:
            fb = "\n\n".join("{}:\n{}".format(f, "\n".join(v)) for f, v in tester_files.items())
            fb += "\n\nThese are lint/type errors from Verify. Fix only these; don't change what any test asserts."
            r.set(step="tester")
            r.route({"rc": rc, "archive": [("tests", "tests_verify_v{}".format(vr))], "feedback": {"tester": fb},
                     "ctx": {"review_note": VERIFY_TESTER_NOTE, "verify_coder": coder_fb},
                     "phase": "tester"}, needs)
        else:
            r.set(step="coder")
            r.route({"rc": rc, "archive": [("code", "code_verify_v{}".format(vr))], "feedback": {"coder": coder_fb},
                     "ctx": {"fix_reason": "address Verify errors", "render_pass": False},
                     "phase": "coder"}, needs)
        return None
    if code == 4:
        nv = md_section(md, "## Not verified")
        r.ctx["not_verified"] = summ.get("not_verified") or []
        if r.st.get("unattended"):
            return r.stop("Verify is INCOMPLETE: required checks couldn't run.\n\n{}\n\nStart Docker (or export "
                          "VERIFY_DATABASE_URL) and `/resume-orchestration {} --from verify`.".format(nv, r.id),
                          "paused", "verify-incomplete")
        return r.ask("verify-accept", "Verify is INCOMPLETE: nothing failed, but required checks couldn't run "
                     "(usually: no Docker).\n\n{}\n\nStart Docker (or export VERIFY_DATABASE_URL pointing at a local "
                     "throwaway Postgres) and resume with `/resume-orchestration {} --from verify`, or accept the "
                     "gap and continue.".format(nv, r.id),
                     ["Stop; I'll resume with --from verify", "Accept the gap and continue"],
                     pause="verify-incomplete")
    return r.stop("Verify hit an infrastructure error (exit {}), not a code verdict:\n\n{}"
                  .format(code, out[-3000:]), "failed", "verify-infrastructure-error")


def ans_verify_accept(r, qid, text):
    if text.lower().startswith("accept"):
        items = r.ctx.get("not_verified") or []
        r.report("[verify] ACCEPTED UNVERIFIED by user: {}".format(", ".join(items)))
        r.ctx["accepted_unverified"] = items
        r.set(step="render")
        r.st["phase"] = "render"
    else:
        r.stop("Paused at verify (INCOMPLETE). Resume with `/resume-orchestration {} --from verify`.".format(r.id),
               "paused", "verify-incomplete")


# ── Stage 6c: render review ───────────────────────────────────────────────────

def ph_render(r):
    r.set(step="render", status="running")
    code, out = r.call("render-capture.mjs", r.repo, r.id)
    try:
        summ = json.loads(read(r.path("render", "summary.json")) or "{}")
    except ValueError:
        summ = {}
    reason = summ.get("reason") or (out.strip().splitlines() or [""])[-1]
    if code == 2:
        r.report("[render] SKIPPED — {}".format(reason))
        r.ctx["render_outcome"] = "SKIPPED ({})".format(reason)
        r.set(step="adr")
        r.st["phase"] = "adr"
        return None
    if code == 1:
        r.ctx["render_reasons"] = reason
        if r.st.get("unattended"):
            return r.stop("Render capture INCOMPLETE: {}\nFix the cause and `/resume-orchestration {} --from render`."
                          .format(reason, r.id), "paused", "render-incomplete")
        return r.ask("render-accept", "Render capture is INCOMPLETE: {}\n\nFix the cause and resume with "
                     "`/resume-orchestration {} --from render`, or accept the gap and continue.".format(reason, r.id),
                     ["Stop; I'll resume with --from render", "Accept the gap and continue"],
                     pause="render-incomplete")
    if code != 0:
        return r.stop("Render capture hit an infrastructure error (exit {}): {}\nDev-server logs: runs/{}/render/logs/"
                      .format(code, reason, r.id), "failed", "render-infrastructure-error")
    if r.st["render_retries"] > 0 and os.path.exists(r.path("render-review.md")):
        r.archive("render-review.md", "render-review_v{}.md".format(r.st["render_retries"]), move=True)
    return r.spawn("render-reviewer")


def ans_render_accept(r, qid, text):
    if text.lower().startswith("accept"):
        reasons = r.ctx.get("render_reasons", "")
        r.report("[render] ACCEPTED UNREVIEWED by user: {}".format(reasons))
        r.ctx["render_outcome"] = "ACCEPTED UNREVIEWED ({})".format(reasons)
        r.set(step="adr")
        r.st["phase"] = "adr"
    else:
        r.stop("Paused at render (INCOMPLETE). Resume with `/resume-orchestration {} --from render`.".format(r.id),
               "paused", "render-incomplete")


def res_render_reviewer(r, res):
    if not os.path.exists(r.path("render-review.md")):
        r.report("[render-reviewer] FAILED — render-review.md is missing")
        return r.stop("The render reviewer returned no render-review.md.", "failed", "missing-artifact")
    r.report("[render-reviewer] {}\n{}".format(res["verdict"], res["_raw"]))
    r.ctx["render_notes"] = as_list(res.get("notes"))
    if res["verdict"] == "PASS":
        r.ctx["render_outcome"] = "PASS"
        r.set(step="adr")
        r.st["phase"] = "adr"
        return None
    blocking = as_list(res.get("blocking"))
    rr = r.st["render_retries"]
    if rr >= CAP:
        return r.stop("Render-review retry cap reached (2/2). Human intervention required.\n\nBlocking:\n{}\n\n"
                      "See `runs/{}/render-review.md` and the screenshots in `runs/{}/render/`. Fix by hand and run "
                      "`/resume-orchestration {} --from render`, or resume from coder."
                      .format("\n".join("- " + b for b in blocking), r.id, r.id, r.id), "failed", "retry-cap")
    rr += 1
    r.st["render_retries"] = rr
    needs = r.escalate("render", "the render review sent findings to the Coder")
    fb = ("{}\n\nFull review with screenshots: runs/{id}/render-review.md and runs/{id}/render/. These are "
          "problems in the rendered page, not test failures. Fix only what is listed; don't change what any test "
          "asserts. If a fix can't be made without changing a reviewed test, return CONTRACT_MISMATCH.").format(
        "\n".join("- " + b for b in blocking), id=r.id)
    r.set(step="coder")
    r.route({"rc": 1, "archive": [("code", "code_render_v{}".format(rr))], "feedback": {"coder": fb},
             "ctx": {"fix_reason": "address render review findings", "render_pass": True},
             "say": "Render review found {} blocking problem(s). Sending them to the Coder (attempt {}/2)...".format(
                 len(blocking), rr), "phase": "coder"}, needs)
    return None


# ── Stage 6d: ADR ─────────────────────────────────────────────────────────────

def ph_adr(r):
    r.set(step="adr", status="running")
    if os.path.exists(r.path("adr")):
        n = 1
        while os.path.exists(r.path("archive", "adr_v{}".format(n))):
            n += 1
        r.archive("adr", "adr_v{}".format(n), move=True)
    code, out = r.call("check-adr.py", r.id, "trivial")
    if code == 0:
        r.report("[adr] NONE — trivial run, writer not run")
        r.ctx["adr_outcome"] = "none (trivial run)"
        r.st["phase"] = "done"
        return None
    r.ctx["adr_attempt"] = 1
    return r.spawn("adr")


def ph_adr_retry(r):
    return r.spawn("adr", feedback=r.take_feedback("adr"))


def res_adr(r, res):
    if res["status"] == "NONE":
        r.report("[adr] NONE — {}\nskipped:\n{}".format(res.get("summary", ""), "\n".join(
            "- " + s for s in as_list(res.get("skipped"))) or "- none"))
        r.ctx["adr_outcome"] = "none ({})".format(res.get("summary", ""))
        r.st["phase"] = "done"
        return None
    stop = r.scope_check()
    if stop:
        return stop
    code, out = r.call("check-adr.py", r.id, r.repo, "adr")
    if code == 1 and r.ctx.get("adr_attempt", 1) == 1:
        n = 1
        while os.path.exists(r.path("archive", "adr_v{}".format(n))):
            n += 1
        r.archive("adr", "adr_v{}".format(n))
        r.ctx["adr_attempt"] = 2
        r.ctx.setdefault("feedback", {})["adr"] = out
        r.st["phase"] = "adr.retry"
        return None
    if code != 0:
        r.report("[adr] NOT WRITTEN — {}".format(out.strip()))
        r.ctx["adr_outcome"] = "NOT WRITTEN ({})".format(out.strip().splitlines()[0] if out.strip() else "check failed")
        r.st["phase"] = "done"
        return None
    copy_tree(r.path("adr"), r.repo)
    git(r.repo, "add", "docs/adr")
    adrs = as_list(res.get("adrs"))
    staged, _ = git(r.repo, "diff", "--cached", "--quiet")
    if staged != 0:
        files = "\n".join(a.split(":", 1)[0].strip() for a in adrs)
        msg = "docs(adr): {}\n\n{}\n\nRun: {}".format(res.get("summary", ""), files, r.id)
        c, o = git(r.repo, "commit", "-m", msg)
        if c != 0:
            return r.stop("Committing the ADRs failed:\n{}".format(o), "failed", "commit-failed")
    lst = lambda k: "\n".join("- " + x for x in as_list(res.get(k))) or "- none"
    r.report("[adr] DONE — {}\nadrs:\n{}\nsuperseded:\n{}\nstale_docs:\n{}\nskipped:\n{}".format(
        res.get("summary", ""), lst("adrs"), lst("superseded"), lst("stale_docs"), lst("skipped")))
    r.ctx["adr_outcome"] = "\n".join(adrs) + ("\nsuperseded: " + "; ".join(as_list(res.get("superseded")))
                                              if as_list(res.get("superseded")) else "")
    r.ctx["stale_docs"] = as_list(res.get("stale_docs"))
    r.st["phase"] = "done"
    return None


# ── Stage 7: done ─────────────────────────────────────────────────────────────

def ph_done(r):
    st, ctx = r.st, r.ctx
    r.set(step="done", status="done", last_artifact=r.rel("code") + "/")
    orig, br, repo = st["original_branch"], st["target_branch"], r.repo
    ref = st.get("render_config_ref")
    render = ctx.get("render_outcome") or "SKIPPED (not run)"
    rc_line = ("from render_config_ref {} (not merged yet)".format(ref) if ref and not merged(repo, ref, orig)
               else "from render_config_ref {}".format(ref) if ref
               else "from {}".format(orig) if render.startswith(("PASS", "ACCEPTED")) else "none")
    none_reason = render_none_reason(r)
    r.report("---\n## Result: PASS ✓\n\nAll tests passed. Pipeline complete.\nRender review: {}\n{}"
             "Render config: {}\nFinished: {}".format(
                 render, "Render targets: none ({})\n".format(none_reason) if none_reason else "", rc_line, now_iso()))
    _, log = git(repo, "log", "--oneline", "{}..{}".format(orig, br))
    _, count = git(repo, "rev-list", "--count", "{}..{}".format(orig, br))
    r.report("\n### Commits on {} ({} ahead of {})\n{}".format(br, count, orig, log))
    code, out = r.call("run-usage.py", r.id)
    m = re.search(r"total_tokens:\s*(\d+)\s*\|\s*weighted:\s*(\d+)", out)
    tokens = "{} (weighted {})".format(m.group(1), m.group(2)) if code == 0 and m else "unavailable"
    usage = md_section(read(r.path("usage.md")), "## By agent") if code == 0 else \
        "unavailable ({})".format(out.strip().splitlines()[-1] if out.strip() else "no transcript")
    size = "small→standard" if ctx.get("escalated") else st["size"]
    add_run_history(r, "- {} | {} | task: {} | result: PASS | size: {} | tokens: {}".format(
        r.id, datetime.now().strftime("%Y-%m-%d"), r.title, size, tokens))
    vmd, _ = verify_summary(r)
    checklist = handoff_checklist(r, vmd, ref, orig)
    msg = ["✓ Pipeline complete! Tests passed and the branch is verified.", "",
           "**Run:** " + r.id,
           "**Branch:** `{}` in {} — {} commit(s) ahead of `{}`:".format(br, repo, count, orig), log, "",
           "Your previous branch (`{0}`) is untouched — switch back anytime with `git checkout {0}`. Everything the "
           "pipeline did is already committed on `{1}`; nothing further needs to be applied.".format(orig, br), "",
           "**Verify:**", "\n".join(l for l in vmd.splitlines() if l.startswith("|")), "",
           "**Render review:** " + render_line(r, render), "",
           "**Run size:** `{}` ({})".format(st["size"], "; ".join(st.get("size_reasons") or [])), "",
           "**ADRs:** " + (ctx.get("adr_outcome") or "none"), "",
           "**Before you merge / deploy:**", "\n".join("- " + c for c in checklist) or "nothing", "",
           "**Token usage:**", usage, "",
           "**Orchestrator-side artifacts (for reference/audit):**",
           "- PRD: `runs/{0}/prd.md`\n- Plan + Interface Contract: `runs/{0}/plan.md`\n- Full report: "
           "`runs/{0}/report.md`\n- Verify details + logs: `runs/{0}/verify.md`, `runs/{0}/verify-logs/`\n"
           "- Render review + screenshots: `runs/{0}/render-review.md`, `runs/{0}/render/`\n- ADRs as written "
           "(before commit): `runs/{0}/adr/`\n- Token usage: `runs/{0}/usage.md` (re-measure any time: "
           "`python3 .claude/scripts/run-usage.py {0}`)".format(r.id)]
    followup = ("Before showing the message: if this run surfaced a gotcha worth remembering, route it as "
                "run-orchestration.md 'Stage 7: gotchas' says (target-repo rule → commit `docs(rules): …` with "
                "body `Run: {id}` on {br}; pipeline gotcha → memory.md `## Pipeline gotchas`). Skip if nothing "
                "new was learned. If you made a docs(rules) commit, refresh the commit list with "
                "`git -C \"{repo}\" log --oneline {orig}..{br}`.").format(id=r.id, br=br, repo=repo, orig=orig)
    inst = r.stop("\n".join(msg), "done")
    inst["followup"] = followup
    return inst


def merged(repo, ref, orig):
    code, _ = git(repo, "merge-base", "--is-ancestor", ref, orig)
    return code == 0


def render_line(r, render):
    if render == "PASS":
        return "`PASS` (notes: {}), see `runs/{}/render-review.md`".format(len(r.ctx.get("render_notes", [])), r.id)
    if render.startswith("SKIPPED"):
        why = ""
        none_reason = render_none_reason(r)
        if none_reason and none_reason not in render:
            why += " Architect declared no render targets: {}.".format(none_reason)
        if r.ctx.get("rs_failed"):
            why += " Render setup failed: {}.".format(r.ctx["rs_failed"])
        return "`{}`. The pages were **not** looked at; this is not a pass.{}".format(render, why)
    return "`{}`. The gap remains.".format(render)


def handoff_checklist(r, vmd, ref, orig):
    items = []
    mig = md_section(vmd, "## Production follow-up: database migrations")
    if mig:
        items.append("Apply the new database migrations to production as part of the deploy:\n  " +
                     "\n  ".join(l for l in mig.splitlines() if l.startswith("- ")))
    pre = md_section(vmd, "## Pre-existing failures")
    if pre:
        items.append("Pre-existing failures (also fail on the base branch):\n  " +
                     "\n  ".join(l for l in pre.splitlines() if l.startswith("- ")))
    if r.ctx.get("accepted_unverified"):
        items.append("Not verified (accepted by you): " + ", ".join(r.ctx["accepted_unverified"]))
    plan = read(r.path("plan.md"))
    ver = md_section(plan, "## Verification")
    m = re.search(r"manual[^\n]*\n((?:\s*[-*] .*\n?)+)", ver, re.I)
    if m:
        items += ["Manual check: " + l.strip()[2:] for l in m.group(1).splitlines() if l.strip()]
    items += ["Render note: " + n for n in r.ctx.get("render_notes", [])]
    items += ["Update " + s for s in r.ctx.get("stale_docs", [])]
    if (r.ctx.get("adr_outcome") or "").startswith("NOT WRITTEN"):
        items.append("ADRs were NOT WRITTEN: " + r.ctx["adr_outcome"])
    if ref and not merged(r.repo, ref, orig):
        pre_req = "; ".join(r.ctx.get("rs_prereq") or [])
        items.append("Merge `{}` (render review config) into `{}` as its own PR, before or with this one. Until "
                     "then, later runs ask to use it.{}".format(ref, orig, " Prerequisites: " + pre_req if pre_req else ""))
    return items


def add_run_history(r, line):
    p = os.path.join(ROOT, "memory.md")
    md = read(p)
    if "## Run history" in md:
        md = md.replace("## Run history\n", "## Run history\n" + line + "\n", 1)
    else:
        md = md.rstrip("\n") + "\n\n## Run history\n" + line + "\n"
    write(p, md)


# ── dispatch tables ───────────────────────────────────────────────────────────

PHASES = {
    "product": ph_product, "architect": ph_architect, "architect.check": ph_architect_check,
    "sizing": ph_sizing, "render-setup": ph_render_setup, "render-setup.recheck": ph_render_recheck,
    "rs.start": ph_rs_start, "rs.agent": ph_rs_agent, "rs.apply": ph_rs_apply, "rs.smoke": ph_rs_smoke,
    "tester.enter": ph_tester_enter, "tester": ph_tester, "tester.paths": ph_tester_paths,
    "tester.commit": ph_tester_commit, "4a": ph_4a, "4b": ph_4b,
    "coder.enter": ph_coder_enter, "coder": ph_coder, "coder.slice": ph_coder_slice, "coder.done": ph_coder_done,
    "5b": ph_5b, "sandbox": ph_sandbox, "verify": ph_verify, "render": ph_render,
    "adr": ph_adr, "adr.retry": ph_adr_retry, "done": ph_done,
}
RESULTS = {"product": res_product, "architect": res_architect, "render-setup": res_render_setup,
           "tester": res_tester, "test-reviewer": res_test_reviewer, "coder": res_coder,
           "render-reviewer": res_render_reviewer, "adr": res_adr}
ANSWERS = {"questions": ans_questions, "render-decision": ans_render_decision, "rs-blocked": ans_rs_blocked,
           "rs-branch": ans_rs_branch, "verify-accept": ans_verify_accept, "render-accept": ans_render_accept}


# ── commands ──────────────────────────────────────────────────────────────────

def out(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def default_branch(repo):
    c, o = git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if c == 0 and o.strip():
        return o.strip().split("/", 1)[-1]
    c, _ = git(repo, "rev-parse", "--verify", "--quiet", "main")
    return "main" if c == 0 else "master"


def cmd_start(a):
    task = (a.task or "").strip()
    if not task:
        return {"action": "ask", "id": "task", "question": "What do you want to build?",
                "then": "re-run orch.py start with --task \"<their answer>\""}
    repo = os.path.abspath(os.path.expanduser(a.repo or os.getcwd()))
    if not os.path.isdir(repo) or not (os.path.exists(os.path.join(repo, "package.json")) or
                                       os.path.exists(os.path.join(repo, "pyproject.toml"))):
        return {"action": "stop", "status": "failed",
                "message": "{} doesn't exist or has no package.json / pyproject.toml.".format(repo)}
    _, dirty = git(repo, "status", "--porcelain")
    if dirty.strip():
        return {"action": "stop", "status": "failed",
                "message": "{} has uncommitted changes. This pipeline commits directly to a new branch in your repo "
                           "as it works, so it needs a clean tree to start from. Commit or stash your changes, then "
                           "re-run.".format(repo)}
    _, current = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    default = default_branch(repo)
    _, ahead = git(repo, "log", "--oneline", "{}..{}".format(default, current))
    ahead = ahead.strip().splitlines()[:10] if current != default else []
    if (current.startswith("orchestrator/") or ahead) and a.base != "continue":
        if a.base == "switch":
            c, o = git(repo, "checkout", default)
            if c != 0:
                return {"action": "stop", "status": "failed", "message": "git checkout {} failed:\n{}".format(default, o)}
        else:
            q = ("{} is on `{}`, not `{}`. This run would be built on top of these {} unmerged commits:\n{}\n"
                 "Continue from `{}`, or should I switch to `{}` first?").format(
                repo, current, default, len(ahead), "\n".join(ahead), current, default)
            if a.unattended:
                return {"action": "stop", "status": "failed", "message": q}
            base_args = '--repo "{}" --task "{}"{}'.format(repo, task.replace('"', '\\"'),
                                                           " --size " + a.size if a.size else "")
            return {"action": "ask", "id": "base", "question": q,
                    "options": ["Continue from `{}`".format(current), "Switch to `{}` first".format(default)],
                    "then": "python3 .claude/scripts/orch.py start {} --base continue|switch (by the answer)".format(base_args)}
    run_id = "run_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    r = Run(run_id)
    if os.path.exists(r.dir):
        raise UsageError("runs/{} already exists; wait a second and retry".format(run_id))
    _, original = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    branch = "orchestrator/" + run_id
    c, o = git(repo, "checkout", "-B", branch)
    if c != 0:
        return {"action": "stop", "status": "failed", "message": "git checkout -B {} failed:\n{}".format(branch, o)}
    os.makedirs(r.path("archive"), exist_ok=True)
    ts = now_iso()
    r.st = {"run_id": run_id, "task": task, "title": task_title(task), "repo": repo, "original_branch": original, "target_branch": branch,
            "step": "init", "status": "running", "timestamp": ts, "last_artifact": "", "pause_reason": "",
            "retry_count": 0, "verify_retries": 0, "render_retries": 0, "render_setup_retries": 0,
            "coder_slices_done": 0, "architect_retries": 0, "size": "pending",
            "size_override": {"small": "small", "full": "full"}.get(a.size or "", ""), "size_reasons": [],
            "render_config_ref": None, "unattended": bool(a.unattended),
            "phase": "product", "pending": None, "ctx": {}}
    write(r.path("task.md"), task + "\n")
    full = "" if task == r.title else " (full text: `runs/{}/task.md`)".format(run_id)
    write(r.path("report.md"), "# Run Report: {}\n\n**Task:** {}{}\n**Repo:** {}\n**Branch:** {} (from {})\n"
                               "**Started:** {}\n\n---\n\n## Stage log\n".format(run_id, r.title, full, repo, branch, original, ts))
    r.save()
    code, dout = r.call("repo-digest.sh", repo, run_id, task)
    r.long_ran = False
    r.report("[setup] run started on {} from {}".format(branch, original))
    r.say("Run `{}` started on branch `{}` in {}. Repo digest ready. Starting pipeline...".format(run_id, branch, repo))
    if code != 0:
        r.save()
        return r.emit(r.stop("repo-digest.sh failed:\n{}".format(dout), "failed", "digest-failed"))
    if "WARNING" in dout and "large" in dout:
        r.save()
        return r.emit(r.do("The repo digest is large ({}). Trim the file list section of runs/{}/repo-digest.md, "
                           "then continue.".format(dout.strip().splitlines()[-1], run_id), "product"))
    r.save()
    return r.advance()


RESUME_ARCHIVE = {
    "product": [("prd.md", "prd_before_resume_{ts}.md", False)],
    "architect": [("plan.md", "plan_before_resume_{ts}.md", False),
                  ("contract.json", "contract_before_resume_{ts}.json", False)],
    "tester": [("tests", "tests_before_resume_{ts}", False)],
    "coder": [("code", "code_before_resume_{ts}", False)],
    "render": [("render-review.md", "render-review_before_resume_{ts}.md", True)],
    "adr": [("adr", "adr_before_resume_{ts}", True)],
}
RESUME_PHASE = {"product": "product", "architect": "architect", "render-setup": "render-setup",
                "tester": "tester.enter", "test-reviewer": "4a", "coder": "coder", "sandbox": "sandbox",
                "verify": "verify", "render": "render", "adr": "adr"}


def status_lines(r):
    st = r.st
    return ["Run:     {}".format(st["run_id"]), "Task:    {}".format(r.title), "Repo:    {}".format(st["repo"]),
            "Branch:  {} (from {})".format(st["target_branch"], st["original_branch"]),
            "Step:    {}".format(st["step"]), "Status:  {}".format(st["status"]),
            "Paused:  {}".format(st.get("pause_reason") or ""),
            "Retries: {} (render: {})".format(st["retry_count"], st["render_retries"]),
            "Size:    {} ({})".format(st["size"], "; ".join(st.get("size_reasons") or []))]


def cmd_resume(a):
    r = Run(a.run_id).load()
    st = r.st
    frm = a.from_step or st.get("step")
    if frm not in RESUME_PHASE:
        if frm == "done":
            return r.emit({"action": "stop", "status": "done", "message": "Run {} is already done.".format(r.id)})
        return r.emit({"action": "stop", "status": st.get("status"),
                       "message": "Valid --from values are: {}.".format(", ".join(STEPS))})
    r.say("\n".join(status_lines(r)))
    r.say("Last report lines:\n" + "\n".join(read(r.path("report.md")).splitlines()[-20:]))
    repo = r.repo
    _, dirty = git(repo, "status", "--porcelain")
    _, cur = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if dirty.strip():
        return r.emit({"action": "stop", "status": st.get("status"),
                       "message": "{} has uncommitted changes on `{}`. This pipeline commits directly to `{}` as it "
                                  "works, so it needs a clean tree before resuming. Commit or stash your changes, then "
                                  "resume again.\n\n{}".format(repo, cur, st["target_branch"], dirty)})
    if cur != st["target_branch"]:
        c, o = git(repo, "checkout", st["target_branch"])
        if c != 0:
            return r.emit({"action": "stop", "status": st.get("status"), "message": "git checkout failed:\n" + o})
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    for src, dst, move in RESUME_ARCHIVE.get(frm, []):
        r.archive(src, dst.format(ts=ts), move=move)
    prev_status, prev_reason = st.get("status"), st.get("pause_reason") or ""
    r.set(step=frm, status="running", pause_reason="")
    st["pending"] = None
    ctx = r.ctx
    for k in ("deferred", "coder_mode", "timeout_review", "verify_coder", "lockfile_tried"):
        ctx.pop(k, None)
    r.report("---\n[RESUMED] step: {} at {}".format(frm, now_iso()))
    st["phase"] = RESUME_PHASE[frm]
    trouble = prev_status == "failed" or (prev_status == "paused" and prev_reason not in
                                          ("awaiting_human_input", "awaiting_render_decision"))
    needs_review = False
    if trouble and frm in ("tester", "test-reviewer", "coder"):
        needs_review = r.escalate(frm, "resumed from {} after {}".format(frm, prev_reason or prev_status))
    if frm == "architect":
        st["architect_retries"] = 0
    if frm in ("tester", "test-reviewer"):
        fb = ctx.get("last_feedback", {}).get("tester")
        if fb and trouble:
            ctx.setdefault("feedback", {})["tester"] = fb
    if frm == "coder":
        unfinished = False
        if not ctx.get("code_committed") and os.path.exists(r.path("coder-slices.json")):
            data = json.loads(read(r.path("coder-slices.json")))
            n = len(data.get("slices", []))
            if n > 1 and st["coder_slices_done"] < n:
                ctx["slices"] = {"n": n, "labels": [s.get("label", "") for s in data["slices"]]}
                unfinished = True
        if not unfinished:
            st["retry_count"] = max(st["retry_count"], 1)
            fb = ctx.get("last_feedback", {}).get("coder") or latest_section(r, "## Test sandbox run").strip()
            if fb:
                ctx.setdefault("feedback", {})["coder"] = fb
            ctx["fix_reason"] = "resume fix pass"
            ctx["render_pass"] = False
        if needs_review:
            t = {"phase": "coder"}
            ctx["deferred"] = t
            ctx["review_skipped"] = False
            ctx["review_note"] = ESCALATION_NOTE
            st["phase"] = "4b"
    if frm == "render-setup":
        st["render_setup_retries"] = 0
    if prev_reason == "awaiting_human_input" and os.path.exists(r.path("questions-{}.md".format(frm))):
        af = r.path("answers-{}.md".format(frm))
        if os.path.exists(af):
            ctx.setdefault("answers", {})[frm] = r.rel("answers-{}.md".format(frm))
        else:
            inst = r.ask("questions:" + frm, "The {} stage asked these questions:\n\n{}".format(
                frm, read(r.path("questions-{}.md".format(frm)))), pause="awaiting_human_input")
            r.save()
            return r.emit(inst)
    r.save()
    return r.advance()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="orch.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("--repo")
    s.add_argument("--task", default="")
    s.add_argument("--size", choices=["small", "full"])
    s.add_argument("--unattended", action="store_true")
    s.add_argument("--base", choices=["continue", "switch"])
    n = sub.add_parser("next")
    n.add_argument("run_id")
    rs = sub.add_parser("result")
    rs.add_argument("run_id")
    rs.add_argument("stage")
    rs.add_argument("file")
    rm = sub.add_parser("resume")
    rm.add_argument("run_id")
    rm.add_argument("--from", dest="from_step")
    stt = sub.add_parser("status")
    stt.add_argument("run_id")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "start":
            out(cmd_start(a))
        elif a.cmd == "next":
            out(Run(a.run_id).load().advance())
        elif a.cmd == "result":
            r = Run(a.run_id).load()
            r.handle_result(a.stage, a.file)
            out(r.advance())
        elif a.cmd == "resume":
            out(cmd_resume(a))
        elif a.cmd == "status":
            r = Run(a.run_id).load(write_import=False)
            print("\n".join(status_lines(r)))
            print("\n".join(read(r.path("report.md")).splitlines()[-20:]))
        return 0
    except UsageError as e:
        print("orch.py: " + str(e), file=sys.stderr)
        return 2
    except OrchError as e:
        print("orch.py: " + str(e), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())

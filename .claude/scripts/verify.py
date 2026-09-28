#!/usr/bin/env python3
"""
verify.py: the pipeline's Verify stage (Stage 6b).

Checks the *committed* orchestrator branch the way CI and the deploy will, not
just the unit tests the sandbox ran:

  1. fresh git worktree of the run's branch (exactly what would be merged)
  2. the target repo's install command (default: frozen lockfile, like CI and Render)
  3. every step in {REPO}/.claude/verify.json (lint, typecheck, builds, all tests, ...)
  4. steps that need a database get a throwaway Postgres (Docker, or a URL the
     user provides in VERIFY_DATABASE_URL), never the real one
  5. the task-specific commands from the plan's ```verify block
  6. any failing step is re-run on the original branch, so failures that already
     existed before this run are reported as PRE-EXISTING, not blamed on the Coder
  7. optional real-data check: if the branch adds migrations and Neon credentials
     are available, apply them to a temporary Neon branch (a copy of real data),
     then delete it
  8. writes runs/{RUN_ID}/verify.md (+ per-step logs) and appends a summary to report.md

Usage:
  verify.py <repo_path> <run_id> [--step-timeout SECONDS]

Exit codes:
  0 PASS        every step passed (pre-existing failures are allowed but reported)
  1 FAIL        at least one step failed because of this run's changes
  3 ERROR       infrastructure problem (worktree, Docker start, Neon API); not a code verdict
  4 INCOMPLETE  a required step could not run (e.g. no Docker for database steps).
                Nothing failed, but the run must not be reported as verified.

Written for Python 3.8+ (macOS Command Line Tools ship 3.9).
"""

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

ORCH_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

PASS, FAIL, TIMEOUT, SKIPPED, PREEXISTING = "PASS", "FAIL", "TIMEOUT", "SKIPPED", "PRE-EXISTING"

# Env vars that could point a step at a real database or cloud account. They are
# removed from every step's environment; only the throwaway URL is set.
SCRUB_PREFIXES = ("DATABASE_URL", "DIRECT_URL", "SHADOW_DATABASE_URL", "POSTGRES_", "PG", "NEON_")
UNREACHABLE_DB = "postgresql://verify:verify@127.0.0.1:9/verify_no_database_provisioned"


class InfraError(Exception):
    pass


# ── helpers ──────────────────────────────────────────────────────────────────

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sh(cmd, cwd=None, env=None, timeout=None, log_path=None):
    """Run a shell command in its own process group. Returns (exit_code, output, seconds).
    exit_code is None on timeout."""
    start = time.time()
    proc = subprocess.Popen(
        ["bash", "-c", cmd], cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        out, _ = proc.communicate()
        code = None
    text = out.decode("utf-8", "replace") if out else ""
    if log_path:
        with open(log_path, "w") as f:
            f.write("$ " + cmd + "\n\n" + text)
    return code, text, time.time() - start


def tail(text, n=60):
    lines = text.rstrip().splitlines()
    return "\n".join(lines[-n:])


ERROR_LINE = re.compile(r"error|fail|✕|✗|×|expected|TS\d{4}|cannot find|not assignable|unexpected", re.I)


def error_lines(text, worktree):
    """Normalized error-looking lines, used to tell 'the same failure as on the original
    branch' from 'new errors on top of a failure that already existed'."""
    out = set()
    for line in text.splitlines():
        if not ERROR_LINE.search(line):
            continue
        if worktree:
            line = line.replace(worktree, "<WT>")
        line = re.sub(r"\S*orchestrator-verify-[\w-]+", "<WT>", line)  # any temp dir (macOS: /var/folders/...)
        line = re.sub(r"\(?\d+(\.\d+)?\s?(ms|s)\)?", "", line)          # durations
        line = re.sub(r"\d{4}-\d\d-\d\dT[\d:.]+Z?", "", line)           # timestamps
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            out.add(line)
    return out


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "step"


def read_state(run_dir):
    state = {}
    path = os.path.join(run_dir, "state.md")
    if os.path.exists(path):
        for line in open(path):
            if ":" in line:
                k, v = line.split(":", 1)
                state[k.strip()] = v.strip()
    return state


def plan_verify_commands(run_dir):
    """Commands from the plan's ```verify fenced block (one per line, # comments allowed)."""
    path = os.path.join(run_dir, "plan.md")
    if not os.path.exists(path):
        return []
    text = open(path).read()
    cmds = []
    for block in re.findall(r"```verify[^\n]*\n(.*?)```", text, re.S):
        for line in block.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                cmds.append(line)
    return cmds


def default_config(worktree):
    """Used when the target repo has no .claude/verify.json."""
    if os.path.exists(os.path.join(worktree, "pnpm-workspace.yaml")):
        run = "pnpm -r --if-present run {}"
        install = "pnpm install --frozen-lockfile"
    elif os.path.exists(os.path.join(worktree, "pnpm-lock.yaml")):
        run = "pnpm run --if-present {}"
        install = "pnpm install --frozen-lockfile"
    else:
        run = "npm run --if-present {}"
        install = "npm ci"
    return {
        "generic": True,
        "install": install,
        "steps": [{"name": s, "run": run.format(s)} for s in ("lint", "typecheck", "build", "test")],
    }


def base_env(config, db_url):
    env = {k: v for k, v in os.environ.items() if not k.startswith(SCRUB_PREFIXES)}
    env["CI"] = "true"
    env["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"
    for k, v in (config.get("env") or {}).items():
        env[k] = str(v)
    db_var = (config.get("database") or {}).get("env", "DATABASE_URL")
    env[db_var] = db_url or UNREACHABLE_DB
    return env


def git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", repo] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if check and r.returncode != 0:
        raise InfraError("git " + " ".join(args) + " failed:\n" + r.stdout.decode("utf-8", "replace"))
    return r.stdout.decode("utf-8", "replace").strip()


# ── throwaway database ───────────────────────────────────────────────────────

class Database:
    """A throwaway Postgres. Docker if available, else VERIFY_DATABASE_URL if the
    user provided one (a local database they're happy to have wiped), else None."""

    def __init__(self, config, label):
        self.config = config.get("database") or {}
        self.label = label
        self.container = None
        self.url = None
        self.unavailable_reason = None

    def start(self):
        provided = os.environ.get("VERIFY_DATABASE_URL")
        if provided:
            self.url = provided
            return self
        if not shutil.which("docker"):
            self.unavailable_reason = "Docker is not installed, and VERIFY_DATABASE_URL is not set"
            return self
        code, _, _ = sh("docker info", timeout=20)
        if code != 0:
            self.unavailable_reason = "Docker is installed but not running (start Docker Desktop), and VERIFY_DATABASE_URL is not set"
            return self
        image = self.config.get("image", "postgres:16-alpine")
        name = "orch-verify-" + slug(self.label)
        sh("docker rm -f {}".format(name), timeout=30)
        cmd = ("docker run -d --rm --name {n} -e POSTGRES_USER=verify -e POSTGRES_PASSWORD=verify "
               "-e POSTGRES_DB=verify -p 127.0.0.1::5432 {img}").format(n=name, img=image)
        code, out, _ = sh(cmd, timeout=300)
        if code != 0:
            raise InfraError("could not start Postgres container:\n" + tail(out, 20))
        self.container = name
        code, out, _ = sh("docker port {} 5432/tcp".format(name), timeout=20)
        m = re.search(r":(\d+)\s*$", out.strip().splitlines()[0] if out.strip() else "")
        if code != 0 or not m:
            raise InfraError("could not read Postgres container port:\n" + out)
        port = m.group(1)
        deadline = time.time() + 60
        while time.time() < deadline:
            c, _, _ = sh("docker exec {} pg_isready -U verify -d verify".format(name), timeout=10)
            if c == 0:
                break
            time.sleep(1)
        else:
            raise InfraError("Postgres container did not become ready within 60s")
        self.url = "postgresql://verify:verify@127.0.0.1:{}/verify".format(port)
        return self

    def stop(self):
        if self.container:
            sh("docker rm -f {}".format(self.container), timeout=60)
            self.container = None


# ── worktree ─────────────────────────────────────────────────────────────────

class Worktree:
    def __init__(self, repo, ref, label):
        self.repo = repo
        self.ref = ref
        self.path = os.path.join(tempfile.gettempdir(), "orchestrator-verify-" + slug(label))

    def __enter__(self):
        if os.path.exists(self.path):
            git(self.repo, "worktree", "remove", "--force", self.path, check=False)
            shutil.rmtree(self.path, ignore_errors=True)
        git(self.repo, "worktree", "add", "--detach", self.path, self.ref)
        return self

    def __exit__(self, *exc):
        git(self.repo, "worktree", "remove", "--force", self.path, check=False)
        shutil.rmtree(self.path, ignore_errors=True)
        git(self.repo, "worktree", "prune", check=False)


# ── running a step list ──────────────────────────────────────────────────────

def run_steps(worktree, config, steps, db, log_dir, prefix, step_timeout):
    """Run install + steps in a worktree. Returns list of result dicts."""
    env = base_env(config, db.url if db else None)
    results = []

    install = config.get("install")
    if install:
        log = os.path.join(log_dir, "{}00-install.log".format(prefix))
        code, out, secs = sh(install, cwd=worktree, env=env, timeout=config.get("installTimeout", 900), log_path=log)
        status = PASS if code == 0 else (TIMEOUT if code is None else FAIL)
        hint = ""
        if status == FAIL and re.search(r"OUTDATED_LOCKFILE|frozen-lockfile|lockfile.*(not up to date|needs updates)", out, re.I):
            hint = "Lockfile is out of date with package.json. Update it (`pnpm install --lockfile-only`) and commit it; CI and the Render build both use a frozen lockfile."
        results.append({"name": "install", "cmd": install, "status": status, "secs": secs,
                        "log": log, "out": out, "hint": hint, "index": 0})
        if status != PASS:
            # Nothing else can run meaningfully without dependencies.
            for i, s in enumerate(steps, 1):
                results.append({"name": s["name"], "cmd": s["run"], "status": SKIPPED, "secs": 0,
                                "log": None, "out": "", "reason": "install failed", "index": i})
            return results

    for i, step in enumerate(steps, 1):
        needs_db = step.get("needs") == "database"
        if needs_db and (db is None or db.url is None):
            reason = db.unavailable_reason if db else "no database configured"
            results.append({"name": step["name"], "cmd": step["run"], "status": SKIPPED, "secs": 0,
                            "log": None, "out": "", "reason": reason, "index": i,
                            "required": step.get("required", True)})
            continue
        log = os.path.join(log_dir, "{}{:02d}-{}.log".format(prefix, i, slug(step["name"])))
        code, out, secs = sh(step["run"], cwd=worktree, env=env,
                             timeout=step.get("timeout", step_timeout), log_path=log)
        status = PASS if code == 0 else (TIMEOUT if code is None else FAIL)
        results.append({"name": step["name"], "cmd": step["run"], "status": status, "secs": secs,
                        "log": log, "out": out, "index": i, "hint": step.get("hint", "")})
    return results


# ── Neon real-data migration check ───────────────────────────────────────────

def load_neon_env():
    """NEON_API_KEY / NEON_PROJECT_ID from the environment, or from
    ~/.config/ai-orchestrator/neon.env (KEY=value lines)."""
    vals = {k: os.environ.get(k) for k in ("NEON_API_KEY", "NEON_PROJECT_ID", "NEON_PARENT_BRANCH")}
    path = os.path.expanduser("~/.config/ai-orchestrator/neon.env")
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k in vals and not vals[k]:
                    vals[k] = v
    return vals


def neon_real_data_check(repo, worktree, config, new_migrations, run_id, log_dir):
    """Returns a result dict, or None if not configured / not applicable."""
    rd = config.get("realDataCheck") or {}
    if rd.get("provider") != "neon":
        return None
    name = "migrations apply to real data (temporary Neon branch)"
    if not new_migrations:
        return {"name": name, "status": SKIPPED, "reason": "this run adds no migrations", "secs": 0,
                "log": None, "out": "", "required": False, "cmd": ""}
    neon = load_neon_env()
    if not (neon["NEON_API_KEY"] and neon["NEON_PROJECT_ID"]):
        return {"name": name, "status": SKIPPED, "secs": 0, "log": None, "out": "", "cmd": "",
                "required": False,
                "reason": "NEON_API_KEY / NEON_PROJECT_ID not set (env or ~/.config/ai-orchestrator/neon.env)"}

    neonctl = "neonctl" if shutil.which("neonctl") else "npx -y neonctl"
    branch = "verify-" + slug(run_id)
    proj = shlex.quote(neon["NEON_PROJECT_ID"])
    env = {k: v for k, v in os.environ.items() if not k.startswith(SCRUB_PREFIXES)}
    env["NEON_API_KEY"] = neon["NEON_API_KEY"]
    parent = " --parent " + shlex.quote(neon["NEON_PARENT_BRANCH"]) if neon.get("NEON_PARENT_BRANCH") else ""
    log = os.path.join(log_dir, "neon-real-data.log")
    transcript = []
    start = time.time()

    def run(cmd, timeout=180, extra_env=None):
        e = dict(env)
        if extra_env:
            e.update(extra_env)
        code, out, _ = sh(cmd, cwd=worktree, env=e, timeout=timeout)
        transcript.append("$ " + cmd.replace(neon["NEON_API_KEY"], "***") + "\n" + out)
        return code, out

    created = False
    try:
        sh("{} branches delete {} --project-id {}".format(neonctl, branch, proj), env=env, timeout=120)
        code, out = run("{} branches create --project-id {} --name {}{} --output json".format(neonctl, proj, branch, parent))
        if code != 0:
            raise InfraError("Neon: could not create branch {}:\n{}".format(branch, tail(out, 20)))
        created = True
        code, out = run("{} connection-string {} --project-id {}".format(neonctl, branch, proj))
        urls = [l.strip() for l in out.splitlines() if l.strip().startswith("postgres")]
        if code != 0 or not urls:
            raise InfraError("Neon: could not get a connection string for branch " + branch)
        url = urls[-1]
        db_var = (config.get("database") or {}).get("env", "DATABASE_URL")
        wd = rd.get("workdir", ".")
        schema = rd.get("schema", "prisma/schema.prisma")
        dbenv = {db_var: url}

        _, status_out = run("cd {} && pnpm exec prisma migrate status".format(shlex.quote(wd)), extra_env=dbenv)
        pending = re.findall(r"^\s*(\d{14}_[\w-]+)\s*$", status_out, re.M)

        code, deploy_out = run("cd {} && pnpm exec prisma migrate deploy".format(shlex.quote(wd)), extra_env=dbenv, timeout=600)
        if code != 0:
            status, detail = FAIL, "`prisma migrate deploy` failed against a copy of the real data. The migration would fail in production too."
        else:
            code, _ = run("cd {} && pnpm exec prisma migrate diff --from-url \"${}\" --to-schema-datamodel {} --exit-code"
                          .format(shlex.quote(wd), db_var, shlex.quote(schema)), extra_env=dbenv)
            if code == 0:
                status, detail = PASS, "Applied cleanly to a copy of the real data; schema matches migrations."
            elif code == 2:
                status, detail = FAIL, "After applying the migrations, the database still differs from schema.prisma: a schema change has no migration."
            else:
                status, detail = FAIL, "`prisma migrate diff` errored."
        return {"name": name, "status": status, "secs": time.time() - start, "log": log,
                "out": "\n".join(transcript), "detail": detail, "pending_in_prod": pending,
                "required": True, "cmd": "neon branch " + branch}
    finally:
        if created:
            code, out = run("{} branches delete {} --project-id {}".format(neonctl, branch, proj), timeout=120)
            if code != 0:
                transcript.append("WARNING: could not delete Neon branch {}; delete it in the Neon console.".format(branch))
        with open(log, "w") as f:
            f.write("\n\n".join(transcript))


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("run_id")
    ap.add_argument("--step-timeout", type=int, default=600)
    args = ap.parse_args()

    repo = os.path.abspath(args.repo)
    run_dir = os.path.join(ORCH_ROOT, "runs", args.run_id)
    log_dir = os.path.join(run_dir, "verify-logs")
    shutil.rmtree(log_dir, ignore_errors=True)
    os.makedirs(log_dir)
    state = read_state(run_dir)
    branch = state.get("target_branch") or git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    original = state.get("original_branch") or "main"
    started = now_iso()

    results, baseline_note, real_data, infra_error = [], "", None, None
    config_note = ""
    config, generic = {}, False
    new_migrations = []

    dbs = []
    try:
        with Worktree(repo, branch, args.run_id) as wt:
            # Config comes from the ORIGINAL branch, so a run can't weaken its own checks
            # by editing verify.json. If only the run's branch has it (first introduction),
            # use that and say so.
            config_note = ""
            raw = git(repo, "show", "{}:.claude/verify.json".format(original), check=False)
            if raw.startswith("{"):
                config = json.loads(raw)
            elif os.path.exists(os.path.join(wt.path, ".claude", "verify.json")):
                config = json.load(open(os.path.join(wt.path, ".claude", "verify.json")))
                config_note = "`.claude/verify.json` is new on this branch (not on `{}`), so this run's own version was used.".format(original)
            else:
                config = default_config(wt.path)
                generic = True
            steps = list(config.get("steps", []))
            for cmd in plan_verify_commands(run_dir):
                steps.append({"name": "plan: " + cmd[:60], "run": cmd, "plan": True})

            mig_dir = (config.get("migrations") or {}).get("dir")
            if mig_dir:
                diff = git(repo, "diff", "--name-only", "--diff-filter=A", "{}...{}".format(original, branch), "--", mig_dir, check=False)
                new_migrations = sorted({p[len(mig_dir):].strip("/").split("/")[0] for p in diff.splitlines() if p.strip()})
                new_migrations = [m for m in new_migrations if m and not m.endswith(".toml")]

            db = None
            if any(s.get("needs") == "database" for s in steps):
                db = Database(config, args.run_id).start()
                dbs.append(db)
            results = run_steps(wt.path, config, steps, db, log_dir, "", args.step_timeout)

            try:
                real_data = neon_real_data_check(repo, wt.path, config, new_migrations, args.run_id, log_dir)
            except InfraError as e:
                real_data = {"name": "migrations apply to real data (temporary Neon branch)", "status": SKIPPED,
                             "secs": 0, "log": os.path.join(log_dir, "neon-real-data.log"), "out": "", "cmd": "",
                             "required": False, "reason": "Neon check could not run: " + str(e).splitlines()[0]}

        # Re-run failing steps on the original branch to separate pre-existing failures.
        failed_idx = [r["index"] for r in results if r["status"] in (FAIL, TIMEOUT) and r["name"] != "install"]
        if failed_idx:
            upto = max(failed_idx)
            base_steps = [s for s in steps[:upto] if not s.get("plan")]
            if base_steps:
                with Worktree(repo, original, args.run_id + "-baseline") as bwt:
                    bdb = None
                    if any(s.get("needs") == "database" for s in base_steps):
                        bdb = Database(config, args.run_id + "-baseline").start()
                        dbs.append(bdb)
                    base = run_steps(bwt.path, config, base_steps, bdb, log_dir, "baseline-", args.step_timeout)
                    base_path = bwt.path
                by_index = {r["index"]: r for r in base}
                for r in results:
                    b = by_index.get(r["index"])
                    if r["index"] not in failed_idx or not b or b["status"] not in (FAIL, TIMEOUT):
                        continue
                    new_errors = error_lines(r["out"], "") - error_lines(b["out"], base_path)
                    if new_errors:
                        # Fails on the original branch too, but this run adds errors of its own.
                        r["detail"] = ("Also fails on `{}`, but this run adds {} new error line(s) "
                                       "not present there:\n\n".format(original, len(new_errors)) +
                                       "\n".join("- " + l for l in sorted(new_errors)[:30]))
                    else:
                        r["status"] = PREEXISTING
                baseline_note = "Failing steps were re-run on `{}`; those that fail there too are marked PRE-EXISTING.".format(original)
    except InfraError as e:
        infra_error = str(e)
    finally:
        for d in dbs:
            d.stop()

    # ── verdict ──
    all_results = results + ([real_data] if real_data else [])
    failed = [r for r in all_results if r["status"] in (FAIL, TIMEOUT)]
    missing = [r for r in all_results if r["status"] == SKIPPED and r.get("required", True) and r.get("reason") != "install failed"]
    if infra_error:
        verdict, code = "ERROR", 3
    elif failed:
        verdict, code = "FAIL", 1
    elif missing:
        verdict, code = "INCOMPLETE", 4
    else:
        verdict, code = "PASS", 0

    # ── verify.md ──
    rel = lambda p: os.path.relpath(p, run_dir) if p else ""
    md = ["# Verify: {}".format(args.run_id), "",
          "**Branch:** `{}` (compared with `{}`)  ".format(branch, original),
          "**Started:** {}  ".format(started),
          "**Verdict:** {}".format(verdict), ""]
    if config_note:
        md += ["> " + config_note, ""]
    if generic:
        md += ["> No `.claude/verify.json` in the target repo, so generic lint/typecheck/build/test defaults were used.", ""]
    if infra_error:
        md += ["## Infrastructure error", "", "```", infra_error, "```", ""]
    md += ["| # | Step | Result | Time | Log |", "|---|------|--------|------|-----|"]
    for r in all_results:
        res = r["status"] + (" ({})".format(r["reason"]) if r.get("reason") else "")
        cell = lambda s: str(s).replace("|", "\\|")
        md.append("| {} | {} | {} | {:.0f}s | {} |".format(r.get("index", "-"), cell(r["name"]), cell(res), r["secs"], rel(r.get("log"))))
    md.append("")
    if baseline_note:
        md += [baseline_note, ""]
    if failed:
        md += ["## Failures caused by this run", ""]
        for r in failed:
            md += ["### {} ({})".format(r["name"], r["status"]), "", "`{}`".format(r["cmd"]), ""]
            if r.get("hint"):
                md += ["**Hint:** " + r["hint"], ""]
            if r.get("detail"):
                md += [r["detail"], ""]
            md += ["```", tail(r["out"]), "```", ""]
    pre = [r for r in all_results if r["status"] == PREEXISTING]
    if pre:
        md += ["## Pre-existing failures (also fail on `{}`; not blocking)".format(original), ""]
        md += ["- {}: `{}`".format(r["name"], r["cmd"]) for r in pre] + [""]
    if missing:
        md += ["## Not verified", "", "These required checks could not run, so this run is **not** verified:", ""]
        md += ["- {}: {}".format(r["name"], r["reason"]) for r in missing] + [""]
    optional_skips = [r for r in all_results if r["status"] == SKIPPED and not r.get("required", True)]
    if optional_skips:
        md += ["## Optional checks skipped", ""] + ["- {}: {}".format(r["name"], r["reason"]) for r in optional_skips] + [""]
    if new_migrations:
        md += ["## Production follow-up: database migrations", "",
               "This branch adds migrations. Deploying the code without applying them breaks the endpoints that use the new schema:", ""]
        md += ["- `{}`".format(m) for m in new_migrations]
        if real_data and real_data.get("pending_in_prod"):
            md += ["", "Pending on the real database right now (from the Neon branch check): " +
                   ", ".join("`{}`".format(p) for p in real_data["pending_in_prod"])]
        md += ["", "Apply with `prisma migrate deploy` against the production database as part of the deploy.", ""]
    md += ["---", "Machine-readable summary:", "", "```json",
           json.dumps({"verdict": verdict, "exit_code": code,
                       "failed": [r["name"] for r in failed],
                       "not_verified": [r["name"] for r in missing],
                       "pre_existing": [r["name"] for r in pre],
                       "new_migrations": new_migrations}, indent=2),
           "```", ""]
    with open(os.path.join(run_dir, "verify.md"), "w") as f:
        f.write("\n".join(md))

    report = os.path.join(run_dir, "report.md")
    if os.path.exists(report):
        with open(report, "a") as f:
            f.write("\n---\n## Verify: {} ({})\n\n".format(verdict, now_iso()))
            f.write("\n".join(l for l in md if l.startswith("|")) + "\n\n")
            f.write("Details: `runs/{}/verify.md`\n".format(args.run_id))

    print("\n".join(md))
    sys.exit(code)


if __name__ == "__main__":
    main()

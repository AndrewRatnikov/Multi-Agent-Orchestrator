#!/usr/bin/env python3
"""
render-config.py: the mechanical half of render-review setup (Stage 2b and /setup-render).

The render review (Stage 6c) needs per-repo config: a `render` section in the target repo's
.claude/verify.json, a .claude/launch.json with the dev-server command, and a sign-in/seed
hook under .claude/render/. render-capture.mjs reads all three from the run's original branch
(or `render_config_ref`), never from the run's own branch, so a run can't edit its own setup.
This script finds out whether that config exists, records a repo's "don't ask again", and
turns what `orch-render-setup` wrote into a commit on a SEPARATE branch cut from the base,
in a throwaway worktree. The user's checkout and the run's branch are never touched.

Usage:
  render-config.py check <run_id> <repo>
      Is render config needed and present for this run?
      exit 0  configured: <ref>          the config ref (render_config_ref or original_branch) has it
      exit 2  not-needed: <why>          no render_targets, and the run changes no UI files (API-only)
      exit 2  no-targets: <reason>       UI files change, no render_targets: the Architect's
                                         render_targets_none_reason (or, for an old contract, its
                                         render_skip_reason / "no reason given"). check-contract
                                         validate already rejects the no-reason case for new runs.
      exit 4  opted-out: <since/why>     the user said "don't ask again" for this repo
      exit 5  pending: <branch> ...      no config on the base, but unmerged local branch(es) have one
      exit 1  missing: <why>             no config anywhere: ask the user
      exit 3  setup error

  render-config.py optout <repo> [--reason TEXT] [--run RUN_ID]
  render-config.py optin <repo>
      Record / clear "don't ask again" in repo-notes/render-optout.json (orchestrator side;
      nothing is written to the target repo).

  render-config.py apply <setup_dir> <repo> --base REF --branch NAME [--contract PATH] [--reuse]
      Validate what orch-render-setup wrote in <setup_dir>, then commit it on branch NAME:
        <setup_dir>/render.json         the `render` section (merged into the base's verify.json;
                                        every other key of verify.json is kept as it is)
        <setup_dir>/files/...           repo-relative files, only .claude/launch.json and
                                        .claude/render/** are allowed
        <setup_dir>/smoke-targets.json  pages that already exist on the base, for the smoke test
      Without --reuse, NAME must not exist yet (cut from REF). With --reuse, NAME must exist and
      the change is committed on top of it (a retry after a failed smoke test).
      exit 0  committed (prints `branch:` and `commit:`; `commit: unchanged` if nothing changed)
      exit 1  violations (one per line; feed them back to orch-render-setup)
      exit 6  NAME already exists and --reuse was not given
      exit 3  setup error (git, worktree)

  render-config.py smoke-prep <setup_dir> <repo> --branch NAME
      Write <setup_dir>/smoke/{state.md,contract.json} so that
        node .claude/scripts/render-capture.mjs <repo> <id> --run-dir <setup_dir>/smoke --only after
      opens the smoke targets on NAME with NAME's own config (it reached a signed-in page = works).
      exit 0 ready, 1 smoke-targets.json invalid, 3 setup error
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ui_files import short_list, ui_files  # noqa: E402

ORCH_ROOT = os.path.dirname(os.path.dirname(HERE))
OPTOUT_FILE = os.path.join(ORCH_ROOT, "repo-notes", "render-optout.json")

ALLOWED_FILE = re.compile(r"^\.claude/(launch\.json|render/.+)$")
SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
KNOWN_RENDER_KEYS = {
    "setup", "fixtures", "viewports", "now", "timezone", "server", "portArgs", "hideSelectors",
    "install", "installTimeoutSec", "readyTimeoutSec", "notes",
}
SEED_EXPORT = re.compile(r"export\s+(async\s+)?function\s+seed\b|export\s+(const|let|var)\s+seed\b|export\s*\{[^}]*\bseed\b")


def die(msg, code=3):
    print(msg, file=sys.stderr)
    sys.exit(code)


def git(repo, *args, check=False):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        die("git {} failed: {}".format(" ".join(args), (r.stderr or r.stdout).strip()))
    return r


def git_show(repo, ref, path):
    r = git(repo, "show", "{}:{}".format(ref, path))
    return r.stdout if r.returncode == 0 else None


def ref_exists(repo, ref):
    return git(repo, "rev-parse", "--verify", "--quiet", ref).returncode == 0


def render_section(repo, ref):
    raw = git_show(repo, ref, ".claude/verify.json")
    if raw is None:
        return None
    try:
        r = json.loads(raw).get("render")
    except Exception:
        return None
    return r if isinstance(r, dict) else None


def read_state(run_dir):
    state = {}
    p = os.path.join(run_dir, "state.md")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            if ":" in line:
                k, v = line.split(":", 1)
                state[k.strip()] = v.strip()
    return state


def repo_key(repo):
    return os.path.realpath(repo)


def load_optouts():
    if not os.path.exists(OPTOUT_FILE):
        return {}
    try:
        return json.load(open(OPTOUT_FILE, encoding="utf-8"))
    except Exception:
        return {}


def save_optouts(data):
    os.makedirs(os.path.dirname(OPTOUT_FILE), exist_ok=True)
    with open(OPTOUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def branches_with_config(repo, exclude):
    out = git(repo, "for-each-ref", "--sort=-committerdate", "--format=%(refname:short)\t%(committerdate:short)\t%(subject)", "refs/heads").stdout
    found = []
    for line in out.splitlines()[:300]:
        name, date, subject = (line.split("\t") + ["", ""])[:3]
        if not name or name in exclude or name.startswith("orchestrator/"):
            continue
        if render_section(repo, name) is not None:
            found.append((name, date, subject))
    return found


# ── check ────────────────────────────────────────────────────────────────────

def cmd_check(run_id, repo):
    run_dir = os.path.join(ORCH_ROOT, "runs", run_id)
    if not os.path.isdir(run_dir):
        die("no such run folder: " + run_dir)
    if not os.path.isdir(repo):
        die("no such repo: " + repo)
    state = read_state(run_dir)
    contract_path = os.path.join(run_dir, "contract.json")
    contract = json.load(open(contract_path, encoding="utf-8")) if os.path.exists(contract_path) else {}
    targets = contract.get("render_targets") or []
    if not targets:
        ui, _ = ui_files(contract, repo, run_dir)
        if not ui:
            print("not-needed: contract.json declares no render_targets and the run changes no UI files")
            return 2
        none_reason = (contract.get("render_targets_none_reason") or "").strip()
        legacy = (contract.get("render_skip_reason") or "").strip()
        why = ("render_targets_none_reason: " + none_reason if none_reason
               else "render_skip_reason (old contract): " + legacy if legacy
               else "no reason given (old contract)")
        print("no-targets: the run changes UI files ({}) but the Architect declared no render targets; "
              "the render review will be SKIPPED. {}".format(short_list(ui), why))
        return 2
    original = state.get("original_branch") or "main"
    ref = state.get("render_config_ref") or original
    if not ref_exists(repo, ref):
        die("unknown git ref: " + ref)
    if render_section(repo, ref) is not None:
        print("configured: " + ref)
        return 0
    opt = load_optouts().get(repo_key(repo))
    if opt:
        print("opted-out: since {} ({}){}".format(opt.get("date", "?"), opt.get("run", "?"),
                                                  ": " + opt["reason"] if opt.get("reason") else ""))
        return 4
    pending = branches_with_config(repo, exclude={ref, state.get("target_branch", "")})
    if pending:
        for name, date, subject in pending:
            print("pending: {} (last commit {}: {})".format(name, date, subject))
        return 5
    print("missing: {}:.claude/verify.json has no \"render\" section, and no local branch has one".format(ref))
    return 1


# ── optout / optin ───────────────────────────────────────────────────────────

def cmd_optout(repo, reason, run):
    data = load_optouts()
    data[repo_key(repo)] = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "run": run or "",
        "reason": reason or "",
    }
    save_optouts(data)
    print("opted out: {} (cleared by `render-config.py optin` or /setup-render)".format(repo_key(repo)))
    return 0


def cmd_optin(repo):
    data = load_optouts()
    if data.pop(repo_key(repo), None) is None:
        print("not opted out: " + repo_key(repo))
    else:
        save_optouts(data)
        print("opt-out cleared: " + repo_key(repo))
    return 0


# ── validation ───────────────────────────────────────────────────────────────

def list_setup_files(setup_dir):
    root = os.path.join(setup_dir, "files")
    out = []
    if os.path.isdir(root):
        for d, _, names in os.walk(root):
            for n in names:
                if n == ".DS_Store":
                    continue
                full = os.path.join(d, n)
                out.append(os.path.relpath(full, root).replace(os.sep, "/"))
    return sorted(out)


def load_json(path, label, v):
    try:
        return json.load(open(path, encoding="utf-8"))
    except FileNotFoundError:
        v.append("MISSING: {} not found ({})".format(label, path))
    except Exception as e:
        v.append("BAD_JSON: {}: {}".format(label, e))
    return None


def validate_targets(targets, fixtures, label, v):
    if not isinstance(targets, list) or not targets:
        v.append("SMOKE_TARGETS: {} must be a non-empty list".format(label))
        return []
    ok = []
    seen = set()
    for i, t in enumerate(targets):
        if not isinstance(t, dict):
            v.append("SMOKE_TARGETS: {}[{}] must be an object".format(label, i))
            continue
        tid, route, st = t.get("id"), t.get("route"), t.get("state")
        if not isinstance(tid, str) or not SLUG.match(tid) or tid in seen:
            v.append("SMOKE_TARGETS: {}[{}].id must be a unique lowercase slug: {}".format(label, i, json.dumps(tid)))
        seen.add(tid)
        if not isinstance(route, str) or not route.startswith("/") or route.startswith("//") or ".." in route:
            v.append("SMOKE_TARGETS: {}[{}].route must be a path starting with /: {}".format(label, i, json.dumps(route)))
        if not isinstance(st, str) or not SLUG.match(st):
            v.append("SMOKE_TARGETS: {}[{}].state must be a lowercase slug: {}".format(label, i, json.dumps(st)))
        fx = t.get("fixture")
        if fx is not None and (not isinstance(fx, str) or fx not in fixtures):
            v.append("FIXTURE_MISSING: {}[{}] uses fixture {}, which render.json's fixtures does not define".format(label, i, json.dumps(fx)))
        ok.append(t)
    return ok


def validate(setup_dir, repo, base, contract_path):
    v, notes = [], []
    files = list_setup_files(setup_dir)
    for f in files:
        if f == ".claude/verify.json":
            v.append("OUTSIDE_SCOPE: don't write files/.claude/verify.json; put only the render section in render.json (it is merged into the base's verify.json, every other key kept)")
        elif not ALLOWED_FILE.match(f):
            v.append("OUTSIDE_SCOPE: files/{} is not allowed; render setup may only add .claude/launch.json and files under .claude/render/. If the app itself needs a change (e.g. a dev-only sign-in bypass), return NEEDS_INPUT instead".format(f))

    render = load_json(os.path.join(setup_dir, "render.json"), "render.json", v)
    if render is not None and not isinstance(render, dict):
        v.append("SCHEMA: render.json must be a JSON object (the value of verify.json's \"render\" key)")
        render = None
    render = render or {}

    for k in render:
        if k not in KNOWN_RENDER_KEYS:
            notes.append("note: render.json key {} is not read by render-capture.mjs".format(json.dumps(k)))

    fixtures = render.get("fixtures", {})
    if not isinstance(fixtures, dict) or not all(isinstance(k, str) and SLUG.match(k) and isinstance(d, str) and d.strip() for k, d in fixtures.items()):
        v.append("SCHEMA: render.fixtures must map lowercase-slug names to a one-line description of the state (the Architect picks fixtures by these descriptions)")
        fixtures = fixtures if isinstance(fixtures, dict) else {}

    setup = render.get("setup")
    hook_src = ""
    if setup is not None:
        if not isinstance(setup, str) or not setup.startswith(".claude/render/") or ".." in setup:
            v.append("SCHEMA: render.setup must be a path under .claude/render/ (e.g. .claude/render/setup.mjs): {}".format(json.dumps(setup)))
        else:
            p = os.path.join(setup_dir, "files", setup)
            if os.path.exists(p):
                hook_src = open(p, encoding="utf-8", errors="replace").read()
            else:
                hook_src = git_show(repo, base, setup) or ""
                if not hook_src:
                    v.append("MISSING: render.setup points at {}, which is neither in files/ nor on {}".format(setup, base))

    vp = render.get("viewports")
    if vp is not None and not (isinstance(vp, dict) and vp and all(
            isinstance(x, list) and len(x) == 2 and all(isinstance(n, int) and n > 0 for n in x) for x in vp.values())):
        v.append("SCHEMA: render.viewports must map names to [width, height] (e.g. {\"desktop\": [1280, 800], \"mobile\": [390, 844]})")
    if "now" in render:
        try:
            datetime.fromisoformat(str(render["now"]).replace("Z", "+00:00"))
        except Exception:
            v.append("SCHEMA: render.now must be an ISO timestamp (e.g. 2026-09-15T12:00:00Z): {}".format(json.dumps(render["now"])))
    for key in ("portArgs", "hideSelectors"):
        if key in render and not (isinstance(render[key], list) and all(isinstance(x, str) for x in render[key])):
            v.append("SCHEMA: render.{} must be a list of strings".format(key))

    # launch.json: from files/ or the base
    lp = os.path.join(setup_dir, "files", ".claude", "launch.json")
    raw_launch = open(lp, encoding="utf-8").read() if os.path.exists(lp) else git_show(repo, base, ".claude/launch.json")
    if raw_launch is None:
        v.append("MISSING: no .claude/launch.json on {} and none in files/; render-capture needs it for the dev-server command".format(base))
    else:
        try:
            confs = json.loads(raw_launch).get("configurations") or []
            want = render.get("server", "dev")
            launch = next((c for c in confs if c.get("name") == want), confs[0] if confs else None)
            if not launch or not launch.get("runtimeExecutable"):
                v.append("SCHEMA: .claude/launch.json needs a configuration with runtimeExecutable (named {} or the first one)".format(json.dumps(want)))
            elif "server" in render and not any(c.get("name") == render["server"] for c in confs):
                v.append("SCHEMA: render.server is {}, but launch.json has no configuration with that name".format(json.dumps(render["server"])))
        except Exception as e:
            v.append("BAD_JSON: .claude/launch.json: {}".format(e))

    # hook syntax
    node = shutil.which("node")
    for f in files:
        if f.startswith(".claude/render/") and f.endswith((".mjs", ".js")) and node:
            r = subprocess.run([node, "--check", os.path.join(setup_dir, "files", f)], capture_output=True, text=True)
            if r.returncode != 0:
                v.append("SYNTAX: files/{}: {}".format(f, (r.stderr or r.stdout).strip().splitlines()[-1:] or "node --check failed"))

    # fixtures the run's targets need
    used = set()
    if contract_path:
        contract = load_json(contract_path, "contract.json", v) or {}
        for t in contract.get("render_targets") or []:
            fx = t.get("fixture")
            if fx:
                used.add(fx)
                if fx not in fixtures:
                    v.append("FIXTURE_MISSING: the run's render target {} uses fixture {}; define it in render.fixtures and make seed() create that state".format(t.get("id"), json.dumps(fx)))

    # smoke targets
    smoke = load_json(os.path.join(setup_dir, "smoke-targets.json"), "smoke-targets.json", v)
    if smoke is not None:
        smoke = validate_targets(smoke, fixtures, "smoke-targets.json", v)
        readies = [t.get("ready") for t in smoke if t.get("ready")]
        if smoke and not readies:
            v.append("SMOKE_TARGETS: at least one smoke target needs a `ready` testid that only a signed-in page shows; without it the smoke test can't tell a real page from a sign-in screen")
        for t in smoke:
            r = t.get("ready")
            if r and git(repo, "grep", "-q", "-F", r, base, "--", ":!*.test.*", ":!*.spec.*", ":!**/__tests__/**").returncode != 0:
                v.append("SMOKE_READY_NOT_ON_BASE: smoke target {} waits for testid {}, which doesn't appear literally in the app code on {}. Smoke targets must be pages that already exist on the base".format(t.get("id"), json.dumps(r), base))
            if t.get("fixture"):
                used.add(t["fixture"])

    if used and not SEED_EXPORT.search(hook_src):
        v.append("SEED_MISSING: fixtures are used ({}) but the setup hook exports no seed(); render-capture only creates a fixture's state through seed({{page, fixture, now}})".format(", ".join(sorted(used))))

    return v, notes, render, files


# ── apply ────────────────────────────────────────────────────────────────────

def insert_render(text, render):
    """verify.json text with `render` set, keeping every existing line byte for byte when the
    file has no render key yet (the usual case), so the PR diff is only the new section."""
    data = json.loads(text) if text.strip() else {}
    indent = detect_indent(text)
    want = dict(data)
    want["render"] = render
    t = text.rstrip()
    if "render" not in data and t.endswith("}"):
        body = t[:-1].rstrip()
        ind = " " * indent if isinstance(indent, int) else indent
        section = json.dumps(render, indent=indent, ensure_ascii=False).replace("\n", "\n" + ind)
        out = body + ("" if body.endswith("{") else ",") + "\n" + ind + '"render": ' + section + "\n}\n"
        try:
            if json.loads(out) == want:
                return out
        except Exception:
            pass
    return json.dumps(want, indent=indent, ensure_ascii=False) + "\n"


def detect_indent(text):
    for line in (text or "").splitlines()[1:]:
        m = re.match(r"^( +|\t)\"", line)
        if m:
            return m.group(1) if m.group(1) == "\t" else len(m.group(1))
    return 2


def cmd_apply(setup_dir, repo, base, branch, contract_path, reuse):
    setup_dir = os.path.abspath(setup_dir)
    if not os.path.isdir(setup_dir):
        die("no such setup folder: " + setup_dir)
    if not ref_exists(repo, base):
        die("unknown base ref: " + base)
    exists = ref_exists(repo, "refs/heads/" + branch)
    if exists and not reuse:
        print("branch-exists: {} already exists in {} (last commit: {}). Re-run with --reuse to commit on top of it, or pick another --branch".format(
            branch, repo, git(repo, "log", "-1", "--format=%h %cs %s", branch).stdout.strip()))
        return 6
    if reuse and not exists:
        die("--reuse given but branch {} does not exist".format(branch))
    if git(repo, "check-ref-format", "--branch", branch).returncode != 0:
        die("not a valid branch name: " + branch)

    v, notes, render, files = validate(setup_dir, repo, branch if reuse else base, contract_path)
    for n in notes:
        print(n)
    if v:
        for x in v:
            print(x)
        return 1

    wt = tempfile.mkdtemp(prefix="orchestrator-render-setup-")
    os.rmdir(wt)
    args = ["worktree", "add", wt, branch] if reuse else ["worktree", "add", "-b", branch, wt, base]
    r = git(repo, *args)
    if r.returncode != 0:
        die("git worktree add failed (is {} checked out somewhere else?): {}".format(branch, (r.stderr or r.stdout).strip()))
    try:
        for f in files:
            dst = os.path.join(wt, f)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(os.path.join(setup_dir, "files", f), dst)
        # Start from the base's verify.json (on a retry: the commit the branch was cut from, so
        # the branch never picks up later changes to the base), then add the render section.
        src_ref = git(repo, "merge-base", base, branch).stdout.strip() if reuse else base
        old = (git_show(repo, src_ref, ".claude/verify.json") if src_ref else None) or ""
        vpath = os.path.join(wt, ".claude", "verify.json")
        new_text = insert_render(old, render)
        if old.strip():
            a, b = json.loads(old), json.loads(new_text)
            a.pop("render", None)
            b.pop("render", None)
            if a != b:
                die("internal: merging render changed other verify.json keys")
        os.makedirs(os.path.dirname(vpath), exist_ok=True)
        with open(vpath, "w", encoding="utf-8") as fh:
            fh.write(new_text)
        git(wt, "add", "-A", ".claude", check=True)
        if git(wt, "diff", "--cached", "--quiet").returncode == 0:
            print("branch: " + branch)
            print("commit: unchanged")
            return 0
        changed = git(wt, "diff", "--cached", "--name-only").stdout.split()
        title = "fix(render): render review config, after a failed smoke test" if reuse else "chore(render): configure render review"
        msg = (title + "\n\n"
               "Config for the AI orchestrator's render review (render-capture.mjs), which opens the pages\n"
               "a change affects in a real browser, before and after. It reads this from the base branch,\n"
               "never from the branch under review.\n\n" + "\n".join("- " + c for c in changed) +
               "\n\nSetup: {}\n".format(os.path.relpath(setup_dir, ORCH_ROOT)))
        git(wt, "commit", "-q", "-m", msg, check=True)
        print("branch: " + branch)
        print("commit: " + git(wt, "rev-parse", "--short", "HEAD").stdout.strip())
        for c in changed:
            print("file: " + c)
        return 0
    finally:
        git(repo, "worktree", "remove", "--force", wt)
        shutil.rmtree(wt, ignore_errors=True)
        git(repo, "worktree", "prune")


# ── smoke-prep ───────────────────────────────────────────────────────────────

def cmd_smoke_prep(setup_dir, repo, branch):
    setup_dir = os.path.abspath(setup_dir)
    if not ref_exists(repo, branch):
        die("unknown branch: " + branch)
    render = render_section(repo, branch)
    if render is None:
        die("{}:.claude/verify.json has no render section; run `apply` first".format(branch))
    v = []
    smoke = load_json(os.path.join(setup_dir, "smoke-targets.json"), "smoke-targets.json", v)
    targets = validate_targets(smoke, render.get("fixtures") or {}, "smoke-targets.json", v) if smoke is not None else []
    if v:
        for x in v:
            print(x)
        return 1
    sd = os.path.join(setup_dir, "smoke")
    shutil.rmtree(os.path.join(sd, "render"), ignore_errors=True)
    os.makedirs(sd, exist_ok=True)
    with open(os.path.join(sd, "state.md"), "w", encoding="utf-8") as f:
        f.write("run_id: render-smoke\nrepo: {r}\noriginal_branch: {b}\ntarget_branch: {b}\nrender_config_ref: {b}\nstep: render-smoke\n".format(r=repo, b=branch))
    with open(os.path.join(sd, "contract.json"), "w", encoding="utf-8") as f:
        json.dump({"version": 1, "render_targets": targets}, f, indent=2)
        f.write("\n")
    print("smoke: {} target(s) on {}".format(len(targets), branch))
    print("run: node .claude/scripts/render-capture.mjs {} render-smoke --run-dir {} --only after".format(json.dumps(repo), json.dumps(os.path.relpath(sd, ORCH_ROOT))))
    return 0


# ── main ─────────────────────────────────────────────────────────────────────

def opt(args, name, default=None):
    if name in args:
        i = args.index(name)
        if i + 1 >= len(args):
            die("{} needs a value".format(name))
        val = args[i + 1]
        del args[i:i + 2]
        return val
    return default


def main():
    args = sys.argv[1:]
    if not args:
        die(__doc__)
    cmd = args.pop(0)
    if cmd == "check" and len(args) == 2:
        return cmd_check(args[0], os.path.abspath(args[1]))
    if cmd == "optout":
        reason, run = opt(args, "--reason", ""), opt(args, "--run", "")
        if len(args) != 1:
            die("usage: render-config.py optout <repo> [--reason TEXT] [--run RUN_ID]")
        return cmd_optout(os.path.abspath(args[0]), reason, run)
    if cmd == "optin" and len(args) == 1:
        return cmd_optin(os.path.abspath(args[0]))
    if cmd == "apply":
        base, branch, contract = opt(args, "--base"), opt(args, "--branch"), opt(args, "--contract")
        reuse = "--reuse" in args
        args = [a for a in args if a != "--reuse"]
        if len(args) != 2 or not base or not branch:
            die("usage: render-config.py apply <setup_dir> <repo> --base REF --branch NAME [--contract PATH] [--reuse]")
        return cmd_apply(args[0], os.path.abspath(args[1]), base, branch, contract, reuse)
    if cmd == "smoke-prep":
        branch = opt(args, "--branch")
        if len(args) != 2 or not branch:
            die("usage: render-config.py smoke-prep <setup_dir> <repo> --branch NAME")
        return cmd_smoke_prep(args[0], os.path.abspath(args[1]), branch)
    die(__doc__)


if __name__ == "__main__":
    sys.exit(main())

"""guard-writes.sh: the PreToolUse hook, fed hook JSON on stdin as Claude Code sends it.
Exit 0 allows the write, exit 2 blocks it (stderr goes to the agent)."""
import json
import os

import pytest

RUN = "runs/run_1"


@pytest.fixture
def guard(orch):
    (orch.root / RUN).mkdir(parents=True)

    def call(role, file_path, tool="Write", cwd=None, project_dir=None):
        payload = {"tool_name": tool, "tool_input": {"file_path": str(file_path)}, "cwd": str(cwd or orch.root)}
        r = orch.run("guard-writes.sh", role, input=json.dumps(payload),
                     env={"CLAUDE_PROJECT_DIR": str(project_dir or orch.root)})
        return r.returncode, r.stderr
    call.root = orch.root
    return call


ALLOWED = [
    ("product", RUN + "/prd.md"),
    ("architect", RUN + "/plan.md"),
    ("architect", RUN + "/contract.json"),
    ("architect", RUN + "/repo-digest.md"),
    ("tester", RUN + "/tests/apps/web/src/x.test.ts"),
    ("coder", RUN + "/code/apps/web/src/x.ts"),
    ("render-reviewer", RUN + "/render-review.md"),
    ("render-setup", "runs/rsetup_1/render-setup/files/.claude/render/seed.mjs"),
    ("render-setup", RUN + "/render-setup/render.json"),
    ("adr-writer", RUN + "/adr/2026-11-12-progress-block.md"),
]


@pytest.mark.parametrize("role, rel", ALLOWED)
def test_allowed_path(guard, role, rel):
    assert guard(role, guard.root / rel) == (0, "")


DENIED = [
    ("product", RUN + "/plan.md"),
    ("architect", RUN + "/code/src/x.ts"),
    ("tester", RUN + "/code/src/x.ts"),
    ("tester", RUN + "/tests"),                      # the folder itself, not a file in it
    ("coder", RUN + "/tests/src/x.test.ts"),
    ("coder", RUN + "/contract.json"),
    ("coder", RUN + "/code/../plan.md"),             # traversal out of code/
    ("coder", "runs/run_1/nested/code/x.ts"),        # run id can't swallow extra segments
    ("render-reviewer", RUN + "/prd.md"),
    ("render-setup", RUN + "/render-setup/other.json"),
    ("adr-writer", RUN + "/report.md"),
]


@pytest.mark.parametrize("role, rel", DENIED)
def test_path_outside_role_folder(guard, role, rel):
    code, err = guard(role, guard.root / rel)
    assert code == 2
    assert err.startswith("Blocked by pipeline write guard: the {} agent may only write".format(role))


@pytest.mark.parametrize("role", ["product", "architect", "tester", "coder", "render-reviewer",
                                  "render-setup", "adr-writer"])
def test_path_outside_project(guard, tmp_path, role):
    target = tmp_path / "target-repo" / "src" / "x.ts"
    code, err = guard(role, target)
    assert code == 2 and str(target) in err


def test_relative_path_resolved_against_cwd(guard):
    assert guard("coder", "code/src/a.ts", cwd=guard.root / RUN) == (0, "")
    assert guard("coder", "plan.md", cwd=guard.root / RUN)[0] == 2


def test_unknown_role_and_non_file_call(guard, orch):
    assert guard("reviewer", guard.root / RUN / "prd.md")[0] == 2
    r = orch.run("guard-writes.sh", "coder", input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}),
                 env={"CLAUDE_PROJECT_DIR": str(orch.root)})
    assert r.returncode == 0


# ── symlinks ─────────────────────────────────────────────────────────────────

def test_project_reached_through_symlink(guard, tmp_path):
    link = tmp_path / "orch-link"
    link.symlink_to(guard.root)
    assert guard("coder", link / RUN / "code" / "a.ts") == (0, "")
    assert guard("coder", guard.root / RUN / "code" / "a.ts", project_dir=link) == (0, "")
    assert guard("coder", link / RUN / "plan.md")[0] == 2


def test_symlinked_directory_out_of_code(guard, tmp_path):
    outside = tmp_path / "target-repo"
    outside.mkdir()
    (guard.root / RUN / "code").mkdir()
    (guard.root / RUN / "code" / "escape").symlink_to(outside)
    code, err = guard("coder", guard.root / RUN / "code" / "escape" / "a.ts")
    assert code == 2 and err.startswith("Blocked by pipeline write guard: the coder agent")


@pytest.mark.parametrize("target_exists", [True, False], ids=["existing-target", "dangling"])
def test_symlinked_file_out_of_code(guard, tmp_path, target_exists):
    """Write follows a symlink, so code/a.ts -> <target repo>/a.ts writes into the target repo."""
    outside = tmp_path / "target-repo"
    outside.mkdir()
    if target_exists:
        (outside / "a.ts").write_text("export {}\n")
    (guard.root / RUN / "code").mkdir()
    (guard.root / RUN / "code" / "a.ts").symlink_to(outside / "a.ts")
    code, err = guard("coder", guard.root / RUN / "code" / "a.ts")
    assert code == 2 and str(outside / "a.ts") in err


def test_symlink_staying_inside_the_folder_is_allowed(guard):
    code_dir = guard.root / RUN / "code"
    (code_dir / "src").mkdir(parents=True)
    (code_dir / "src" / "real.ts").write_text("export {}\n")
    (code_dir / "alias.ts").symlink_to(code_dir / "src" / "real.ts")
    assert guard("coder", code_dir / "alias.ts") == (0, "")


# ── coder-only rules ─────────────────────────────────────────────────────────

@pytest.fixture
def sliced(guard):
    run = guard.root / RUN
    (run / "coder-slices.json").write_text(json.dumps({"slices": [
        {"index": 1, "label": "data & core", "files": [{"path": "src/lib/a.ts", "action": "create"}]},
        {"index": 2, "label": "app & UI", "files": [{"path": "src/ui/b.tsx", "action": "modify"}]},
    ]}))
    return run


def test_later_slice_file_blocked(guard, sliced):
    (sliced / ".coder-slice").write_text("1\n")
    assert guard("coder", sliced / "code/src/lib/a.ts") == (0, "")
    code, err = guard("coder", sliced / "code/src/ui/b.tsx", tool="Edit")
    assert code == 2 and "belongs to a later Coder slice, 2 (app & UI)" in err


@pytest.mark.parametrize("current", ["2", "0"])
def test_later_slice_rule_off_for_current_slice_or_no_slicing(guard, sliced, current):
    (sliced / ".coder-slice").write_text(current + "\n")
    assert guard("coder", sliced / "code/src/ui/b.tsx") == (0, "")


def test_generated_file_blocked(guard):
    run = guard.root / RUN
    (run / "generated.json").write_text(json.dumps({"files": [{"path": "src/gen/routes.ts", "command": "pnpm gen:routes"}]}))
    for tool in ("Write", "Edit"):
        code, err = guard("coder", run / "code/src/gen/routes.ts", tool=tool)
        assert code == 2 and "generated file (`pnpm gen:routes`)" in err
    assert guard("tester", run / "tests/src/gen/routes.test.ts") == (0, "")


@pytest.mark.parametrize("size, tool, role, expected", [
    (8001, "Write", "coder", 2),
    (8000, "Write", "coder", 0),     # limit is "larger than 8000"
    (8001, "Edit", "coder", 0),      # targeted edits are the point
    (8001, "Write", "tester", 0),    # only the Coder rewrites seeded files
])
def test_write_over_large_existing_file(guard, size, tool, role, expected):
    folder = "code" if role == "coder" else "tests"
    f = guard.root / RUN / folder / "src" / "big.ts"
    f.parent.mkdir(parents=True)
    f.write_text("x" * size)
    code, err = guard(role, f, tool=tool)
    assert code == expected, err
    if expected:
        assert "already exists ({} bytes)".format(size) in err


def test_write_new_file_in_code_allowed(guard):
    assert guard("coder", guard.root / RUN / "code" / "src" / "new.ts") == (0, "")

"""coder-slices.py: single pass for small runs, layered slices for big ones."""
import json
import shutil

from conftest import FIXTURES

RUN = "run_cs"


def make_run(orch, contract):
    run_dir = orch.run_dir(RUN)
    (run_dir / "contract.json").write_text(json.dumps(contract))
    return run_dir


def test_small_run_is_single_pass(orch, tmp_path):
    run_dir = orch.run_dir(RUN)
    shutil.copy(FIXTURES / "contract.json", run_dir / "contract.json")
    repo = tmp_path / "repo"
    (repo / "apps/web/src/components/layout").mkdir(parents=True)
    (repo / "apps/web/src/components/layout/site-nav.tsx").write_text("x" * 3000)
    r = orch.run("coder-slices.py", "plan", RUN, repo)
    assert r.returncode == 1, r.stdout
    data = json.loads((run_dir / "coder-slices.json").read_text())
    [only] = data["slices"]
    assert only["label"] == "all files"
    # created files first (by path), then modified ones
    assert [f["path"] for f in only["files"]] == [
        "apps/web/src/components/sets/index.ts", "apps/web/src/components/sets/set-progress.tsx",
        "apps/web/src/components/layout/site-nav.tsx"]


def test_big_run_sliced_by_layer(orch, tmp_path):
    files = [{"path": "src/lib/f{:02d}.ts".format(i), "action": "create", "owner": "coder"} for i in range(15)]
    files += [{"path": "src/components/c{:02d}.tsx".format(i), "action": "create", "owner": "coder"} for i in range(15)]
    files += [{"path": "src/i18n/locales/{}.ts".format(l), "action": "create", "owner": "coder"} for l in ("en", "es")]
    files += [{"path": "src/gen/routes.ts", "action": "modify", "owner": "coder"},
              {"path": "src/lib/f00.test.ts", "action": "create", "owner": "tester"}]
    run_dir = make_run(orch, {"files": files})
    (run_dir / "generated.json").write_text(json.dumps({"files": [{"path": "src/gen/routes.ts", "command": "gen"}]}))
    (run_dir / "tests/src/lib").mkdir(parents=True)
    (run_dir / "tests/src/lib/f00.test.ts").write_text("import { f } from './f00'\n")
    (run_dir / "tests/src/components").mkdir(parents=True)
    (run_dir / "tests/src/components/c03.test.tsx").write_text("import { C } from '@/components/c03'\n")

    r = orch.run("coder-slices.py", "plan", RUN, tmp_path)
    assert r.returncode == 0, r.stdout
    data = json.loads((run_dir / "coder-slices.json").read_text())
    assert [s["label"] for s in data["slices"]] == ["data & core", "app & UI + content & docs"]
    assert [len(s["files"]) for s in data["slices"]] == [15, 17]
    assert data["slices"][0]["tests"] == ["src/lib/f00.test.ts"]
    assert data["slices"][1]["tests"] == ["src/components/c03.test.tsx"]
    assert data["generated_left_out"] == ["src/gen/routes.ts"]
    assert all(f["path"] != "src/gen/routes.ts" for s in data["slices"] for f in s["files"])


def test_set_current_slice(orch):
    run_dir = orch.run_dir(RUN)
    assert orch.run("coder-slices.py", "set", RUN, "2").returncode == 0
    assert (run_dir / ".coder-slice").read_text() == "2\n"
    assert orch.run("coder-slices.py", "set", RUN, "two").returncode == 3


def test_missing_contract_is_setup_error(orch, tmp_path):
    orch.run_dir(RUN)
    r = orch.run("coder-slices.py", "plan", RUN, tmp_path)
    assert r.returncode == 3 and "contract.json not found" in r.stderr

"""render-config.py check: what it says for a run without render targets."""
import json

import pytest

RUN = "run_rc"
UI = {"path": "apps/web/src/app/collection/page.tsx", "action": "modify", "owner": "coder"}
API = {"path": "apps/api/src/collection.service.ts", "action": "modify", "owner": "coder"}
TEST = {"path": "apps/web/src/app/collection/page.test.tsx", "action": "create", "owner": "tester"}


@pytest.fixture
def check(orch, make_repo):
    def go(files, **extra):
        repo = make_repo({"README.md": "x\n"})
        d = orch.run_dir(RUN)
        (d / "state.md").write_text("run_id: {}\noriginal_branch: main\n".format(RUN))
        (d / "contract.json").write_text(json.dumps(dict({"version": 1, "files": files}, **extra)))
        r = orch.run("render-config.py", "check", RUN, repo)
        return r.returncode, r.stdout.strip()
    return go


def test_api_only_run_is_not_needed(check):
    code, out = check([API, TEST])
    assert code == 2 and out.startswith("not-needed:") and "no UI files" in out


def test_ui_run_with_none_reason_is_not_not_needed(check):
    code, out = check([UI, API], render_targets=[], render_targets_none_reason="pure refactor, same markup")
    assert code == 2 and out.startswith("no-targets:")
    assert "page.tsx" in out and "SKIPPED" in out and "pure refactor, same markup" in out


def test_ui_run_old_contract_names_its_legacy_reason(check):
    code, out = check([UI], render_skip_reason="repo has no render config")
    assert code == 2 and out.startswith("no-targets:") and "render_skip_reason (old contract): repo has no render config" in out


def test_ui_run_old_contract_without_reason(check):
    code, out = check([UI])
    assert code == 2 and out.startswith("no-targets:") and "no reason given" in out

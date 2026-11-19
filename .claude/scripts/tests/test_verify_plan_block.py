import importlib.util, os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("verify", os.path.join(HERE, "..", "verify.py"))
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class PlanVerifyCommands(unittest.TestCase):
    def _cmds(self, text):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "plan.md"), "w").write(text)
            return verify.plan_verify_commands(d)

    def test_inline_fence_mention_is_not_a_block(self):
        text = ("- Every line of the ```verify block below passed.\n"
                "1. prose step with `code`\n\n"
                "```verify\ntrue\n# comment\ntest 1 = 1\n```\n")
        self.assertEqual(self._cmds(text), ["true", "test 1 = 1"])


if __name__ == "__main__":
    unittest.main()

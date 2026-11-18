"""Tests for copy-to-test.py. Run: python3 -m unittest discover -s skills/jigx-performance/scripts -p 'test_*.py'"""
import importlib.util
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("copy_to_test", os.path.join(HERE, "copy-to-test.py"))
ct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ct)


class CopyToTestTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, ".git"))
        os.makedirs(os.path.join(self.root, ".vscode"))
        with open(os.path.join(self.root, ".vscode", "settings.json"), "w") as f:
            f.write('{"jigx.workspace.organizationId": "org"}')
        self.src = os.path.join(self.root, "Solution")
        os.makedirs(os.path.join(self.src, "jigs"))
        with open(os.path.join(self.src, "index.jigx"), "w") as f:
            f.write("name: field-services\ntitle: Field Services\ncategory: business\nhome:\n  name: x\n")
        with open(os.path.join(self.src, "jigs", "a.jigx"), "w") as f:
            f.write("title: A\n")

    def test_copy_gets_the_test_name_and_the_organisation(self):
        out = os.path.join(self.root, "copy")
        ct.copy_to_test(self.src, out, "field-services-test")
        with open(os.path.join(out, "index.jigx")) as f:
            text = f.read()
        self.assertIn("name: field-services-test\ntitle: field-services-test\n", text)
        self.assertIn("home:\n  name: x\n", text)  # nested name: untouched
        self.assertTrue(os.path.exists(os.path.join(out, "jigs", "a.jigx")))
        self.assertTrue(os.path.exists(os.path.join(out, ".vscode", "settings.json")))
        with open(os.path.join(self.src, "index.jigx")) as f:
            self.assertIn("name: field-services\n", f.read())  # original untouched

    def test_refuses_the_original_name(self):
        with self.assertRaises(SystemExit):
            ct.copy_to_test(self.src, os.path.join(self.root, "copy"), "Field-Services")


    def test_settings_from_the_main_checkout_of_a_worktree(self):
        wt = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, ".git", "worktrees", "perf"))
        with open(os.path.join(wt, ".git"), "w") as f:
            f.write(f"gitdir: {os.path.join(self.root, '.git', 'worktrees', 'perf')}\n")
        os.makedirs(os.path.join(wt, "Solution"))
        self.assertEqual(ct.find_settings(os.path.join(wt, "Solution")), os.path.join(self.root, ".vscode", "settings.json"))
    def test_settings_without_the_organisation_are_not_reported_as_found(self):
        # Peter's end-to-end rerun: "copied from …/.vscode/settings.json", but that file had no organizationId
        import contextlib, io
        os.makedirs(os.path.join(self.src, ".vscode"))
        with open(os.path.join(self.src, ".vscode", "settings.json"), "w") as f:
            f.write('{\n  // editor only\n  "editor.tabSize": 2,\n}')
        # the nearest file has no organisation, the repository root's has: that one is used
        self.assertEqual(ct.find_settings(self.src), os.path.join(self.root, ".vscode", "settings.json"))
        with open(os.path.join(self.root, ".vscode", "settings.json"), "w") as f:
            f.write('{"editor.tabSize": 2}')
        self.assertEqual(ct.find_settings(self.src), os.path.join(self.src, ".vscode", "settings.json"))  # the nearest, as a fallback
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ct.main([self.src, "--name", "demo-test", "--out", os.path.join(self.root, "copy")])
        self.assertIn("Organisation settings: NOT FOUND.", out.getvalue())
        self.assertIn("has no jigx.workspace.organizationId", out.getvalue())
        self.assertNotIn("Organisation settings: copied from", out.getvalue())
        outside = tempfile.mkdtemp()
        with open(os.path.join(outside, "index.jigx"), "w") as f:
            f.write("name: demo\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ct.main([outside, "--name", "demo-test", "--out", os.path.join(self.root, "copy2")])
        self.assertIn("Organisation settings: NOT FOUND (no .vscode/settings.json", out.getvalue())
        self.assertIn("copy the line from the solution repo's .vscode/settings.json", out.getvalue())


if __name__ == "__main__":
    unittest.main()

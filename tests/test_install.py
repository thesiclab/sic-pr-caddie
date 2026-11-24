"""Tests for install.sh in a throwaway HOME. Run from the repo root: python3 -m unittest"""
import glob
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(ROOT, "install.sh")
INSTALLED = ["agents/pr-caddie.md", "hooks/pr-caddie-guard.py", "hooks/pr-opened.py",
             "scripts/pr-caddie-watch.py", "scripts/pr-caddie-gate.py"]


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class Install(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.claude = os.path.join(self.home, ".claude")

    def tearDown(self):
        self.tmp.cleanup()

    def run_install(self, *args):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
        env["HOME"] = self.home
        p = subprocess.run(["bash", INSTALL] + list(args), capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        return p.stdout

    def test_dry_run_changes_nothing(self):
        out = self.run_install("--dry-run")
        self.assertIn("would: installed", out)
        self.assertFalse(os.path.exists(self.claude))

    def test_install_twice_then_uninstall(self):
        out = self.run_install()
        for f in INSTALLED:
            self.assertTrue(os.path.isfile(os.path.join(self.claude, f)), f)
        self.assertTrue(os.path.isfile(os.path.join(self.claude, "pr-caddie.json")))
        self.assertIn("PostToolUse", out)
        self.assertFalse(os.path.exists(os.path.join(self.claude, "settings.json")))
        out = self.run_install()
        self.assertIn("same:", out)
        self.assertEqual(glob.glob(os.path.join(self.claude, "**", "*.bak-*"), recursive=True), [])
        self.run_install("--uninstall")
        for f in INSTALLED:
            self.assertFalse(os.path.exists(os.path.join(self.claude, f)), f)
        self.assertTrue(os.path.isfile(os.path.join(self.claude, "pr-caddie.json")))  # config kept

    def test_changed_file_is_backed_up(self):
        os.makedirs(os.path.join(self.claude, "agents"))
        mine = os.path.join(self.claude, "agents", "pr-caddie.md")
        with open(mine, "w") as fh:
            fh.write("my own version\n")
        with open(os.path.join(self.claude, "pr-caddie.json"), "w") as fh:
            fh.write('{"watch_days": 3}\n')
        out = self.run_install()
        self.assertIn("backed up", out)
        baks = glob.glob(mine + ".bak-*")
        self.assertEqual(len(baks), 1)
        with open(baks[0]) as fh:
            self.assertEqual(fh.read(), "my own version\n")
        with open(os.path.join(self.claude, "pr-caddie.json")) as fh:
            self.assertEqual(fh.read(), '{"watch_days": 3}\n')  # never overwritten

    def test_uninstall_keeps_edited_copy(self):
        self.run_install()
        guard = os.path.join(self.claude, "hooks", "pr-caddie-guard.py")
        with open(guard, "a") as fh:
            fh.write("# local edit\n")
        self.run_install("--uninstall")
        self.assertFalse(os.path.exists(guard))
        self.assertEqual(len(glob.glob(guard + ".bak-*")), 1)


if __name__ == "__main__":
    unittest.main()

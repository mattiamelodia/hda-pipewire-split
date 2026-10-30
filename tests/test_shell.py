"""Static checks of install.sh / uninstall.sh. The scripts themselves are never executed."""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401
from hermetic import ROOT, HermeticTestCase, HermeticViolation  # noqa: E402

UNINSTALL = ROOT / "uninstall.sh"
INSTALL = ROOT / "install.sh"


def pkill_pattern():
    match = re.search(r'^\s*pkill\s+-f\s+"([^"]+)"', UNINSTALL.read_text(), re.MULTILINE)
    assert match, "uninstall.sh has no `pkill -f \"...\"` line"
    return match.group(1)


class ShellScriptTests(HermeticTestCase):
    def test_scripts_have_valid_bash_syntax(self):
        # The only test allowed to use the real subprocess, and only for `bash -n` (parse, do not run).
        for script in (INSTALL, UNINSTALL):
            with self.subTest(script=script.name):
                result = self.run_real_bash_syntax_check(script)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_real_subprocess_gate_refuses_everything_else(self):
        with self.assertRaises(HermeticViolation):
            self.run_real_bash_syntax_check("/etc/passwd")
        with self.assertRaises(HermeticViolation):
            self.run_real_bash_syntax_check(ROOT / "main.py")

    def test_uninstall_pkill_pattern_matches_the_installed_launchers(self):
        pattern = re.compile(pkill_pattern())
        for cmdline in (
            "python3 /x/hda-pipewire-split/main.py",
            "python3 /home/me/.local/share/hda-pipewire-split/main.py --tray",
            "/usr/bin/python3 /home/me/.local/share/hda-pipewire-split/main.py",
            "python3 /usr/lib/hda-pipewire-split/main.py",   # PKGBUILD wrapper
            "python /usr/lib/hda-pipewire-split/main.py",
        ):
            with self.subTest(cmdline=cmdline):
                self.assertRegex(cmdline, pattern)

    def test_uninstall_pkill_pattern_does_not_match_unrelated_processes(self):
        pattern = re.compile(pkill_pattern())
        for cmdline in (
            "python3 /x/other-project/main.py",
            "python3 -m unittest discover -s /home/me/Projects/hda-pipewire-split/tests",
            "vim /x/hda-pipewire-split/main.py",
            "bash ./uninstall.sh",
            "python3 /x/hda-pipewire-split/wizard.py",
            "hda-audio-control --tray",
        ):
            with self.subTest(cmdline=cmdline):
                self.assertNotRegex(cmdline, pattern)

    def test_installed_launcher_command_line_is_what_the_pattern_expects(self):
        # install.sh writes `exec python3 "$HOME/.local/share/hda-pipewire-split/main.py" "$@"`
        text = INSTALL.read_text()
        self.assertIn('exec python3 "$HOME/.local/share/hda-pipewire-split/main.py"', text)
        self.assertRegex("python3 /home/u/.local/share/hda-pipewire-split/main.py", re.compile(pkill_pattern()))

    def test_scripts_do_not_source_the_config_file(self):
        # config.env is data (written by the wizard); sourcing it would execute whatever it contains
        for script in (INSTALL, UNINSTALL):
            text = script.read_text()
            self.assertNotRegex(text, r"(?m)^\s*(source|\.)\s+[\"']?\$?\{?CONFIG_FILE", script.name)

    def test_uninstall_never_sets_volume_or_mutes(self):
        text = UNINSTALL.read_text()
        self.assertNotRegex(text, r"set-sink-volume|set-sink-mute|amixer[^\n]*\d+%")


if __name__ == "__main__":
    import unittest

    unittest.main(verbosity=2)

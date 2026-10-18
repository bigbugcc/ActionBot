"""Check that diagnostic logging retains command output and failure status."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BASH = r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else shutil.which("bash")


class LogTests(unittest.TestCase):
    def test_heredoc_input_reaches_the_child_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([
                BASH, (ROOT / "bin/3x-ui/run-step.sh").as_posix(), "export",
                BASH, "-euo", "pipefail",
            ], env={**os.environ, "DIAGNOSTICS_DIR": Path(tmp).as_posix()},
                input="echo heredoc-diagnostic\nexit 93\n", capture_output=True, text=True)
            self.assertEqual(result.returncode, 93, result.stderr)
            self.assertIn("heredoc-diagnostic", (Path(tmp) / "export.log").read_text())

    def test_log_failure_cannot_replace_an_existing_command_error(self):
        driver = r'''
tee() {
  if [[ " $* " == *" -a "* ]]; then cat >/dev/null; return 73; fi
  command tee "$@"
}
export -f tee
exec bash "$TEST_SCRIPT" configure bash -c "exit $TEST_STATUS"
'''
        for command_status, expected in ((0, 73), (42, 42)):
            with self.subTest(status=command_status), tempfile.TemporaryDirectory() as tmp:
                result = subprocess.run([BASH, "-c", driver], env={
                    **os.environ, "DIAGNOSTICS_DIR": Path(tmp).as_posix(),
                    "TEST_SCRIPT": (ROOT / "bin/3x-ui/run-step.sh").as_posix(),
                    "TEST_STATUS": str(command_status),
                }, capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stderr)

    def test_command_output_and_exit_status_reach_log_and_summary(self):
        for status in (0, 42, 124, 137):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                result = subprocess.run([
                    BASH, (ROOT / "bin/3x-ui/run-step.sh").as_posix(), "configure",
                    BASH, "-c", f"echo stdout-diagnostic; echo stderr-diagnostic >&2; exit {status}",
                ], env={**os.environ, "DIAGNOSTICS_DIR": (root / "logs").as_posix(),
                        "GITHUB_STEP_SUMMARY": (root / "summary").as_posix()},
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, status, result.stderr)
                log = (root / "logs/configure.log").read_text()
                for diagnostic in ("stdout-diagnostic", "stderr-diagnostic", f"exit_code={status}"):
                    self.assertIn(diagnostic, log)
                self.assertIn(f"exit_code={status}", (root / "summary").read_text())
                self.assertIn("::endgroup::", result.stdout)


if __name__ == "__main__":
    unittest.main()

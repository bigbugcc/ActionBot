"""Run the cross-build shell driver with fake network/compiler boundaries."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BASH = r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else shutil.which("bash")
SCRIPT = ROOT / "bin/3x-ui/cross-build.sh"

DRIVER = r'''
if command -v cygpath >/dev/null; then
  export TOOLCHAIN_DIR=$(cygpath -u "$TEST_ROOT")/toolchain
fi
curl() {
  printf 'download %s\n' "$*" >> "$TEST_LOG"
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == --output ]]; then printf invalid-archive > "$2"; return; fi
    shift
  done
  return 90
}
sha256sum() {
  if [[ "$TEST_MODE" == checksum ]]; then command sha256sum "$@"; return; fi
  cat >> "$TEST_LOG"
}
tar() {
  printf 'extract\n' >> "$TEST_LOG"
  [[ "$TEST_MODE" != extract ]] || return 91
  mkdir -p "$TOOLCHAIN_DIR/bin"
  [[ "$TEST_MODE" != compiler_missing ]] || return 0
  if [[ "$TEST_MODE" == compiler_nonexec ]]; then
    printf 'not executable\n' > "$TOOLCHAIN_DIR/bin/test-gcc.br_real"
    chmod 644 "$TOOLCHAIN_DIR/bin/test-gcc.br_real"
    return
  fi
  printf '#!/bin/sh\nexit 0\n' > "$TOOLCHAIN_DIR/bin/test-gcc.br_real"
  chmod +x "$TOOLCHAIN_DIR/bin/test-gcc.br_real"
  if [[ "$TEST_MODE" == compiler_hardlink ]]; then
    mv "$TOOLCHAIN_DIR/bin/test-gcc.br_real" "$TOOLCHAIN_DIR/bin/test-cc.br_real"
    ln "$TOOLCHAIN_DIR/bin/test-cc.br_real" "$TOOLCHAIN_DIR/bin/test-gcc.br_real"
    [[ "$TOOLCHAIN_DIR/bin/test-cc.br_real" -ef "$TOOLCHAIN_DIR/bin/test-gcc.br_real" ]] || return 96
  fi
  # MSYS defaults to copying symlink targets. Use its symlink representation
  # so Git Bash tests exercise -L; GNU ln on Linux creates a native symlink.
  MSYS=winsymlinks:sys ln -s test-gcc.br_real "$TOOLCHAIN_DIR/bin/test-linux-gcc.br_real"
  [[ -L "$TOOLCHAIN_DIR/bin/test-linux-gcc.br_real" ]] || return 96
  case "$TEST_MODE" in
    compiler_multiple)
      cp "$TOOLCHAIN_DIR/bin/test-gcc.br_real" "$TOOLCHAIN_DIR/bin/other-gcc.br_real"
      chmod +x "$TOOLCHAIN_DIR/bin/other-gcc.br_real" ;;
    compiler_broken_link)
      rm "$TOOLCHAIN_DIR/bin/test-gcc.br_real" ;;
  esac
}
go() {
  if [[ "$1" == version ]]; then printf 'go version go1.27 linux/amd64\n'; return; fi
  printf 'compile %s/%s/%s cgo=%s procs=%s cc=%s args=%s\n' \
    "$GOOS" "$GOARCH" "${GOARM:-}" "$CGO_ENABLED" "$GOMAXPROCS" "$CC" "$*" >> "$TEST_LOG"
  [[ "$TEST_MODE" != compiler ]] || return 92
  printf binary > build/x-ui
}
file() { printf 'ELF statically linked\n'; }
readelf() {
  [[ "$TEST_MODE" != elf_error ]] || return 93
  [[ "$TEST_MODE" != dynamic ]] || printf ' INTERP \n'
}
export -f curl sha256sum tar go file readelf
cd "$TEST_ROOT"
bash "$TEST_SCRIPT" install && bash "$TEST_SCRIPT" build '-w -s -X example.buildCommit=abc12345'
'''


class CrossBuildTests(unittest.TestCase):
    def invoke(self, arch="amd64", variant="", mode="success", buildarch="amd64", targetos="linux"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trace = root / "trace"
            env = {**os.environ, "TEST_ROOT": root.as_posix(), "TEST_LOG": trace.as_posix(),
                   "TEST_SCRIPT": SCRIPT.as_posix(), "TEST_MODE": mode,
                   "TOOLCHAIN_DIR": (root / "toolchain").as_posix(),
                   "BUILDARCH": buildarch, "TARGETOS": targetos,
                   "TARGETARCH": arch, "TARGETVARIANT": variant}
            result = subprocess.run([BASH, "-c", DRIVER], env=env, text=True, capture_output=True)
            return result, trace.read_text() if trace.exists() else ""

    def test_all_five_targets_keep_cgo_and_use_the_matching_musl_toolchain(self):
        for arch, variant, toolchain, goarm in (
            ("amd64", "", "x86-64", ""), ("386", "", "x86-i686", ""),
            ("arm64", "v8", "aarch64", ""), ("arm", "v7", "armv7-eabihf", "7"),
            ("arm", "v6", "armv6-eabihf", "6"),
        ):
            with self.subTest(arch=arch, variant=variant):
                result, trace = self.invoke(arch, variant)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"{toolchain}--musl--stable-2025.08-1.tar.xz", trace)
                self.assertIn(f"compile linux/{arch}/{goarm} cgo=1 procs=2", trace)
                self.assertIn("test-gcc.br_real", trace)
                self.assertRegex(trace, r"cc=\S*/test-gcc\.br_real args=")
                self.assertIn("-p 2", trace)
                self.assertIn("-X example.buildCommit=abc12345", trace)
                self.assertIn("-linkmode external -extldflags '-static'", trace)
                self.assertRegex(trace, r"[0-9a-f]{64}  .+archive.tar.xz")

    def test_arm64_without_variant_is_supported(self):
        result, trace = self.invoke("arm64")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("aarch64--musl", trace)

    def test_real_compiler_can_be_a_hard_link_to_the_cc_entry(self):
        result, trace = self.invoke("arm64", mode="compiler_hardlink")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(trace, r"cc=\S*/test-gcc\.br_real args=")

    def test_invalid_compiler_layout_stops_before_go_and_lists_candidates(self):
        for mode, count, candidate in (
            ("compiler_missing", 0, "(none)"),
            ("compiler_nonexec", 0, "test-gcc.br_real"),
            ("compiler_multiple", 2, "other-gcc.br_real"),
            ("compiler_broken_link", 0, "test-linux-gcc.br_real"),
        ):
            with self.subTest(mode=mode):
                result, trace = self.invoke(mode=mode)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertNotIn("compile", trace)
                self.assertNotIn("go version", result.stdout)
                self.assertIn(f"found {count}", result.stderr)
                self.assertIn(candidate, result.stderr)
                self.assertIn("phase=compiler-selection", result.stderr)

    def test_unsupported_target_or_host_fails_before_download(self):
        for args in ({"arch": "arm", "variant": "v5"}, {"arch": "arm"},
                     {"arch": "riscv64"}, {"arch": "amd64", "variant": "v3"},
                     {"buildarch": "arm64"}, {"targetos": "windows"}):
            with self.subTest(args=args):
                result, trace = self.invoke(**args)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(trace, "")

    def test_bad_checksum_never_extracts_or_compiles(self):
        result, trace = self.invoke(mode="checksum")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("download", trace)
        self.assertNotIn("extract", trace)
        self.assertNotIn("compile", trace)

    def test_extraction_and_compiler_errors_are_preserved(self):
        for mode, status in (("extract", 91), ("compiler", 92), ("elf_error", 93)):
            with self.subTest(mode=mode):
                result, trace = self.invoke(mode=mode)
                self.assertEqual(result.returncode, status, result.stderr)
                if mode == "extract":
                    self.assertNotIn("compile", trace)

    def test_dynamic_binary_is_rejected(self):
        result, _ = self.invoke(mode="dynamic")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("static", result.stderr.lower())


class RuntimeTests(unittest.TestCase):
    CID = "0123456789abcdef" * 4

    def invoke(self, mode, cid_mode="valid", cleanup_failure=False):
        driver = r'''
timeout() {
  printf 'timeout %s\n' "$*" >> "$TEST_LOG"
  while [[ "$1" == -* ]]; do shift; done
  shift
  local is_run=false
  [[ "${1:-}" != docker || "${2:-}" != run ]] || is_run=true
  "$@"
  local result=$?
  if "$is_run"; then
    case "$TEST_MODE" in
      timeout) return 124 ;;
      killed) return 137 ;;
    esac
  fi
  return "$result"
}
docker() {
  printf 'docker %s\n' "$*" >> "$TEST_LOG"
  if [[ "$1" == rm ]]; then
    [[ "$TEST_CLEANUP_FAILURE" == false ]] || return 95
    return 0
  fi
  [[ "$1" == run ]] || return 96
  local cidfile=''
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --cidfile) cidfile=$2; shift ;;
      --cidfile=*) cidfile=${1#--cidfile=} ;;
    esac
    shift
  done
  if [[ -n "$cidfile" ]]; then
    case "$TEST_CID_MODE" in
      valid) printf '%s\n' "$TEST_CID" > "$cidfile" ;;
      invalid) printf 'unrelated-container\n' > "$cidfile" ;;
      short) printf '0123456789ab\n' > "$cidfile" ;;
      extra) printf '%s\n%s\n' "$TEST_CID" unrelated-container > "$cidfile" ;;
      empty) : > "$cidfile" ;;
      missing) : ;;
    esac
  fi
  printf 'runtime stdout diagnostic\n'
  printf 'runtime stderr diagnostic\n' >&2
  case "$TEST_MODE" in
    good) printf 'current panel settings as follows:\nport: 20531\n' ;;
    wrong_value) printf 'port: 2053\n' ;;
    db_error) printf 'Database initialization failed\n' ;;
    failure) return 94 ;;
    timeout|killed) : ;;
  esac
}
export -f timeout docker
exec bash "$TEST_SCRIPT" 'example/3x-ui:v3.10.2-armv6' 'linux/arm/v6'
'''
        with tempfile.TemporaryDirectory() as tmp:
            trace = Path(tmp) / "trace"
            result = subprocess.run([BASH, "-c", driver], env={
                **os.environ, "TEST_MODE": mode, "TEST_LOG": trace.as_posix(),
                "TEST_CID": self.CID, "TEST_CID_MODE": cid_mode,
                "TEST_CLEANUP_FAILURE": str(cleanup_failure).lower(),
                "TEST_SCRIPT": (ROOT / "bin/3x-ui/verify-runtime.sh").as_posix(),
            }, text=True, capture_output=True)
            return result, trace.read_text()

    def test_success_requires_persisted_sqlite_value_and_ephemeral_isolated_container(self):
        result, trace = self.invoke("good")
        self.assertEqual(result.returncode, 0, result.stderr)
        for flag in ("--rm", "--platform linux/arm/v6", "--network none",
                     "--tmpfs /etc/x-ui", "--env XUI_DB_TYPE=sqlite", "--env XUI_DB_DSN=",
                     "--cidfile"):
            self.assertIn(flag, trace)
        self.assertIn("setting -port 20531; /app/x-ui setting -show", trace)

    def test_zero_exit_on_db_error_or_wrong_value_is_rejected(self):
        for mode in ("db_error", "wrong_value"):
            with self.subTest(mode=mode):
                result, _ = self.invoke(mode)
                self.assertNotEqual(result.returncode, 0)

    def test_container_error_is_preserved(self):
        result, _ = self.invoke("failure")
        self.assertEqual(result.returncode, 94, result.stderr)

    def test_failure_and_timeout_preserve_both_diagnostic_streams(self):
        for mode, status in (("failure", 94), ("timeout", 124), ("killed", 137)):
            with self.subTest(mode=mode):
                result, _ = self.invoke(mode)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertIn("runtime stdout diagnostic", result.stdout)
                self.assertIn("runtime stderr diagnostic", result.stdout + result.stderr)
                self.assertRegex(result.stderr, r"stage=runtime operation=run platform=linux/arm/v6 duration_s=\d+ exit_code=" + str(status))

    def test_exit_cleans_only_the_container_recorded_by_this_run(self):
        for mode in ("good", "failure", "timeout", "killed", "wrong_value"):
            with self.subTest(mode=mode):
                _, trace = self.invoke(mode)
                removals = [line for line in trace.splitlines() if line.startswith("docker rm ")]
                self.assertEqual(len(removals), 1, trace)
                self.assertRegex(removals[0], r"^docker rm (?:-f|--force) (?:-- )?" + self.CID + r"$")

    def test_cleanup_failure_does_not_replace_original_exit_status(self):
        for mode, status in (("good", 0), ("failure", 94), ("timeout", 124), ("killed", 137)):
            with self.subTest(mode=mode):
                result, trace = self.invoke(mode, cleanup_failure=True)
                self.assertIn("docker rm ", trace)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertIn("cleanup=unconfirmed", result.stderr)

    def test_missing_empty_or_invalid_cid_never_removes_any_container(self):
        for cid_mode in ("missing", "empty", "invalid", "short", "extra"):
            with self.subTest(cid_mode=cid_mode):
                result, trace = self.invoke("timeout", cid_mode=cid_mode)
                self.assertEqual(result.returncode, 124, result.stderr)
                self.assertNotIn("docker rm ", trace)


if __name__ == "__main__":
    unittest.main()

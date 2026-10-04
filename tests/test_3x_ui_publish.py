"""Exercise the real publication script without touching a registry."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") or (r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else None)

MOCK = r'''
python3() { "$TEST_PYTHON" "$@"; }
docker() {
  if [[ "$*" == *"imagetools inspect"* ]]; then
    if [[ "$*" == *"@sha256:"* && "$TEST_MODE" == source_mismatch ]]; then
      echo sha256:wrong
    elif [[ "$*" != *"@sha256:"* && "$TEST_MODE" == published_mismatch ]]; then
      echo sha256:wrong
    else
      echo "$IMAGE_DIGEST"
    fi
  elif [[ "$*" == *"imagetools create"* ]]; then
    [[ "$*" == *"--prefer-index=false"* ]] || return 10
    [[ "$*" == *"$IMAGE_REPO@$IMAGE_DIGEST"* ]] || return 11
    if [[ "$*" == *"--dry-run"* ]]; then
      if [[ "$TEST_MODE" == transformed_index ]]; then
        echo '{}'
      else
        cat "$TEST_MANIFEST"
        printf '\n'
      fi
    else
      printf '%s\n' "$*" >> "$TEST_MUTATIONS"
      [[ "$TEST_MODE" != push_failure ]] || return 12
    fi
  else
    return 13
  fi
}
export -f python3 docker
exec bash "$TEST_SCRIPT" "$TEST_ARTIFACTS"
'''


@unittest.skipUnless(BASH and Path(BASH).exists(), "Bash is required")
class PublicationTests(unittest.TestCase):
    def invoke(self, mode="success", corrupt=False, tags="example/3x-ui:v3.9.0\nexample/3x-ui:3.9.0\nexample/3x-ui:latest"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = json.dumps({"schemaVersion": 2, "manifests": [
                {"digest": "sha256:platform", "platform": {"os": "linux", "architecture": "amd64"}},
                {"digest": "sha256:attestation", "platform": {"os": "unknown", "architecture": "unknown"},
                 "annotations": {"vnd.docker.reference.type": "attestation-manifest"}},
            ]}, separators=(",", ":")).encode()
            (root / "manifest.json").write_bytes(manifest)
            digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
            (root / "SHA256SUMS").write_text(f"{digest[7:]}  manifest.json\n", newline="\n")
            if corrupt:
                (root / "manifest.json").write_text("corrupt")
            mutations = root / "mutations"
            env = {**os.environ, "IMAGE_REPO": "example/3x-ui", "IMAGE_DIGEST": digest,
                   "PUBLISH_TAGS": tags, "TEST_MODE": mode,
                   "TEST_PYTHON": Path(sys.executable).as_posix(),
                   "TEST_MANIFEST": (root / "manifest.json").as_posix(),
                   "TEST_MUTATIONS": mutations.as_posix(),
                   "TEST_ARTIFACTS": root.as_posix(),
                   "TEST_SCRIPT": (ROOT / "bin/3x-ui/publish.sh").as_posix()}
            result = subprocess.run([BASH, "-c", MOCK], env=env, text=True, capture_output=True)
            return result, mutations.read_text() if mutations.exists() else ""

    def test_success_promotes_complete_index_by_digest(self):
        result, mutations = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(mutations.splitlines()), 1)
        self.assertIn("--tag example/3x-ui:v3.9.0 --tag example/3x-ui:3.9.0 --tag example/3x-ui:latest", mutations)

    def test_historical_version_publishes_without_latest(self):
        result, mutations = self.invoke(tags="example/3x-ui:v3.8.0\nexample/3x-ui:3.8.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--tag example/3x-ui:v3.8.0 --tag example/3x-ui:3.8.0", mutations)
        self.assertNotIn(":latest", mutations)

    def test_corrupt_export_never_publishes(self):
        result, mutations = self.invoke(corrupt=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(mutations, "")

    def test_changed_source_or_index_never_publishes(self):
        for mode in ("source_mismatch", "transformed_index"):
            with self.subTest(mode=mode):
                result, mutations = self.invoke(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(mutations, "")

    def test_unexpected_repository_never_publishes(self):
        result, mutations = self.invoke(tags="other/image:latest")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(mutations, "")

    def test_push_or_postpublish_failure_stops_release(self):
        for mode in ("push_failure", "published_mismatch"):
            with self.subTest(mode=mode):
                result, mutations = self.invoke(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotEqual(mutations, "")


if __name__ == "__main__":
    unittest.main()

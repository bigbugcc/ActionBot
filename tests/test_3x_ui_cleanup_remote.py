"""Keep the remote deletion exercise confined to its own temporary tag."""

from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("remote_cleanup", ROOT / "tests/verify_3x_ui_cleanup_remote.py")
remote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(remote)


class Response(io.BytesIO):
    def __init__(self, data, status=200, headers=None):
        super().__init__(data if isinstance(data, bytes) else json.dumps(data).encode())
        self.status, self.headers = status, headers or {}


class RegistryWire:
    def __init__(self, failed_put=False, delayed_delete=False, timed_out_put=False):
        self.manifest = json.dumps({"mediaType": "application/vnd.oci.image.index.v1+json",
                                    "schemaVersion": 2, "manifests": []}).encode()
        self.digest = "sha256:" + hashlib.sha256(self.manifest).hexdigest()
        self.names = {"latest", "v3.11.0", "3.11.0", "dev"}
        self.names.update(f"dev-{index:03d}" for index in range(96))
        self.original = set(self.names)
        self.writes = []
        self.failed_put, self.delayed_delete = failed_put, delayed_delete
        self.timed_out_put = timed_out_put

    def open(self, request, timeout):
        url, method = urlparse(request.full_url), request.get_method()
        name = url.path.rstrip("/").split("/")[-1]
        if url.path in ("/v2/auth/token", "/token"):
            return Response({"token": "registry-token", "access_token": "hub-token"})
        if method in ("PUT", "DELETE"):
            self.writes.append((method, name))
            if method == "PUT":
                self.names.add(name)
                if self.timed_out_put:
                    raise TimeoutError("Main phase timed out after PUT")
                if self.failed_put:
                    raise HTTPError(request.full_url, 503, "Ambiguous PUT", {}, None)
                return Response({}, status=201)
            if self.delayed_delete:
                self.delayed_delete = False
                raise HTTPError(request.full_url, 404, "Stale Hub", {}, None)
            self.names.discard(name)
            return Response({}, status=204)
        if url.path.endswith("/tags/list"):
            names = sorted(self.names)
            last = parse_qs(url.query).get("last", [""])[0]
            names = [name for name in names if name > last]
            headers = {}
            if len(names) > 100:
                headers["Link"] = f'<{url.path}?n=100&last={names[99]}>; rel="next"'
            return Response({"name": "bigbugcc/3x-ui", "tags": names[:100]}, headers=headers)
        if name not in self.names:
            raise HTTPError(request.full_url, 404, "Not Found", {}, None)
        if url.netloc == "registry-1.docker.io":
            return Response(self.manifest, headers={"Docker-Content-Digest": self.digest})
        return Response({"name": name, "digest": self.digest, "tag_last_pushed": "2026-10-26T01:00:00Z"})


class RemoteSafetyTests(unittest.TestCase):
    def test_transport_rejects_writes_to_any_preexisting_tag(self):
        opener = Mock()
        guard = remote.BoundedOpener(opener, {"build-123-1"}, time.monotonic() + 10)
        for method, url in (
                ("DELETE", "https://hub.docker.com/v2/namespaces/bigbugcc/repositories/3x-ui/tags/latest/"),
                ("PUT", "https://registry-1.docker.io/v2/bigbugcc/3x-ui/manifests/v3.11.0"),
                ("DELETE", "https://registry-1.docker.io/v2/bigbugcc/3x-ui/manifests/sha256:abc"),
                ("GET", "https://hub.docker.com/v2/namespaces/bigbugcc/repositories/3x-ui/tags/")):
            with self.subTest(method=method, url=url), self.assertRaisesRegex(ValueError, "permitted"):
                guard.open(Request(url, method=method))
        opener.open.assert_not_called()

    def test_expired_main_deadline_does_not_consume_cleanup_deadline(self):
        opener = Mock()
        guard = remote.BoundedOpener(opener, {"build-123-1"}, time.monotonic() - 1)
        request = Request("https://hub.docker.com/v2/namespaces/bigbugcc/repositories/3x-ui/tags/build-123-1/",
                          method="DELETE")
        with self.assertRaisesRegex(ValueError, "deadline"):
            guard.open(request)
        opener.open.assert_not_called()
        guard.deadline = time.monotonic() + 10
        guard.open(request)
        self.assertEqual(opener.open.call_count, 1)

    def exercise(self, wire, delete=True):
        env = {"GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
               "DOCKERHUB_USERNAME": "example", "DOCKERHUB_TOKEN": "secret"}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(remote, "ROOT", Path(directory)), \
                    patch.object(remote, "PhaseTimer"), \
                    patch.object(remote.retention, "build_opener", return_value=wire), \
                    patch.dict(os.environ, env), patch.object(remote.time, "sleep"), \
                    patch.object(remote.retention.sys, "argv", ["verify"] + (["--exercise-delete"] if delete else [])), \
                    redirect_stdout(io.StringIO()):
                try:
                    remote.main()
                    error = None
                except Exception as caught:
                    error = caught
            report = json.loads((Path(directory) / "diagnostics/3x-ui-cleanup/report.json").read_text())
        return report, error

    def test_full_exercise_deletes_only_owned_staging_and_preserves_shared_aliases(self):
        wire = RegistryWire()
        report, error = self.exercise(wire)
        self.assertIsNone(error)
        self.assertEqual(report["deleted"], ["build-123-1"])
        self.assertEqual(report["count_after_push"], 101)
        self.assertEqual(report["registry_pages_after_push"], 2)
        self.assertTrue(report["cleanup_verified"])
        self.assertEqual(wire.names, wire.original)
        self.assertEqual({name for _, name in wire.writes}, {"build-123-1"})

    def test_ambiguous_put_still_removes_owned_tag_and_reports_original_failure(self):
        wire = RegistryWire(failed_put=True)
        report, error = self.exercise(wire)
        self.assertIsInstance(error, HTTPError)
        self.assertIn("503", report["error"])
        self.assertTrue(report["cleanup_verified"])
        self.assertEqual(wire.names, wire.original)

    def test_hub_delete_404_is_not_accepted_while_registry_tag_still_exists(self):
        wire = RegistryWire(failed_put=True, delayed_delete=True)
        report, error = self.exercise(wire)
        self.assertIsInstance(error, HTTPError)
        self.assertTrue(report["cleanup_verified"])
        self.assertEqual(wire.writes.count(("DELETE", "build-123-1")), 2)
        self.assertEqual(wire.names, wire.original)

    def test_default_preview_never_creates_or_deletes_tags(self):
        wire = RegistryWire()
        report, error = self.exercise(wire, delete=False)
        self.assertIsNone(error)
        self.assertEqual(wire.writes, [])
        self.assertEqual(wire.names, wire.original)
        self.assertFalse(report["exercise_delete"])

    def test_small_baseline_cannot_claim_remote_pagination_was_exercised(self):
        wire = RegistryWire()
        wire.names = {"latest", "v3.11.0", "3.11.0"}
        report, error = self.exercise(wire)
        self.assertIsInstance(error, ValueError)
        self.assertIn("at least 100", report["error"])
        self.assertEqual(wire.writes, [])

    def test_main_phase_timeout_after_put_still_cleans_up(self):
        wire = RegistryWire(timed_out_put=True)
        report, error = self.exercise(wire)
        self.assertIsInstance(error, TimeoutError)
        self.assertTrue(report["cleanup_verified"])
        self.assertEqual(wire.names, wire.original)

    @unittest.skipUnless(hasattr(signal, "setitimer"), "POSIX timer is exercised on the Linux runner")
    def test_process_deadline_interrupts_a_slow_response_body(self):
        class SlowResponse:
            def read(self):
                for _ in range(20):
                    time.sleep(0.01)
                return b"{}"

        timer = remote.PhaseTimer()
        try:
            timer.arm(0.05)
            with self.assertRaises(TimeoutError):
                json.load(SlowResponse())
        finally:
            timer.close()


if __name__ == "__main__":
    unittest.main()

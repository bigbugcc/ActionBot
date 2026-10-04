"""Exercise tag retention and Hub API failures without deleting real tags."""

from contextlib import redirect_stdout
from datetime import datetime, timezone
import importlib.util
import io
import os
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cleanup_tags", ROOT / "bin/3x-ui/cleanup_tags.py")
retention = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retention)
DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64
CUTOFF = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)


def tag(name, day=1, digest=DIGEST):
    return {"name": name, "tag_last_pushed": f"2026-10-{day:02d}T13:00:00Z", "digest": digest}


def versions(count=12):
    return {name: tag(name, patch_number + 1)
            for patch_number in range(count) for name in (f"v3.0.{patch_number}", f"3.0.{patch_number}")}


class FakeHub:
    def __init__(self, tags):
        self.tags = tags
        self.reads = []
        self.deleted = []
        self.changed = None
        self.fail_read = None
        self.fail_delete = None

    def list_tags(self):
        return self.tags

    def get_tag(self, name):
        self.reads.append(name)
        if name == self.fail_read:
            raise ValueError("API unavailable")
        item = dict(self.tags[name])
        if name == self.changed:
            item["digest"] = OTHER
        return item

    def delete_tag(self, name):
        if name == self.fail_delete:
            raise ValueError("HTTP 403")
        self.deleted.append(name)


class RetentionTests(unittest.TestCase):
    def test_ten_tags_not_ten_versions_with_latest_and_unknown_preserved(self):
        tags = versions()
        tags.update({name: tag(name) for name in ("latest", "dev", "sha-123456789abc", "v3.1.0-rc.1")})
        kept, removed = retention.plan_cleanup(tags, 10)
        self.assertEqual(set(kept), {name for i in range(7, 12) for name in (f"v3.0.{i}", f"3.0.{i}")})
        self.assertEqual(len(removed), 14)
        self.assertTrue(all(retention.VERSION.fullmatch(name) for name in removed))

    def test_rebuilt_historical_version_is_retained_by_push_time(self):
        tags = versions()
        tags["v1.0.0"] = tag("v1.0.0", 20)
        tags["1.0.0"] = tag("1.0.0", 20)
        kept, _ = retention.plan_cleanup(tags, 10, ["v1.0.0", "1.0.0"], DIGEST)
        self.assertEqual(len(kept), 10)
        self.assertTrue({"v1.0.0", "1.0.0"}.issubset(kept))

    def test_current_and_old_staging_deleted_but_newer_staging_preserved(self):
        tags = versions(2)
        tags.update({name: tag(name, day) for name, day in
                     (("build-123-1", 1), ("build-456-2", 5), ("build-789-1", 6), ("build-custom", 1))})
        _, removed = retention.plan_cleanup(tags, 10, digest=DIGEST, staging="build-456-2", cutoff=CUTOFF)
        self.assertEqual(removed, ["build-123-1", "build-456-2"])

    def test_small_repository_never_deletes_version_tags(self):
        self.assertEqual(retention.plan_cleanup(versions(2), 10)[1], [])

    def test_invalid_push_time_or_retention_count_fails_closed(self):
        for timestamp in (None, "bad", "2026-10-01T00:00:00"):
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                retention.plan_cleanup({"v3.0.0": {"tag_last_pushed": timestamp}}, 10)
        for keep in (0, -1):
            with self.subTest(keep=keep), self.assertRaises(ValueError):
                retention.plan_cleanup(versions(), keep)

    def test_legacy_last_updated_fallback_and_timestamp_offsets(self):
        tags = {"v3.0.0": {"last_updated": "2026-10-01T00:00:00+08:00"},
                "v3.1.0": {"tag_last_pushed": "2026-09-30T18:00:00Z"}}
        self.assertEqual(retention.plan_cleanup(tags, 1), (["v3.1.0"], ["v3.0.0"]))

    def test_missing_mismatched_or_outside_window_publication_stops_deletion(self):
        for published, digest in ((["v9.9.9"], DIGEST), (["v3.0.11"], OTHER), (["v3.0.0"], DIGEST)):
            client = FakeHub(versions())
            with self.subTest(published=published, digest=digest), self.assertRaises(ValueError):
                retention.cleanup(client, 10, published, digest, apply=True)
            self.assertEqual(client.deleted, [])

    def test_mismatched_current_staging_stops_deletion(self):
        tags = versions()
        tags["build-123-1"] = tag("build-123-1", digest=OTHER)
        with self.assertRaises(ValueError):
            retention.plan_cleanup(tags, 10, digest=DIGEST, staging="build-123-1", cutoff=CUTOFF)

    def test_dry_run_is_read_only(self):
        client = FakeHub(versions())
        with redirect_stdout(io.StringIO()):
            kept, removed = retention.cleanup(client, 10)
        self.assertEqual(len(kept), 10)
        self.assertEqual(len(removed), 14)
        self.assertEqual(client.deleted, [])
        self.assertEqual(client.reads, [])

    def test_changed_tag_or_failed_preflight_never_deletes_anything(self):
        for failure in ("changed", "fail_read"):
            client = FakeHub(versions())
            setattr(client, failure, "v3.0.0")
            with self.subTest(failure=failure), redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                retention.cleanup(client, 10, apply=True)
            self.assertEqual(client.deleted, [])

    def test_success_checks_all_candidates_and_only_deletes_names(self):
        client = FakeHub(versions())
        with redirect_stdout(io.StringIO()):
            _, removed = retention.cleanup(client, 10, apply=True)
        self.assertEqual(client.reads, removed)
        self.assertEqual(client.deleted, removed)
        self.assertTrue(all("sha256:" not in name for name in client.deleted))

    def test_delete_failure_stops_remaining_deletions(self):
        client = FakeHub(versions())
        client.fail_delete = "3.0.1"
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "403"):
            retention.cleanup(client, 10, apply=True)
        self.assertEqual(client.deleted, ["3.0.0"])


class HubApiTests(unittest.TestCase):
    def client(self, pages):
        client = retention.HubClient("example/3x-ui")
        client.calls = []

        def request(method, path, data=None):
            client.calls.append((method, path, data))
            if isinstance(pages[path], Exception):
                raise pages[path]
            return pages[path]

        client.request = request
        return client

    def test_all_pages_are_collected_before_planning(self):
        path = "/v2/namespaces/example/repositories/3x-ui/tags/"
        client = self.client({path + "?page_size=100": {"count": 2, "results": [tag("v3.0.0")],
                              "next": retention.HUB + path + "?page=2"},
                              path + "?page=2": {"count": 2, "results": [tag("v3.1.0")], "next": None}})
        self.assertEqual(set(client.list_tags()), {"v3.0.0", "v3.1.0"})
        self.assertEqual(len(client.calls), 2)

    def test_bad_pagination_and_incomplete_list_fail_before_delete(self):
        path = "/v2/namespaces/example/repositories/3x-ui/tags/"
        for next_url in ("https://evil.example/tags/", retention.HUB + "/v2/namespaces/other/repositories/image/tags/",
                         retention.HUB + path + "?page_size=100"):
            client = self.client({path + "?page_size=100": {"count": 2, "results": [tag("v3.0.0")], "next": next_url}})
            with self.subTest(next_url=next_url), self.assertRaises(ValueError):
                retention.cleanup(client, 10, apply=True)
            self.assertTrue(all(call[0] == "GET" for call in client.calls))
        client = self.client({path + "?page_size=100": {"count": 2, "results": [tag("v3.0.0")], "next": None}})
        with self.assertRaisesRegex(ValueError, "incomplete"):
            client.list_tags()

    def test_duplicate_tags_and_page_failure_fail_before_delete(self):
        path = "/v2/namespaces/example/repositories/3x-ui/tags/"
        for second_page in ({"count": 2, "results": [tag("v3.0.0")], "next": None}, ValueError("HTTP 429")):
            client = self.client({path + "?page_size=100": {"count": 2, "results": [tag("v3.0.0")],
                                  "next": retention.HUB + path + "?page=2"}, path + "?page=2": second_page})
            with self.subTest(second_page=second_page), self.assertRaises(ValueError):
                retention.cleanup(client, 10, apply=True)
            self.assertTrue(all(call[0] == "GET" for call in client.calls))

    def test_login_uses_documented_token_exchange_and_masks_bearer(self):
        client = self.client({"/v2/auth/token": {"access_token": "derived-secret"}})
        output = io.StringIO()
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), redirect_stdout(output):
            client.login("example", "pat-secret")
        self.assertEqual(client.calls, [("POST", "/v2/auth/token", {"identifier": "example", "secret": "pat-secret"})])
        self.assertEqual(client.token, "derived-secret")
        self.assertIn("::add-mask::derived-secret", output.getvalue())

    def test_delete_uses_namespace_scoped_tag_url_not_shared_digest(self):
        path = "/v2/namespaces/example/repositories/3x-ui/tags/v3.0.0/"
        client = self.client({path: None})
        client.delete_tag("v3.0.0")
        self.assertEqual(client.calls, [("DELETE", path, None)])

    def test_http_delete_error_is_reported_without_response_body_or_secrets(self):
        client = retention.HubClient("example/3x-ui")
        client.token = "bearer-secret"
        error = HTTPError("https://hub.docker.com/", 403, "Forbidden", {}, io.BytesIO(b"pat-secret"))
        with patch.object(client.opener, "open", side_effect=error), self.assertRaises(ValueError) as raised:
            client.delete_tag("v3.0.0")
        self.assertIn("HTTP 403", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))

    def test_apply_without_verified_publication_never_authenticates_or_deletes(self):
        for published in ("", "example/3x-ui:latest", "other/image:v3.9.0"):
            env = {"IMAGE_REPO": "example/3x-ui", "PUBLISH_TAGS": published,
                   "IMAGE_DIGEST": DIGEST, "STAGING_TAG": "build-123-1",
                   "BUILD_DATE": "2026-10-04T12:00:00Z"}
            with self.subTest(published=published), patch.dict(os.environ, env, clear=True), \
                    patch.object(retention.sys, "argv", ["cleanup_tags.py", "--apply"]), \
                    patch.object(retention.HubClient, "login") as login, \
                    patch.object(retention.HubClient, "delete_tag") as delete, self.assertRaises(ValueError):
                retention.main()
            login.assert_not_called()
            delete.assert_not_called()

    def test_malformed_listing_is_rejected(self):
        path = "/v2/namespaces/example/repositories/3x-ui/tags/?page_size=100"
        for page in (None, [], {"count": 1, "results": [None]}, {"count": -1, "results": []}):
            client = self.client({path: page})
            with self.subTest(page=page), self.assertRaises(ValueError):
                client.list_tags()


if __name__ == "__main__":
    unittest.main()

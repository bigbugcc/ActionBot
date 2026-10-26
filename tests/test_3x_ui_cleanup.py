"""Exercise tag retention and Hub API failures without deleting real tags."""

from contextlib import redirect_stdout
from datetime import datetime, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
from unittest.mock import Mock, patch
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
        client.registry = Mock()
        client.registry.get_digest.return_value = DIGEST

        def request(method, path, data=None):
            client.calls.append((method, path, data))
            if isinstance(pages[path], Exception):
                raise pages[path]
            return pages[path]

        client.request = request
        return client

    def test_registry_names_and_individual_metadata_replace_hub_listing(self):
        path = "/v2/namespaces/example/repositories/3x-ui/tags/"
        client = self.client({path + "v3.0.0/": tag("v3.0.0"),
                              path + "build-123-1/": tag("build-123-1"),
                              path + "latest/": tag("latest"),
                              path + "?page_size=100": {"count": 0, "results": [], "next": None}})
        client.registry = Mock()
        client.registry.get_digest.return_value = DIGEST
        names = ["v3.0.0", "build-123-1", "latest", "dev"]
        client.registry.list_tags.return_value = names
        tags = client.list_tags()
        self.assertEqual(set(tags), set(names))
        self.assertEqual(tags["v3.0.0"]["digest"], DIGEST)
        self.assertEqual(tags["dev"], {"name": "dev"})
        self.assertTrue(all("?page" not in call[1] for call in client.calls))
        self.assertEqual(len(client.calls), 3)

    def test_changed_registry_snapshot_stops_before_delete(self):
        path = "/v2/namespaces/example/repositories/3x-ui/tags/"
        client = self.client({path + "v3.0.0/": tag("v3.0.0")})
        client.registry = Mock()
        client.registry.get_digest.return_value = DIGEST
        client.registry.list_tags.side_effect = [["v3.0.0"], ["v3.0.0", "v3.1.0"]]
        with self.assertRaisesRegex(ValueError, "changed"):
            retention.cleanup(client, 10, apply=True)
        self.assertTrue(all(call[0] == "GET" for call in client.calls))

    def test_invalid_or_missing_individual_metadata_never_deletes(self):
        path = "/v2/namespaces/example/repositories/3x-ui/tags/v3.0.0/"
        for detail in (None, tag("v9.9.9"), {"name": "v3.0.0"},
                       tag("v3.0.0", digest="invalid"),
                       {**tag("v3.0.0"), "tag_last_pushed": None}, ValueError("HTTP 404")):
            client = self.client({path: detail})
            client.registry = Mock()
            client.registry.list_tags.return_value = ["v3.0.0"]
            with self.subTest(detail=detail), self.assertRaises(ValueError):
                retention.cleanup(client, 10, apply=True)
            self.assertTrue(all(call[0] == "GET" for call in client.calls))

    def test_registry_digest_change_is_detected_before_deletion(self):
        path = "/v2/namespaces/example/repositories/3x-ui/tags/"
        client = self.client({path + name + "/": item for name, item in versions(2).items()})
        client.registry = Mock()
        client.registry.list_tags.return_value = list(versions(2))
        client.registry.get_digest.return_value = OTHER
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "digest"):
            retention.cleanup(client, 1, apply=True)
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

class Response(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(json.dumps(data).encode())
        self.headers = headers or {}


class RegistryApiTests(unittest.TestCase):
    def client(self, pages):
        client = retention.RegistryClient("example/3x-ui")
        client.token = "registry-token"
        self.calls = []

        def open_request(request, timeout):
            self.calls.append(request)
            page = pages[request.full_url]
            if isinstance(page, Exception):
                raise page
            return Response(*page)

        client.opener.open = open_request
        return client

    def test_registry_pagination_collects_more_than_one_hundred_tags(self):
        url = "https://registry-1.docker.io/v2/example/3x-ui/tags/list?n=100"
        next_url = "/v2/example/3x-ui/tags/list?last=v3.0.99&n=100"
        first = [f"v3.0.{i}" for i in range(100)]
        client = self.client({url: ({"name": "example/3x-ui", "tags": first},
                                   {"Link": f'<{next_url}>; rel="next"'}),
                              "https://registry-1.docker.io" + next_url:
                              ({"name": "example/3x-ui", "tags": ["v3.1.0"]}, {})})
        self.assertEqual(set(client.list_tags()), set(first + ["v3.1.0"]))
        self.assertEqual(len(self.calls), 2)

    def test_untrusted_looping_or_malformed_links_are_rejected(self):
        url = "https://registry-1.docker.io/v2/example/3x-ui/tags/list?n=100"
        links = [f'<{url}>; rel="next"', '<https://evil.example/tags>; rel="next"',
                 '<https://registry-1.docker.io/v2/other/image/tags/list?n=100>; rel="next"',
                 '<http://registry-1.docker.io/v2/example/3x-ui/tags/list?n=2>; rel="next"',
                 'invalid', '</v2/example/3x-ui/tags/list?n=2#fragment>; rel="next"']
        for link in links:
            client = self.client({url: ({"name": "example/3x-ui", "tags": ["v3.0.0"]}, {"Link": link})})
            with self.subTest(link=link), self.assertRaises(ValueError):
                client.list_tags()
            self.assertLessEqual(len(self.calls), 1)

    def test_duplicate_invalid_or_wrong_repository_records_are_rejected(self):
        url = "https://registry-1.docker.io/v2/example/3x-ui/tags/list?n=100"
        for page in (None, [], {"name": "other/image", "tags": []},
                     {"name": "example/3x-ui", "tags": ["v3.0.0", "v3.0.0"]},
                     {"name": "example/3x-ui", "tags": ["../latest"]},
                     {"name": "example/3x-ui", "tags": "latest"}):
            client = self.client({url: (page, {})})
            with self.subTest(page=page), self.assertRaises(ValueError):
                client.list_tags()

    def test_later_page_failure_does_not_return_partial_results(self):
        url = "https://registry-1.docker.io/v2/example/3x-ui/tags/list?n=100"
        next_url = url + "&last=v3.0.0"
        client = self.client({url: ({"name": "example/3x-ui", "tags": ["v3.0.0"]},
                                   {"Link": f'<{next_url}>; rel="next"'}),
                              next_url: HTTPError(next_url, 429, "Too Many Requests", {}, None)})
        with self.assertRaisesRegex(ValueError, "429"):
            client.list_tags()

    def test_digest_head_preserves_multiarch_content_negotiation(self):
        url = "https://registry-1.docker.io/v2/example/3x-ui/manifests/latest"
        client = self.client({url: ({}, {"Docker-Content-Digest": DIGEST})})
        self.assertEqual(client.get_digest("latest"), DIGEST)
        self.assertEqual(self.calls[0].get_method(), "HEAD")
        self.assertIn("application/vnd.oci.image.index.v1+json", self.calls[0].get_header("Accept"))

    def test_authentication_scopes_credentials_to_docker_auth_service(self):
        client = retention.RegistryClient("example/3x-ui")
        calls = []

        def open_request(request, timeout):
            calls.append(request)
            return Response({"token": "pull-token"})

        client.opener.open = open_request
        client.login("example", "pat-secret")
        self.assertEqual(client.token, "pull-token")
        self.assertTrue(calls[0].full_url.startswith("https://auth.docker.io/token?"))
        self.assertIn("repository%3Aexample%2F3x-ui%3Apull", calls[0].full_url)
        self.assertTrue(calls[0].get_header("Authorization").startswith("Basic "))


if __name__ == "__main__":
    unittest.main()

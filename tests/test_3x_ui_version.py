"""Check version selection and source pinning without contacting GitHub."""

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("resolve_version", ROOT / "bin/3x-ui/resolve_version.py")
resolver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resolver)
COMMIT = "a" * 40
TAG_OBJECT = "b" * 40


class VersionTests(unittest.TestCase):
    def api(self, selected="v3.9.0", annotated=False):
        data = {
            "releases/latest": {"tag_name": "v3.9.0", "draft": False, "prerelease": False},
            f"git/ref/tags/{selected}": {
                "ref": f"refs/tags/{selected}",
                "object": {"type": "tag" if annotated else "commit", "sha": TAG_OBJECT if annotated else COMMIT},
            },
            f"git/tags/{TAG_OBJECT}": {"object": {"type": "commit", "sha": COMMIT}},
        }
        return data

    def test_default_empty_and_latest_choose_official_release(self):
        for requested in ("", "  ", "latest"):
            with self.subTest(requested=requested):
                result = resolver.resolve_version(requested, self.api().__getitem__)
                self.assertEqual(result, {"tag": "v3.9.0", "sha": COMMIT, "latest": "true"})

    def test_historical_version_never_promotes_latest(self):
        for requested in ("v3.8.0", "3.8.0", " v3.8.0 "):
            with self.subTest(requested=requested):
                result = resolver.resolve_version(requested, self.api("v3.8.0").__getitem__)
                self.assertEqual(result["tag"], "v3.8.0")
                self.assertEqual(result["latest"], "false")

    def test_explicit_latest_version_can_promote_latest(self):
        self.assertEqual(resolver.resolve_version("v3.9.0", self.api().__getitem__)["latest"], "true")

    def test_annotated_tag_pins_commit_instead_of_tag_object(self):
        self.assertEqual(resolver.resolve_version("", self.api(annotated=True).__getitem__)["sha"], COMMIT)

    def test_branch_commit_and_malformed_inputs_fail_before_api(self):
        def unexpected_api(path):
            self.fail(f"Invalid input reached API: {path}")
        for requested in ("main", COMMIT, "v3.9", "v03.9.0", "v3.9.0-rc.1", "v3.9.0\nx=evil"):
            with self.subTest(requested=requested), self.assertRaises(ValueError):
                resolver.resolve_version(requested, unexpected_api)

    def test_missing_tag_or_api_failure_does_not_fall_back(self):
        def unavailable(path):
            raise ValueError("API unavailable")
        with self.assertRaisesRegex(ValueError, "API unavailable"):
            resolver.resolve_version("", unavailable)
        data = self.api()
        def missing_tag(path):
            if path not in data:
                raise ValueError("Official tag returned HTTP 404")
            return data[path]
        with self.assertRaisesRegex(ValueError, "HTTP 404"):
            resolver.resolve_version("v0.0.1", missing_tag)

    def test_invalid_latest_release_is_rejected(self):
        for update in ({"prerelease": True}, {"draft": True}, {"tag_name": "main"}):
            with self.subTest(update=update):
                data = self.api()
                data["releases/latest"].update(update)
                with self.assertRaises(ValueError):
                    resolver.resolve_version("", data.__getitem__)

    def test_invalid_tag_reference_and_non_commit_objects_are_rejected(self):
        for change in ({"ref": "refs/heads/v3.9.0"}, {"object": {"type": "tree", "sha": COMMIT}},
                       {"object": {"type": "commit", "sha": "not-a-sha"}}):
            with self.subTest(change=change):
                data = self.api()
                data["git/ref/tags/v3.9.0"].update(change)
                with self.assertRaises(ValueError):
                    resolver.resolve_version("", data.__getitem__)

    def test_cyclic_annotated_tag_is_rejected(self):
        data = self.api(annotated=True)
        data[f"git/tags/{TAG_OBJECT}"]["object"] = {"type": "tag", "sha": TAG_OBJECT}
        with self.assertRaisesRegex(ValueError, "Too many"):
            resolver.resolve_version("", data.__getitem__)


if __name__ == "__main__":
    unittest.main()

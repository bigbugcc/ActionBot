"""Preview real retention; optionally exercise deletion of one run-owned staging tag."""

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cleanup_tags", ROOT / "bin/3x-ui/cleanup_tags.py")
retention = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retention)
REPOSITORY = "bigbugcc/3x-ui"


class PhaseTimer:
    def __init__(self):
        if not hasattr(signal, "setitimer"):
            raise ValueError("Remote validation requires a Linux/POSIX runner")
        self.previous = signal.signal(signal.SIGALRM, self.expired)

    @staticmethod
    def expired(signum, frame):
        raise TimeoutError("Remote validation phase deadline exceeded")

    def arm(self, seconds):
        signal.setitimer(signal.ITIMER_REAL, seconds)

    def close(self):
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, self.previous)


class BoundedOpener:
    def __init__(self, opener, owned, deadline):
        self.opener, self.owned, self.deadline = opener, owned, deadline
        self.requests = 0
        self.tag_pages = 0

    def open(self, request, timeout=30):
        parsed, method = urlparse(request.full_url), request.get_method()
        hub_tags = f"/v2/namespaces/{REPOSITORY.replace('/', '/repositories/')}/tags/"
        allowed = False
        if parsed.netloc == "hub.docker.com":
            allowed = ((method == "POST" and parsed.path == "/v2/auth/token")
                       or (method == "GET" and parsed.path.startswith(hub_tags)
                           and retention.TAG.fullmatch(parsed.path[len(hub_tags):].rstrip("/")))
                       or (method == "DELETE" and parsed.path in {hub_tags + name + "/" for name in self.owned}))
        elif parsed.netloc == "auth.docker.io":
            allowed = method == "GET" and parsed.path == "/token"
        elif parsed.netloc == "registry-1.docker.io":
            manifests = f"/v2/{REPOSITORY}/manifests/"
            allowed = ((method == "GET" and parsed.path == f"/v2/{REPOSITORY}/tags/list")
                       or (method in ("GET", "HEAD") and parsed.path.startswith(manifests)
                           and retention.TAG.fullmatch(parsed.path[len(manifests):]))
                       or (method == "PUT" and parsed.path in {manifests + name for name in self.owned}))
        if parsed.scheme != "https" or not allowed:
            raise ValueError("Remote validation request outside permitted endpoints")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("Remote validation deadline exceeded")
        self.requests += 1
        if method == "GET" and parsed.path == f"/v2/{REPOSITORY}/tags/list":
            self.tag_pages += 1
        return self.opener.open(request, timeout=min(timeout, 8, remaining))


class GuardedHub(retention.HubClient):
    def __init__(self):
        super().__init__(REPOSITORY)
        self.owned, self.snapshot = set(), None
        deadline = time.monotonic() + 360
        self.opener = BoundedOpener(self.opener, self.owned, deadline)
        self.registry.opener = BoundedOpener(self.registry.opener, self.owned, deadline)

    def list_tags(self):
        self.snapshot = super().list_tags()
        return self.snapshot

    def delete_tag(self, name):
        if name not in self.owned:
            raise ValueError("Refusing to delete a tag not created by this validation")
        super().delete_tag(name)


def absent(opener, url, headers, method="GET"):
    try:
        with opener.open(Request(url, headers=headers, method=method)):
            return False
    except HTTPError as error:
        if error.code == 404:
            return True
        raise ValueError(f"Absence check returned HTTP {error.code}") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exercise-delete", action="store_true")
    args = parser.parse_args()
    client = GuardedHub()
    report = {"repository": REPOSITORY, "sha": os.environ["GITHUB_SHA"],
              "exercise_delete": args.exercise_delete, "cleanup_verified": False}
    target = ROOT / "diagnostics/3x-ui-cleanup/report.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary, baseline, attempted = None, None, False
    timer = PhaseTimer()
    timer.arm(360)
    try:
        client.login(os.environ["DOCKERHUB_USERNAME"], os.environ["DOCKERHUB_TOKEN"])
        retained, planned = retention.cleanup(client, 10)
        baseline = client.snapshot
        report.update(registry_count=len(baseline), retained=retained, planned=planned)
        if not args.exercise_delete:
            report["cleanup_verified"] = True
            return
        if len(baseline) < 100:
            raise ValueError("Need at least 100 existing tags to exercise Registry pagination with one temporary tag")

        run, attempt = os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"]
        if not re.fullmatch(r"[1-9][0-9]*", run) or not re.fullmatch(r"[1-9][0-9]*", attempt):
            raise ValueError("Invalid workflow run identity")
        temporary = f"build-{run}-{attempt}"
        report["temporary_tag"] = temporary
        baseline_digests = {name: item.get("digest") or client.registry.get_digest(name)
                            for name, item in baseline.items()}
        digest = baseline_digests["latest"]
        aliases = [name for name in retained if baseline_digests[name] == digest]
        if len(aliases) < 2:
            raise ValueError("Need two retained version aliases sharing latest's digest")
        source = aliases[0]
        hub_headers = {"Authorization": "Bearer " + client.token}
        registry_headers = {"Authorization": "Bearer " + client.registry.token,
                            "Accept": retention.MANIFEST_TYPES}
        hub_url = retention.HUB + client.tags_path + temporary + "/"
        registry_url = retention.REGISTRY + f"/v2/{REPOSITORY}/manifests/{temporary}"
        if (temporary in baseline or not absent(client.opener, hub_url, hub_headers)
                or not absent(client.registry.opener, registry_url, registry_headers, "HEAD")):
            raise ValueError("Validation staging tag already exists; refusing to overwrite it")

        query = urlencode({"service": "registry.docker.io", "scope": f"repository:{REPOSITORY}:pull,push"})
        basic = base64.b64encode((os.environ["DOCKERHUB_USERNAME"] + ":"
                                 + os.environ["DOCKERHUB_TOKEN"]).encode()).decode()
        with client.registry.opener.open(Request(retention.AUTH + "?" + query,
                                                 headers={"Authorization": "Basic " + basic})) as response:
            token_result = json.load(response)
        push_token = token_result.get("token") or token_result.get("access_token")
        if not isinstance(push_token, str) or not push_token:
            raise ValueError("Registry returned no validation push token")
        print("::add-mask::" + push_token, flush=True)
        source_url = retention.REGISTRY + f"/v2/{REPOSITORY}/manifests/{source}"
        with client.registry.opener.open(Request(source_url, headers=registry_headers)) as response:
            manifest = response.read(5_000_001)
        if len(manifest) > 5_000_000 or "sha256:" + hashlib.sha256(manifest).hexdigest() != digest:
            raise ValueError("Source manifest changed; no validation tag created")
        media_type = json.loads(manifest).get("mediaType")
        if media_type not in ("application/vnd.oci.image.index.v1+json",
                              "application/vnd.docker.distribution.manifest.list.v2+json"):
            raise ValueError("Source must be a multi-platform index")
        client.owned.add(temporary)
        attempted = True  # An ambiguous failed PUT must also enter finally cleanup.
        with client.registry.opener.open(Request(registry_url, data=manifest, method="PUT",
                                                 headers={"Authorization": "Bearer " + push_token,
                                                          "Content-Type": media_type})) as response:
            if response.status != 201:
                raise ValueError("Validation tag PUT did not return HTTP 201")
        pages_before = client.registry.opener.tag_pages
        names = client.registry.list_tags()
        pages = client.registry.opener.tag_pages - pages_before
        report.update(count_after_push=len(names), registry_pages_after_push=pages,
                      immediate_registry_complete=set(names) == set(baseline) | {temporary})
        if not report["immediate_registry_complete"]:
            raise ValueError("Registry enumeration after push did not include the complete baseline and staging tag")
        if len(names) != len(baseline) + 1 or pages < 2:
            raise ValueError("Remote Registry pagination was not exercised")
        # Hub single-tag metadata may become visible after Registry publication.
        for index in range(6):
            try:
                client.get_tag(temporary)
                break
            except ValueError:
                if index == 5:
                    raise
                time.sleep(2)
        snapshot = client.list_tags()

        class FixtureView:
            def list_tags(self):
                return {name: snapshot[name] for name in ["latest", *aliases, temporary]}

            get_tag = client.get_tag
            delete_tag = client.delete_tag

        _, removed = retention.cleanup(FixtureView(), 10, ["latest", *aliases], digest, temporary,
                                       datetime.now(timezone.utc), apply=True)
        if removed != [temporary]:
            raise ValueError("Unexpected controlled deletion plan")
        report.update(deleted=removed, shared_digest=digest, protected_aliases=["latest", *aliases])
        report["baseline_digests"] = baseline_digests
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        try:
            # Reserve a fresh deadline for cleanup even when the main test timed out.
            timer.arm(120)
            client.opener.deadline = client.registry.opener.deadline = time.monotonic() + 120
            if attempted:
                for index in range(3):
                    client.delete_tag(temporary)
                    if (absent(client.opener, hub_url, hub_headers)
                            and absent(client.registry.opener, registry_url, registry_headers, "HEAD")):
                        break
                    if index == 2:
                        raise ValueError("Owned staging tag still present after cleanup")
                    time.sleep(2)
                names = client.registry.list_tags()
                if set(names) != set(baseline):
                    raise ValueError("Registry names differ from the pre-test baseline")
                actual = {name: client.registry.get_digest(name) for name in names}
                if actual != baseline_digests:
                    raise ValueError("Pre-existing tag digests changed during validation")
                report["cleanup_verified"] = True
        except Exception as error:
            report["cleanup_error"] = f"{type(error).__name__}: {error}"
            raise
        finally:
            timer.close()
            report["requests"] = {"hub": client.opener.requests, "registry": client.registry.opener.requests}
            target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()

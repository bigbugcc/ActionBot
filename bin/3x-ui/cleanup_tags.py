"""Retain recent Docker Hub version tags after a verified publication.

Enumerates Registry tags and reads Hub metadata for push-time retention.
Deletes Hub tags by name rather than deleting shared manifests by digest.
Run without --apply to preview the cleanup; no credentials are needed for a
public repository preview. Unknown tags are outside this workflow's ownership.
"""

import argparse
import base64
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


HUB = "https://hub.docker.com"
REGISTRY = "https://registry-1.docker.io"
AUTH = "https://auth.docker.io/token"
MANIFEST_TYPES = ", ".join(("application/vnd.oci.image.index.v1+json",
                            "application/vnd.docker.distribution.manifest.list.v2+json",
                            "application/vnd.oci.image.manifest.v1+json",
                            "application/vnd.docker.distribution.manifest.v2+json"))
VERSION = re.compile(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
STAGING = re.compile(r"build-[1-9][0-9]*-[1-9][0-9]*")
TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
REPOSITORY = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*/[a-z0-9]+(?:[._-][a-z0-9]+)*")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RegistryClient:
    def __init__(self, repository):
        if not REPOSITORY.fullmatch(repository):
            raise ValueError("IMAGE_REPO must be a Docker Hub namespace/repository")
        self.repository = repository
        self.tags_path = f"/v2/{repository}/tags/list"
        self.token = None
        self.opener = build_opener(NoRedirect())

    def login(self, username=None, secret=None):
        query = urlencode({"service": "registry.docker.io", "scope": f"repository:{self.repository}:pull"})
        headers = {"Accept": "application/json"}
        if username and secret:
            basic = base64.b64encode(f"{username}:{secret}".encode()).decode()
            headers["Authorization"] = "Basic " + basic
        try:
            with self.opener.open(Request(AUTH + "?" + query, headers=headers), timeout=30) as response:
                result = json.load(response)
        except HTTPError as error:
            raise ValueError(f"Registry authentication returned HTTP {error.code}") from None
        token = (result.get("token") or result.get("access_token")) if isinstance(result, dict) else None
        if not isinstance(token, str) or not token:
            raise ValueError("Registry authentication returned no token")
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::add-mask::{token}")
        self.token = token

    def request(self, method, path):
        if not self.token:
            self.login()
        headers = {"Authorization": "Bearer " + self.token,
                   "Accept": MANIFEST_TYPES if method == "HEAD" else "application/json",
                   "User-Agent": "ActionBot-3x-ui"}
        try:
            with self.opener.open(Request(REGISTRY + path, method=method, headers=headers), timeout=30) as response:
                return (None if method == "HEAD" else json.load(response)), response.headers
        except HTTPError as error:
            raise ValueError(f"Registry {method} {path} returned HTTP {error.code}; refusing cleanup") from None

    def list_tags(self):
        path = self.tags_path + "?n=100"
        tags, visited = set(), set()
        while path:
            if path in visited:
                raise ValueError("Registry pagination loop; refusing cleanup")
            visited.add(path)
            page, headers = self.request("GET", path)
            if (not isinstance(page, dict) or page.get("name") != self.repository
                    or not isinstance(page.get("tags"), list)):
                raise ValueError("Invalid Registry tag listing; refusing cleanup")
            for name in page["tags"]:
                if not isinstance(name, str) or not TAG.fullmatch(name) or name in tags:
                    raise ValueError("Invalid or duplicate Registry tag; refusing cleanup")
                tags.add(name)
            link = headers.get("Link")
            path = None
            if link:
                match = re.fullmatch(r'\s*<([^<>]+)>\s*;\s*rel="?next"?\s*', link)
                if not match or not page["tags"]:
                    raise ValueError("Invalid Registry pagination Link; refusing cleanup")
                parsed = urlparse(urljoin(REGISTRY + self.tags_path, match[1]))
                if (parsed.scheme != "https" or parsed.netloc != "registry-1.docker.io"
                        or parsed.path != self.tags_path or parsed.fragment):
                    raise ValueError("Unexpected Registry pagination URL; refusing cleanup")
                path = parsed.path + ("?" + parsed.query if parsed.query else "")
        return sorted(tags)

    def get_digest(self, name):
        if not isinstance(name, str) or not TAG.fullmatch(name):
            raise ValueError("Invalid Registry tag name")
        _, headers = self.request("HEAD", f"/v2/{self.repository}/manifests/{name}")
        digest = headers.get("Docker-Content-Digest", "")
        if not DIGEST.fullmatch(digest):
            raise ValueError(f"Registry tag {name} returned no valid digest; refusing cleanup")
        return digest


class HubClient:
    def __init__(self, repository):
        if not REPOSITORY.fullmatch(repository):
            raise ValueError("IMAGE_REPO must be a Docker Hub namespace/repository")
        namespace, name = repository.split("/")
        self.tags_path = f"/v2/namespaces/{namespace}/repositories/{name}/tags/"
        self.token = None
        self.opener = build_opener(NoRedirect())
        self.registry = RegistryClient(repository)

    def request(self, method, path, data=None):
        headers = {"Accept": "application/json", "User-Agent": "ActionBot-3x-ui"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(HUB + path, method=method, headers=headers,
                          data=json.dumps(data).encode() if data is not None else None)
        try:
            with self.opener.open(request, timeout=30) as response:
                return None if method == "DELETE" else json.load(response)
        except HTTPError as error:
            if method == "DELETE" and error.code == 404:
                return None
            raise ValueError(f"Docker Hub {method} {path} returned HTTP {error.code}; "
                             "cleanup requires a token with Delete permission") from error

    def login(self, username, secret):
        if not username or not secret:
            raise ValueError("DOCKERHUB_USERNAME and DOCKERHUB_TOKEN are required for --apply")
        result = self.request("POST", "/v2/auth/token", {"identifier": username, "secret": secret})
        token = result.get("access_token") if isinstance(result, dict) else None
        if not isinstance(token, str) or not token:
            raise ValueError("Docker Hub authentication returned no access token")
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::add-mask::{token}")
        self.token = token
        self.registry.login(username, secret)

    def list_tags(self):
        names = self.registry.list_tags()
        tags = {}
        for name in names:
            # Unknown tags remain protected without needing their metadata.
            tags[name] = (self.get_tag(name) if name == "latest" or VERSION.fullmatch(name)
                          or STAGING.fullmatch(name) else {"name": name})
        if names != self.registry.list_tags():
            raise ValueError("Registry tag listing changed during metadata reads; refusing cleanup")
        return tags

    def get_tag(self, name):
        if not isinstance(name, str) or not TAG.fullmatch(name):
            raise ValueError("Invalid Docker Hub tag name")
        item = self.request("GET", self.tags_path + name + "/")
        if (not isinstance(item, dict) or item.get("name") != name
                or not isinstance(item.get("digest"), str) or not DIGEST.fullmatch(item["digest"])):
            raise ValueError(f"Invalid Docker Hub metadata for {name}; refusing cleanup")
        pushed_at(item)
        if self.registry.get_digest(name) != item["digest"]:
            raise ValueError(f"Registry and Docker Hub digest differ for {name}; refusing cleanup")
        return item

    def delete_tag(self, name):
        # Tag-name deletion leaves other aliases for the same digest intact.
        self.request("DELETE", self.tags_path + name + "/")


def pushed_at(item):
    value = item.get("tag_last_pushed") or item.get("last_updated")
    return parse_time(value)


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError("Missing tag push time or BUILD_DATE; refusing cleanup")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Tag push time and BUILD_DATE must include a timezone")
    return result


def plan_cleanup(tags, keep, published=(), digest=None, staging=None, cutoff=None):
    if keep < 1:
        raise ValueError("KEEP_TAGS must be positive")
    versions = sorted((name for name in tags if VERSION.fullmatch(name)),
                      key=lambda name: (pushed_at(tags[name]), name), reverse=True)
    retained = versions[:keep]
    for name in published:
        if name not in tags or tags[name].get("digest") != digest:
            raise ValueError(f"Published tag {name} is not visible with the verified digest")
        if name != "latest" and name not in retained:
            raise ValueError(f"Published tag {name} is outside the retention window; refusing cleanup")
    removed = versions[keep:]
    for name, item in tags.items():
        if not STAGING.fullmatch(name):
            continue
        if name == staging:
            if item.get("digest") != digest:
                raise ValueError("Current staging tag has an unexpected digest")
            removed.append(name)
        elif cutoff is not None and pushed_at(item) < cutoff:
            removed.append(name)
    return retained, sorted(removed)


def cleanup(client, keep, published=(), digest=None, staging=None, cutoff=None, apply=False):
    tags = client.list_tags()
    retained, removed = plan_cleanup(tags, keep, published, digest, staging, cutoff)
    print("Retain version tags: " + ", ".join(retained))
    print("Delete tags: " + (", ".join(removed) or "none"))
    if apply:
        # Finish every read/check before the first deletion. Abort if a tag was
        # republished since listing; workflow concurrency serializes our runs.
        for name in removed:
            current = client.get_tag(name)
            if (not isinstance(current, dict) or current.get("name") != name
                    or current.get("digest") != tags[name].get("digest")
                    or pushed_at(current) != pushed_at(tags[name])):
                raise ValueError(f"Tag {name} changed during cleanup; refusing deletion")
        for name in removed:
            client.delete_tag(name)
            print(f"Deleted tag: {name}")
    else:
        print("Dry run: no tags deleted")
    return retained, removed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("IMAGE_REPO", "bigbugcc/3x-ui"))
    parser.add_argument("--keep", type=int, default=int(os.environ.get("KEEP_TAGS", "10")))
    parser.add_argument("--apply", action="store_true", help="Delete planned tags (default is dry run)")
    args = parser.parse_args()
    client = HubClient(args.repo)
    published = []
    for tag in os.environ.get("PUBLISH_TAGS", "").splitlines():
        if not tag.startswith(args.repo + ":") or not TAG.fullmatch(tag[len(args.repo) + 1:]):
            raise ValueError("Unexpected publication tag")
        published.append(tag[len(args.repo) + 1:])
    digest = os.environ.get("IMAGE_DIGEST", "")
    staging = os.environ.get("STAGING_TAG", "")
    cutoff = parse_time(os.environ["BUILD_DATE"]) if os.environ.get("BUILD_DATE") else None
    if args.apply:
        if (not any(VERSION.fullmatch(name) for name in published)
                or not DIGEST.fullmatch(digest) or not STAGING.fullmatch(staging)
                or cutoff is None or any(name != "latest" and not VERSION.fullmatch(name)
                                         for name in published)):
            raise ValueError("Verified publication tags, digest, staging tag and BUILD_DATE are required")
        client.login(os.environ.get("DOCKERHUB_USERNAME"), os.environ.get("DOCKERHUB_TOKEN"))
    retained, removed = cleanup(client, args.keep, published, digest, staging, cutoff, args.apply)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write(f"\n### Docker Hub tag retention\n\n"
                         f"Retained {len(retained)} version tags; `latest` and unknown tags are preserved.\n\n"
                         f"{'Deleted' if args.apply else 'Would delete'} {len(removed)} old version/staging tags.\n")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f"::warning::Docker Hub cleanup failed: {error}", file=sys.stderr)
        sys.exit(1)

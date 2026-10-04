"""Resolve an official version tag to a pinned source commit before building."""

import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen


REPOSITORY = "MHSanaei/3x-ui"
VERSION = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
SHA = re.compile(r"[0-9a-f]{40}")


def github_json(path):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ActionBot-3x-ui",
    }
    token = os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(f"https://api.github.com/repos/{REPOSITORY}/{path}", headers=headers)
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        raise ValueError(f"Official API {path} returned HTTP {error.code}") from error


def resolve_version(requested, fetch_json=github_json):
    version = requested.strip()
    if version and version != "latest":
        version = version if version.startswith("v") else f"v{version}"
        if not VERSION.fullmatch(version):
            raise ValueError("Version must be vX.Y.Z, X.Y.Z, latest, or empty")

    release = fetch_json("releases/latest")
    latest_tag = release.get("tag_name", "")
    if release.get("draft") or release.get("prerelease") or not VERSION.fullmatch(latest_tag):
        raise ValueError("Official latest release must have a stable vX.Y.Z tag")
    if not version or version == "latest":
        version = latest_tag

    reference = fetch_json(f"git/ref/tags/{version}")
    if reference.get("ref") != f"refs/tags/{version}":
        raise ValueError("Official version tag did not match the requested version")
    obj = reference.get("object", {})
    # GitHub's release tags may be annotated; their object SHA is not a commit.
    for _ in range(10):
        sha = obj.get("sha", "")
        if not SHA.fullmatch(sha):
            raise ValueError("Invalid source object SHA")
        if obj.get("type") == "commit":
            return {"tag": version, "sha": sha, "latest": str(version == latest_tag).lower()}
        if obj.get("type") != "tag":
            raise ValueError("Official version tag must point to a commit")
        obj = fetch_json(f"git/tags/{sha}").get("object", {})
    raise ValueError("Too many nested annotated tags")


def main():
    result = resolve_version(os.environ.get("VERSION_TAG", ""))
    output = "".join(f"{key}={value}\n" for key, value in result.items())
    print(f"Official source: {result['tag']} ({result['sha']}), latest={result['latest']}")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(output)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        print(f"Version resolution failed: {error}", file=sys.stderr)
        sys.exit(1)

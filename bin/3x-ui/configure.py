"""Apply checked, minimal patches to the upstream Docker build."""

import os
import re
import stat
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


# Upstream v3.11.0 (c621ba9014a6aa21fd7c9ecf45655e44e24ebf13).
# Check the complete resource mapping before relying on its filenames.
PLATFORM_RESOURCES = '''case $1 in
    amd64 | "")
        PLATFORM="amd64"
        ;;
    arm64 | armv8 | aarch64)
        PLATFORM="arm64"
        ;;
    arm | arm32 | armv7)
        PLATFORM="armv7"
        if [ "$2" = "v6" ]; then
            PLATFORM="armv6"
        fi
        ;;
    armv6)
        PLATFORM="armv6"
        ;;
    386 | i386)
        PLATFORM="386"
        ;;
    *)
        echo "DockerInit: unsupported architecture '$1'" >&2
        exit 1
        ;;
esac
case $PLATFORM in
    amd64) XRAY_ZIP="64" ;;
    arm64) XRAY_ZIP="arm64-v8a" ;;
    armv7) XRAY_ZIP="arm32-v7a" ;;
    armv6) XRAY_ZIP="arm32-v6" ;;
    386) XRAY_ZIP="32" ;;
esac
# The panel looks binaries up by GOARCH; on arm Xray's is "arm32"
# (internal/xray/process.go) and mtg's plain "arm", as install.sh renames them.
case $PLATFORM in
    armv7 | armv6)
        XRAY_BIN="xray-linux-arm32"
        MTG_BIN="mtg-linux-arm"
        ;;
    *)
        XRAY_BIN="xray-linux-${PLATFORM}"
        MTG_BIN="mtg-linux-${PLATFORM}"
        ;;
esac
'''



def replace_once(text, old, new, label):
    if text.count(old) != 1:
        raise ValueError(f"Upstream {label} changed; review the build patch")
    return text.replace(old, new, 1)


def stage_file(path, content):
    """Stage beside the destination so each replacement stays on one filesystem."""
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.actionbot-", dir=path.parent)
    staged = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        staged.chmod(stat.S_IMODE(path.stat().st_mode))
        return staged
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def write_configuration(files):
    # This is not a crash-atomic multi-file transaction. Keep staged originals
    # for recovery if a later replacement fails during this invocation.
    staged, backups, replaced, retained = {}, {}, [], set()
    try:
        for path, content in files.items():
            staged[path] = stage_file(path, content.encode("utf-8"))
            backups[path] = stage_file(path, path.read_bytes())
        for path in files:
            os.replace(staged[path], path)
            replaced.append(path)
    except OSError as error:
        failures = []
        for path in reversed(replaced):
            try:
                os.replace(backups[path], path)
            except OSError:
                retained.add(backups[path])
                failures.append(f"{path} (original retained at {backups[path]})")
        if failures:
            raise RuntimeError("Configuration rollback failed: " + "; ".join(failures)) from error
        raise
    finally:
        for temporary in (*staged.values(), *backups.values()):
            if temporary not in retained:
                temporary.unlink(missing_ok=True)


def configure(root):
    dockerfile = root / "Dockerfile"
    initfile = root / "DockerInit.sh"
    docker = dockerfile.read_text(encoding="utf-8")
    init = initfile.read_text(encoding="utf-8")

    docker, count = re.subn(r"^ENV TZ=.*$", "ENV TZ=Asia/Shanghai", docker, flags=re.M)
    if count != 1:
        raise ValueError("Expected exactly one upstream timezone setting")

    # Bootlin SDKs need an amd64 glibc host. Keep upstream's Go version, but
    # use its native Debian image for both compilation and resource downloads.
    builder = re.search(r"^FROM golang:(\d+\.\d+(?:\.\d+)?)-alpine(?:\d+\.\d+)? AS builder$", docker, re.M)
    if builder is None:
        raise ValueError("Upstream Go builder image changed; review the build patch")
    docker = replace_once(
        docker, builder[0],
        f"FROM --platform=$BUILDPLATFORM golang:{builder[1]} AS build-base",
        "Go builder image",
    )
    docker = replace_once(docker, "ARG TARGETARCH\n", "", "target architecture args")
    # Normalize only whitespace/continuations in this known instruction. Do not
    # silently discard new packages, shell operators, or apk flags.
    packages = re.findall(r"^RUN apk --no-cache(?:[^\n]*\\\n)*[^\n]*", docker, re.M)
    if len(packages) != 1 or re.sub(r"\\\n", " ", packages[0]).split() != [
        "RUN", "apk", "--no-cache", "--update", "add", "build-base", "gcc", "curl", "unzip",
    ]:
        raise ValueError("Upstream native build tools changed; review the build patch")
    docker = replace_once(
        docker, packages[0],
        "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
        "  ca-certificates curl unzip xz-utils file binutils \\\n"
        "  && rm -rf /var/lib/apt/lists/*\n\n"
        "COPY --from=actionbot cross-build.sh /usr/local/bin/3x-ui-cross-build\n"
        "RUN chmod +x /usr/local/bin/3x-ui-cross-build",
        "native build tools",
    )
    docker = replace_once(
        docker, "COPY . .",
        "COPY go.mod go.sum ./\nRUN go mod download\n\n"
        "FROM build-base AS builder\nARG BUILDARCH\nARG TARGETOS\nARG TARGETARCH\nARG TARGETVARIANT\n"
        "RUN /usr/local/bin/3x-ui-cross-build install\n\nCOPY . .",
        "Go source COPY",
    )
    docker = replace_once(
        docker, 'RUN go build -ldflags "-w -s" -o build/x-ui main.go',
        "RUN --mount=type=cache,id=3x-ui-go-${TARGETARCH}-${TARGETVARIANT},target=/root/.cache/go-build,sharing=locked \\\n"
        '    /usr/local/bin/3x-ui-cross-build build "-w -s"',
        "native Go cross-compilation",
    )
    invocation = re.search(r'^RUN \./DockerInit\.sh "\$TARGETARCH"(?: "\$TARGETVARIANT")?$', docker, re.M)
    if invocation is None:
        raise ValueError("Upstream DockerInit invocation changed; review the build patch")
    docker = replace_once(
        docker, invocation[0],
        'FROM build-base AS resources\nARG TARGETARCH\nARG TARGETVARIANT\n'
        'COPY DockerInit.sh ./\nRUN ./DockerInit.sh "$TARGETARCH" "$TARGETVARIANT"',
        "DockerInit invocation",
    )
    docker = replace_once(
        docker, "COPY --from=builder /app/build/ /app/",
        "COPY --from=resources /app/build/ /app/\nCOPY --from=builder /app/build/x-ui /app/x-ui",
        "runtime resource COPY",
    )

    # Dev builds must advertise their actual source commit in the panel too.
    if os.environ.get("STABLE_BUILD", "false") != "true":
        sha = os.environ["SOURCE_SHA"]
        # Commit time is stable across retries of the same source revision.
        date = os.environ["SOURCE_DATE"]
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("Invalid source SHA")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", date):
            raise ValueError("Invalid source commit date")
        module = re.search(r"^module\s+(\S+)", (root / "go.mod").read_text(), re.M)
        config = root / "internal/config/config.go"
        if module is None or not config.exists():
            raise ValueError("Upstream dev-build config layout changed")
        config_text = config.read_text(encoding="utf-8")
        if "buildCommit string" not in config_text or "buildDate" not in config_text:
            raise ValueError("Upstream dev-build stamp fields changed")
        flags = (
            f'-w -s -X {module[1]}/internal/config.buildCommit={sha[:8]} '
            f'-X {module[1]}/internal/config.buildDate={date}'
        )
        docker = replace_once(
            docker, '3x-ui-cross-build build "-w -s"',
            f'3x-ui-cross-build build "{flags}"', "Go build flags",
        )

    # v3.11.0 separates platform, archive and runtime names. Older releases
    # still need the checked 386/ARM corrections below.
    if "case $PLATFORM in\n" in init:
        platform_resources = PLATFORM_RESOURCES.replace(
            '        if [ "$2" = "v6" ]; then\n            PLATFORM="armv6"\n        fi\n',
            '        case ${2:-v7} in\n'
            '            v6) PLATFORM="armv6" ;;\n'
            '            v7) ;;\n'
            '            *) echo "Unsupported ARM variant: $2" >&2; exit 1 ;;\n'
            '        esac\n',
        )
        init = replace_once(init, PLATFORM_RESOURCES, platform_resources, "platform resource mapping")
    else:
        init = replace_once(init, '    i386)\n        ARCH="32"', '    386 | i386)\n        ARCH="32"', "386 case")
        init = replace_once(init, 'FNAME="i386"', 'FNAME="386"', "386 filename")
        init = init.replace('i386) MTGARCH="386"', '386 | i386) MTGARCH="386"')
        init = replace_once(
            init, '    armv7 | arm | arm32)\n        ARCH="arm32-v7a"\n        FNAME="arm32"\n        ;;',
            '    armv7 | arm32)\n        ARCH="arm32-v7a"\n        FNAME="arm32"\n        ;;\n'
            '    arm)\n        case ${2:-v7} in\n'
            '            v6) ARCH="arm32-v6" ;;\n'
            '            v7) ARCH="arm32-v7a" ;;\n'
            '            *) echo "Unsupported ARM variant: $2" >&2; exit 1 ;;\n'
            '        esac\n        FNAME="arm32"\n        ;;',
            "ARM variant mapping",
        )
        init = replace_once(init, 'FNAME="armv6"', 'FNAME="arm32"', "ARM v6 filename")
        if 'MTGARCH=' in init:
            init = replace_once(
                init, 'arm32) MTGARCH="armv7" ;;',
                'arm32)\n        case $ARCH in\n'
                '            arm32-v6) MTGARCH="armv6" ;;\n'
                '            *) MTGARCH="armv7" ;;\n        esac\n        ;;',
                "MTG ARM variant mapping",
            )
        init = replace_once(
            init, '    *)\n        ARCH="64"\n        FNAME="amd64"\n        ;;',
            '    *)\n        echo "Unsupported architecture: $1" >&2\n        exit 1\n        ;;',
            "architecture fallback",
        )
    init = init.replace("curl -", "curl --show-error --retry 5 --retry-all-errors --retry-delay 3 -")
    init = replace_once(
        init, "set -e\n",
        """set -e
resource_started=$(date +%s)
resource_platform="$1/${2:-}"
resource_finish() {
    resource_status=$?
    trap - 0
    printf 'time=%s stage=resources platform=%s duration_s=%s exit_code=%s\\n' \\
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$resource_platform" \\
        "$(($(date +%s) - resource_started))" "$resource_status" >&2
    exit "$resource_status"
}
trap resource_finish 0
printf 'time=%s stage=resources event=start platform=%s\\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$resource_platform" >&2
""",
        "resource logging prelude",
    )

    # Validate every transformation before modifying either upstream file.
    write_configuration({dockerfile: docker, initfile: init})
    print(f"stage=configure go={builder[1]} files=Dockerfile,DockerInit.sh", flush=True)


if __name__ == "__main__":
    started = time.monotonic()
    status = 1
    try:
        configure(Path(sys.argv[1] if len(sys.argv) > 1 else "."))
        status = 0
    finally:
        print(f"time={datetime.now(timezone.utc).isoformat()} stage=configure "
              f"duration_s={time.monotonic() - started:.3f} exit_code={status}", file=sys.stderr)

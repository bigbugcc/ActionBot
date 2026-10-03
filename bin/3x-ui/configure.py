"""Apply checked, minimal patches to the upstream Docker build."""

import os
import re
import sys
from pathlib import Path


def replace_once(text, old, new, label):
    if text.count(old) != 1:
        raise ValueError(f"Upstream {label} changed; review the build patch")
    return text.replace(old, new, 1)


def configure(root):
    dockerfile = root / "Dockerfile"
    initfile = root / "DockerInit.sh"
    docker = dockerfile.read_text(encoding="utf-8")
    init = initfile.read_text(encoding="utf-8")

    docker, count = re.subn(r"^ENV TZ=.*$", "ENV TZ=Asia/Shanghai", docker, flags=re.M)
    if count != 1:
        raise ValueError("Expected exactly one upstream timezone setting")

    docker = replace_once(docker, "ARG TARGETARCH\n", "ARG TARGETARCH\nARG TARGETVARIANT\n", "target architecture args")
    docker = replace_once(
        docker, "RUN go build ",
        'RUN if [ "$TARGETARCH" = "arm" ]; then export GOARM="${TARGETVARIANT#v}"; fi; \\\n    go build ',
        "Go ARM compilation",
    )

    docker = replace_once(
        docker, "COPY . .",
        "COPY go.mod go.sum ./\nRUN go mod download\n\nCOPY . .",
        "Go source COPY",
    )
    docker = replace_once(
        docker, 'RUN ./DockerInit.sh "$TARGETARCH"',
        'FROM builder AS resources\nARG TARGETVARIANT\nRUN ./DockerInit.sh "$TARGETARCH" "$TARGETVARIANT"',
        "DockerInit invocation",
    )
    docker = replace_once(
        docker, "COPY --from=builder /app/build/ /app/",
        "COPY --from=resources /app/build/ /app/", "runtime resource COPY",
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
        docker = replace_once(docker, '-ldflags "-w -s"', f'-ldflags "{flags}"', "Go build flags")

    # BuildKit uses 386; runtime filenames are derived from runtime.GOARCH.
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
    init = init.replace("curl -", "curl --retry 5 --retry-all-errors --retry-delay 3 -")

    # Validate every transformation before modifying either upstream file.
    dockerfile.write_text(docker, encoding="utf-8", newline="\n")
    initfile.write_text(init, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    configure(Path(sys.argv[1] if len(sys.argv) > 1 else "."))

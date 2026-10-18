#!/usr/bin/env bash
# Bootlin stable musl SDKs are x86_64-hosted; only the output is cross-compiled.
set -euo pipefail
operation=${1:-usage}
phase=validate
tmp=''
SECONDS=0
finish() {
  local status=$?
  trap - EXIT
  set +e
  if [[ -n "$tmp" ]]; then
    rm -f -- "$tmp/archive.tar.xz"
    rmdir -- "$tmp"
  fi
  printf 'time=%s stage=cross-build operation=%s phase=%s platform=%s/%s/%s duration_s=%s exit_code=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$operation" "$phase" \
    "${TARGETOS:-unknown}" "${TARGETARCH:-unknown}" "${TARGETVARIANT:-}" "$SECONDS" "$status" >&2
  exit "$status"
}
trap finish EXIT

if [[ ${BUILDARCH:?Missing BUILDARCH} != amd64 || ${TARGETOS:?Missing TARGETOS} != linux ]]; then
  echo 'Cross-build requires a linux/amd64 builder and a Linux target' >&2
  exit 1
fi

# Checksums: https://toolchains.bootlin.com/downloads/releases/toolchains/ARCH/tarballs/NAME.sha256
case "${TARGETARCH:?Missing TARGETARCH}/${TARGETVARIANT:-}" in
  amd64/)
    sdk_arch=x86-64
    checksum=09fca3aa89540f1b01b5f4210d488cbeb00f522044c53e9989b1dd8a38076912 ;;
  386/)
    sdk_arch=x86-i686
    checksum=3a340ebc386a057f5ddb24d8f4bbde07d2c013c99cebc02ac38565e2849bfda0 ;;
  arm64/|arm64/v8)
    sdk_arch=aarch64
    checksum=defba831ffa1175236f137069333e21ed46d4d19feb5080a90cf248b6fc2cb08 ;;
  arm/v7)
    sdk_arch=armv7-eabihf
    checksum=2f3a34458c3a8b961bd09f89669130fcdc4c1dbc6e31ada720527e4ad3741c11 ;;
  arm/v6)
    sdk_arch=armv6-eabihf
    checksum=1a283f0936953c139d7171187414839c119997ea816de201f97e79ea3cfd6947 ;;
  *) echo "Unsupported target: $TARGETARCH/${TARGETVARIANT:-}" >&2; exit 1 ;;
esac

TOOLCHAIN_DIR=${TOOLCHAIN_DIR:-/opt/3x-ui-toolchain}
sdk="${sdk_arch}--musl--stable-2025.08-1"
printf 'time=%s stage=cross-build event=start operation=%s platform=%s/%s/%s sdk=%s\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$operation" "$TARGETOS" "$TARGETARCH" "${TARGETVARIANT:-}" "$sdk" >&2

case ${1:-} in
  install)
    tmp=$(mktemp -d)
    phase=download
    curl --fail --location --retry 5 --retry-all-errors --retry-delay 3 \
      --connect-timeout 20 --max-time 300 \
      "https://toolchains.bootlin.com/downloads/releases/toolchains/$sdk_arch/tarballs/$sdk.tar.xz" \
      --output "$tmp/archive.tar.xz"
    phase=checksum
    printf '%s  %s\n' "$checksum" "$tmp/archive.tar.xz" | sha256sum --check --strict
    mkdir -p "$TOOLCHAIN_DIR"
    phase=extract
    tar -xJf "$tmp/archive.tar.xz" --strip-components=1 -C "$TOOLCHAIN_DIR"
    ;;
  build)
    export PATH="$TOOLCHAIN_DIR/bin:$PATH"
    # Match upstream's use of the actual compiler, rather than the SDK wrapper.
    set -- "$TOOLCHAIN_DIR"/bin/*-gcc.br_real "${2:--w -s}"
    if [[ $# != 2 || ! -x $1 ]]; then
      echo "Expected exactly one Bootlin C compiler in $TOOLCHAIN_DIR/bin" >&2
      exit 1
    fi
    export CC=$1 CGO_ENABLED=1 GOOS=linux GOARCH=$TARGETARCH GOMAXPROCS=2
    unset GOARM
    if [[ $TARGETARCH == arm ]]; then export GOARM=${TARGETVARIANT#v}; fi
    mkdir -p build
    phase=tool-versions
    go version
    compiler_version=$("$CC" --version)
    printf 'compiler=%s version=%s\n' "$CC" "${compiler_version%%$'\n'*}"
    phase=compile
    go build -v -p 2 -ldflags "$2 -linkmode external -extldflags '-static'" -o build/x-ui main.go
    file build/x-ui
    phase=elf-check
    program_headers=$(readelf -l build/x-ui)
    dynamic_tags=$(readelf -d build/x-ui)
    if grep -q ' INTERP ' <<< "$program_headers" || grep -q '(NEEDED)' <<< "$dynamic_tags"; then
      echo 'Expected a static musl binary without an ELF interpreter or shared dependencies' >&2
      exit 1
    fi
    ;;
  *) echo 'Usage: cross-build.sh install | build [Go linker flags]' >&2; exit 1 ;;
esac

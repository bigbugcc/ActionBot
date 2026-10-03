#!/usr/bin/env bash
set -euo pipefail

artifacts=${1:-artifacts}
: "${IMAGE_REPO:?Missing image repository}"
: "${IMAGE_DIGEST:?Missing verified image digest}"
: "${PUBLISH_TAGS:?Missing publication tags}"

# No registry mutation until all exported files pass integrity checks.
(cd "$artifacts" && sha256sum -c SHA256SUMS)
test -f "$artifacts/manifest.json"

tags=()
while IFS= read -r tag; do
  [ -n "$tag" ] || continue
  [[ "$tag" == "${IMAGE_REPO}:"* ]] || { echo "Unexpected publication tag: $tag" >&2; exit 1; }
  tags+=("$tag")
done <<< "$PUBLISH_TAGS"
test "${#tags[@]}" -gt 0

source_image="${IMAGE_REPO}@${IMAGE_DIGEST}"
inspect_digest() {
  docker buildx imagetools inspect --format '{{.Manifest.Digest}}' "$1"
}
test "$(inspect_digest "$source_image")" = "$IMAGE_DIGEST"

# Copy the complete verified index, including SBOM/provenance descriptors.
# Check Buildx's intended digest before touching any publication tag.
docker buildx imagetools create --dry-run --prefer-index=false "$source_image" > "$artifacts/publish-index.json"
expected=$(python3 -c 'import hashlib,sys; data=open(sys.argv[1], "rb").read(); print("sha256:" + hashlib.sha256(data[:-1] if data.endswith(b"\n") else data).hexdigest())' "$artifacts/publish-index.json")
test "$expected" = "$IMAGE_DIGEST"

args=()
for tag in "${tags[@]}"; do args+=(--tag "$tag"); done
docker buildx imagetools create --prefer-index=false "${args[@]}" "$source_image"
for tag in "${tags[@]}"; do
  actual=$(inspect_digest "$tag")
  if [ "$actual" != "$IMAGE_DIGEST" ]; then
    echo "Published digest mismatch for $tag: $actual (expected $IMAGE_DIGEST)" >&2
    exit 1
  fi
done

#!/usr/bin/env bash
# Download the ALIKED + LightGlue ONNX models into <repo>/models and verify SHA-256.
# Idempotent: files that already match are skipped. Any mismatch is a hard failure.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${WAYMARK_MODELS_DIR:-$ROOT/models}"
BASE="https://raw.githubusercontent.com/YangGuanyuhan/lightglue_opencv_project/main/model"

MODELS=(
  "aliked-n16rot-top1k-640.onnx b46868de0794ea0f2d878985d99dc17d3639dfab85d852994254a4cdf0cdc0ea"
  "aliked_lightglue.onnx a1bd99d294c4c6c08ca978af3128c85f8c0e603b04a03e4391428455d9624439"
)

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  else shasum -a 256 "$1" | awk '{print $1}'; fi
}

mkdir -p "$DEST"
for entry in "${MODELS[@]}"; do
  name="${entry%% *}"; want="${entry##* }"; out="$DEST/$name"
  if [[ -f "$out" && "$(sha256 "$out")" == "$want" ]]; then
    echo "ok       $name (cached)"; continue
  fi
  tmp="$(mktemp "$DEST/.${name}.XXXXXX")"
  trap 'rm -f "$tmp"' EXIT
  echo "download $name"
  curl -fL --retry 5 --retry-delay 2 --connect-timeout 20 -sS -o "$tmp" "$BASE/$name"
  got="$(sha256 "$tmp")"
  if [[ "$got" != "$want" ]]; then
    echo "ERROR: SHA-256 mismatch for $name" >&2
    echo "  expected $want" >&2
    echo "  got      $got" >&2
    rm -f "$tmp"
    exit 1
  fi
  chmod 0644 "$tmp"
  mv -f "$tmp" "$out"
  trap - EXIT
  echo "ok       $name ($(wc -c <"$out" | tr -d ' ') bytes)"
done

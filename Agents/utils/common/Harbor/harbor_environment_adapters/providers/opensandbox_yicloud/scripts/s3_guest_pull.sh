#!/usr/bin/env bash
# Download an anonymous-read S3 artifact inside the guest, verify its SHA-256
# digest before mutating the destination, and place or extract it.
#
# Positional arguments:
#   $1: kind ("file" | "directory")
#   $2: download URL
#   $3: expected lowercase SHA-256 hex digest of the payload
#   $4: temporary file path inside /tmp
#   $5: destination file or directory path
#   $6: octal file mode (for kind="file") or compression ("gzip"|"none" for kind="directory")
#   $7: per-attempt download timeout in seconds
#
# Exit codes:
#   0:   materialized successfully
#   67:  SHA-256 digest mismatch (never fallback)
#   123: download transport failed prior to destination mutation
#   125: guest lacks download or SHA-256 verification tools (emits HARBOR_S3_GUEST_PULL_NO_TOOLS)

set -euo pipefail

kind="$1"
url="$2"
expected="$3"
tmp="$4"
target="$5"
arg6="$6"
per_try_timeout="$7"

trap 'rm -f -- "$tmp"' EXIT INT TERM HUP

# Verify both downloader and SHA-256 tool availability before starting network I/O.
if command -v curl >/dev/null 2>&1; then
  downloader="curl"
elif command -v wget >/dev/null 2>&1; then
  downloader="wget"
elif command -v python3 >/dev/null 2>&1; then
  downloader="python3"
elif command -v python >/dev/null 2>&1 && python -c 'import urllib.request' >/dev/null 2>&1; then
  downloader="python"
else
  printf '%s\n' "HARBOR_S3_GUEST_PULL_NO_TOOLS" >&2
  exit 125
fi

if command -v sha256sum >/dev/null 2>&1; then
  hasher="sha256sum"
elif command -v python3 >/dev/null 2>&1; then
  hasher="python3"
elif command -v python >/dev/null 2>&1; then
  hasher="python"
elif command -v openssl >/dev/null 2>&1; then
  hasher="openssl"
else
  printf '%s\n' "HARBOR_S3_GUEST_PULL_NO_TOOLS" >&2
  exit 125
fi

case "$downloader" in
  curl)
    curl -fsSL --retry 2 --retry-delay 1 --connect-timeout 15 --max-time "$per_try_timeout" -o "$tmp" "$url" || exit 123
    ;;
  wget)
    wget_args=(-q)
    if wget --help 2>&1 | grep -q -- --no-proxy; then
      wget_args+=(--no-proxy)
    fi
    if wget --help 2>&1 | grep -q -- --tries; then
      wget_args+=(--tries=3 --timeout="$per_try_timeout")
    fi
    wget "${wget_args[@]}" -O "$tmp" "$url" || exit 123
    ;;
  python3|python)
    "$downloader" -c 'import shutil, sys, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
timeout = int(sys.argv[3])
last_err = None
for _ in range(3):
    try:
        with opener.open(sys.argv[1], timeout=timeout) as resp, open(sys.argv[2], "wb") as out:
            shutil.copyfileobj(resp, out, 1048576)
        last_err = None
        break
    except Exception as exc:
        last_err = exc
if last_err is not None:
    raise SystemExit(123)' "$url" "$tmp" "$per_try_timeout" || exit 123
    ;;
esac

case "$hasher" in
  sha256sum)
    actual=$(sha256sum "$tmp")
    actual=${actual%% *}
    ;;
  python3|python)
    actual=$("$hasher" -c 'import hashlib, sys
h = hashlib.sha256()
with open(sys.argv[1], "rb") as f:
    for chunk in iter(lambda: f.read(1048576), b""):
        h.update(chunk)
print(h.hexdigest())' "$tmp")
    ;;
  openssl)
    actual=$(openssl dgst -sha256 -r "$tmp")
    actual=${actual%% *}
    ;;
esac

if [ "$actual" != "$expected" ]; then
  echo "S3 payload sha256 mismatch: expected=$expected actual=$actual" >&2
  exit 67
fi

case "$kind" in
  file)
    parent_dir=$(dirname -- "$target")
    mkdir -p -- "$parent_dir"
    chmod "$arg6" "$tmp"
    mv -f -- "$tmp" "$target"
    ;;
  directory)
    mkdir -p -- "$target"
    if [ "$arg6" = "gzip" ]; then
      tar xzf "$tmp" -C "$target"
    else
      tar xf "$tmp" -C "$target"
    fi
    rm -f -- "$tmp"
    ;;
  *)
    echo "Unsupported S3 guest-pull kind: $kind" >&2
    exit 64
    ;;
esac

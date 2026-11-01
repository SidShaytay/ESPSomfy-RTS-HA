#!/usr/bin/env bash
# Writes a release's version into the integration: bash .github/set_version.sh v3.0.4
# The release job zips the integration with it and commits it.
# Tested by tests/scripts/test_set_version.sh.
set -euo pipefail

VERSION="${1#v}"
# The version goes into sed replacements and into manifest.json, so only
# digits, letters, dots and dashes: 3.0.4, 3.1.0.beta, 3.1.0-rc1.
if [[ ! "$VERSION" =~ ^[0-9]+(\.[0-9A-Za-z-]+)*$ ]]; then
  echo "Not a version: $1" >&2
  exit 1
fi

DIR=custom_components/espsomfy_rts

# Each pattern matches whatever version is there now, suffixes included.
sed -i "s/\"version\": \"[^\"]*\"/\"version\": \"${VERSION}\"/" "$DIR/manifest.json"
sed -i "s/^VERSION = \"v[^\"]*\"/VERSION = \"v${VERSION}\"/" "$DIR/const.py"

# sed succeeds when nothing matches, so check each file got the version.
check() {
  if ! grep -qF -- "$2" "$1"; then
    echo "$1 doesn't have $2: its version string changed shape" >&2
    exit 1
  fi
}
check "$DIR/manifest.json" "\"version\": \"${VERSION}\""
check "$DIR/const.py" "VERSION = \"v${VERSION}\""

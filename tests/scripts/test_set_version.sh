#!/usr/bin/env bash
# Tests .github/set_version.sh on copies of the real files:
#   bash tests/scripts/test_set_version.sh
set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
DIR=custom_components/espsomfy_rts
FILES=("$DIR/manifest.json" "$DIR/const.py")
failures=0

fail() { echo "FAIL: $1"; failures=$((failures + 1)); }

# A fresh copy of the two files in a temp dir, which becomes the cwd.
fresh() {
  WORK="$(mktemp -d)"
  for f in "${FILES[@]}"; do
    mkdir -p "$WORK/$(dirname "$f")"
    cp "$REPO/$f" "$WORK/$f"
  done
  cd "$WORK"
}

run() { bash "$REPO/.github/set_version.sh" "$1" >/dev/null 2>&1; }

has() { grep -qF -- "$2" "$1" || fail "$3: $1 lacks $2"; }

# Both version strings get the new version, and only they change.
for tag in v3.0.4 v3.1.0.beta v4.0.0-rc1; do
  fresh
  v="${tag#v}"
  run "$tag" || fail "$tag: exited non-zero"
  has "$DIR/manifest.json" "\"version\": \"$v\"" "$tag"
  has "$DIR/const.py" "VERSION = \"v$v\"" "$tag"
  for f in "${FILES[@]}"; do
    changed="$(diff --strip-trailing-cr "$REPO/$f" "$f" | grep -c '^>')"
    ((changed <= 1)) || fail "$tag: changed $changed lines of $f"
  done
  # The manifest is still JSON, with the version Home Assistant reads.
  if python3 -c "" 2>/dev/null; then
    got="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$DIR/manifest.json")"
    [[ "$got" == "$v" ]] || fail "$tag: manifest.json's version is $got"
  fi
done

# Running it twice with the same tag still passes its own check.
fresh
run v3.0.4 && run v3.0.4 || fail "a second run with the same tag failed"

# Tags that aren't versions are rejected and nothing is written. v.3.0.0
# is the shape of an old release's tag.
for tag in "v1/2" "v3.0;rm" "v3.0&" "" "vx.y" "v3..0" "v.3.0.0"; do
  fresh
  if run "$tag"; then fail "accepted '$tag'"; fi
  for f in "${FILES[@]}"; do
    diff -q --strip-trailing-cr "$REPO/$f" "$f" >/dev/null || fail "'$tag' changed $f"
  done
done

# A file whose version string changed shape fails loudly instead of
# silently keeping the old version.
fresh
sed -i 's/^VERSION = /INTEGRATION_VERSION = /' "$DIR/const.py"
if run v3.0.4; then fail "passed with no VERSION line"; fi

if ((failures)); then
  echo "$failures failure(s)"
  exit 1
fi
echo "set_version.sh: all passed"

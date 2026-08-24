#!/usr/bin/env bash
set -euo pipefail

addon_dir=$(find . -mindepth 1 -maxdepth 1 -type d \
        ! -name ".git" ! -name ".github" \
    -exec test -f "{}/config.yaml" \; -print | head -1)

if [ -z "$addon_dir" ]; then
    echo "ERROR: No add-on directory containing config.yaml found"
    exit 1
fi

SRC="${addon_dir}/README.md"
DST="README.md"

if [ ! -f "$SRC" ]; then
    echo "ERROR: $SRC does not exist"
    exit 1
fi

if [ ! -f "$DST" ]; then
    echo "Root README missing; copying from $SRC"
    cp -a "$SRC" "$DST"
    git add "$DST"
    git commit -m "Copied $SRC to $DST [skip ci]"
    git push
    exit 0
fi

if cmp -s "$SRC" "$DST"; then
    echo "Sync skipped: $SRC and $DST identical"
    exit 0
fi

SRC_CT=$(git log -1 --format=%ct -- "$SRC" 2>/dev/null || true)
SRC_CT=${SRC_CT:-0}

DST_CT=$(git log -1 --format=%ct -- "$DST" 2>/dev/null || true)
DST_CT=${DST_CT:-0}

# Use mtime if both 0
if [ "$SRC_CT" -eq 0 ] && [ "$DST_CT" -eq 0 ]; then
    SRC_CT=$(stat -c %Y "$SRC" 2>/dev/null || echo 0)
    DST_CT=$(stat -c %Y "$DST" 2>/dev/null || echo 0)
fi

if [ "$SRC_CT" -gt "$DST_CT" ]; then
    echo "Add-on README is newer; copying $SRC -> $DST"
    cp -a "$SRC" "$DST"
    git add "$DST"
    git commit -m "Copied $SRC to $DST [skip ci]"
    git push
    exit 0
fi

if [ "$DST_CT" -gt "$SRC_CT" ]; then
    echo "ERROR: $DST is newer than $SRC. Update the add-on README first."
    exit 1
fi

echo "ERROR: READMEs differ but commit times are equal; resolve manually."
exit 1

#!/bin/sh
# Fails unless FusionExport.manifest and pyproject.toml carry the same version.
# With an argument (X.Y.Z), both must also equal it.
set -eu
cd "$(dirname "$0")/.."

manifest=$(python3 -c 'import json; print(json.load(open("FusionExport/FusionExport.manifest"))["version"])')
project=$(sed -n 's/^version = "\([^"]*\)".*/\1/p' pyproject.toml | head -n1)
if [ "$manifest" != "$project" ]; then
  echo "FusionExport.manifest has $manifest, pyproject.toml has $project" >&2
  exit 1
fi
if [ $# -gt 0 ] && [ "$manifest" != "$1" ]; then
  echo "version is $manifest, expected $1" >&2
  exit 1
fi
echo "$manifest"

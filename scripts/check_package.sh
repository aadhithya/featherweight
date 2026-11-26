#!/bin/sh
# Build the package, install it into new environments (without extras, and with all), and import it. `make package-check`
# runs this in the test container. Dependencies are the newest that the package allows, as for a user, not those of uv.lock.
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir "$work/source"
cp -R "$root/pyproject.toml" "$root/README.md" "$root/LICENSE" "$root/src" "$work/source/"
find "$work/source" -name '*.egg-info' -prune -exec rm -rf {} +
uv build -q --out-dir "$work/dist" "$work/source"
wheel=$(ls "$work"/dist/*.whl)
for kind in base all; do
    uv venv -q "$work/$kind"
    if [ "$kind" = all ]; then target="$wheel[all]"; else target="$wheel"; fi
    uv pip install -q --python "$work/$kind/bin/python" "$target"
    (cd "$work" && env -u PYTHONPATH "$work/$kind/bin/python" -I "$root/scripts/smoke_package.py" "$kind")
done

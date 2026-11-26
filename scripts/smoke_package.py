"""Run with the Python of an environment that has only the built package: is it installed, complete and importable?

python -I smoke_package.py base   # installed without extras: the package and its public names
python -I smoke_package.py all    # installed with [all]: every module too
"""

import importlib
import pkgutil
import sys
from importlib.metadata import version
from pathlib import Path

import featherweight


def main(kind: str) -> None:
    where = Path(featherweight.__file__).parent
    assert "site-packages" in where.parts, f"not the installed package: {where}"
    assert featherweight.__version__ == version("featherweight"), "the two versions differ"
    assert (where / "py.typed").exists(), "py.typed is not in the package"
    needs_an_extra = []
    for name in featherweight.__all__:
        try:
            getattr(featherweight, name)
        except ImportError as exc:  # without extras, a name may need one, if the error says which to install
            if kind != "base" or "pip install 'featherweight[" not in str(exc):
                raise
            needs_an_extra.append(name)
    modules = ["featherweight"]
    if kind == "all":
        modules += [m.name for m in pkgutil.walk_packages(featherweight.__path__, "featherweight.")]
        for module in modules:
            importlib.import_module(module)
    print(f"{kind}: version {featherweight.__version__}, {len(featherweight.__all__)} names, {len(modules)} modules")
    if needs_an_extra:
        print(f"{kind}: names that ask for an extra: {', '.join(needs_an_extra)}")


if __name__ == "__main__":
    main(sys.argv[1])

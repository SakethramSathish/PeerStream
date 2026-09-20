"""Packaging: the wheel has to contain the program.

A distribution that forgets its subpackages installs without complaint and
imports without success: ``app`` lands as a directory with an ``__init__.py``
and nothing else, and the first ``from app.ui.app import build_ui`` fails in a
way that looks like a bug in the code rather than in the build. Listing three
top-level names in ``pyproject.toml`` does exactly that, so the config is read
here rather than trusted.

Three things are checked:

* **every package on disk is matched by the build config** — the failure above;
* **the console script points at a callable that exists** — an entry point is a
  string, and a typo in it is only discovered when someone runs the command
  after installing;
* **the declared licence has text beside it** — package metadata advertises a
  licence whether or not the repository contains one.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from importlib import import_module
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"

#: The packages the wheel is built from, in the order setuptools looks for them.
SHIPPED: tuple[str, ...] = ("app", "cli", "tools")


def _packages_on_disk() -> set[str]:
    """Every directory under a shipped root that is an importable package."""
    found: set[str] = set()
    for root in SHIPPED:
        base = ROOT / root
        for init in base.rglob("__init__.py"):
            package = init.parent.relative_to(ROOT)
            found.add(str(package).replace("/", "."))
    return found


def test_every_package_is_covered_by_the_build_config() -> None:
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["setuptools"]
    packages = config["packages"]

    assert isinstance(packages, dict) and "find" in packages, (
        "setuptools is configured with a fixed package list, which ships the "
        "top-level directories and none of their modules; use a find directive"
    )
    include = packages["find"].get("include") or ["*"]

    from setuptools import find_packages

    discovered = set(find_packages(where=str(ROOT), include=list(include)))
    missing = sorted(_packages_on_disk() - discovered)
    assert not missing, f"these packages exist but would not be shipped: {missing}"


def test_the_shipped_roots_still_exist() -> None:
    for root in SHIPPED:
        assert (ROOT / root / "__init__.py").is_file(), f"{root}/__init__.py is gone"


@pytest.mark.parametrize("package", sorted(_packages_on_disk()))
def test_every_package_imports(package: str) -> None:
    """A package the wheel ships must be importable from the repository too.

    Guards against the other half of the same mistake: a directory that *is*
    shipped but has never been imported by anything, so a syntax error in it
    waits for the user who installs the wheel.
    """
    try:
        import_module(package)
    except ImportError as error:  # a missing Qt is not this test's problem
        pytest.skip(f"{package} needs an optional dependency: {error}")


def test_the_console_script_points_at_a_real_callable() -> None:
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["scripts"]
    assert config, "the package installs no command at all"
    for name, target in config.items():
        module_name, _, attribute = target.partition(":")
        assert module_name and attribute, f"{name}: entry points look like 'module:callable'"
        module = import_module(module_name)
        assert callable(getattr(module, attribute, None)), f"{name}: {target} is not callable"


def test_the_module_form_of_the_client_answers_help() -> None:
    """``python -m cli.main --help`` is what a user is told to run."""
    completed = subprocess.run(
        [sys.executable, "-m", "cli.main", "--help"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "usage:" in completed.stdout.lower()


def test_the_declared_licence_has_text_beside_it() -> None:
    """``license = { text = "MIT" }`` is a promise the wheel repeats.

    Setuptools copies that string into the metadata, so a repository with no
    licence file still installs as a package that claims to be MIT. Nobody
    notices until they try to comply with the licence they were granted, and by
    then the versions in the wild are the ones without the text.
    """
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    declared = project.get("license")
    identifier = None
    if isinstance(declared, dict):
        identifier = declared.get("text") or declared.get("file")
    elif isinstance(declared, str):
        identifier = declared  # PEP 639: a plain SPDX expression
    if identifier is None:
        pytest.skip("no licence is declared, so there is nothing to check against")

    licence = ROOT / "LICENSE"
    assert licence.is_file(), f"pyproject declares {identifier!r} but ships no LICENSE"

    body = licence.read_text(encoding="utf-8")
    if isinstance(declared, dict) and declared.get("text"):
        assert declared["text"] in body, f"LICENSE is not the {declared['text']} text"
    assert "Copyright" in body, "LICENSE names no copyright holder"
    # The template's own blanks: a licence copied and not filled in grants
    # nothing, and looks like it grants everything.
    for placeholder in ("<year>", "<copyright holders>", "Your Name", "your-org"):
        assert placeholder not in body, f"LICENSE still contains the template's {placeholder!r}"

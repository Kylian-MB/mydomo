"""The conversation core knows no channel: nothing in it imports a web or channel module."""

import ast
import json
import subprocess
import sys
from pathlib import Path

import domo_core
import pytest

FORBIDDEN = ("fastapi", "starlette", "jinja2", "html", "domo_core.web", "domo_core.channels")


def _named(module: str) -> str | None:
    for forbidden in FORBIDDEN:
        if module == forbidden or module.startswith(forbidden + "."):
            return forbidden
    return None


def forbidden_imports(source: str) -> list[str]:
    """The forbidden modules ``source`` imports, in any form: ``import x``, ``from x import y``,
    and ``from domo_core import web`` (the sub-package named as an imported name)."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules = [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
        for module in modules:
            if _named(module) is not None:
                found.append(module)
    return found


def test_the_checker_flags_what_it_should():
    assert forbidden_imports("import fastapi") == ["fastapi"]
    assert forbidden_imports("from domo_core.web import deps") == [
        "domo_core.web",
        "domo_core.web.deps",
    ]
    assert forbidden_imports("from domo_core import web") == ["domo_core.web"]
    assert forbidden_imports("from domo_core import channels") == ["domo_core.channels"]
    assert forbidden_imports("from domo_core.texts import view\nimport json") == []


def test_the_conversation_knows_no_channel():
    root = Path(domo_core.__file__).parent / "conversation"
    files = sorted(root.rglob("*.py"))
    assert files, "the conversation package has no module"
    found = [
        f"{path.relative_to(root).as_posix()} imports {module}"
        for path in files
        for module in forbidden_imports(path.read_text(encoding="utf-8"))
    ]
    assert not found, "the conversation must not import a channel: " + "; ".join(found)


# What renders HTML: the templates (``jinja2``, and the doors to it: ``fastapi.templating``,
# ``starlette.templating``, ``markupsafe`` and the repository's own ``domo_core.web.templating``)
# and ``html``.
RENDERING = (
    "jinja2",
    "fastapi.templating",
    "starlette.templating",
    "markupsafe",
    "domo_core.web.templating",
    "html",
)

# The modules that render a page (Ruling R5: one the plan forgot is added here, with why).
RENDERS_A_PAGE = (
    "channels/page/",
    "web/",
    "setup_flow/",
    "household/routes.py",
    "accounts/",
    "confirmations/routes.py",
    # The audit page (owner only): the plan forgot it, it renders through ``web.templating``.
    "audit/routes.py",
)


def rendering_imports(source: str) -> list[str]:
    """The modules of ``RENDERING`` that ``source`` imports, in any form."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules = [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
        found += [
            module
            for module in modules
            if any(module == name or module.startswith(name + ".") for name in RENDERING)
        ]
    return found


def test_the_rendering_checker_flags_what_it_should():
    assert rendering_imports("import html") == ["html"]
    assert rendering_imports("from fastapi.templating import Jinja2Templates") == [
        "fastapi.templating",
        "fastapi.templating.Jinja2Templates",
    ]
    assert rendering_imports("from jinja2 import Environment") == ["jinja2", "jinja2.Environment"]
    assert rendering_imports("from domo_core.web.templating import render") == [
        "domo_core.web.templating",
        "domo_core.web.templating.render",
    ]
    assert rendering_imports("from starlette.templating import Jinja2Templates") == [
        "starlette.templating",
        "starlette.templating.Jinja2Templates",
    ]
    assert rendering_imports("from fastapi import Request\nimport json") == []


def test_only_the_page_channel_renders_html():
    root = Path(domo_core.__file__).parent
    found = [
        f"{relative} imports {module}"
        for path in sorted(root.rglob("*.py"))
        if not (relative := path.relative_to(root).as_posix()).startswith(RENDERS_A_PAGE)
        for module in rendering_imports(path.read_text(encoding="utf-8"))
    ]
    assert not found, "only the modules of a page render HTML: " + "; ".join(found)


# What loading the conversation must never load, even through a module it imports: the web, the
# page, and the routes of the confirmations (the FastAPI router, its dependencies and templates).
NEVER_LOADED = (
    "fastapi",
    "starlette",
    "jinja2",
    "domo_core.web",
    "domo_core.channels",
    "domo_core.confirmations.routes",
)

_LOADED = """
import importlib
import json
import sys

importlib.import_module(sys.argv[1])
names = json.loads(sys.argv[2])
print(json.dumps(sorted(
    n for n in sys.modules if any(n == b or n.startswith(b + ".") for b in names)
)))
"""


def _conversation_modules() -> list[str]:
    package = Path(domo_core.__file__).parent
    modules: list[str] = []
    for path in (package / "conversation").rglob("*.py"):
        parts = path.relative_to(package.parent).with_suffix("").parts
        modules.append(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    return sorted(modules)


@pytest.mark.parametrize("module", _conversation_modules())
def test_loading_the_conversation_loads_no_channel(module: str):
    result = subprocess.run(
        [sys.executable, "-c", _LOADED, module, json.dumps(NEVER_LOADED)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    loaded = json.loads(result.stdout)
    assert not loaded, f"{module} drags in: " + ", ".join(loaded)

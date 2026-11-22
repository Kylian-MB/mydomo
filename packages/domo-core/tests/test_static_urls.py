"""Static files carry the version of the core in their address (task 14).

Every template names a file of ``web/static`` through ``static_url``, which adds ``?v=`` and the
version of the ``domo-core`` package: after an update, a browser that kept the earlier files in
its cache asks for the new ones, instead of running an old script against a new page.
"""

import re
from importlib.metadata import version
from pathlib import Path

import pytest
from domo_core.texts.view import CatalogTexts
from domo_core.web import app as web_app
from domo_core.web.templating import create_templates, render_fragment, static_url

TEMPLATES_DIR = Path(web_app.__file__).parent / "templates"
VERSION = version("domo-core")


@pytest.fixture
def rendered_base_page() -> str:
    """The page every other one extends, as an English visitor gets it."""
    return render_fragment(create_templates(), "base.html", CatalogTexts("en"))


# --- the brief's test ----------------------------------------------------------------------------


def test_static_urls_carry_the_version(rendered_base_page):
    assert re.search(r"/static/htmx\.min\.js\?v=[0-9]", rendered_base_page)


# --- every file, every template ------------------------------------------------------------------


def test_the_version_is_the_one_of_the_core_package():
    assert static_url("app.css") == f"/static/app.css?v={VERSION}"


def test_every_file_of_the_base_page_carries_the_version(rendered_base_page):
    urls = re.findall(r'(?:src|href)="(/static/[^"]*)"', rendered_base_page)
    assert sorted(urls) == sorted(
        static_url(name)
        for name in (
            "icon.svg",
            "app.css",
            "htmx.min.js",
            "htmx-ext-sse.js",
        )
    )


def test_no_template_names_a_static_file_without_static_url():
    for path in TEMPLATES_DIR.rglob("*.html"):
        html = path.read_text(encoding="utf-8")
        assert not re.search(r"""(?:src|href)=["']/static/""", html), path.name


def test_every_file_a_template_names_exists():
    named: set[str] = set()
    for path in TEMPLATES_DIR.rglob("*.html"):
        html = path.read_text(encoding="utf-8")
        named |= set(re.findall(r"""static_url\(["']([^"']+)["']\)""", html))
    # Each page's own script (R87: passkeys.js too), and the files of the base page.
    assert {"chat.js", "model-step.js", "passkeys.js", "htmx.min.js", "app.css"} <= named
    for name in named:
        assert (web_app.STATIC_DIR / name).is_file(), name


async def test_a_versioned_address_is_served(offline_client):
    r = await offline_client.get(static_url("htmx.min.js"))
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]


async def test_the_pages_name_their_files_with_the_version(offline_client):
    html = (await offline_client.get("/login")).text
    assert f'<link rel="stylesheet" href="/static/app.css?v={VERSION}">' in html
    assert f'<script src="/static/htmx.min.js?v={VERSION}" defer></script>' in html

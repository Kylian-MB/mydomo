"""The web app shell: static files, templates, error pages and the database lifecycle."""

import asyncio
import hashlib
import json
import re
import subprocess
import sys
from importlib import resources
from pathlib import Path

import domo_core
import httpx
import pytest
from domo_common.i18n import t
from domo_common.meanings import SIGN
from domo_core.config import ConfigurationError, CoreSettings
from domo_core.web.app import WAKE_STOP_SECONDS, create_app
from domo_core.web.security import CONTENT_SECURITY_POLICY
from domo_core.web.templating import static_url
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.requests import Request
from starlette.responses import Response
from web_support import build_test_app

WEB_DIR = Path(domo_core.__file__).parent / "web"
STATIC_DIR = WEB_DIR / "static"
TEMPLATES_DIR = WEB_DIR / "templates"


# --- static files ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "content_type"),
    [
        ("app.css", "text/css"),
        ("htmx.min.js", "javascript"),
        ("htmx-ext-sse.js", "javascript"),
        ("icon.svg", "image/svg+xml"),
    ],
)
async def test_static_files_are_served_with_their_type(offline_client, name, content_type):
    r = await offline_client.get(f"/static/{name}")
    assert r.status_code == 200
    assert content_type in r.headers["content-type"]
    assert r.content == (STATIC_DIR / name).read_bytes()


async def test_a_missing_static_file_is_a_404_page(offline_client):
    r = await offline_client.get("/static/nothing.js")
    assert r.status_code == 404
    assert "text/html" in r.headers["content-type"]


async def test_static_files_cannot_escape_their_folder(offline_client):
    for path in ("/static/../app.py", "/static/%2e%2e/app.py", "/static/..%2fapp.py"):
        assert (await offline_client.get(path)).status_code == 404


async def test_the_manifest_describes_an_installable_app(client):
    # Served by the core: its description is the house's version of it.
    r = await client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    manifest = r.json()
    assert manifest["name"] == "Domo"
    assert manifest["start_url"] == "/chat"
    assert manifest["display"] == "standalone"
    assert manifest["description"] == t("manifest.description", "en")
    assert any(icon["src"] == "/static/icon.svg" for icon in manifest["icons"])
    assert not (STATIC_DIR / "manifest.webmanifest").exists()


def test_static_files_ship_as_package_data():
    static = resources.files("domo_core").joinpath("web", "static")
    for name in (
        "app.css",
        "htmx.min.js",
        "htmx-ext-sse.js",
        "icon.svg",
        "model-step.js",
        "VENDORED.md",
    ):
        assert static.joinpath(name).is_file(), name
    templates = resources.files("domo_core").joinpath("web", "templates")
    for name in (
        "base.html",
        "login.html",
        "error.html",
        "audit.html",
        "household.html",
        "invite.html",
        "setup_owner.html",
        "setup_model.html",
        "fragments/model_form.html",
        "fragments/secret_wait.html",
        "fragments/secret_poll.html",
        "fragments/model_error.html",
        "fragments/action_card.html",
        "fragments/confirmation_card.html",
        "fragments/approvals.html",
        "fragments/key_form.html",
        "fragments/key_poll.html",
        "fragments/key_result.html",
        "fragments/key_rekey.html",
    ):
        assert templates.joinpath(name).is_file(), name


def test_vendored_files_match_the_recorded_versions_and_hashes():
    notes = (STATIC_DIR / "VENDORED.md").read_text(encoding="utf-8")
    for name in ("htmx.min.js", "htmx-ext-sse.js"):
        digest = hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()
        line = next(line for line in notes.splitlines() if f"`{name}`" in line)
        assert digest in line, name
        assert re.search(r"\b2\.\d+\.\d+\b", line), f"{name}: no 2.x version recorded"
        assert "https://" in line, f"{name}: no source URL recorded"


# --- templates -------------------------------------------------------------------------------


def test_templates_have_no_inline_script_or_style():
    """The CSP forbids inline code: scripts and styles live in static files."""
    for path in TEMPLATES_DIR.rglob("*.html"):
        html = path.read_text(encoding="utf-8")
        for script in re.findall(r"<script\b[^>]*>", html, re.IGNORECASE):
            assert re.search(r"\bsrc=", script), f"{path.name}: inline script"
        assert not re.search(r"<style\b", html, re.IGNORECASE), f"{path.name}: <style>"
        assert not re.search(r"\sstyle=", html, re.IGNORECASE), f"{path.name}: style attribute"
        assert not re.search(r"\son[a-z]+=", html, re.IGNORECASE), f"{path.name}: event handler"
        assert not re.search(r"javascript:", html, re.IGNORECASE), f"{path.name}: javascript: URL"
        assert not re.search(r"\shx-on", html, re.IGNORECASE), f"{path.name}: hx-on handler"


async def test_pages_load_scripts_and_styles_from_static_files_only(offline_client):
    html = (await offline_client.get("/login")).text
    assert f'<link rel="stylesheet" href="{static_url("app.css")}">' in html
    assert f'<script src="{static_url("htmx.min.js")}" defer></script>' in html
    assert f'<script src="{static_url("htmx-ext-sse.js")}" defer></script>' in html
    assert html.index("htmx.min.js") < html.index("htmx-ext-sse.js")  # the extension needs htmx
    assert '<link rel="manifest" href="/manifest.webmanifest">' in html


async def test_htmx_is_configured_for_the_content_security_policy(offline_client):
    html = (await offline_client.get("/login")).text
    match = re.search(r'<meta name="htmx-config" content=\'([^\']*)\'>', html)
    assert match
    config = json.loads(match.group(1))
    assert config["allowEval"] is False  # no 'unsafe-eval' in the policy
    assert config["includeIndicatorStyles"] is False  # htmx would inject an inline <style>
    assert config["allowScriptTags"] is False
    assert "unsafe" not in CONTENT_SECURITY_POLICY


async def test_pages_are_designed_for_the_phone_first(offline_client):
    html = (await offline_client.get("/login")).text
    assert re.search(r'<meta name="viewport" content="width=device-width, initial-scale=1', html)
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert "@media (min-width" in css  # larger screens are an enhancement...
    assert "@media (max-width" not in css  # ...of the base layout, which is the phone's


@pytest.fixture
async def installing_client(settings, session, clock, client_factory):
    """A browser of an instance whose first launch is not over (its fixed texts)."""
    app = build_test_app(settings, session, clock)
    async with client_factory(app) as http_client:
        yield http_client


async def test_the_page_language_follows_accept_language_during_the_installation(
    installing_client,
):
    english = await installing_client.get("/login")
    french = await installing_client.get("/login", headers={"accept-language": "fr-CH,fr;q=0.9"})
    other = await installing_client.get("/login", headers={"accept-language": "de"})
    assert '<html lang="en">' in english.text
    assert '<html lang="fr">' in french.text
    assert '<html lang="en">' in other.text
    assert t("login.title", "en") in english.text
    assert t("login.title", "fr") in french.text
    assert t("login.title", "en") != t("login.title", "fr")


async def test_once_installed_a_visitor_reads_their_default_phrases_else_the_house_language(
    client,
):
    # French has default phrases of its own: a French browser reads them.
    french = await client.get("/login", headers={"accept-language": "fr-CH,fr;q=0.9"})
    assert '<html lang="fr-CH">' in french.text
    assert t("login.title", "fr") in french.text
    # German has none, nor a version: the house's language.
    german = await client.get("/login", headers={"accept-language": "de-CH,de;q=0.9"})
    assert '<html lang="en">' in german.text
    assert t("login.title", "en") in german.text


async def test_texts_that_cannot_be_read_show_their_default_phrases(offline_client):
    # No database: the page is shown all the same, each text its English default phrase, never
    # a sign in its place.
    r = await offline_client.get("/login")
    assert r.status_code == 200
    assert '<html lang="und">' in r.text
    assert f"{t('login.title', 'en')} - Domo</title>" in r.text
    assert SIGN not in r.text


# --- health ----------------------------------------------------------------------------------


async def test_healthz_says_the_core_is_up_without_touching_the_database(offline_client):
    r = await offline_client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert "application/json" in r.headers["content-type"]
    assert r.headers["cache-control"] == "no-store"


async def test_healthz_answers_head_requests_too(offline_client):
    assert (await offline_client.head("/healthz")).status_code == 200


# --- error pages -----------------------------------------------------------------------------


async def test_unknown_pages_get_a_translated_html_404(client, installing_client):
    english = await client.get("/no-such-page")
    french = await installing_client.get("/setup/nothing", headers={"accept-language": "fr"})
    assert english.status_code == french.status_code == 404
    assert "text/html" in english.headers["content-type"]
    # Domo's words once installed (the house's language), the fixed ones before.
    assert t("error.not_found.title", "en") in english.text
    assert t("error.not_found.title", "fr") in french.text
    # A static file not found: its texts are read for its page.
    static = await client.get("/static/nothing.js")
    assert static.status_code == 404 and t("error.not_found.title", "en") in static.text


async def test_a_wrong_method_gets_an_html_page_and_keeps_the_allow_header(offline_client):
    r = await offline_client.put("/login")
    assert r.status_code == 405
    assert "text/html" in r.headers["content-type"]
    assert "GET" in r.headers["allow"]


async def test_no_api_documentation_is_exposed(offline_client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await offline_client.get(path)).status_code == 404


async def test_an_unexpected_error_is_a_hardened_html_500_without_details(app):
    async def boom(request: Request) -> Response:
        raise RuntimeError("internal detail: do-not-show-me")

    app.add_route("/boom", boom)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as http:
        r = await http.get("/boom")
    assert r.status_code == 500
    assert "text/html" in r.headers["content-type"]
    assert "do-not-show-me" not in r.text
    assert t("error.server.title", "en") in r.text
    assert r.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "same-origin"
    assert r.headers["cache-control"] == "no-store"


async def test_the_root_redirects_to_the_chat(offline_client):
    r = await offline_client.get("/")
    assert r.status_code == 303
    assert r.headers["location"] == "/chat"


# --- lifecycle -------------------------------------------------------------------------------


def test_create_app_does_not_touch_the_database_until_it_starts(offline_settings):
    app = create_app(offline_settings)
    assert not hasattr(app.state, "session_factory")
    assert not hasattr(app.state, "engine")
    assert app.state.settings is offline_settings


# A fresh interpreter that only creates the application, then resolves every foreign key: a table
# names another one, and the name only resolves once the module of that table was imported.
_RESOLVE_EVERY_TABLE = """
import sys
from domo_core.config import CoreSettings
from domo_core.web.app import create_app
create_app(CoreSettings(database_url_override=sys.argv[1], core_token_file=sys.argv[2]))
from domo_core.db import Base
for table in Base.metadata.tables.values():
    for key in table.foreign_keys:
        key.column
print(sorted(Base.metadata.tables))
"""


def test_creating_the_app_makes_every_table_known(offline_settings):  # R53
    """``message_actions.confirmation_id`` refers to ``confirmations`` by name: in the running
    application (which imports neither the confirmations nor the notes on its own), inserting an
    action must not fail to find it."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _RESOLVE_EVERY_TABLE,
            offline_settings.database_url,
            str(offline_settings.core_token_file),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert "'confirmations'" in result.stdout and "'notes'" in result.stdout


async def test_the_engine_and_session_factory_live_with_the_application(
    settings, monkeypatch: pytest.MonkeyPatch
):
    disposed: list[AsyncEngine] = []
    real_dispose = AsyncEngine.dispose

    async def recording_dispose(self: AsyncEngine, close: bool = True) -> None:
        disposed.append(self)
        await real_dispose(self, close)

    monkeypatch.setattr(AsyncEngine, "dispose", recording_dispose)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        engine = app.state.engine
        async with app.state.session_factory() as db_session:
            assert (await db_session.execute(text("SELECT 1"))).scalar_one() == 1
        assert disposed == []
    assert disposed == [engine]


async def test_the_wakes_of_domo_stop_with_the_application(settings):
    app = create_app(settings)
    started = asyncio.Event()
    gatekeeper_open: list[bool] = []

    async def a_wake_that_waits() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            # Stopped before the HTTP client is closed: a wake may still be talking to a model.
            gatekeeper_open.append(not app.state.gatekeeper._http.is_closed)

    async with app.router.lifespan_context(app):
        task = app.state.wakes.spawn(a_wake_that_waits())
        await started.wait()
        assert app.state.wakes.pending == 1
    assert task.cancelled()
    assert app.state.wakes.pending == 0
    assert gatekeeper_open == [True]


def test_the_wakes_have_as_long_to_stop_as_the_upkeep():
    assert WAKE_STOP_SECONDS == 5.0


async def test_starting_without_a_database_password_is_a_clear_error(tmp_path, core_token_file):
    app = create_app(
        CoreSettings(db_password_file=tmp_path / "missing", core_token_file=core_token_file)
    )
    with pytest.raises(ConfigurationError, match="missing"):
        async with app.router.lifespan_context(app):
            pytest.fail("the application must not start")

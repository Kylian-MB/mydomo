"""The audit page: owner only, the latest entries, and the verdict of the chain check.

Each line is said in Domo's words (spec 4.5, scenario 24): the text of its kind of line
(``audit.line.<action>``, or ``audit.line.other``), with who did it, when, and the facts of its
details that the page shows (``audit.routes.LINE_FACTS``); never the raw type, the JSON of the
details, a secret or a reference to one. The page never costs a call: it reads the recueil only.
"""

import ast
import importlib
import re
from datetime import date, timedelta
from decimal import Decimal
from html import unescape
from pathlib import Path
from typing import get_args
from zoneinfo import ZoneInfo

import domo_core
import pytest
from domo_common.i18n import t
from domo_common.meanings import SIGN, all_keys, has_meaning, holes
from domo_core.accounts.sessions import SESSION_COOKIE, create_session
from domo_core.audit import routes as audit_routes
from domo_core.audit.log import FORBIDDEN_DETAIL_KEYS, append_audit
from domo_core.confirmations.judge import Verdict
from domo_core.confirmations.service import AmendOutcome, DecideOutcome
from domo_core.texts.formatting import local_amount, local_date
from domo_core.texts.models import WrittenText
from sqlalchemy import text, update
from web_support import in_language, in_words, log_in, reads_in

TAMPER = "UPDATE audit_log SET details = '{\"i\": 99}' WHERE id = :id"


def chain_status(html: str) -> str:
    """The value of the one ``data-chain-status`` attribute of the page (the end-to-end tests
    read the chain's state from it)."""
    values = re.findall(r'data-chain-status="([^"]*)"', html)
    assert len(values) == 1, values
    return values[0]


def shown_ids(html: str) -> list[int]:
    return [int(value) for value in re.findall(r'data-entry-id="(\d+)"', html)]


async def append_n(session, count):
    return [
        await append_audit(session, actor="system", action="test.event", details={"i": i})
        for i in range(count)
    ]


@pytest.fixture
async def member_account(add_person, add_account, owner_password_hash):
    """A logged-in-capable account that is not the owner (it has the owner's password)."""
    person = await add_person("Member", "adult")
    return await add_account(person, "member", owner_password_hash)


# --- access ----------------------------------------------------------------------------------


async def test_a_visitor_is_sent_to_the_login(client):
    r = await client.get("/audit")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


async def test_a_person_who_is_not_the_owner_is_refused(client, owner_account, member_account):
    await log_in(client, "member")
    r = await client.get("/audit")
    assert r.status_code == 403
    assert "data-chain-status" not in r.text
    assert t("error.forbidden.title", "en") in r.text


async def test_only_get_is_allowed(client, owner_account):
    await log_in(client)
    assert (await client.post("/audit")).status_code == 405


# --- the page --------------------------------------------------------------------------------


async def test_the_owner_sees_the_entries_and_an_intact_chain(client, owner_account, session):
    await log_in(client)
    r = await client.get("/audit")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert chain_status(r.text) == "ok"
    assert t("audit.chain.ok", "en", count="1") in r.text
    assert t("audit.title", "en") in r.text
    # The line of the login, in Domo's words: no raw type, no raw actor.
    assert "auth.login" not in r.text
    assert f"person:{owner_account.person_id}" not in r.text
    assert len(shown_ids(r.text)) == 1


async def test_the_page_is_dynamic_and_hardened(client, owner_account):
    await log_in(client)
    r = await client.get("/audit")
    assert r.headers["cache-control"] == "no-store"
    assert "default-src 'self'" in r.headers["content-security-policy"]


async def test_the_latest_entries_come_first_and_there_are_at_most_two_hundred(
    client, owner_account, session
):
    assert audit_routes.AUDIT_PAGE_SIZE == 200
    await log_in(client)
    entries = await append_n(session, 205)  # 206 with the login
    r = await client.get("/audit")
    ids = shown_ids(r.text)
    newest = entries[-1].id
    assert ids == list(range(newest, newest - 200, -1))
    # The whole chain is verified, not only what is shown.
    assert t("audit.chain.ok", "en", count="206") in r.text


async def test_an_empty_log_says_so(client, session, owner_account):
    token = await create_session(session, owner_account.id, ttl=timedelta(days=1))
    r = await client.get("/audit", headers={"cookie": f"{SESSION_COOKIE}={token}"})
    assert r.status_code == 200
    assert chain_status(r.text) == "ok"
    assert t("audit.chain.ok", "en", count="0") in r.text
    assert t("audit.empty", "en") in r.text
    assert shown_ids(r.text) == []


def plain(text: str) -> str:
    """``text`` with its spaces as a person reads them (the narrow space of a time is a space)."""
    return " ".join(text.split())


def lines_of(html: str) -> dict[int, str]:
    """The line of each entry the page shows, by its id, as a person reads it."""
    found = re.findall(r'data-entry-id="(\d+)"[^>]*>(.*?)</li>', html, re.DOTALL)
    return {int(entry_id): plain(unescape(line)) for entry_id, line in found}


async def test_each_audit_line_is_said_in_domo_words(
    client, owner_account, session, add_person
):  # Scenario 24
    await log_in(client)
    owner = "Owner"
    robin = await add_person("Robin", "adult")
    turn = await append_audit(
        session,
        actor=f"person:{owner_account.person_id}",
        action="chat.turn",
        details={"model": "small-model-1", "cost_usd": "0.012300", "input_tokens": 12},
    )
    note = await append_audit(
        session,
        actor=f"person:{owner_account.person_id}",
        action="note.kept",
        details={"note_id": "n-1", "recipient_id": str(robin.id), "until": "2030-01-05"},
    )
    refresh = await append_audit(
        session, actor="system", action="catalog.refreshed", details={"models": 42}
    )
    html = (await client.get("/audit")).text
    lines = lines_of(html)
    # Names and times of the house, the facts of each line put in by the code.
    assert lines[turn.id] == t(
        "audit.line.chat.turn",
        "en",
        who=owner,
        when=in_words(turn.ts),
        model="small-model-1",
        cost=local_amount(Decimal("0.0123"), "USD", language="en"),
    )
    assert lines[note.id] == t(
        "audit.line.note.kept",
        "en",
        who=owner,
        when=in_words(note.ts),
        name="Robin",
        day=in_words(date(2030, 1, 5)),
    )
    assert lines[refresh.id] == t(
        "audit.line.catalog.refreshed",
        "en",
        who=t("audit.who.system", "en"),
        when=in_words(refresh.ts),
        count="42",
    )
    # Never the raw type, the JSON of the details, nor the actor as it is stored.
    for raw in ("chat.turn", "note.kept", "catalog.refreshed", "input_tokens", "person:", "{"):
        assert raw not in "".join(lines.values())
    assert "audit-details" not in html

    # A reader of French reads the lines of the French version, dates in French.
    await reads_in(session, owner_account.person_id, "fr")
    french = lines_of((await client.get("/audit")).text)
    assert french[refresh.id] == plain(
        in_language(
            "fr",
            "audit.line.catalog.refreshed",
            who=in_language("fr", "audit.who.system"),
            when=local_date(refresh.ts, language="fr", zone=ZoneInfo("UTC")),
            count="42",
        )
    )


async def test_an_unknown_action_has_the_general_line(client, owner_account, session):
    await log_in(client)
    entry = await append_audit(
        session, actor="system", action="test.event", details={"i": 1, "note": "words"}
    )
    other = await append_audit(
        session, actor="person:not-a-person", action="audit.line", details={}
    )
    lines = lines_of((await client.get("/audit")).text)
    assert lines[entry.id] == t(
        "audit.line.other", "en", who=t("audit.who.system", "en"), when=in_words(entry.ts)
    )
    # Someone the household does not know: a sign without words.
    assert lines[other.id] == t("audit.line.other", "en", who=SIGN, when=in_words(other.ts))


# The calls of ``append_audit`` whose action is built where they are, not written in the call:
# each site (its module, and the expression of its action) with the actions it writes, each
# written as it is in that module.
DYNAMIC_SITES = {
    ("channels/page/key_routes.py", "action"): {"provider.rekeyed", "key.admin_added"},
    ("household/routes.py", "action"): {"household.joined", "household.brought_back"},
}


def _module_texts(tree: ast.Module) -> dict[str, str]:
    """The texts a module names at its top (``KEY_ADDED: Final = "key.added"``)."""
    named: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if (
            isinstance(target, ast.Name)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            named[target.id] = value.value
    return named


def _audit_calls(tree: ast.Module) -> list[ast.Call]:
    """The calls of ``append_audit`` in a module: by its name, by a name it is imported as, or as
    an attribute (``log.append_audit``)."""
    names = {"append_audit"} | {
        alias.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name == "append_audit" and alias.asname
    }
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id in names)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "append_audit")
        )
    ]


def _imported_text(tree: ast.Module, name: str) -> str | None:
    """The text a module imports under ``name`` from another module of the code, if it is one."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if (alias.asname or alias.name) == name:
                    value = getattr(importlib.import_module(node.module), alias.name, None)
                    return value if isinstance(value, str) else None
    return None


def audited_actions() -> set[str]:
    """Each action the code audits: the ``action=`` of each call of ``append_audit``, a text as
    it is written, named at the top of its module or imported from another; or, for an action
    built where it is written, the actions its site writes (``DYNAMIC_SITES``, each written as it
    is in its module). Any other action fails: its site must be listed."""
    root = Path(domo_core.__file__).parent
    actions: set[str] = set()
    unknown: list[tuple[str, str]] = []
    found_sites: set[tuple[str, str]] = set()
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        named = _module_texts(tree)
        module = path.relative_to(root).as_posix()
        for call in _audit_calls(tree):
            given = [keyword.value for keyword in call.keywords if keyword.arg == "action"]
            assert len(given) == 1 and not call.args[1:], f"{module}:{call.lineno}"
            (action,) = given
            text: str | None = None
            if isinstance(action, ast.Constant) and isinstance(action.value, str):
                text = action.value
            elif isinstance(action, ast.Name):
                text = named.get(action.id) or _imported_text(tree, action.id)
            if text is not None:
                actions.add(text)
                continue
            site = (module, ast.unparse(action))
            if site not in DYNAMIC_SITES:
                unknown.append((f"{module}:{call.lineno}", site[1]))
                continue
            found_sites.add(site)
            for dynamic in DYNAMIC_SITES[site]:
                assert f'"{dynamic}"' in source, (site, dynamic)
            actions |= DYNAMIC_SITES[site]
    assert unknown == [], "list these sites in DYNAMIC_SITES, with the actions they write"
    assert found_sites == set(DYNAMIC_SITES)
    return actions


def test_every_audited_action_has_its_line():
    actions = audited_actions()
    assert len(actions) > 50
    missing = sorted(action for action in actions if not has_meaning(f"audit.line.{action}"))
    assert missing == []
    # And every line says who did it (or why not), when, and the facts the page puts in.
    for action in actions:
        facts = set(audit_routes.LINE_FACTS.get(action, {}))
        assert "when" in holes(f"audit.line.{action}"), action
        assert holes(f"audit.line.{action}") - {"who"} == {"when"} | facts, action
    assert set(audit_routes.LINE_FACTS) <= actions
    assert holes("audit.line.other") == {"who", "when"}
    # And no line is left for an action the code no longer audits.
    lines = {key for key in all_keys() if key.startswith("audit.line.")}
    assert lines == {f"audit.line.{action}" for action in actions} | {"audit.line.other"}


async def test_a_hole_the_page_cannot_fill_shows_a_sign(client, owner_account, session):
    # A text kept with a hole the page has no value for (one kept for an older version, say):
    # the line shows a sign there, and the page still answers.
    await session.execute(
        update(WrittenText)
        .where(WrittenText.key == "audit.line.auth.login")
        .values(text="{when}: {who} signed in with {method}.")
    )
    await log_in(client)
    r = await client.get("/audit")
    assert r.status_code == 200
    [line] = lines_of(r.text).values()
    assert line.endswith(f"Owner signed in with {SIGN}.")


async def test_an_amount_that_is_no_number_shows_a_sign(client, owner_account, session):
    await log_in(client)
    turn = await append_audit(
        session,
        actor=f"person:{owner_account.person_id}",
        action="chat.turn",
        details={"model": "small-model-1", "cost_usd": "NaN"},
    )
    limit = await append_audit(
        session,
        actor=f"person:{owner_account.person_id}",
        action="spending.limit_set",
        details={"amount": "Infinity", "currency": "EUR"},
    )
    lines = lines_of((await client.get("/audit")).text)
    assert lines[turn.id].endswith(f"for {SIGN}.") and "NaN" not in lines[turn.id]
    assert lines[limit.id].endswith(f"of {SIGN}.") and "nfinity" not in lines[limit.id]


async def test_a_removed_limit_reads_as_removed(client, owner_account, session):
    await log_in(client)
    removed = await append_audit(
        session,
        actor=f"person:{owner_account.person_id}",
        action="spending.limit_removed",
        details={"provider_id": "p-0123abcd", "person": None},
    )
    lines = lines_of((await client.get("/audit")).text)
    assert lines[removed.id] == t(
        "audit.line.spending.limit_removed", "en", who="Owner", when=in_words(removed.ts)
    )


def test_every_decision_has_its_words():
    words = {
        "outcome": set(get_args(DecideOutcome)) | set(get_args(AmendOutcome)),
        "verdict": set(get_args(Verdict)),
        "barrier": {"allow", "needs_approval", "refuse"},
    }
    for kind, values in words.items():
        assert all(has_meaning(f"audit.{kind}.{value}") for value in values), kind
        assert all(not holes(f"audit.{kind}.{value}") for value in values), kind


# What the details of a key's actions may hold that no line may show: a secret's reference, the
# id of a request of the vault, the path of a form and its token.
SECRETS = {
    "ref": "provider:p-0123abcd:api_key",
    "admin_ref": "provider:p-0123abcd:admin_key",
    "request_id": "req-9f8e7d6c",
    "form_path": "/vault/r/Zm9ybS10b2tlbi1zaG93bi1vbmNl",
    "provider_id": "p-0123abcd",
}


async def test_no_secret_reaches_an_audit_line(client, owner_account, session):
    await log_in(client)
    # The facts a line shows are never read from where a secret, a reference or a form is kept.
    read = {detail for facts in audit_routes.LINE_FACTS.values() for detail, _ in facts.values()}
    assert not read & (set(SECRETS) | FORBIDDEN_DETAIL_KEYS | {"confirmation", "why", "reason"})
    # Every kind of line, its details holding all of these as well.
    actions = audited_actions() | {"test.event"}
    for action in sorted(actions):
        details: dict[str, object] = {**SECRETS, "check": "ok", "host": "api.example.com"}
        for detail, kind in audit_routes.LINE_FACTS.get(action, {}).values():
            details.setdefault(detail, {"count": 3, "day": "2030-01-05"}.get(kind, "1.5"))
        await append_audit(
            session, actor=f"person:{owner_account.person_id}", action=action, details=details
        )
    html = (await client.get("/audit")).text
    assert len(lines_of(html)) == len(actions) + 1  # and the login
    for value in SECRETS.values():
        assert value not in html
    assert "/vault/" not in html


async def test_the_page_is_translated(client, owner_account, session):
    await log_in(client)
    english = await client.get("/audit")
    await reads_in(session, owner_account.person_id, "fr")
    french = await client.get("/audit")
    # The page escapes apostrophes (d&#39;audit): compare the text a person would read.
    french_text = unescape(french.text)
    assert in_language("fr", "audit.chain.ok", count="1") in french_text
    assert f">{t('audit.chain.ok', 'en', count='1')}<" not in french_text
    assert in_language("fr", "audit.title") in french_text
    assert in_language("fr", "audit.intro", limit="200") in french_text
    assert t("audit.intro", "en", limit="200") in unescape(english.text)
    assert chain_status(french.text) == "ok"  # the hook for tests does not depend on the language


async def test_what_is_recorded_is_escaped(client, owner_account, session):
    payload = "<script>alert(1)</script>"
    await log_in(client)
    await append_audit(
        session,
        actor=f"person:{owner_account.person_id}",
        action="household.invited",
        details={"name": payload, "role": "guest"},
    )
    await append_audit(
        session, actor=f"person:{payload}", action=f"t.{payload}", details={"note": payload}
    )
    html = (await client.get("/audit")).text
    assert payload not in html
    # The name a line shows, escaped; the raw actor, action and details are never shown.
    assert html.count("&lt;script&gt;") == 1


# --- tampering -------------------------------------------------------------------------------


async def test_a_tampered_chain_is_reported_with_the_entry(client, owner_account, session):
    await log_in(client)
    entries = await append_n(session, 3)
    await session.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_append_only"))
    await session.execute(text(TAMPER), {"id": entries[1].id})
    r = await client.get("/audit")
    assert r.status_code == 200
    assert chain_status(r.text) == "broken"
    assert t("audit.chain.broken", "en", id=str(entries[1].id)) in r.text
    assert 'role="alert"' in r.text


async def test_a_tampered_chain_is_reported_in_the_language_of_the_reader(
    client, owner_account, session
):
    await log_in(client)
    await reads_in(session, owner_account.person_id, "fr")
    entries = await append_n(session, 2)
    await session.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_append_only"))
    await session.execute(text(TAMPER), {"id": entries[0].id})
    r = await client.get("/audit")
    assert chain_status(r.text) == "broken"
    assert in_language("fr", "audit.chain.broken", id=str(entries[0].id)) in unescape(r.text)


HUGE_INTEGER = "1" * 5000  # more digits than Python decodes

# What hand-editing the log can leave behind, and the page must still show (it reports the
# tampering, it does not fail on it).
UNREADABLE = [
    pytest.param("UPDATE audit_log SET details = 'null'::jsonb WHERE id = :id", id="null details"),
    pytest.param("UPDATE audit_log SET details = '[1]'::jsonb WHERE id = :id", id="list details"),
    pytest.param(
        f"UPDATE audit_log SET details = '{{\"x\": {HUGE_INTEGER}}}'::jsonb WHERE id = :id",
        id="a huge number",
    ),
    pytest.param("UPDATE audit_log SET ts = 'infinity' WHERE id = :id", id="infinite time"),
    pytest.param(
        "UPDATE audit_log SET ts = '10000-01-01 00:00:00+00' WHERE id = :id", id="far future"
    ),
    pytest.param(
        "UPDATE audit_log SET ts = '0001-01-01 00:00:00+00 BC' WHERE id = :id", id="far past"
    ),
]


@pytest.mark.parametrize("statement", UNREADABLE)
async def test_entries_that_were_edited_into_something_odd_do_not_break_the_page(
    client, owner_account, session, statement
):
    await log_in(client)
    entries = await append_n(session, 3)
    await session.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_append_only"))
    await session.execute(text(statement), {"id": entries[1].id})
    r = await client.get("/audit")
    assert r.status_code == 200
    assert chain_status(r.text) == "broken"
    assert t("audit.chain.broken", "en", id=str(entries[1].id)) in r.text
    assert entries[1].id in shown_ids(r.text)  # the edited entry is still listed

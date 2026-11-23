"""Going out, on the real stack (step 2, spec 13): a program of Domo sends HTTP requests through
the gatekeeper, to the addresses open to it, with an account whose secret it never sees.

Both installations of a device of the house: on another machine (``device``, on the gatekeeper's
way out) and on the machine that hosts Domo (``device-on-host``, published on a port of this
machine and reached as the host, ``host.docker.internal``). And a site with a login, over HTTPS
with the tests' own authority (``site``). See ``compose.going-out.yaml``.

Everything goes through the core's real path: the people, the addresses and the accounts are
written by the core's own services, the accounts' secrets are typed into the vault's forms through
the edge as a person would, and each program is launched by hand for Dana
(``launch.launch_by_hand``), its requests judged by the barrier, checked by the core and sent by the
gatekeeper. The fake model is not needed.

The owner's yes to a port of the host is the opening of that address by the owner, as the core's
service records it once he said yes (the conversation that asks him is the browser journey's).
Not tested here (ruling R12): a name whose answer changes between the core's look and the send,
refused ``zone_changed`` by the gatekeeper; it needs a resolver that changes its answer, which the
stack does not have (the gatekeeper's unit tests cover it).
"""

import base64
import json
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from .stack_support import GOING_OUT_HOST_DEVICE_PORT, GoingOutSecrets, GoingOutStack

pytestmark = pytest.mark.stack

HOST_NAME = "host.docker.internal"
HOST_PORT = GOING_OUT_HOST_DEVICE_PORT
# The account of the devices: a token, as a header.
DEVICE_PLACE = {"kind": "header", "name": "authorization", "template": "Bearer {value}"}
# The account of the site: a user and a password, in the body of its login request.
SITE_PLACE = {
    "kind": "login",
    "path": "/login",
    "encoding": "form",
    "user_field": "user",
    "password_field": "password",
}
SITE = "https://site:8443"
DEVICE = "http://device:8000"
ON_HOST = f"http://{HOST_NAME}:{HOST_PORT}"
REDACTED = "[REDACTED]"
# How long the steps may take in all: the slow answer alone waits out_seconds (30).
STEPS_TIMEOUT_S = 600

# The one program of these tests, run by hand for Dana: it sends each request it is given, in
# order, and says what came of each (the answer, or the reason of the refusal).
SENDS = """
import domo

def run(entry):
    domo.begin(entry["arguments"]["operation"])
    came = []
    for r in entry["arguments"]["requests"]:
        try:
            came.append(domo.out(
                r["method"], r["url"], r.get("headers"), r.get("body"), r.get("account")
            ))
        except domo.Refused as refused:
            came.append({"refused": refused.reason})
    return came
"""

# Run in the core's container, with one JSON argument. Phase "setup": Alex (the owner) and Dana
# (an adult) are written, and the house's accounts asked for by Alex, each with its one-time
# form; prints their ids, the forms' paths and the addresses of Domo's own services. Phase
# "steps": each step in order, opening or closing an address (by Alex, Dana or Domo on his
# own) through the core's service, or launching SENDS by hand for Dana; prints what came of each,
# and the audit lines written since the first step.
IN_THE_CORE = """
import asyncio, json, secrets, socket, sys, uuid
from datetime import UTC, datetime
from sqlalchemy import func, select
from domo_common.gatekeeper_api import SecretRequestCreate, location_fields
from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.config import CoreSettings
from domo_core.db import create_engine, load_models, make_session_factory
from domo_core.gatekeeper_client import GatekeeperClient, load_core_token
from domo_core.gatekeeper_client import new_http_client as gatekeeper_http
from domo_core.madetools.launch import ForWhom, LaunchDeps, launch_by_hand
from domo_core.madetools.models import HandRun
from domo_core.madetools.risks import kept_operations, read_operations
from domo_core.madetools.service import fingerprint
from domo_core.outward.service import (
    PLACE, ClosedByOwner, ask_account, bindings_of, close_address, open_address,
)
from domo_core.policy.catalog import DEFAULT_POLICY
from domo_core.sandbox_client import SandboxClient, new_http_client

GIVEN = json.loads(sys.argv[1])
TALK = {
    "talk": {"says": "Talks to a device or a site.", "risks": []},
}


def now():
    return datetime.now(UTC)


async def main():
    load_models()
    settings = CoreSettings()
    factory = make_session_factory(create_engine(settings))
    http = gatekeeper_http()
    gatekeeper = GatekeeperClient(
        settings.gatekeeper_url, load_core_token(settings.core_token_file), http
    )
    sandbox_http = new_http_client(settings.sandbox_socket)
    try:
        if GIVEN["phase"] == "setup":
            found = await setup(factory, gatekeeper)
        else:
            found = await steps(factory, gatekeeper, SandboxClient(sandbox_http))
    finally:
        await http.aclose()
        await sandbox_http.aclose()
    print(json.dumps(found))


async def setup(factory, gatekeeper):
    tag = secrets.token_hex(3)
    async with factory() as session:
        alex = Person(display_name=f"Alex {tag}", role="owner")
        dana = Person(display_name=f"Dana {tag}", role="adult")
        session.add_all([alex, dana])
        await session.flush()
        forms = {}
        for name, account in GIVEN["accounts"].items():
            place = PLACE.validate_python(account["place"])
            asked = await ask_account(
                session,
                gatekeeper,
                name=name,
                person_id=None,
                giver=alex,
                hosts=[tuple(h) for h in account["hosts"]],
                place=place,
                by=str(alex.id),
                form=None,
            )
            view = await gatekeeper.create_secret_request(
                SecretRequestCreate(
                    ref=asked.account.ref,
                    purpose="An account of the tests.",
                    bindings=bindings_of(asked.account),
                    fields=location_fields(place),
                )
            )
            forms[name] = view.form_path
        await session.commit()
    own = {name: socket.gethostbyname(name) for name in ("core", "db", "gatekeeper", "edge")}
    return {"alex": str(alex.id), "dana": str(dana.id), "forms": forms, "own": own}


async def steps(factory, gatekeeper, sandbox):
    deps = LaunchDeps(
        session_factory=factory,
        sandbox=sandbox,
        clock=now,
        policy_base=DEFAULT_POLICY,
        gatekeeper=gatekeeper,
    )
    alex, dana = uuid.UUID(GIVEN["alex"]), uuid.UUID(GIVEN["dana"])
    by = {"alex": str(alex), "dana": str(dana), "domo": "domo"}
    async with factory() as session:
        first = await session.scalar(select(func.coalesce(func.max(AuditEntry.id), 0)))
        person = await session.get(Person, dana)
    found = {}
    for step in GIVEN["steps"]:
        do = step["do"]
        if do == "open":
            async with factory() as session:
                try:
                    await open_address(
                        session, tool_name=None, host=step["host"], port=step["port"],
                        on_host=step.get("on_host", False), by=by[step["by"]], now=now(),
                        reopening=step.get("reopening", False),
                    )
                    await session.commit()
                    came = "opened"
                except ClosedByOwner:
                    came = "closed_by_owner"
        elif do == "close":
            async with factory() as session:
                closed = await close_address(
                    session, tool_name=None, host=step["host"], port=step["port"],
                    by=by[step["by"]], now=now(),
                )
                await session.commit()
                came = "closed" if closed else "not_open"
        else:
            async with factory() as session:
                run = HandRun(
                    code=GIVEN["program"],
                    fingerprint=fingerprint(GIVEN["program"]),
                    operations=kept_operations(read_operations(TALK)),
                    arguments={
                        "operation": step.get("operation", "talk"),
                        "requests": step["requests"],
                    },
                    opened_by=dana,
                    for_person_id=dana,
                    max_seconds=120,
                    created_at=now(),
                )
                session.add(run)
                await session.commit()
                run_id = run.id
            launched = await launch_by_hand(deps, run_id, for_whom=ForWhom(person))
            came = {"status": launched.status, "result": launched.result}
        if "name" in step:
            found[step["name"]] = came
    async with factory() as session:
        rows = await session.scalars(
            select(AuditEntry).where(AuditEntry.id > first).order_by(AuditEntry.id)
        )
        found["audit"] = [
            {"actor": row.actor, "action": row.action, "details": row.details} for row in rows
        ]
    return found


asyncio.run(main())
"""


def in_the_core(stack: GoingOutStack, given: dict[str, Any]) -> dict[str, Any]:
    result = stack.compose(
        "exec", "-T", "core", "python", "-c", IN_THE_CORE, json.dumps(given),
        timeout=STEPS_TIMEOUT_S,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr[-3000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def secrets_of(stack: GoingOutStack) -> GoingOutSecrets:
    assert stack.going_out is not None
    return stack.going_out


def get(url: str, account: str | None = None, **more: Any) -> dict[str, Any]:
    return {"method": "GET", "url": url, "account": account, **more}


def post(url: str, body: str, account: str | None = None, **more: Any) -> dict[str, Any]:
    return {"method": "POST", "url": url, "body": body, "account": account, **more}


def opened(host: str, port: int, *, by: str = "alex", on_host: bool = False) -> dict[str, Any]:
    return {"do": "open", "host": host, "port": port, "on_host": on_host, "by": by}


def fill_form(stack: GoingOutStack, form_path: str, fields: dict[str, str]) -> None:
    """Type an account into its vault form, through the edge, as its giver would."""
    response = httpx.post(stack.base_url + form_path, data=fields, verify=False, timeout=30)
    assert response.status_code == 200, response.status_code


@dataclass(frozen=True)
class Target:
    """An address a program names, opened (``on_host``: as the host's), and the URL it asks."""

    host: str
    port: int
    on_host: bool
    scheme: str = "http"

    @property
    def url(self) -> str:
        return url_of(self.host, self.port, self.scheme)


def never_targets(stack: GoingOutStack, own: dict[str, str]) -> dict[str, Target]:
    """Every address of Domo's own services a program could name, by name and by address: the
    core, its database, the gatekeeper's internal port, and the edge by every address that leads
    to it from the gatekeeper."""
    settings = {"https": 18463, "http": 18093}
    lan = stack.inspect_network("lan")["IPAM"]["Config"][0]["Gateway"]
    egress = stack.inspect_network("egress")["IPAM"]["Config"][0]["Gateway"]
    gatekeeper_networks = stack.inspect("gatekeeper")["NetworkSettings"]["Networks"]
    gatekeeper_out = gatekeeper_networks[stack.network_name("egress")]["IPAddress"]
    edge_networks = stack.inspect("edge")["NetworkSettings"]["Networks"]
    edge_lan = edge_networks[stack.network_name("lan")]["IPAddress"]
    targets = {
        "core by name": Target("core", 8000, False),
        "core by address": Target(own["core"], 8000, False),
        "db by name": Target("db", 5432, False),
        "db by address": Target(own["db"], 5432, False),
        "gatekeeper by name": Target("gatekeeper", 8081, False),
        "gatekeeper by address": Target(own["gatekeeper"], 8081, False),
        "gatekeeper on its way out": Target(gatekeeper_out, 8081, False),
        "gatekeeper by loopback": Target("127.0.0.1", 8081, False),
        "gatekeeper by localhost": Target("localhost", 8081, False),
        "edge by name": Target("edge", settings["https"], False, "https"),
        "edge by address": Target(own["edge"], settings["https"], False, "https"),
        # Its address on its own lan: HTTPS, and plain HTTP on both of its ports.
        "edge on the lan (https)": Target(edge_lan, settings["https"], False, "https"),
    }
    for kind, port in settings.items():
        targets[f"edge on the lan ({kind} port, plain)"] = Target(edge_lan, port, False)
        targets[f"edge through the lan gateway ({kind})"] = Target(lan, port, True, kind)
        targets[f"edge through the way out's gateway ({kind})"] = Target(egress, port, True, kind)
        targets[f"edge through the host's name ({kind})"] = Target(HOST_NAME, port, True, kind)
        targets[f"edge through localhost ({kind})"] = Target("localhost", port, True, kind)
    return targets


def url_of(host: str, port: int, scheme: str = "http") -> str:
    shown = f"[{host}]" if ":" in host else host
    return f"{scheme}://{shown}:{port}/"


@pytest.fixture(scope="module")
def going_out(going_out_stack: GoingOutStack) -> dict[str, Any]:
    """What came of every step, in the order below, with what the tests need to read it."""
    stack, given = going_out_stack, secrets_of(going_out_stack)
    made = in_the_core(
        stack,
        {
            "phase": "setup",
            "accounts": {
                "device": {
                    "hosts": [["device", 8000], [HOST_NAME, HOST_PORT]],
                    "place": DEVICE_PLACE,
                },
                "site": {"hosts": [["site", 8443]], "place": SITE_PLACE},
            },
        },
    )
    fill_form(stack, made["forms"]["device"], {"value": given.device_token})
    fill_form(
        stack, made["forms"]["site"], {"user": given.site_user, "password": given.site_password}
    )
    never = never_targets(stack, made["own"])
    core_url = url_of(made["own"]["core"], 8000)
    steps: list[dict[str, Any]] = [
        opened("device", 8000),
        opened("site", 8443),
        *(opened(t.host, t.port, on_host=t.on_host) for t in never.values()),
        # The device on another machine, with the house's account.
        {
            "do": "out",
            "name": "other machine",
            "requests": [
                get(f"{DEVICE}/things", "device"),
                get(f"{DEVICE}/state/lamp", "device"),
                post(f"{DEVICE}/state/lamp", json.dumps({"state": "on"}), "device"),
                get(f"{DEVICE}/state/lamp", "device"),
                get(f"{DEVICE}/things"),
                get(f"{DEVICE}/things", "no_such_account"),
            ],
        },
        {
            "do": "out",
            "name": "echo",
            "requests": [
                get(f"{DEVICE}/echo", "device"),
                get(f"{DEVICE}/echo", headers={"Authorization": "Bearer the-program-s-own"}),
            ],
        },
        {
            "do": "out",
            "name": "never",
            "requests": [get(t.url) for t in never.values()],
        },
        {
            "do": "out",
            "name": "redirects",
            "requests": [
                get(f"{DEVICE}/redirect?to={core_url}"),
                get(f"{DEVICE}/redirect?to=http://core:8000/"),
                get(f"{DEVICE}/redirect?to={DEVICE}/health"),
            ],
        },
        {"do": "out", "name": "endless", "requests": [get(f"{DEVICE}/endless")]},
        {"do": "out", "name": "slow", "requests": [get(f"{DEVICE}/slow?seconds=45")]},
        # The device on the machine that hosts Domo: Dana opens its port as any address (she
        # has not the right to open the host), and the gatekeeper finds the host there.
        opened(HOST_NAME, HOST_PORT, by="dana"),
        {
            "do": "out",
            "name": "host before the yes",
            "requests": [get(f"{ON_HOST}/state/lamp", "device"), get(f"{ON_HOST}/health")],
        },
        # The owner said yes: the port is opened as the host's, by him.
        opened(HOST_NAME, HOST_PORT, on_host=True),
        {
            "do": "out",
            "name": "host after the yes",
            "requests": [
                get(f"{ON_HOST}/things", "device"),
                get(f"{ON_HOST}/state/lamp", "device"),
                post(f"{ON_HOST}/state/lamp", json.dumps({"state": "on"}), "device"),
                get(f"{ON_HOST}/state/lamp", "device"),
            ],
        },
        # A redirect from the house to an open address of another zone (ruling R6).
        {
            "do": "out",
            "name": "redirect to the host",
            "requests": [get(f"{DEVICE}/redirect?to={ON_HOST}/health")],
        },
        # The owner closes the device: nothing opens it again blindly; Domo's reopening, which
        # weighed that closing (his judgement, ruling R24), does.
        {"do": "close", "name": "owner closes", "host": "device", "port": 8000, "by": "alex"},
        {"do": "out", "name": "after the closing", "requests": [get(f"{DEVICE}/health")]},
        {**opened("device", 8000, by="dana"), "name": "dana opens again"},
        {**opened("device", 8000, by="domo"), "name": "domo opens again"},
        {"do": "out", "name": "still closed", "requests": [get(f"{DEVICE}/health")]},
        {**opened("device", 8000, by="domo"), "reopening": True, "name": "domo reopens"},
        {"do": "out", "name": "open again", "requests": [get(f"{DEVICE}/things", "device")]},
        # The site: the login places the account, the session is the gatekeeper's.
        {
            "do": "out",
            "name": "site",
            "requests": [
                get(f"{SITE}/account", "site"),
                post(f"{SITE}/login", "remember=yes", "site"),
                get(f"{SITE}/account", "site"),
                get(f"{SITE}/account"),
            ],
        },
    ]
    came = in_the_core(
        stack,
        {
            "phase": "steps",
            "alex": made["alex"],
            "dana": made["dana"],
            "program": SENDS,
            "steps": steps,
        },
    )
    came["never names"] = list(never)
    for name, value in came.items():
        print(f"going out, {name}: {json.dumps(value)}")
    return came


def answers(came: dict[str, Any]) -> list[dict[str, Any]]:
    """What each request of a launch gave: the answer, or ``{"refused": reason}``."""
    assert came["status"] == "done", came
    return came["result"]


def body(answer: dict[str, Any]) -> Any:
    assert "refused" not in answer, answer
    return json.loads(answer["body"])


# --- the other machine ----------------------------------------------------------------------------


def test_a_program_reads_and_sets_a_state_on_another_machine_with_a_house_account(going_out):
    things, state, set_to, again, without, unknown = answers(going_out["other machine"])
    assert things["status"] == 200 and body(things) == {"things": ["lamp"]}
    assert body(state) == {"thing": "lamp", "state": "off"}
    assert set_to["status"] == 200 and body(set_to) == {"thing": "lamp", "state": "on"}
    assert body(again) == {"thing": "lamp", "state": "on"}
    # Without the account the device does not know it; an account it may not use is refused.
    assert without["status"] == 401
    assert unknown == {"refused": "account_not_yours"}


def test_the_token_the_device_echoes_comes_back_scrubbed(going_out_stack, going_out):
    token = secrets_of(going_out_stack).device_token
    echo, own = answers(going_out["echo"])
    echoed = body(echo)
    # What the account placed is masked as it was placed: the whole header.
    assert echoed["authorization"] == REDACTED
    assert echo["headers"]["x-echo-authorization"] == REDACTED
    # Every request says it comes from a tool of Domo.
    assert echoed["via"] == "1.1 domo-out"
    # What identifies is never the program's to send.
    assert body(own)["authorization"] == ""
    assert token not in json.dumps(going_out)


def test_the_header_the_device_echoes_in_base64_comes_back_scrubbed(going_out_stack, going_out):
    """A device that echoes the header it received in base64: the token must not be read back
    by decoding it."""
    token = secrets_of(going_out_stack).device_token
    echo, _ = answers(going_out["echo"])
    echoed = base64.b64decode(body(echo)["authorization_b64"]).decode("utf-8", errors="replace")
    assert token not in echoed


# --- what is never reachable --------------------------------------------------------------------


def test_domo_s_own_services_are_never_reachable_by_name_and_by_address(going_out):
    """The edge's lan included: its address there, and the lan's gateway, are of Domo's own
    networks."""
    came = dict(zip(going_out["never names"], answers(going_out["never"]), strict=True))
    assert sum(name.startswith("edge on the lan") for name in came) == 3
    reached = {name: a for name, a in came.items() if a != {"refused": "never_reachable"}}
    assert reached == {}


def test_a_redirect_to_the_core_is_a_bad_redirect(going_out):
    to_the_address, to_the_name, same_zone = answers(going_out["redirects"])
    assert to_the_address == {"refused": "bad_redirect"}
    assert to_the_name == {"refused": "bad_redirect"}
    # The control: a redirect that stays on the device is followed.
    assert same_zone["status"] == 200 and body(same_zone) == {"ok": True}


def test_an_endless_answer_is_too_big_and_a_slow_one_too_slow(going_out):
    [endless] = answers(going_out["endless"])
    assert endless == {"refused": "too_big"}
    [slow] = answers(going_out["slow"])
    assert slow == {"refused": "too_slow"}


# --- the machine that hosts Domo ---------------------------------------------------------------


def test_the_host_s_port_is_closed_before_the_owner_s_yes(going_out):
    for answer in answers(going_out["host before the yes"]):
        assert answer == {"refused": "host_not_opened"}


def test_after_the_owner_s_yes_a_program_reads_and_sets_a_state_on_the_host(going_out):
    """Under an ordinary operation (it declares no risk): a port of the host opened as the
    host's is the house."""
    came = answers(going_out["host after the yes"])
    assert all("refused" not in answer for answer in came), came
    things, before, set_to, again = came
    assert things["status"] == 200 and body(things) == {"things": ["lamp"]}
    assert body(before) == {"thing": "lamp", "state": "off"}
    assert body(set_to) == {"thing": "lamp", "state": "on"}
    assert body(again) == {"thing": "lamp", "state": "on"}


def test_a_redirect_from_the_house_to_the_host_is_a_bad_redirect(going_out):
    assert answers(going_out["redirect to the host"]) == [{"refused": "bad_redirect"}]


# --- what the owner closed ------------------------------------------------------------------------


def test_what_the_owner_closed_opens_again_only_on_a_reopening_that_weighed_it(going_out):
    """At the service: an opening that did not weigh the owner's closing is refused, whoever
    asks; the one Domo judged knowing it (``reopening``) opens it (the judgement itself is the
    tool's, ``address_open``: its unit tests and the end-to-end story play it)."""
    assert going_out["owner closes"] == "closed"
    assert answers(going_out["after the closing"]) == [{"refused": "closed_address"}]
    assert going_out["dana opens again"] == "closed_by_owner"
    assert going_out["domo opens again"] == "closed_by_owner"
    assert answers(going_out["still closed"]) == [{"refused": "closed_address"}]
    assert going_out["domo reopens"] == "opened"
    [things] = answers(going_out["open again"])
    assert things["status"] == 200


# --- the site -----------------------------------------------------------------------------------


def test_the_login_places_the_account_and_the_session_stays_with_the_gatekeeper(
    going_out_stack, going_out
):
    given = secrets_of(going_out_stack)
    before, login, account, without = answers(going_out["site"])
    # No session yet.
    assert before["status"] == 401
    assert login["status"] == 200
    assert body(login) == {"welcome": REDACTED, "password_seen": REDACTED}
    # The cookie is kept and never returned.
    assert "set-cookie" not in {name.lower() for name in login["headers"]}
    assert account["status"] == 200
    assert body(account) == {"account": REDACTED, "session_seen": REDACTED}
    # Without the account, the program has no session of its own.
    assert without["status"] == 401
    dumped = json.dumps(going_out)
    assert given.site_password not in dumped and given.site_user not in dumped


# --- what is written ----------------------------------------------------------------------------


def test_each_request_out_has_its_audit_lines_with_metadata_only(going_out_stack, going_out):
    given = secrets_of(going_out_stack)
    lines = [line for line in going_out["audit"] if line["action"] == "madetool.out_done"]
    assert lines, going_out["audit"]
    for line in lines:
        assert set(line["details"]) >= {"host", "port", "method", "status", "refused", "zone"}
    dumped = json.dumps(going_out["audit"])
    for secret in (given.device_token, given.site_password, given.site_user):
        assert secret not in dumped
    # Never a path nor a query.
    for path in ("/things", "/state/lamp", "/redirect", "/login", "/account", "seconds="):
        assert path not in dumped, path


def test_no_secret_is_written_in_a_log(going_out_stack, going_out):
    given = secrets_of(going_out_stack)
    for service in ("core", "gatekeeper", "edge", "device", "device-on-host", "site"):
        log = going_out_stack.logs(service)
        for secret in (given.device_token, given.site_password):
            assert secret not in log, service


def test_the_edge_of_this_stack_refuses_a_request_of_my_tools(going_out_stack):
    """The edge's refusal, seen from the gatekeeper's own side (core_net, its plain HTTP port),
    where the relay never sends: whatever reaches the edge with the mark of a tool is refused,
    the others are sent on to the page."""
    code = (
        "import httpx\n"
        "for via in ('1.1 domo-out', ''):\n"
        "    headers = {'Via': via} if via else {}\n"
        "    r = httpx.get('http://edge:18093/', headers=headers, trust_env=False)\n"
        "    print(r.status_code)\n"
    )
    result = going_out_stack.exec("gatekeeper", ["python", "-c", code])
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["403", "308"]

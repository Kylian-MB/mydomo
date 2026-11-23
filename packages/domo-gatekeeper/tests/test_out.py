"""The relay for programs, against real local servers (one per case) and a fake resolver.

Every server listens on 127.0.0.1. The resolver sends the test names there, and the zone of that
address is set by each case (``zones``): by the address alone, loopback is never reachable.
"""

import asyncio
import base64
import datetime
import gzip
import json
import socket
import ssl
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from domo_common.gatekeeper_api import (
    BasicLocation,
    Binding,
    HeaderLocation,
    LoginLocation,
    OpenAddress,
    OutRequest,
    OutResponse,
    Zone,
)
from domo_gatekeeper.errors import OutRefused
from domo_gatekeeper.internal_api import create_internal_app
from domo_gatekeeper.out import VIA, ProgramRelay
from domo_gatekeeper.providers import ProviderRegistry
from domo_gatekeeper.reach import IPAddress, Lookup, Reach, zone
from domo_gatekeeper.scrub import MASK
from domo_gatekeeper.secret_requests import SecretRequestStore
from domo_gatekeeper.sessions import Sessions
from domo_gatekeeper.vault import Vault, join_account
from fastapi import FastAPI

LOCAL = IPv4Address("127.0.0.1")
OTHER_LOOPBACK = IPv4Address("127.0.0.2")  # loopback: never reachable by its address
CORE = IPv4Address("10.9.0.5")  # in Domo's own network below
EMPTY = Reach(own_networks=(), host_addresses=())
OWN = Reach(own_networks=(IPv4Network("10.9.0.0/16"),), host_addresses=())

TOKEN = "t0ken-s3cret-value"
USER = "alex"
PASSWORD = "pa55word-secret"
SID = "s3ssion-cookie-value"


# --- Local servers --------------------------------------------------------------------------------


@dataclass
class Seen:
    """A request as a test server received it."""

    method: str
    target: str
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str) -> str | None:
        values = self.all(name)
        return ", ".join(values) if values else None

    def all(self, name: str) -> list[str]:
        return [value for key, value in self.headers if key == name.lower()]


Handler = Callable[[Seen, asyncio.StreamWriter], Awaitable[None]]


def raw(
    status: int = 200,
    headers: Sequence[tuple[str, str]] = (),
    body: bytes = b"",
    *,
    length: bool = True,
) -> bytes:
    """An HTTP/1.1 answer, closing the connection after it."""
    lines = [f"HTTP/1.1 {status} Status", *(f"{name}: {value}" for name, value in headers)]
    if length:
        lines.append(f"Content-Length: {len(body)}")
    lines.append("Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def reply(answer: bytes) -> Handler:
    async def handler(seen: Seen, writer: asyncio.StreamWriter) -> None:
        writer.write(answer)

    return handler


def routes(by_path: Mapping[str, Handler]) -> Handler:
    async def handler(seen: Seen, writer: asyncio.StreamWriter) -> None:
        path = seen.target.split("?")[0]
        await by_path.get(path, reply(raw(404)))(seen, writer)

    return handler


OK = reply(raw(200, [("Content-Type", "text/plain")], b"hello"))


class Site:
    """A local HTTP (or HTTPS) server: every request it got is in ``seen``."""

    def __init__(self, handler: Handler = OK, *, tls: ssl.SSLContext | None = None) -> None:
        self._handler = handler
        self._tls = tls
        self.seen: list[Seen] = []
        self.port = 0
        self._tasks: set[asyncio.Task[None]] = set()
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> "Site":
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0, ssl=self._tls)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            first, *lines = head.decode("latin-1").split("\r\n")
            method, target, _ = first.split(" ", 2)
            headers = [
                (name.strip().lower(), value.strip())
                for name, _, value in (line.partition(":") for line in lines if line)
            ]
            length = int(dict(headers).get("content-length", "0"))
            body = await reader.readexactly(length) if length else b""
            seen = Seen(method, target, headers, body)
            self.seen.append(seen)
            await self._handler(seen, writer)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()
            self._tasks.discard(task)


def free_port() -> int:
    """A port nothing listens on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# --- TLS ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Pki:
    server: ssl.SSLContext
    client: ssl.SSLContext
    names_asked: list[str | None] = field(default_factory=list[str | None])


def _cert(
    subject: str,
    key: ec.EllipticCurvePrivateKey,
    issuer: tuple[str, ec.EllipticCurvePrivateKey] | None,
    *,
    san: str | None = None,
) -> x509.Certificate:
    now = datetime.datetime.now(datetime.UTC)
    issuer_name, issuer_key = issuer or (subject, key)
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_name)]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), False
        )
    )
    if san is None:
        builder = builder.add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
        builder = builder.add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False), True
        )
    else:
        builder = builder.add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(san)]), False)
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False
        )
    return builder.sign(issuer_key, hashes.SHA256())


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> Pki:
    """A certificate authority the relay trusts, and a server certificate for site.test."""
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = _cert("test authority", ca_key, None)
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = _cert("site.test", key, ("test authority", ca_key), san="site.test")
    folder: Path = tmp_path_factory.mktemp("pki")
    (folder / "cert.pem").write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    (folder / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server.load_cert_chain(folder / "cert.pem", folder / "key.pem")
    pki = Pki(
        server=server,
        client=ssl.create_default_context(
            cadata=ca.public_bytes(serialization.Encoding.PEM).decode()
        ),
    )

    def remember(_: ssl.SSLObject, name: str | None, __: ssl.SSLContext) -> None:
        pki.names_asked.append(name)

    server.sni_callback = remember  # pyright: ignore[reportAttributeAccessIssue]
    return pki


# --- The relay ------------------------------------------------------------------------------------


class Names:
    """A fake resolver: a name answers its lists of addresses in turn (the last one again once
    they are used up); every lookup is noted."""

    def __init__(self, answers: Mapping[str, Sequence[Sequence[IPAddress]]]) -> None:
        self._answers = {name: list(lists) for name, lists in answers.items()}
        self.calls: list[str] = []

    async def __call__(self, name: str) -> list[IPAddress]:
        self.calls.append(name)
        lists = self._answers.get(name)
        if not lists:
            raise OSError("no such name")
        return list(lists.pop(0) if len(lists) > 1 else lists[0])


def local_names(*extra: str) -> Names:
    names = ("device.test", "site.test", "other.test", *extra)
    return Names({name: [[LOCAL]] for name in names})


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_relay(
    vault: Vault,
    *,
    sessions: Sessions | None = None,
    names: Lookup | None = None,
    zones: Mapping[IPAddress, Zone] | None = None,
    reach: Reach = EMPTY,
    tls: ssl.SSLContext | None = None,
    port_zones: Mapping[int, Zone] | None = None,
) -> ProgramRelay:
    """A relay to the local servers; ``port_zones`` sets the zone of a local server by its port
    (every server shares the address 127.0.0.1)."""
    given: Mapping[IPAddress, Zone] = {LOCAL: "house"} if zones is None else zones
    by_port: Mapping[int, Zone] = port_zones or {}

    def zone_of(address: IPAddress, port: int) -> Zone:
        if address == LOCAL and port in by_port:
            return by_port[port]
        return given.get(address) or zone(address, reach, port)

    return ProgramRelay(
        vault=vault,
        sessions=sessions or Sessions(),
        reach=reach,
        ssl_context=tls or ssl.create_default_context(),
        lookup=names or local_names(),
        zone_of=zone_of,
    )


def ask(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    account: str | None = None,
    open: Sequence[OpenAddress] | None = None,
    seconds: int = 5,
    most_bytes: int = 1_000_000,
    idle: int = 600,
    resolve_only: bool = False,
    expect_zone: Zone | None = None,
) -> OutRequest:
    if open is None:
        parsed = httpx.URL(url)
        default = 443 if parsed.scheme == "https" else 80
        open = [OpenAddress(host=parsed.host, port=parsed.port or default)]
    return OutRequest.model_validate(
        {
            "method": method,
            "url": url,
            "headers": headers or {},
            "body_b64": None if body is None else base64.b64encode(body).decode(),
            "account": account,
            "open": [address.model_dump() for address in open],
            "seconds": seconds,
            "most_bytes": most_bytes,
            "session_idle_seconds": idle,
            "resolve_only": resolve_only,
            **({} if expect_zone is None else {"expect_zone": expect_zone}),
        }
    )


def body_of(answer: OutResponse) -> bytes:
    return base64.b64decode(answer.body_b64)


async def refused(relay: ProgramRelay, request: OutRequest) -> str:
    with pytest.raises(OutRefused) as caught:
        await relay.send(request)
    assert str(caught.value) == caught.value.code  # nothing but the code
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    return caught.value.code


def put_token(vault: Vault, host: str = "device.test", name: str = "authorization") -> str:
    template = "Bearer {value}" if name == "authorization" else "{value}"
    vault.put(
        "device:token",
        TOKEN,
        purpose="the device's token",
        bindings=[
            Binding(host=host, location=HeaderLocation(kind="header", name=name, template=template))
        ],
    )
    return "device:token"


def put_basic(vault: Vault) -> str:
    vault.put(
        "site:basic",
        join_account(USER, PASSWORD),
        purpose="the site's account",
        bindings=[Binding(host="site.test", location=BasicLocation(kind="basic"))],
    )
    return "site:basic"


def put_login(vault: Vault, encoding: str = "form", password: str = PASSWORD) -> str:
    location = LoginLocation.model_validate(
        {
            "kind": "login",
            "path": "/login",
            "encoding": encoding,
            "user_field": "user",
            "password_field": "password",
        }
    )
    vault.put(
        "site:login",
        join_account(USER, password),
        purpose="the site's login",
        bindings=[Binding(host="site.test", location=location)],
    )
    return "site:login"


# --- Sending to an open address -------------------------------------------------------------------


async def test_a_request_to_an_open_address_gets_its_answer(vault):
    answer = raw(201, [("Content-Type", "text/plain"), ("X-Thing", "a")], b"made")
    async with Site(reply(answer)) as site:
        result = await make_relay(vault).send(
            ask(f"http://device.test:{site.port}/things?x=1", method="POST", body=b"payload")
        )
    assert (result.status, body_of(result), result.zone, result.hops) == (
        201,
        b"made",
        "house",
        0,
    )
    assert result.headers["x-thing"] == "a" and result.headers["content-type"] == "text/plain"
    [seen] = site.seen
    assert (seen.method, seen.target, seen.body) == ("POST", "/things?x=1", b"payload")
    # The name is kept for the Host header, though the connection goes to the address.
    assert seen.header("host") == f"device.test:{site.port}"


async def test_every_request_says_it_comes_from_a_tool_of_mine(vault):
    async with Site() as site:
        relay = make_relay(vault)
        await relay.send(ask(f"http://device.test:{site.port}/"))
        await relay.send(ask(f"http://device.test:{site.port}/", headers={"Via": "1.0 proxy"}))
    assert [seen.header("via") for seen in site.seen] == [VIA, f"1.0 proxy, {VIA}"]
    assert VIA == "1.1 domo-out"


async def test_a_redirect_hop_says_it_too(vault):
    handler = routes({"/a": reply(raw(302, [("Location", "/b")])), "/b": OK})
    async with Site(handler) as site:
        result = await make_relay(vault).send(
            ask(f"http://device.test:{site.port}/a", headers={"via": "1.0 proxy"})
        )
    assert result.hops == 1
    assert [seen.header("via") for seen in site.seen] == [f"1.0 proxy, {VIA}"] * 2


async def test_length_and_hop_by_hop_headers_of_the_answer_are_not_returned(vault):
    answer = raw(200, [("Keep-Alive", "timeout=5"), ("X-Kept", "yes")], b"abc")
    async with Site(reply(answer)) as site:
        result = await make_relay(vault).send(ask(f"http://device.test:{site.port}/"))
    assert result.headers.get("x-kept") == "yes"
    for name in ("content-length", "connection", "keep-alive"):
        assert name not in result.headers


async def test_a_head_request_gets_no_body(vault):
    async with Site(reply(raw(200, [("Content-Length", "5")], length=False))) as site:
        result = await make_relay(vault).send(
            ask(f"http://device.test:{site.port}/", method="HEAD")
        )
    assert (result.status, body_of(result)) == (200, b"")


# --- Rule 1: an open address ----------------------------------------------------------------------


async def test_a_host_that_is_not_open_is_closed(vault):
    async with Site() as site:
        request = ask(
            f"http://device.test:{site.port}/", open=[OpenAddress(host="site.test", port=site.port)]
        )
        assert await refused(make_relay(vault), request) == "closed_address"
    assert site.seen == []


async def test_another_port_of_an_open_host_is_closed(vault):
    async with Site() as site:
        request = ask(
            f"http://device.test:{site.port}/",
            open=[OpenAddress(host="device.test", port=site.port + 1)],
        )
        assert await refused(make_relay(vault), request) == "closed_address"
    assert site.seen == []


async def test_a_name_is_compared_as_written_not_as_resolved(vault):
    # 127.0.0.1 is where device.test leads, but it is not what was opened.
    async with Site() as site:
        request = ask(
            f"http://device.test:{site.port}/",
            open=[OpenAddress(host="127.0.0.1", port=site.port)],
        )
        assert await refused(make_relay(vault), request) == "closed_address"


async def test_the_default_port_of_the_scheme_is_the_port(vault):
    relay = make_relay(vault)
    for url in ("http://device.test/", "http://device.test:80/", "HTTP://Device.Test/"):
        answer = await relay.send(
            ask(url, open=[OpenAddress(host="device.test", port=80)], resolve_only=True)
        )
        assert answer.zone == "house"
    https = ask("https://device.test/", open=[OpenAddress(host="device.test", port=80)])
    assert await refused(relay, https) == "closed_address"


async def test_a_scheme_other_than_http_is_closed(vault):
    request = ask("ftp://device.test:21/", open=[OpenAddress(host="device.test", port=21)])
    assert await refused(make_relay(vault), request) == "closed_address"


@pytest.mark.parametrize(
    "url",
    [
        "http://user:pw@device.test:8000/",
        "device.test:8000/no-scheme",
        "http:///no-host",
        "http://device.test:99999/",
        "not a url at all",
    ],
)
async def test_a_url_that_cannot_be_sent_is_a_bad_request(vault, url):
    # Credentials in the URL included: a secret goes in a request through an account only.
    request = ask(url, open=[OpenAddress(host="device.test", port=8000)])
    assert await refused(make_relay(vault), request) == "bad_request"


@pytest.mark.parametrize(
    "headers",
    [{"bad name": "x"}, {"x-ok": "line\nbreak"}, {"x-ok": "nul\x00"}, {"x-ok": "é"}],
)
async def test_a_header_that_cannot_be_sent_is_a_bad_request(vault, headers):
    async with Site() as site:
        request = ask(f"http://device.test:{site.port}/", headers=headers)
        assert await refused(make_relay(vault), request) == "bad_request"
    assert site.seen == []


async def test_a_body_that_is_not_base64_is_a_bad_request(vault):
    async with Site() as site:
        request = ask(f"http://device.test:{site.port}/", method="POST").model_copy(
            update={"body_b64": "not base64!"}
        )
        assert await refused(make_relay(vault), request) == "bad_request"


# --- Rule 2: where the address leads --------------------------------------------------------------


async def test_a_loopback_address_is_never_reachable(vault):
    names = Names({"device.test": [[OTHER_LOOPBACK]]})
    request = ask("http://device.test:8000/")
    assert await refused(make_relay(vault, names=names), request) == "never_reachable"


async def test_a_service_of_mine_is_never_reachable_even_open(vault):
    names = Names({"core.test": [[CORE]]})
    relay = make_relay(vault, names=names, reach=OWN)
    assert await refused(relay, ask("http://core.test:8000/")) == "never_reachable"
    assert await refused(relay, ask("http://10.9.0.5:8000/")) == "never_reachable"
    # Even a resolve_only request says nothing more.
    assert await refused(relay, ask("http://core.test:8000/", resolve_only=True)) == (
        "never_reachable"
    )


async def test_a_name_that_gives_no_address_is_unreachable(vault):
    request = ask("http://nowhere.test:8000/")
    assert await refused(make_relay(vault), request) == "unreachable"


async def test_the_machine_that_hosts_me_is_closed_unless_opened_as_such(vault):
    async with Site() as site:
        relay = make_relay(vault, zones={LOCAL: "host"})
        closed = ask(f"http://device.test:{site.port}/")
        assert await refused(relay, closed) == "host_not_opened"
        assert site.seen == []
        opened = ask(
            f"http://device.test:{site.port}/",
            open=[OpenAddress(host="device.test", port=site.port, on_host=True)],
        )
        result = await relay.send(opened)
    assert (result.status, result.zone) == (200, "host")
    assert len(site.seen) == 1


async def test_an_address_opened_on_the_host_that_leads_elsewhere_is_closed(vault):
    async with Site() as site:
        request = ask(
            f"http://device.test:{site.port}/",
            open=[OpenAddress(host="device.test", port=site.port, on_host=True)],
        )
        assert await refused(make_relay(vault), request) == "closed_address"
    assert site.seen == []


async def test_a_name_is_resolved_once_and_the_connection_goes_where_it_was_judged(vault):
    # The name leads to the house when judged, then to a service of mine: the second answer is
    # never asked for, and the request reaches the first address.
    names = Names({"device.test": [[LOCAL], [CORE]]})
    async with Site() as site:
        result = await make_relay(vault, names=names, reach=OWN).send(
            ask(f"http://device.test:{site.port}/")
        )
    assert result.status == 200 and len(site.seen) == 1
    assert names.calls == ["device.test"]


async def test_resolve_only_says_the_zone_and_sends_nothing(vault):
    async with Site() as site:
        relay = make_relay(vault, zones={LOCAL: "internet"})
        result = await relay.send(ask(f"http://device.test:{site.port}/", resolve_only=True))
        closed = ask(
            f"http://device.test:{site.port}/",
            open=[OpenAddress(host="other.test", port=1)],
            resolve_only=True,
        )
        assert await refused(relay, closed) == "closed_address"
    assert result == OutResponse(status=0, headers={}, body_b64="", zone="internet", hops=0)
    assert site.seen == []


async def test_a_zone_other_than_the_one_expected_is_refused_and_nothing_sent(vault):
    # Ruling R12: the core judged the house (at a resolve_only, say); the name now leads to the
    # internet. Refused on the very resolution the connection would be pinned to.
    names = Names({"device.test": [[LOCAL], [OTHER_LOOPBACK]]})
    zones: dict[IPAddress, Zone] = {LOCAL: "house", OTHER_LOOPBACK: "internet"}
    async with Site() as site:
        relay = make_relay(vault, names=names, zones=zones)
        looked = await relay.send(ask(f"http://device.test:{site.port}/", resolve_only=True))
        assert looked.zone == "house"
        sent = ask(f"http://device.test:{site.port}/", expect_zone="house")
        assert await refused(relay, sent) == "zone_changed"
    assert site.seen == []


async def test_the_zone_expected_lets_the_request_through_and_none_changes_nothing(vault):
    async with Site() as site:
        relay = make_relay(vault)
        same = await relay.send(ask(f"http://device.test:{site.port}/", expect_zone="house"))
        unsaid = await relay.send(ask(f"http://device.test:{site.port}/"))
        internet = make_relay(vault, zones={LOCAL: "internet"})
        # Without an expected zone, what was sent before is sent (the core judged it otherwise).
        anywhere = await internet.send(ask(f"http://device.test:{site.port}/"))
    assert (same.status, unsaid.status, anywhere.status) == (200, 200, 200)
    assert len(site.seen) == 3


# --- Rule 3: TLS by name --------------------------------------------------------------------------


async def test_tls_is_checked_on_the_name_though_the_address_is_pinned(vault, pki):
    pki.names_asked.clear()
    async with Site(tls=pki.server) as site:
        result = await make_relay(vault, tls=pki.client).send(
            ask(f"https://site.test:{site.port}/secure")
        )
    assert (result.status, body_of(result)) == (200, b"hello")
    assert pki.names_asked == ["site.test"]
    assert site.seen[0].header("host") == f"site.test:{site.port}"


async def test_a_certificate_for_another_name_is_unreachable(vault, pki):
    async with Site(tls=pki.server) as site:
        request = ask(f"https://other.test:{site.port}/")
        assert await refused(make_relay(vault, tls=pki.client), request) == "unreachable"
    assert site.seen == []


async def test_an_untrusted_certificate_is_unreachable(vault, pki):
    async with Site(tls=pki.server) as site:
        request = ask(f"https://site.test:{site.port}/")
        relay = make_relay(vault, tls=ssl.create_default_context())
        assert await refused(relay, request) == "unreachable"


async def test_nothing_listening_is_unreachable(vault):
    request = ask(f"http://device.test:{free_port()}/")
    assert await refused(make_relay(vault), request) == "unreachable"


# --- Rule 4: what a program cannot send -----------------------------------------------------------


async def test_identifying_and_hop_by_hop_headers_of_a_program_are_dropped(vault):
    headers = {
        "Authorization": "Bearer forged",
        "Cookie": "sid=forged",
        "Proxy-Authorization": "Basic forged",
        "X-Api-Key": "forged",
        "Connection": "x-private",
        "X-Private": "hop",
        "Keep-Alive": "timeout=5",
        "TE": "trailers",
        "Upgrade": "websocket",
        "Host": "elsewhere.test",
        "X-Kept": "yes",
    }
    async with Site() as site:
        await make_relay(vault).send(ask(f"http://device.test:{site.port}/", headers=headers))
    [seen] = site.seen
    names = {name for name, _ in seen.headers}
    for dropped in (
        "authorization",
        "cookie",
        "proxy-authorization",
        "x-api-key",
        "x-private",
        "keep-alive",
        "te",
        "upgrade",
    ):
        assert dropped not in names
    assert seen.header("x-kept") == "yes"
    assert seen.header("host") == f"device.test:{site.port}"
    assert "forged" not in str(seen.headers)


async def test_a_header_the_account_places_cannot_be_sent_by_the_program(vault):
    ref = put_token(vault, name="x-device-token")
    async with Site() as site:
        await make_relay(vault).send(
            ask(
                f"http://device.test:{site.port}/",
                headers={"X-Device-Token": "forged"},
                account=ref,
            )
        )
    assert site.seen[0].all("x-device-token") == [TOKEN]


async def test_compression_is_never_asked_for(vault):
    async with Site() as site:
        await make_relay(vault).send(
            ask(f"http://device.test:{site.port}/", headers={"Accept-Encoding": "gzip, br"})
        )
    assert site.seen[0].header("accept-encoding") == "identity"


# --- Rule 5: accounts -----------------------------------------------------------------------------


async def test_a_token_is_placed_by_its_binding(vault):
    ref = put_token(vault)
    async with Site() as site:
        await make_relay(vault).send(
            ask(
                f"http://device.test:{site.port}/",
                headers={"Authorization": "Bearer forged"},
                account=ref,
            )
        )
    assert site.seen[0].all("authorization") == [f"Bearer {TOKEN}"]


async def test_a_basic_account_is_placed_as_basic(vault):
    ref = put_basic(vault)
    async with Site() as site:
        await make_relay(vault).send(ask(f"http://site.test:{site.port}/", account=ref))
    expected = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    assert site.seen[0].all("authorization") == [f"Basic {expected}"]


async def test_a_login_is_placed_in_the_body_of_a_post_to_its_path(vault):
    ref = put_login(vault)
    async with Site() as site:
        await make_relay(vault).send(
            ask(
                f"http://site.test:{site.port}/login",
                method="POST",
                body=b"remember=1&user=forged",
                headers={"Content-Type": "text/plain"},
                account=ref,
            )
        )
    [seen] = site.seen
    assert seen.body == b"remember=1&user=alex&password=pa55word-secret"
    assert seen.header("content-type") == "application/x-www-form-urlencoded"
    assert seen.header("authorization") is None


async def test_a_json_login_is_placed_in_the_object(vault):
    ref = put_login(vault, encoding="json")
    async with Site() as site:
        await make_relay(vault).send(
            ask(
                f"http://site.test:{site.port}/login",
                method="POST",
                body=b'{"remember": true}',
                account=ref,
            )
        )
    [seen] = site.seen
    assert json.loads(seen.body) == {"remember": True, "user": USER, "password": PASSWORD}
    assert seen.header("content-type") == "application/json"


async def test_a_json_login_on_a_body_that_is_not_an_object_is_a_bad_request(vault):
    ref = put_login(vault, encoding="json")
    async with Site() as site:
        request = ask(
            f"http://site.test:{site.port}/login", method="POST", body=b"[1]", account=ref
        )
        assert await refused(make_relay(vault), request) == "bad_request"
    assert site.seen == []


@pytest.mark.parametrize(("method", "path"), [("GET", "/login"), ("POST", "/elsewhere")])
async def test_a_login_is_placed_nowhere_else(vault, method, path):
    ref = put_login(vault)
    async with Site() as site:
        await make_relay(vault).send(
            ask(f"http://site.test:{site.port}{path}", method=method, body=b"a=1", account=ref)
        )
    [seen] = site.seen
    assert seen.body == b"a=1"
    assert PASSWORD not in str(seen.headers) and USER not in str(seen.headers)


async def test_an_account_goes_only_where_it_is_bound(vault):
    ref = put_token(vault, host="site.test")
    async with Site() as site:
        request = ask(f"http://device.test:{site.port}/", account=ref)
        assert await refused(make_relay(vault), request) == "account_not_for_address"
    assert site.seen == []


async def test_an_account_bound_to_a_port_goes_to_that_port_only(vault):
    async with Site() as site:
        vault.put(
            "device:port",
            TOKEN,
            purpose="the device's token",
            bindings=[
                Binding(
                    host="device.test",
                    port=site.port + 1,
                    location=HeaderLocation(kind="header", name="x-token"),
                )
            ],
        )
        request = ask(f"http://device.test:{site.port}/", account="device:port")
        assert await refused(make_relay(vault), request) == "account_not_for_address"
    assert site.seen == []


async def test_an_unknown_account_is_not_found(vault):
    async with Site() as site:
        request = ask(f"http://device.test:{site.port}/", account="device:none")
        assert await refused(make_relay(vault), request) == "account_not_found"
    assert site.seen == []


async def test_an_account_never_goes_in_clear_to_the_internet(vault):
    ref = put_token(vault)
    async with Site() as site:
        relay = make_relay(vault, zones={LOCAL: "internet"})
        request = ask(f"http://device.test:{site.port}/", account=ref)
        assert await refused(relay, request) == "needs_https"
        # Without the account, the same address is fine.
        assert (await relay.send(ask(f"http://device.test:{site.port}/"))).zone == "internet"
    assert len(site.seen) == 1 and site.seen[0].header("authorization") is None


async def test_an_account_may_go_in_clear_to_the_house_and_to_an_opened_host_port(vault):
    ref = put_token(vault)
    async with Site() as site:
        await make_relay(vault).send(ask(f"http://device.test:{site.port}/", account=ref))
        await make_relay(vault, zones={LOCAL: "host"}).send(
            ask(
                f"http://device.test:{site.port}/",
                account=ref,
                open=[OpenAddress(host="device.test", port=site.port, on_host=True)],
            )
        )
    assert [seen.header("authorization") for seen in site.seen] == [f"Bearer {TOKEN}"] * 2


async def test_an_account_goes_over_https_to_the_internet(vault, pki):
    ref = put_token(vault, host="site.test")
    async with Site(tls=pki.server) as site:
        relay = make_relay(vault, zones={LOCAL: "internet"}, tls=pki.client)
        result = await relay.send(ask(f"https://site.test:{site.port}/", account=ref))
    assert result.zone == "internet"
    assert site.seen[0].header("authorization") == f"Bearer {TOKEN}"


async def test_the_use_of_an_account_is_recorded(vault):
    ref = put_token(vault)
    async with Site() as site:
        await make_relay(vault).send(ask(f"http://device.test:{site.port}/", account=ref))
    metadata = vault.metadata(ref)
    assert metadata is not None and metadata.last_used_at is not None


# --- Sessions -------------------------------------------------------------------------------------


def login_site() -> Handler:
    """A site whose login sets a session cookie, and whose pages echo what they were sent."""

    async def page(seen: Seen, writer: asyncio.StreamWriter) -> None:
        writer.write(raw(200, [("X-Cookie", seen.header("cookie") or "")], b"page"))

    return routes(
        {
            "/login": reply(
                raw(
                    200,
                    [("Set-Cookie", f"sid={SID}; Path=/; HttpOnly"), ("X-Echo", SID)],
                    f"welcome, your session is {SID}".encode(),
                )
            ),
            "/page": page,
        }
    )


async def test_the_session_of_a_login_is_kept_and_never_returned(vault):
    ref = put_login(vault)
    async with Site(login_site()) as site:
        relay = make_relay(vault)
        login = await relay.send(
            ask(f"http://site.test:{site.port}/login", method="POST", body=b"", account=ref)
        )
        page = await relay.send(ask(f"http://site.test:{site.port}/page", account=ref))
        alone = await relay.send(ask(f"http://site.test:{site.port}/page"))
    assert "set-cookie" not in login.headers
    assert SID not in body_of(login).decode() and MASK in body_of(login).decode()
    assert login.headers["x-echo"] == MASK
    # The next request with the account carries the session; one without it does not.
    assert site.seen[1].header("cookie") == f"sid={SID}"
    assert site.seen[2].header("cookie") is None
    # The page echoes the cookie it got: masked on the way back.
    assert page.headers["x-cookie"] == f"sid={MASK}"
    assert alone.headers["x-cookie"] == ""


async def test_a_session_unused_for_the_idle_time_is_forgotten(vault):
    ref = put_login(vault)
    clock = Clock()
    async with Site(login_site()) as site:
        relay = make_relay(vault, sessions=Sessions(clock=clock))
        await relay.send(
            ask(f"http://site.test:{site.port}/login", method="POST", body=b"", account=ref)
        )
        clock.now += 61
        await relay.send(ask(f"http://site.test:{site.port}/page", account=ref, idle=60))
    assert site.seen[1].header("cookie") is None


async def test_the_set_cookie_of_a_request_without_an_account_is_returned(vault):
    answer = raw(200, [("Set-Cookie", "theme=dark-mode-on; Path=/")])
    async with Site(reply(answer)) as site:
        result = await make_relay(vault).send(ask(f"http://site.test:{site.port}/"))
    assert result.headers["set-cookie"] == "theme=dark-mode-on; Path=/"


# --- Rule 6: redirects ----------------------------------------------------------------------------


async def test_a_redirect_to_an_open_address_is_followed_without_the_account(vault):
    ref = put_login(vault)
    async with Site(login_site()) as site:
        relay = make_relay(vault)
        await relay.send(
            ask(f"http://site.test:{site.port}/login", method="POST", body=b"", account=ref)
        )
        async with Site(
            reply(raw(302, [("Location", f"http://site.test:{site.port}/page")]))
        ) as start:
            result = await relay.send(
                ask(
                    f"http://site.test:{start.port}/start",
                    account=ref,
                    open=[
                        OpenAddress(host="site.test", port=start.port),
                        OpenAddress(host="site.test", port=site.port),
                    ],
                )
            )
    assert (result.status, result.hops) == (200, 1)
    # The first request carried the session; the hop carries neither the session nor the account.
    assert start.seen[0].header("cookie") == f"sid={SID}"
    hop = site.seen[-1]
    assert hop.target == "/page"
    assert hop.header("cookie") is None and hop.header("authorization") is None


async def test_a_redirect_never_carries_the_basic_account(vault):
    ref = put_basic(vault)
    handler = routes({"/a": reply(raw(307, [("Location", "/b")])), "/b": OK})
    async with Site(handler) as site:
        result = await make_relay(vault).send(ask(f"http://site.test:{site.port}/a", account=ref))
    assert result.hops == 1
    assert site.seen[0].header("authorization") is not None
    assert site.seen[1].header("authorization") is None


async def test_a_307_after_a_login_resends_the_program_body_not_the_password(vault):
    ref = put_login(vault)
    handler = routes({"/login": reply(raw(307, [("Location", "/done")])), "/done": OK})
    async with Site(handler) as site:
        await make_relay(vault).send(
            ask(f"http://site.test:{site.port}/login", method="POST", body=b"a=1", account=ref)
        )
    first, hop = site.seen
    assert PASSWORD.encode() in first.body
    assert (hop.method, hop.target, hop.body) == ("POST", "/done", b"a=1")


async def test_a_303_becomes_a_get_without_a_body(vault):
    handler = routes({"/a": reply(raw(303, [("Location", "/b")])), "/b": OK})
    async with Site(handler) as site:
        await make_relay(vault).send(
            ask(
                f"http://device.test:{site.port}/a",
                method="POST",
                body=b"payload",
                headers={"Content-Type": "text/plain"},
            )
        )
    hop = site.seen[1]
    assert (hop.method, hop.body, hop.header("content-type")) == ("GET", b"", None)


async def test_a_redirect_to_a_closed_address_is_refused(vault):
    async with Site(reply(raw(302, [("Location", "http://other.test:9/")]))) as site:
        request = ask(f"http://device.test:{site.port}/")
        assert await refused(make_relay(vault), request) == "bad_redirect"


async def test_a_redirect_to_the_gatekeeper_itself_is_refused(vault):
    names = Names({"device.test": [[LOCAL]], "gatekeeper": [[CORE]]})
    async with Site(reply(raw(302, [("Location", "http://gatekeeper:8081/internal")]))) as site:
        request = ask(
            f"http://device.test:{site.port}/",
            open=[
                OpenAddress(host="device.test", port=site.port),
                OpenAddress(host="gatekeeper", port=8081),
            ],
        )
        relay = make_relay(vault, names=names, reach=OWN)
        assert await refused(relay, request) == "bad_redirect"
    assert len(site.seen) == 1


async def test_a_redirect_to_loopback_by_its_address_is_refused(vault):
    async with Site(reply(raw(302, [("Location", "http://127.0.0.2:8081/")]))) as site:
        request = ask(
            f"http://device.test:{site.port}/",
            open=[
                OpenAddress(host="device.test", port=site.port),
                OpenAddress(host="127.0.0.2", port=8081),
            ],
        )
        assert await refused(make_relay(vault), request) == "bad_redirect"


async def test_a_redirect_to_another_scheme_is_refused(vault):
    async with Site(reply(raw(302, [("Location", "file:///etc/passwd")]))) as site:
        assert await refused(make_relay(vault), ask(f"http://device.test:{site.port}/")) == (
            "bad_redirect"
        )


async def test_a_redirect_with_credentials_in_its_url_is_refused(vault):
    async with Site() as site:
        location = f"http://user:pw@device.test:{site.port}/"
        async with Site(reply(raw(302, [("Location", location)]))) as start:
            request = ask(
                f"http://device.test:{start.port}/",
                open=[
                    OpenAddress(host="device.test", port=start.port),
                    OpenAddress(host="device.test", port=site.port),
                ],
            )
            assert await refused(make_relay(vault), request) == "bad_redirect"
    assert site.seen == []


async def test_redirects_are_followed_five_times_at_most(vault):
    async with Site(reply(raw(302, [("Location", "/again")]))) as site:
        request = ask(f"http://device.test:{site.port}/")
        assert await refused(make_relay(vault), request) == "bad_redirect"
    assert len(site.seen) == 6

    handler: dict[str, Handler] = {
        f"/{n}": reply(raw(302, [("Location", f"/{n + 1}")])) for n in range(5)
    }
    handler["/5"] = OK
    async with Site(routes(handler)) as site:
        result = await make_relay(vault).send(ask(f"http://device.test:{site.port}/0"))
    assert (result.status, result.hops) == (200, 5)


async def test_a_redirect_without_a_location_is_the_answer(vault):
    async with Site(reply(raw(302, body=b"moved"))) as site:
        result = await make_relay(vault).send(ask(f"http://device.test:{site.port}/"))
    assert (result.status, body_of(result), result.hops) == (302, b"moved", 0)


# --- Rule 7: reading and scrubbing the answer -----------------------------------------------------


@pytest.mark.parametrize("length", [True, False])
async def test_a_body_one_byte_over_the_bound_is_too_big(vault, length):
    async with Site(reply(raw(200, body=b"x" * 11, length=length))) as site:
        relay = make_relay(vault)
        assert (
            await refused(relay, ask(f"http://device.test:{site.port}/", most_bytes=10))
            == "too_big"
        )
    async with Site(reply(raw(200, body=b"x" * 10, length=length))) as site:
        result = await relay.send(ask(f"http://device.test:{site.port}/", most_bytes=10))
    assert body_of(result) == b"x" * 10


async def test_a_chunked_body_over_the_bound_is_too_big(vault):
    chunks = b"6\r\nxxxxxx\r\n6\r\nxxxxxx\r\n0\r\n\r\n"
    answer = raw(200, [("Transfer-Encoding", "chunked")], chunks, length=False)
    async with Site(reply(answer)) as site:
        request = ask(f"http://device.test:{site.port}/", most_bytes=11)
        assert await refused(make_relay(vault), request) == "too_big"


async def test_a_compressed_body_is_bounded_on_what_it_decodes_to(vault):
    bomb = gzip.compress(b"\0" * 20_000_000)
    answer = raw(200, [("Content-Encoding", "gzip")], bomb)
    async with Site(reply(answer)) as site:
        request = ask(f"http://device.test:{site.port}/", most_bytes=1000)
        assert await refused(make_relay(vault), request) == "too_big"


async def test_a_compressed_body_is_scrubbed_on_its_decoded_bytes(vault):
    ref = put_token(vault)
    answer = raw(200, [("Content-Encoding", "gzip")], gzip.compress(f"got {TOKEN}".encode()))
    async with Site(reply(answer)) as site:
        result = await make_relay(vault).send(ask(f"http://device.test:{site.port}/", account=ref))
    assert body_of(result) == f"got {MASK}".encode()
    assert "content-encoding" not in result.headers


@pytest.mark.parametrize("coding", ["br", "x-gzip", "gzip, gzip", "compress"])
async def test_a_coding_that_cannot_be_scrubbed_is_refused(vault, coding):
    answer = raw(200, [("Content-Encoding", coding)], b"whatever")
    async with Site(reply(answer)) as site:
        request = ask(f"http://device.test:{site.port}/")
        assert await refused(make_relay(vault), request) == "unreachable"


async def test_a_body_that_never_ends_is_too_slow(vault):
    async def endless(seen: Seen, writer: asyncio.StreamWriter) -> None:
        writer.write(raw(200, [("Content-Length", "1000000")], b"start", length=False))
        while True:
            await writer.drain()
            await asyncio.sleep(0.1)
            writer.write(b"x")

    async with Site(endless) as site:
        started = time.monotonic()
        request = ask(f"http://device.test:{site.port}/", seconds=1)
        assert await refused(make_relay(vault), request) == "too_slow"
        assert time.monotonic() - started < 1 + 2


async def test_a_server_that_never_answers_is_too_slow(vault):
    async def silent(seen: Seen, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(3600)

    async with Site(silent) as site:
        started = time.monotonic()
        request = ask(f"http://device.test:{site.port}/", seconds=1)
        assert await refused(make_relay(vault), request) == "too_slow"
        assert time.monotonic() - started < 1 + 2


async def test_a_lookup_that_never_ends_is_too_slow(vault):
    async def stuck(name: str) -> list[IPAddress]:
        await asyncio.sleep(3600)
        return []

    relay = make_relay(vault, names=stuck)
    started = time.monotonic()
    assert await refused(relay, ask("http://device.test:8000/", seconds=1)) == "too_slow"
    assert time.monotonic() - started < 1 + 2


async def test_a_token_echoed_by_the_service_is_scrubbed_in_the_body_and_the_headers(vault):
    ref = put_token(vault)
    encoded = base64.b64encode(TOKEN.encode()).decode()
    answer = raw(
        200,
        [("X-Echo", f"Bearer {TOKEN}"), ("X-Encoded", encoded)],
        f"you sent {TOKEN} ({encoded})".encode(),
    )
    async with Site(reply(answer)) as site:
        result = await make_relay(vault).send(ask(f"http://device.test:{site.port}/", account=ref))
    # The header echoed whole is the value as it was placed: masked whole.
    assert result.headers["x-echo"] == MASK
    assert result.headers["x-encoded"] == MASK
    assert body_of(result) == f"you sent {MASK} ({MASK})".encode()


async def test_every_secret_of_the_vault_is_scrubbed_even_without_an_account(vault):
    put_token(vault)
    async with Site(reply(raw(200, body=f"leaked {TOKEN}".encode()))) as site:
        result = await make_relay(vault).send(ask(f"http://device.test:{site.port}/"))
    assert body_of(result) == f"leaked {MASK}".encode()


async def test_the_user_of_an_account_is_masked_in_its_own_answer_only(vault):
    ref = put_basic(vault)
    put_token(vault, host="site.test")
    answer = reply(raw(200, [("X-User", USER)], f"hello {USER}, {PASSWORD}".encode()))
    async with Site(answer) as site:
        relay = make_relay(vault)
        own = await relay.send(ask(f"http://site.test:{site.port}/", account=ref))
        other = await relay.send(ask(f"http://site.test:{site.port}/", account="device:token"))
        none = await relay.send(ask(f"http://site.test:{site.port}/"))
    assert body_of(own) == f"hello {MASK}, {MASK}".encode() and own.headers["x-user"] == MASK
    for result in (other, none):
        assert body_of(result) == f"hello {USER}, {MASK}".encode()
        assert result.headers["x-user"] == USER


# --- What was placed is masked as it was placed ---------------------------------------------------


def mirror() -> Handler:
    """A device that echoes what it got: the placed header, in clear and base64-encoded, the
    base64 part of a Basic value encoded again, and its body as it came."""

    async def handler(seen: Seen, writer: asyncio.StreamWriter) -> None:
        placed = seen.header("authorization") or ""
        basic_part = placed.removeprefix("Basic ")
        echo = (
            f"clear={placed}\n"
            f"b64={base64.b64encode(placed.encode()).decode()}\n"
            f"basic={base64.b64encode(basic_part.encode()).decode()}\n"
        ).encode() + seen.body
        writer.write(raw(200, [("X-Placed", placed)], echo))

    return handler


def leaks(result: OutResponse, *secrets: str) -> list[str]:
    shown = body_of(result).decode() + "\n" + "\n".join(result.headers.values())
    return [secret for secret in secrets if secret and secret in shown]


async def test_a_placed_header_echoed_in_base64_is_masked(vault):
    ref = put_token(vault)
    placed = f"Bearer {TOKEN}"
    async with Site(mirror()) as site:
        result = await make_relay(vault).send(ask(f"http://device.test:{site.port}/", account=ref))
    assert leaks(result, TOKEN, placed, base64.b64encode(placed.encode()).decode()) == []
    assert result.headers["x-placed"] == MASK


async def test_a_basic_value_echoed_in_base64_is_masked(vault):
    ref = put_basic(vault)
    pair = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    placed = f"Basic {pair}"
    async with Site(mirror()) as site:
        result = await make_relay(vault).send(ask(f"http://site.test:{site.port}/", account=ref))
    assert (
        leaks(
            result,
            pair,
            base64.b64encode(placed.encode()).decode(),
            base64.b64encode(pair.encode()).decode(),
        )
        == []
    )


async def test_a_login_field_echoed_as_it_was_sent_is_masked(vault):
    password = "pa55 w@rd!s3cret"
    ref = put_login(vault, password=password)
    async with Site(mirror()) as site:
        result = await make_relay(vault).send(
            ask(f"http://site.test:{site.port}/login", method="POST", body=b"", account=ref)
        )
    sent = site.seen[0].body.decode()
    assert "password=pa55+w%40rd%21s3cret" in sent  # the field as it was placed
    assert leaks(result, "pa55+w%40rd%21s3cret", "password=pa55+w%40rd%21s3cret", password) == []


async def test_a_json_login_field_echoed_as_it_was_sent_is_masked(vault):
    password = 'pa55"word\\s3cret'
    ref = put_login(vault, encoding="json", password=password)
    async with Site(mirror()) as site:
        result = await make_relay(vault).send(
            ask(f"http://site.test:{site.port}/login", method="POST", body=b"{}", account=ref)
        )
    as_sent = json.dumps(password)[1:-1]
    assert as_sent in site.seen[0].body.decode()
    assert leaks(result, as_sent, password) == []


async def test_a_password_echoed_in_a_json_error_with_its_escapes_is_masked(vault):
    # A quote, a backslash, a slash and an accented letter: a JSON error writes each otherwise.
    password = 'pa"ss\\wo/rd-é1'
    vault.put(
        "site:basic",
        join_account(USER, password),
        purpose="the site's account",
        bindings=[Binding(host="site.test", location=BasicLocation(kind="basic"))],
    )
    shown = json.dumps(password)[1:-1].replace("/", "\\/")
    error = f'{{"error": "wrong password {shown}"}}'.encode()
    async with Site(reply(raw(401, [("Content-Type", "application/json")], error))) as site:
        relay = make_relay(vault)
        own = await relay.send(ask(f"http://site.test:{site.port}/", account="site:basic"))
        none = await relay.send(ask(f"http://site.test:{site.port}/"))
    for result in (own, none):
        assert body_of(result) == f'{{"error": "wrong password {MASK}"}}'.encode()


async def test_a_request_without_an_account_is_scrubbed_as_before(vault):
    put_token(vault)
    placed = base64.b64encode(f"Bearer {TOKEN}".encode()).decode()
    async with Site(reply(raw(200, body=f"seen {placed}".encode()))) as site:
        result = await make_relay(vault).send(ask(f"http://device.test:{site.port}/"))
    # Nothing was placed in it: only the vault's own values are masked.
    assert body_of(result) == f"seen {placed}".encode()


# --- A binding never replaces what the relay decides ----------------------------------------------


async def test_a_binding_in_a_header_the_relay_decides_is_never_placed(vault, monkeypatch):
    # Such a binding cannot be stored (the model refuses it): should one be read anyway, the
    # account is not used and the marker of my tools stays.
    put_token(vault)
    forged = Binding.model_construct(
        host="device.test",
        port=None,
        location=HeaderLocation.model_construct(kind="header", name="via", template="{value}"),
    )
    monkeypatch.setattr(vault, "reveal_bound", lambda ref: (TOKEN, [forged]))
    async with Site() as site:
        request = ask(f"http://device.test:{site.port}/", account="device:token")
        assert await refused(make_relay(vault), request) == "account_not_for_address"
    assert site.seen == []


# --- Only the request that carried the account feeds its session ----------------------------------


async def test_the_cookies_of_a_redirect_hop_never_go_to_the_session(vault):
    ref = put_login(vault)
    handler = routes(
        {
            "/login": reply(raw(302, [("Set-Cookie", f"sid={SID}; Path=/"), ("Location", "/hop")])),
            "/hop": reply(raw(200, [("Set-Cookie", "hop=set-by-the-hop-value; Path=/")])),
            "/page": OK,
        }
    )
    async with Site(handler) as site:
        relay = make_relay(vault)
        await relay.send(
            ask(f"http://site.test:{site.port}/login", method="POST", body=b"", account=ref)
        )
        await relay.send(ask(f"http://site.test:{site.port}/page", account=ref))
    assert site.seen[-1].header("cookie") == f"sid={SID}"


# --- A redirect stays in its zone -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "second", "followed"),
    [
        ("house", "internet", False),
        ("internet", "house", False),
        ("house", "house", True),
        ("internet", "internet", True),
    ],
)
async def test_a_redirect_is_followed_within_the_zone_of_the_first_address_only(
    vault, first, second, followed
):
    async with Site() as there:
        location = f"http://other.test:{there.port}/there"
        async with Site(reply(raw(302, [("Location", location)]))) as here:
            relay = make_relay(vault, port_zones={here.port: first, there.port: second})
            request = ask(
                f"http://device.test:{here.port}/",
                open=[
                    OpenAddress(host="device.test", port=here.port),
                    OpenAddress(host="other.test", port=there.port),
                ],
            )
            if followed:
                result = await relay.send(request)
                assert (result.status, result.hops, result.zone) == (200, 1, first)
            else:
                assert await refused(relay, request) == "bad_redirect"
    assert len(there.seen) == (1 if followed else 0)


# --- The cookies of a session are scrubbed from that account's answers only -----------------------


LONG_SID = "0123456789abcdef0123456789abcdef"  # 32 characters


def cookie_site(value: str, page_body: bytes) -> Handler:
    return routes(
        {
            "/login": reply(raw(200, [("Set-Cookie", f"sid={value}; Path=/")], b"ok")),
            "/page": reply(raw(200, body=page_body)),
        }
    )


async def test_a_short_cookie_value_is_not_scrubbed(vault):
    ref = put_login(vault)
    async with Site(cookie_site("1", b"1 + 1 = 2, 10 items")) as site:
        relay = make_relay(vault)
        login = await relay.send(
            ask(f"http://site.test:{site.port}/login", method="POST", body=b"", account=ref)
        )
        page = await relay.send(ask(f"http://site.test:{site.port}/page", account=ref))
    assert "set-cookie" not in login.headers
    assert body_of(page) == b"1 + 1 = 2, 10 items"
    assert site.seen[1].header("cookie") == "sid=1"


async def test_a_session_value_is_scrubbed_from_its_account_answers_only(vault):
    ref = put_login(vault)
    put_token(vault, host="site.test")
    echoed = f"session {LONG_SID}".encode()
    async with Site(cookie_site(LONG_SID, echoed)) as site:
        relay = make_relay(vault)
        await relay.send(
            ask(f"http://site.test:{site.port}/login", method="POST", body=b"", account=ref)
        )
        own = await relay.send(ask(f"http://site.test:{site.port}/page", account=ref))
        other = await relay.send(ask(f"http://site.test:{site.port}/page", account="device:token"))
        none = await relay.send(ask(f"http://site.test:{site.port}/page"))
    assert body_of(own) == f"session {MASK}".encode()
    assert body_of(other) == echoed and body_of(none) == echoed


# --- Rule 8 and the internal route ----------------------------------------------------------------


CORE_TOKEN = "core-token-for-tests"


@asynccontextmanager
async def internal(
    vault: Vault, store: SecretRequestStore, registry: ProviderRegistry, relay: ProgramRelay
) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as http_client:
        app: FastAPI = create_internal_app(
            vault=vault,
            requests=store,
            providers=registry,
            http_client=http_client,
            core_token=CORE_TOKEN,
            out_relay=relay,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gatekeeper.test"
        ) as client:
            yield client


BEARER = {"authorization": f"Bearer {CORE_TOKEN}"}


async def test_the_route_answers_a_program_request(vault, store, registry):
    async with Site() as site, internal(vault, store, registry, make_relay(vault)) as client:
        request = ask(f"http://device.test:{site.port}/")
        r = await client.post("/internal/out", headers=BEARER, content=request.model_dump_json())
    assert r.status_code == 200
    answer = OutResponse.model_validate(r.json())
    assert (answer.status, body_of(answer), answer.zone) == (200, b"hello", "house")


async def test_the_route_needs_the_core_token(vault, store, registry):
    async with internal(vault, store, registry, make_relay(vault)) as client:
        request = ask("http://device.test:8000/", resolve_only=True)
        r = await client.post("/internal/out", content=request.model_dump_json())
    assert r.status_code == 401


@pytest.mark.parametrize(
    ("request_", "status", "code"),
    [
        (ask("http://device.test:8000/", open=[], resolve_only=True), 403, "closed_address"),
        (ask(f"http://device.test:{free_port()}/"), 502, "unreachable"),
        (ask("http://device.test:8000/", account="device:none"), 404, "account_not_found"),
        (ask("http://device.test:8000/", expect_zone="internet"), 403, "zone_changed"),
    ],
)
async def test_a_refusal_is_its_code_alone(vault, store, registry, request_, status, code):
    async with internal(vault, store, registry, make_relay(vault)) as client:
        r = await client.post("/internal/out", headers=BEARER, content=request_.model_dump_json())
    assert (r.status_code, r.json()) == (status, {"code": code})


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"{}",
        json.dumps({**ask("http://device.test:8000/").model_dump(), "seconds": 0}).encode(),
        json.dumps({**ask("http://device.test:8000/").model_dump(), "extra": 1}).encode(),
    ],
)
async def test_a_request_that_is_not_one_is_a_bad_request(vault, store, registry, content):
    async with internal(vault, store, registry, make_relay(vault)) as client:
        r = await client.post("/internal/out", headers=BEARER, content=content)
    assert (r.status_code, r.json()) == (400, {"code": "bad_request"})


def test_most_bytes_cannot_exceed_the_ceiling_of_an_answer():
    from domo_common.sandbox_api import ANSWER_CEILING_BYTES
    from pydantic import ValidationError

    ask("http://device.test:8000/", most_bytes=ANSWER_CEILING_BYTES)
    with pytest.raises(ValidationError):
        ask("http://device.test:8000/", most_bytes=ANSWER_CEILING_BYTES + 1)

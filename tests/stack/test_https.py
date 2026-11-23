"""The edge: HTTPS with the local authority, the routes to the core and to the gatekeeper, what
it forwards to the core, and the limits it puts on request bodies."""

import json
import re
import ssl
import time
import uuid
from collections.abc import Iterator

import httpx
import pytest

from .stack_support import REPO_ROOT, StackInfo, run

pytestmark = pytest.mark.stack

LOG_WAIT_S = 15


def wait_for_log_line(stack, service: str, needle: str) -> str:
    """The first line of ``service``'s log that contains ``needle`` (logs can lag a little)."""
    deadline = time.monotonic() + LOG_WAIT_S
    while True:
        lines = [line for line in stack.logs(service).splitlines() if needle in line]
        if lines:
            return lines[0]
        if time.monotonic() > deadline:
            raise AssertionError(f"no line with {needle!r} in the log of {service}")
        time.sleep(0.5)


def test_http_redirects_to_https(stack):
    r = httpx.get(stack.http_url + "/", follow_redirects=False)
    assert r.status_code in (301, 308) and r.headers["location"].startswith(
        "https://localhost:18443"
    )


def test_ca_certificate_downloadable(stack):
    r = httpx.get(stack.base_url + "/ca.crt", verify=False)
    assert r.status_code == 200 and "BEGIN CERTIFICATE" in r.text


def test_root_redirects_to_setup(stack):
    r = httpx.get(stack.base_url + "/", verify=False, follow_redirects=True)
    assert r.url.path == "/setup/owner"


def test_vault_path_routed_to_gatekeeper(stack):
    r = httpx.get(stack.base_url + "/vault/r/unknown", verify=False)
    assert r.status_code == 404 and r.headers["x-domo-component"] == "gatekeeper"


def test_setup_code_logged(stack):
    assert re.search(
        r"The assistant setup code: [A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}",
        stack.logs("core"),
    )


# --- beyond the brief's list ------------------------------------------------------------------


def test_the_ca_certificate_is_offered_as_a_certificate(stack):
    r = httpx.get(stack.base_url + "/ca.crt", verify=False)
    assert r.headers["content-type"] == "application/x-x509-ca-cert"
    assert "PRIVATE KEY" not in r.text


def test_the_page_is_trusted_once_the_ca_certificate_is(stack):
    """What a device does after installing /ca.crt: the page's certificate then verifies."""
    authority = httpx.get(stack.base_url + "/ca.crt", verify=False).text
    context = ssl.create_default_context(cadata=authority)
    r = httpx.get(stack.base_url + "/login", verify=context)
    assert r.status_code == 200


@pytest.mark.parametrize("path", ["/root.key", "/ca.key", "/ca.crt/../root.key", "/data/caddy"])
def test_the_authority_key_is_never_served(stack, path):
    r = httpx.get(stack.base_url + path, verify=False, follow_redirects=True)
    assert "PRIVATE KEY" not in r.text


def test_a_same_origin_post_through_the_edge_passes_the_origin_check(stack):
    """The edge hands the core the Host header the browser sent: a POST from the page itself is
    judged on its merits (wrong credentials: 400), not refused as cross-site (403)."""
    r = httpx.post(
        stack.base_url + "/login",
        data={"username": f"nobody-{uuid.uuid4().hex[:8]}", "password": "not-the-password"},
        headers={"origin": stack.base_url},
        verify=False,
    )
    assert r.status_code == 400


def test_a_cross_site_post_through_the_edge_is_refused(stack):
    r = httpx.post(
        stack.base_url + "/login",
        data={"username": f"nobody-{uuid.uuid4().hex[:8]}", "password": "not-the-password"},
        headers={"origin": "https://evil.example.net"},
        verify=False,
    )
    assert r.status_code == 403


def test_the_core_sees_the_client_not_the_edge_and_not_what_the_client_claims(stack):
    edge = stack.inspect("edge")["NetworkSettings"]["Networks"][stack.network_name("core_net")]
    probe = uuid.uuid4().hex
    httpx.get(
        f"{stack.base_url}/login?probe={probe}",
        headers={"x-forwarded-for": "203.0.113.9"},
        verify=False,
    )
    line = wait_for_log_line(stack, "core", probe)
    # The edge's X-Forwarded-For is trusted (its address is replaced by the client's)...
    assert f"{edge['IPAddress']}:" not in line
    # ...and the edge replaced the one the client sent.
    assert "203.0.113.9" not in line


def test_health_checks_stay_out_of_the_core_log(stack):
    container = stack.inspect("core")
    # The health check has run (and passed) since the core started.
    assert container["State"]["Health"]["Status"] == "healthy"
    assert container["State"]["Health"]["Log"]
    probe = uuid.uuid4().hex
    httpx.get(f"{stack.base_url}/login?probe={probe}", verify=False)
    wait_for_log_line(stack, "core", probe)  # the access log is on
    assert "/healthz" not in stack.logs("core")


FORM = {"content-type": "application/x-www-form-urlencoded"}


def edge_body_limits(stack) -> dict[str, int]:
    """The request body limits of the running edge (its live configuration, from its admin
    endpoint), by the path its route matches ("*" for the routes without one)."""
    answer = stack.exec("edge", ["wget", "-q", "-O", "-", "http://127.0.0.1:2019/config/"])
    assert answer.returncode == 0, answer.stderr
    limits: dict[str, int] = {}

    def walk(node, path):
        if isinstance(node, dict):
            # A route's matchers are a list of dicts (a TLS policy's "match" is a dict).
            matchers = node.get("match")
            for matcher in matchers if isinstance(matchers, list) else []:
                if isinstance(matcher, dict) and "path" in matcher:
                    path = ",".join(matcher["path"])
            if node.get("handler") == "request_body":
                limits[path] = node["max_size"]
            for value in node.values():
                walk(value, path)
        elif isinstance(node, list):
            for item in node:
                walk(item, path)

    walk(json.loads(answer.stdout)["apps"]["http"]["servers"], "*")
    return limits


def post(url: str, body: bytes, headers: dict[str, str]) -> httpx.Response | None:
    """POST ``body``; ``None`` when the server cut the connection during the upload (what a
    server that stops reading an oversized body may do before the client reads its 413)."""
    try:
        return httpx.post(url, content=body, headers=headers, verify=False, timeout=30)
    except httpx.TransportError:
        return None


def test_the_edge_limits_request_bodies(stack):
    # The vault form's own limit (256 KiB, the gatekeeper's) for the gatekeeper; 1 MB for the core.
    assert edge_body_limits(stack) == {"/vault/*": 256 * 1024, "*": 1_000_000}


def test_bodies_within_the_limits_go_through(stack):
    headers = {**FORM, "origin": stack.base_url}
    vault = post(stack.base_url + "/vault/r/unknown", b"value=" + b"a" * (200 * 1024), headers)
    assert vault is not None and vault.headers.get("x-domo-component") == "gatekeeper"
    core = post(stack.base_url + "/login", b"username=a&password=" + b"a" * (512 * 1024), headers)
    assert core is not None and core.status_code != 413
    assert "content-security-policy" in core.headers  # the core answered


def test_a_body_over_the_core_limit_never_gets_an_answer_from_the_core(stack):
    headers = {**FORM, "origin": stack.base_url}
    body = b"username=a&password=" + b"a" * (2 * 1024 * 1024)
    refused = post(stack.base_url + "/login", body, headers)
    if refused is not None:  # else the edge cut the upload short: refused all the same
        assert refused.status_code == 413
        assert "content-security-policy" not in refused.headers


def test_a_body_over_the_vault_limit_is_refused(stack):
    """Refused by the edge, or by the gatekeeper's own check of the declared length, whichever
    comes first: either way the gatekeeper never reads more than 256 KiB."""
    headers = {**FORM, "origin": stack.base_url}
    body = b"value=" + b"a" * (300 * 1024)
    refused = post(stack.base_url + "/vault/r/unknown", body, headers)
    assert refused is None or refused.status_code == 413


def test_the_redirect_to_https_keeps_the_path_and_the_query(stack):
    r = httpx.get(stack.http_url + "/login?next=%2Fchat", follow_redirects=False)
    assert r.status_code == 308
    assert r.headers["location"] == "https://localhost:18443/login?next=%2Fchat"


# A page opened by IP address: the browser sends no server name, and behind Docker's port
# publishing Caddy cannot see the address either. An edge of its own (no core behind it), on
# other ports, with an address in its site list.
BY_IP_HTTPS_PORT = 18444
BY_IP_HTTP_PORT = 18081


@pytest.fixture
def edge_by_ip(stack: StackInfo) -> Iterator[str]:
    image = stack.inspect("edge")["Image"]
    name = f"{stack.project}-edge-by-ip-{uuid.uuid4().hex[:8]}"
    command = ["docker", "run", "--detach", "--rm", "--name", name]
    command += ["-e", "DOMO_SITE_ADDRESSES=localhost, 127.0.0.1"]
    command += ["-e", f"DOMO_HTTPS_PORT={BY_IP_HTTPS_PORT}"]
    command += ["-e", f"DOMO_HTTP_PORT={BY_IP_HTTP_PORT}"]
    command += ["-p", f"127.0.0.1:{BY_IP_HTTPS_PORT}:{BY_IP_HTTPS_PORT}"]
    for name_in_repo in ("Caddyfile", "start.sh"):
        source, target = REPO_ROOT / "deploy" / "edge" / name_in_repo, f"/etc/caddy/{name_in_repo}"
        command += ["--mount", f"type=bind,source={source},target={target},readonly"]
    command += [image, "/bin/sh", "/etc/caddy/start.sh"]
    started = run(command, env=stack.env)
    assert started.returncode == 0, started.stderr
    try:
        yield f"https://127.0.0.1:{BY_IP_HTTPS_PORT}"
    finally:
        run(["docker", "rm", "--force", name], env=stack.env)


def test_a_page_opened_by_ip_address_gets_the_certificate_of_that_address(edge_by_ip):
    deadline = time.monotonic() + LOG_WAIT_S
    while True:
        try:
            authority = httpx.get(edge_by_ip + "/ca.crt", verify=False, timeout=5)
            break
        except httpx.TransportError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)
    # Trusting the authority is enough: the certificate is valid for the address itself.
    context = ssl.create_default_context(cadata=authority.text)
    assert httpx.get(edge_by_ip + "/ca.crt", verify=context).status_code == 200


# What the edge forwards: an edge of its own, on other ports, in front of a stand-in that answers
# every request with the headers it received, under both upstream names (``core`` and
# ``gatekeeper``). The real services never show the headers they get.
ECHO_HTTPS_PORT = 18445
ECHO_HTTP_PORT = 18082
ECHO_SERVER = """
import http.server, json, threading

class Echo(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps([[name.lower(), value] for name, value in self.headers.items()]).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

servers = [http.server.ThreadingHTTPServer(("0.0.0.0", port), Echo) for port in (8000, 8082)]
threading.Thread(target=servers[1].serve_forever, daemon=True).start()
servers[0].serve_forever()
"""


@pytest.fixture
def edge_before_echo(stack: StackInfo) -> Iterator[str]:
    suffix = uuid.uuid4().hex[:8]
    network = f"{stack.project}-echo-{suffix}"
    echo, edge = f"{network}-upstream", f"{network}-edge"
    created = run(["docker", "network", "create", network], env=stack.env)
    assert created.returncode == 0, created.stderr
    try:
        command = ["docker", "run", "--detach", "--rm", "--name", echo, "--network", network]
        command += ["--network-alias", "core", "--network-alias", "gatekeeper"]
        command += ["--no-healthcheck", stack.inspect("core")["Image"], "python", "-c", ECHO_SERVER]
        started = run(command, env=stack.env)
        assert started.returncode == 0, started.stderr
        command = ["docker", "run", "--detach", "--rm", "--name", edge, "--network", network]
        command += ["-e", "DOMO_SITE_ADDRESSES=localhost"]
        command += ["-e", f"DOMO_HTTPS_PORT={ECHO_HTTPS_PORT}"]
        command += ["-e", f"DOMO_HTTP_PORT={ECHO_HTTP_PORT}"]
        command += ["-p", f"127.0.0.1:{ECHO_HTTPS_PORT}:{ECHO_HTTPS_PORT}"]
        for name_in_repo in ("Caddyfile", "start.sh"):
            source = REPO_ROOT / "deploy" / "edge" / name_in_repo
            target = f"/etc/caddy/{name_in_repo}"
            command += ["--mount", f"type=bind,source={source},target={target},readonly"]
        command += [stack.inspect("edge")["Image"], "/bin/sh", "/etc/caddy/start.sh"]
        started = run(command, env=stack.env)
        assert started.returncode == 0, started.stderr
        yield f"https://localhost:{ECHO_HTTPS_PORT}"
    finally:
        run(["docker", "rm", "--force", echo, edge], env=stack.env)
        run(["docker", "network", "rm", network], env=stack.env)


def forwarded_headers(url: str, headers: dict[str, str]) -> list[list[str]]:
    """The headers the stand-in behind the edge received for a GET of ``url``."""
    deadline = time.monotonic() + LOG_WAIT_S
    while True:
        try:
            r = httpx.get(url, headers=headers, verify=False, timeout=5)
            if r.status_code == 200:
                return r.json()
        except httpx.TransportError:
            pass
        if time.monotonic() > deadline:
            raise AssertionError(f"no answer from the stand-in behind the edge at {url}")
        time.sleep(0.5)


def test_the_core_session_cookie_never_reaches_the_gatekeeper(edge_before_echo):
    """The vault form needs no cookie, and the core's session must stay with the core: the edge
    drops every cookie on the way to the gatekeeper, and keeps them on the way to the core."""
    cookie = {"cookie": "domo_session=not-a-real-session; other=value"}
    to_gatekeeper = forwarded_headers(edge_before_echo + "/vault/r/unknown", cookie)
    assert ["x-forwarded-proto", "https"] in to_gatekeeper  # it went through the edge
    assert not [name for name, _ in to_gatekeeper if name == "cookie"]
    to_core = forwarded_headers(edge_before_echo + "/login", cookie)
    assert ["cookie", cookie["cookie"]] in to_core

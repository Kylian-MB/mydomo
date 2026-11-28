import base64
import json
import re
from urllib.parse import quote

import pytest
from domo_common.gatekeeper_api import BasicLocation, Binding, LoginLocation
from domo_gatekeeper.scrub import (
    MASK,
    SHORTEST_DETECTED,
    Scrubber,
    SecretDetector,
    SecretFound,
    scrub_text,
)
from domo_gatekeeper.vault import join_account, secret_parts

SECRET = "sk-test-0123456789"  # gitleaks:allow (a fake value for the tests)


def scrub_in_chunks(secrets: list[str], data: bytes, size: int) -> bytes:
    scrubber = Scrubber(secrets)
    out = b"".join(scrubber.feed(data[i : i + size]) for i in range(0, len(data), size))
    return out + scrubber.flush()


def test_the_mask_is_exactly_redacted_in_brackets():
    assert MASK == "[REDACTED]"


def test_scrubber_handles_secret_split_across_chunks():
    s = Scrubber(["abcdef123456"])
    out = s.feed(b"xx abcdef") + s.feed(b"123456 yy") + s.flush()
    assert b"abcdef123456" not in out and b"[REDACTED]" in out


def test_scrub_base64_variant():
    secret = "sk-test-0123456789"  # gitleaks:allow
    encoded = base64.b64encode(secret.encode()).decode()
    assert encoded not in scrub_text(f"token={encoded}", [secret])


def test_raw_secret_is_replaced_by_the_mask():
    assert scrub_text(f"key={SECRET}!", [SECRET]) == "key=[REDACTED]!"


def test_text_without_a_secret_is_unchanged():
    assert scrub_text("nothing to see here", [SECRET]) == "nothing to see here"


@pytest.mark.parametrize(
    "encode",
    [
        lambda raw: base64.b64encode(raw).decode(),
        lambda raw: base64.b64encode(raw).decode().rstrip("="),
        lambda raw: base64.urlsafe_b64encode(raw).decode(),
        lambda raw: base64.urlsafe_b64encode(raw).decode().rstrip("="),
    ],
    ids=["standard", "standard-unpadded", "urlsafe", "urlsafe-unpadded"],
)
def test_every_base64_variant_is_masked(encode):
    # Chosen so that the standard and URL-safe alphabets differ, and padding is needed.
    secret = "key?>>>~~~??>>a"
    encoded = encode(secret.encode())
    assert scrub_text(f"x {encoded} y", [secret]) == "x [REDACTED] y"


def test_padded_base64_is_masked_as_a_whole():
    secret = "sk-test-01234567"  # gitleaks:allow (16 bytes: the padded form ends with "==")
    padded = base64.b64encode(secret.encode()).decode()
    assert padded.endswith("==")
    assert scrub_text(f"[{padded}]", [secret]) == "[[REDACTED]]"


@pytest.mark.parametrize("hex_case", [str.upper, str.lower])
def test_percent_encoded_variant_is_masked(hex_case):
    secret = "key/with space&more+stuff"
    encoded = quote(secret, safe="")
    assert encoded != secret
    # Hex digits are case-insensitive: both spellings are masked.
    spelled = re.sub(r"%[0-9A-Fa-f]{2}", lambda match: hex_case(match.group()), encoded)
    assert scrub_text(f"?k={spelled}&x=1", [secret]) == "?k=[REDACTED]&x=1"


def test_secrets_shorter_than_eight_characters_are_masked_in_raw_form_only():
    secret = "abc1234"  # 7 characters
    assert scrub_text(f"a {secret} b", [secret]) == "a [REDACTED] b"
    encoded = base64.b64encode(secret.encode()).decode()
    assert scrub_text(f"a {encoded} b", [secret]) == f"a {encoded} b"


def test_secrets_of_eight_characters_get_every_variant():
    secret = "abcd1234"
    encoded = base64.b64encode(secret.encode()).decode()
    assert scrub_text(encoded, [secret]) == "[REDACTED]"


def test_every_secret_of_the_list_is_masked():
    assert scrub_text("a first-secret b second-secret c", ["first-secret", "second-secret"]) == (
        "a [REDACTED] b [REDACTED] c"
    )


def test_an_empty_secret_is_ignored():
    assert scrub_text("some text", [""]) == "some text"
    assert scrub_text("some text", []) == "some text"


def test_duplicate_secrets_are_harmless():
    assert scrub_text(f"a {SECRET} b", [SECRET, SECRET]) == "a [REDACTED] b"


def test_scrub_text_survives_text_that_is_not_valid_unicode():
    # A lone surrogate cannot be encoded as UTF-8 text: it must neither raise nor leak the text.
    text = f"before \ud800 {SECRET} after"
    assert scrub_text(text, [SECRET]) == "before \ud800 [REDACTED] after"


def test_binary_data_passes_through_untouched():
    data = bytes(range(256)) * 4
    assert scrub_in_chunks([SECRET], data, 7) == data


def test_the_scrubber_holds_back_only_what_could_still_become_a_secret():
    scrubber = Scrubber(["abcdef123456"])
    # At most len(longest variant) - 1 bytes are kept back between two chunks.
    out = scrubber.feed(b"x" * 1000)
    longest_variant = len(base64.b64encode(b"abcdef123456"))
    assert len(out) == 1000 - (longest_variant - 1)
    assert out + scrubber.flush() == b"x" * 1000


def test_a_partial_secret_at_the_end_of_the_stream_is_released_by_flush():
    scrubber = Scrubber(["abcdef123456"])
    assert scrubber.feed(b"data abcdef") + scrubber.flush() == b"data abcdef"


def test_a_secret_ending_the_stream_is_masked_by_flush():
    scrubber = Scrubber(["abcdef123456"])
    assert scrubber.feed(b"data abcdef123456") + scrubber.flush() == b"data [REDACTED]"


def test_without_secrets_chunks_pass_straight_through():
    scrubber = Scrubber([])
    assert scrubber.feed(b"abc") == b"abc"
    assert scrubber.flush() == b""


def test_an_empty_chunk_is_harmless():
    scrubber = Scrubber(["abcdef123456"])
    assert scrubber.feed(b"") == b""
    assert scrubber.feed(b"x abcdef123456") + scrubber.flush() == b"x [REDACTED]"


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 11, 64])
def test_the_output_does_not_depend_on_how_the_stream_is_chunked(size):
    secrets = [SECRET, "second/secret+value"]
    encoded = base64.urlsafe_b64encode(SECRET.encode()).decode()
    data = (
        f"head {SECRET} mid {quote(secrets[1], safe='')} {encoded} "
        f"{secrets[1]}{SECRET}{SECRET} tail {SECRET[:-1]}"
    ).encode()
    whole = Scrubber(secrets)
    expected = whole.feed(data) + whole.flush()
    assert SECRET.encode() not in expected
    assert expected.count(b"[REDACTED]") == 6
    assert scrub_in_chunks(secrets, data, size) == expected


# --- an account: each of its values is masked on its own --------------------------------------

USER = "house-user-01"
PASSWORD = "s3cr3t-pass/word"  # gitleaks:allow (a fake value for the tests)
ENCODINGS = {
    "clear": lambda text: text,
    "base64": lambda text: base64.b64encode(text.encode()).decode(),
    "base64-urlsafe-unpadded": lambda text: (
        base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")
    ),
    "percent": lambda text: quote(text, safe=""),
}


ACCOUNT = join_account(USER, PASSWORD)


@pytest.fixture
def account_scrubber() -> Scrubber:
    """The scrubber of the answer to a request that carried the account: every part of it."""
    return Scrubber(secret_parts(ACCOUNT))


@pytest.fixture
def everywhere_scrubber(vault) -> Scrubber:
    """The scrubber of every answer, as a relay builds it: from ``vault.values()``."""
    login = Binding(
        host="site.example.com",
        location=LoginLocation(
            kind="login",
            path="/login",
            encoding="form",
            user_field="username",
            password_field="password",
        ),
    )
    vault.put("account:site", ACCOUNT, purpose="p", bindings=[login])
    return Scrubber(vault.values())


@pytest.mark.parametrize("encoding", ENCODINGS)
@pytest.mark.parametrize("part", [USER, PASSWORD], ids=["user", "password"])
def test_each_value_of_an_account_is_masked_alone(account_scrubber, part, encoding):
    shown = ENCODINGS[encoding](part)
    out = account_scrubber.feed(f'{{"echo": "{shown}"}}'.encode()) + account_scrubber.flush()
    assert out == b'{"echo": "[REDACTED]"}'


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_every_answer_is_masked_of_the_password_but_not_of_the_user(everywhere_scrubber, encoding):
    out = everywhere_scrubber.feed(
        f"{ENCODINGS[encoding](USER)} {ENCODINGS[encoding](PASSWORD)}".encode()
    )
    out += everywhere_scrubber.flush()
    assert out == f"{ENCODINGS[encoding](USER)} [REDACTED]".encode()


@pytest.mark.parametrize("scrubber", ["account_scrubber", "everywhere_scrubber"])
@pytest.mark.parametrize("encoding", ["base64", "base64-urlsafe-unpadded"])
def test_an_echoed_basic_header_is_masked(request, scrubber, encoding):
    # What Basic sends: the two values joined by a colon, in base64.
    masking: Scrubber = request.getfixturevalue(scrubber)
    shown = ENCODINGS[encoding](f"{USER}:{PASSWORD}")
    out = masking.feed(f"authorization: Basic {shown}".encode()) + masking.flush()
    assert out == b"authorization: Basic [REDACTED]"


def test_an_echoed_login_body_is_masked(account_scrubber):
    body = f"username={quote(USER, safe='')}&password={quote(PASSWORD, safe='')}"
    out = account_scrubber.feed(body.encode()) + account_scrubber.flush()
    assert out == b"username=[REDACTED]&password=[REDACTED]"


# --- a secret echoed inside JSON, with its escapes ---------------------------------------------

# A password holding a quote, a backslash, a slash and an accented letter: JSON writes each of them
# otherwise than raw.
ESCAPED = 'pa"ss\\wo/rd-é1'  # gitleaks:allow (a fake value for the tests)
JSON_FORMS = {
    "ascii": json.dumps(ESCAPED)[1:-1],
    "ascii-upper-hex": json.dumps(ESCAPED)[1:-1].replace("\\u00e9", "\\u00E9"),
    "unicode": json.dumps(ESCAPED, ensure_ascii=False)[1:-1],
    "ascii-slash": json.dumps(ESCAPED)[1:-1].replace("/", "\\/"),
    "unicode-slash": json.dumps(ESCAPED, ensure_ascii=False)[1:-1].replace("/", "\\/"),
}


@pytest.mark.parametrize("form", JSON_FORMS)
def test_a_secret_echoed_in_a_json_string_with_its_escapes_is_masked(form):
    shown = JSON_FORMS[form]
    assert shown != ESCAPED
    body = f'{{"error": "bad password {shown}"}}'
    assert scrub_text(body, [ESCAPED]) == '{"error": "bad password [REDACTED]"}'


@pytest.mark.parametrize("form", ["ascii", "unicode-slash"])
def test_every_answer_is_masked_of_a_json_escaped_password(vault, form):
    # The set masked in every answer (``vault.values()``) knows the escaped forms too.
    vault.put(
        "account:device",
        join_account(USER, ESCAPED),
        purpose="p",
        bindings=[Binding(host="device.lan", location=BasicLocation(kind="basic"))],
    )
    masking = Scrubber(vault.values())
    out = masking.feed(f'{{"echo": "{JSON_FORMS[form]}"}}'.encode()) + masking.flush()
    assert out == b'{"echo": "[REDACTED]"}'


def test_matches_split_at_every_possible_position_are_masked():
    data = f"aa {SECRET} bb".encode()
    for cut in range(len(data) + 1):
        scrubber = Scrubber([SECRET])
        out = scrubber.feed(data[:cut]) + scrubber.feed(data[cut:]) + scrubber.flush()
        assert out == b"aa [REDACTED] bb", cut


# --- The detector of secrets in sound -----------------------------------------------------------


def detect_in_chunks(secrets: list[str], data: bytes, size: int) -> bytes:
    detector = SecretDetector(secrets)
    out = b"".join(detector.feed(data[i : i + size]) for i in range(0, len(data), size))
    return out + detector.flush()


def test_the_detector_passes_everything_through_unchanged_without_a_secret():
    data = bytes(range(256)) * 40 + MASK.encode()
    assert detect_in_chunks([SECRET], data, 7) == data
    assert detect_in_chunks([], data, 7) == data


def test_the_detector_gives_the_same_bytes_whatever_the_chunking():
    data = bytes(range(256)) * 8
    results = {detect_in_chunks([SECRET], data, size) for size in (1, 2, 5, 17, 300, len(data))}
    assert results == {data}


def test_the_detector_finds_a_secret_cut_anywhere_and_releases_none_of_it():
    data = f"aa {SECRET} bb".encode()
    for cut in range(len(data) + 1):
        detector = SecretDetector([SECRET])
        released = b""
        with pytest.raises(SecretFound):
            released += detector.feed(data[:cut])
            released += detector.feed(data[cut:])
        assert SECRET[:2].encode() not in released, cut


def test_the_detector_holds_back_the_end_until_flushed():
    detector = SecretDetector([SECRET])
    data = b"x" * 100
    released = detector.feed(data)
    assert len(released) < len(data)
    assert released + detector.flush() == data
    assert detector.flush() == b""


def test_short_secrets_are_not_looked_for_in_sound():
    # A code of a few characters cannot be told from noise: it would cut every sound.
    assert SHORTEST_DETECTED == 8
    data = bytes(range(256)) * 4 + b"1234" + b"ab"
    assert detect_in_chunks(["ab", "1234", "1234567"], data, 64) == data
    with pytest.raises(SecretFound):
        detect_in_chunks(["ab", "12345678"], data + b"12345678", 64)

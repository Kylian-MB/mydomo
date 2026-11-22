import base64
import re
from urllib.parse import quote

import pytest
from domo_gatekeeper.scrub import MASK, Scrubber, scrub_text

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


def test_matches_split_at_every_possible_position_are_masked():
    data = f"aa {SECRET} bb".encode()
    for cut in range(len(data) + 1):
        scrubber = Scrubber([SECRET])
        out = scrubber.feed(data[:cut]) + scrubber.feed(data[cut:]) + scrubber.flush()
        assert out == b"aa [REDACTED] bb", cut

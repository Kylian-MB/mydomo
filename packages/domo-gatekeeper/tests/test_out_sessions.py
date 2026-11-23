from domo_gatekeeper.sessions import Sessions

REF = "site:alex"
OTHER_REF = "site:dana"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_kept_cookie_goes_back_to_its_host_with_its_account():
    sessions = Sessions()
    sessions.keep(REF, "site.test", ["sid=s3ssion-value; Path=/; HttpOnly"])
    assert sessions.cookies_for(REF, "site.test", 443, "/home", True) == "sid=s3ssion-value"


def test_a_cookie_never_goes_with_another_account():
    sessions = Sessions()
    sessions.keep(REF, "site.test", ["sid=s3ssion-value; Path=/"])
    assert sessions.cookies_for(OTHER_REF, "site.test", 443, "/", True) is None


def test_a_cookie_never_goes_to_another_host():
    sessions = Sessions()
    sessions.keep(REF, "site.test", ["sid=s3ssion-value; Path=/"])
    assert sessions.cookies_for(REF, "other.test", 443, "/", True) is None


def test_a_cookie_for_a_domain_the_host_is_not_in_is_refused():
    sessions = Sessions()
    sessions.keep(REF, "site.test", ["sid=s3ssion-value; Path=/; Domain=other.test"])
    assert sessions.cookies_for(REF, "other.test", 443, "/", True) is None
    assert sessions.values(REF) == []


def test_a_secure_cookie_goes_over_https_only():
    sessions = Sessions()
    sessions.keep(REF, "site.test", ["sid=s3ssion-value; Path=/; Secure"])
    assert sessions.cookies_for(REF, "site.test", 80, "/", False) is None
    assert sessions.cookies_for(REF, "site.test", 443, "/", True) == "sid=s3ssion-value"


def test_a_cookie_keeps_to_its_path():
    sessions = Sessions()
    sessions.keep(REF, "site.test", ["sid=s3ssion-value; Path=/api"])
    assert sessions.cookies_for(REF, "site.test", 443, "/other", True) is None
    assert sessions.cookies_for(REF, "site.test", 443, "/api/state", True) == "sid=s3ssion-value"


def test_several_cookies_go_together():
    sessions = Sessions()
    sessions.keep(REF, "site.test", ["a=first-value; Path=/", "b=second-value; Path=/"])
    sent = sessions.cookies_for(REF, "site.test", 443, "/", True)
    assert sent is not None and sorted(sent.split("; ")) == ["a=first-value", "b=second-value"]


def test_an_expired_cookie_is_removed():
    sessions = Sessions()
    sessions.keep(REF, "site.test", ["sid=s3ssion-value; Path=/"])
    sessions.keep(REF, "site.test", ["sid=gone; Path=/; Max-Age=0"])
    assert sessions.cookies_for(REF, "site.test", 443, "/", True) is None


def test_an_ip_address_host_keeps_its_cookies():
    sessions = Sessions()
    sessions.keep(REF, "192.168.1.20", ["sid=s3ssion-value; Path=/"])
    assert sessions.cookies_for(REF, "192.168.1.20", 8123, "/", False) == "sid=s3ssion-value"


def test_the_values_of_an_account_are_what_its_answers_are_scrubbed_of():
    sessions = Sessions()
    sessions.keep(REF, "site.test", ["sid=s3ssion-value; Path=/", "lang=en; Path=/"])
    sessions.keep(OTHER_REF, "site.test", ["sid=another-value; Path=/"])
    # Each account's own; a value under 8 characters is no credential and is left alone.
    assert sessions.values(REF) == ["s3ssion-value"]
    assert sessions.values(OTHER_REF) == ["another-value"]
    assert sessions.values("site:none") == []


def test_a_session_unused_for_the_idle_time_is_forgotten():
    clock = Clock()
    sessions = Sessions(clock=clock)
    sessions.keep(REF, "site.test", ["sid=s3ssion-value; Path=/"])
    clock.now += 60
    sessions.forget_idle(60)
    assert sessions.cookies_for(REF, "site.test", 443, "/", True) == "sid=s3ssion-value"
    clock.now += 61
    sessions.forget_idle(60)
    assert sessions.cookies_for(REF, "site.test", 443, "/", True) is None
    assert sessions.values(REF) == []


def test_using_a_session_keeps_it_alive():
    clock = Clock()
    sessions = Sessions(clock=clock)
    sessions.keep(REF, "site.test", ["sid=s3ssion-value; Path=/"])
    for _ in range(3):
        clock.now += 50
        sessions.forget_idle(60)
        assert sessions.cookies_for(REF, "site.test", 443, "/", True) == "sid=s3ssion-value"


def test_a_header_that_sets_no_cookie_keeps_nothing():
    sessions = Sessions()
    sessions.keep(REF, "site.test", ["", "no equal sign"])
    assert sessions.values(REF) == []
    assert sessions.cookies_for(REF, "site.test", 443, "/", True) is None

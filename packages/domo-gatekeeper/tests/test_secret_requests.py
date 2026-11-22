import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from domo_common.gatekeeper_api import (
    FORM_TEXT_KEYS,
    Binding,
    InjectionLocation,
    SecretRequestCreate,
)
from domo_gatekeeper.errors import (
    BindingMismatchError,
    InvalidSecretValueError,
    RequestNotPendingError,
)
from domo_gatekeeper.secret_requests import PendingRequest, SecretRequestStore

EVIL = Binding(host="evil.example.net", location=InjectionLocation(kind="header", name="x-api-key"))


def token_of(form_path: str | None) -> str:
    assert form_path is not None
    return form_path.rsplit("/", 1)[1]


def test_create_returns_a_pending_view_with_a_form_path(store, vault, req, clock):
    view = store.create(req, vault)
    assert view.status == "pending"
    assert view.ref == req.ref
    assert re.fullmatch(r"/vault/r/[A-Za-z0-9_-]{43}", view.form_path or "")
    assert view.expires_at.utcoffset() == timedelta(0)


def test_create_expires_after_the_ttl(store_with_clock, vault, req, clock):
    default = store_with_clock.create(req, vault)
    assert default.expires_at == clock() + timedelta(minutes=15)
    custom = store_with_clock.create(req, vault, ttl=timedelta(hours=2))
    assert custom.expires_at == clock() + timedelta(hours=2)


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(seconds=-1)])
def test_create_rejects_a_non_positive_ttl(store, vault, req, ttl):
    with pytest.raises(ValueError, match="ttl"):
        store.create(req, vault, ttl=ttl)


def test_create_ids_and_tokens_are_unique(store, vault, req):
    views = [store.create(req, vault) for _ in range(5)]
    assert len({v.id for v in views}) == 5
    assert len({v.form_path for v in views}) == 5


def test_request_is_single_use(store, vault, req):
    view = store.create(req, vault)
    token = token_of(view.form_path)
    store.fulfil(token, "v", vault)
    with pytest.raises(RequestNotPendingError):
        store.fulfil(token, "v2", vault)
    assert vault.reveal(req.ref) == "v"


def test_expired_request_is_refused(store_with_clock, vault, req, clock):
    view = store_with_clock.create(req, vault)
    clock.advance(timedelta(minutes=16))
    assert store_with_clock.pending_by_token(token_of(view.form_path)) is None
    assert store_with_clock.get(view.id).status == "expired"


def test_expired_request_cannot_be_fulfilled(store_with_clock, vault, req, clock):
    token = token_of(store_with_clock.create(req, vault).form_path)
    clock.advance(timedelta(minutes=15))  # the expiry instant itself is already too late
    with pytest.raises(RequestNotPendingError):
        store_with_clock.fulfil(token, "v", vault)
    assert vault.metadata(req.ref) is None


def test_new_request_supersedes_pending(store, vault, req):
    first = store.create(req, vault)
    second = store.create(req, vault)
    assert store.get(first.id).status == "superseded"
    assert store.get(second.id).status == "pending"
    with pytest.raises(RequestNotPendingError):
        store.fulfil(token_of(first.form_path), "v", vault)
    assert store.pending_by_token(token_of(first.form_path)) is None
    assert vault.metadata(req.ref) is None


def test_superseding_leaves_other_refs_and_expired_requests_alone(
    store_with_clock, vault, req, clock
):
    other = req.model_copy(update={"ref": "provider:other:api_key"})
    expired = store_with_clock.create(req, vault, ttl=timedelta(minutes=1))
    clock.advance(timedelta(minutes=2))
    unrelated = store_with_clock.create(other, vault)
    store_with_clock.create(req, vault)
    assert store_with_clock.get(expired.id).status == "expired"
    assert store_with_clock.get(unrelated.id).status == "pending"


def test_conflicting_binding_is_refused_at_creation(store, vault, req):
    vault.put(req.ref, "v", purpose="p", bindings=[EVIL])
    with pytest.raises(BindingMismatchError):
        store.create(req, vault)


def test_conflicting_binding_creation_leaves_the_pending_request_alone(store, vault, req):
    first = store.create(req, vault)
    vault.put(req.ref, "v", purpose="p", bindings=list(req.bindings) + [EVIL])
    with pytest.raises(BindingMismatchError):
        store.create(req, vault)
    assert store.get(first.id).status == "pending"


def test_same_bindings_in_the_vault_allow_a_rotation_request(store, vault, req):
    vault.put(req.ref, "old", purpose="p", bindings=req.bindings)
    token = token_of(store.create(req, vault).form_path)
    store.fulfil(token, "new", vault)
    assert vault.reveal(req.ref) == "new"
    assert vault.metadata(req.ref).rotated_at is not None


def test_token_not_stored_in_clear(store, vault, req, requests_db_path):
    token = token_of(store.create(req, vault).form_path)
    assert token.encode() not in requests_db_path.read_bytes()


def test_token_is_stored_as_a_sha256_hash(store, vault, req, requests_db_path):
    import hashlib

    token = token_of(store.create(req, vault).form_path)
    with sqlite3.connect(requests_db_path) as conn:
        hashes = [row[0] for row in conn.execute("SELECT token_hash FROM secret_requests")]
    assert hashes == [hashlib.sha256(token.encode()).hexdigest()]


def test_get_never_returns_a_form_path(store, vault, req):
    view = store.create(req, vault)
    fetched = store.get(view.id)
    assert fetched is not None
    assert fetched.form_path is None
    assert fetched.expires_at == view.expires_at
    assert fetched.ref == req.ref


def test_get_unknown_id_is_none(store):
    assert store.get("nope") is None


def test_get_reports_fulfilled(store, vault, req):
    view = store.create(req, vault)
    result = store.fulfil(token_of(view.form_path), "v", vault)
    assert result.status == "fulfilled" and result.form_path is None and result.id == view.id
    assert store.get(view.id).status == "fulfilled"


def test_pending_by_token_returns_what_the_form_needs(store, vault, req):
    view = store.create(req, vault)
    pending = store.pending_by_token(token_of(view.form_path))
    assert pending == PendingRequest(
        id=view.id, ref=req.ref, purpose=req.purpose, bindings=list(req.bindings)
    )


def test_pending_by_token_unknown_is_none(store):
    assert store.pending_by_token("unknown") is None


def test_fulfil_unknown_token_is_refused(store, vault, req):
    with pytest.raises(RequestNotPendingError):
        store.fulfil("unknown", "v", vault)
    assert vault.metadata(req.ref) is None


def test_fulfil_stores_the_value_with_the_requested_purpose_and_bindings(store, vault, req):
    store.fulfil(token_of(store.create(req, vault).form_path), "sk-test-123", vault)
    meta = vault.metadata(req.ref)
    assert vault.reveal(req.ref) == "sk-test-123"
    assert meta.purpose == req.purpose and meta.bindings == list(req.bindings)


def test_fulfil_refused_when_the_secret_got_other_bindings_meanwhile(store, vault, req):
    view = store.create(req, vault)
    vault.put(req.ref, "v", purpose="p", bindings=[EVIL])
    with pytest.raises(BindingMismatchError):
        store.fulfil(token_of(view.form_path), "v2", vault)
    assert vault.reveal(req.ref) == "v"
    assert store.get(view.id).status == "pending"


def test_fulfil_with_an_invalid_value_leaves_the_request_pending(store, vault, req):
    view = store.create(req, vault)
    for bad in ("", "\ud800"):
        with pytest.raises(InvalidSecretValueError):
            store.fulfil(token_of(view.form_path), bad, vault)
    assert store.get(view.id).status == "pending"
    store.fulfil(token_of(view.form_path), "good", vault)
    assert vault.reveal(req.ref) == "good"


def test_concurrent_submissions_of_one_token_succeed_exactly_once(store, vault, req):
    token = token_of(store.create(req, vault).form_path)

    def submit(index: int) -> str:
        try:
            store.fulfil(token, f"value-{index}", vault)
        except RequestNotPendingError:
            return "refused"
        return "ok"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(submit, range(8)))
    assert outcomes.count("ok") == 1 and outcomes.count("refused") == 7
    assert vault.reveal(req.ref).startswith("value-")
    assert vault.metadata(req.ref).rotated_at is None  # stored once, never rotated


def test_errors_never_carry_the_token_or_the_value(store, vault, req):
    token = token_of(store.create(req, vault).form_path)
    store.fulfil(token, "s3cr3t-value", vault)
    with pytest.raises(RequestNotPendingError) as excinfo:
        store.fulfil(token, "s3cr3t-value", vault)
    assert token not in str(excinfo.value) and "s3cr3t-value" not in repr(excinfo.value)


def test_a_naive_clock_is_refused(requests_db_path, vault, req):
    store = SecretRequestStore(requests_db_path, clock=lambda: datetime(2030, 1, 1))
    with pytest.raises(ValueError, match="timezone"):
        store.create(req, vault)


def test_clock_in_another_timezone_is_normalised_to_utc(requests_db_path, vault, req):
    paris = timezone(timedelta(hours=2))
    store = SecretRequestStore(
        requests_db_path, clock=lambda: datetime(2030, 1, 1, 14, 0, tzinfo=paris)
    )
    view = store.create(req, vault)
    assert view.expires_at == datetime(2030, 1, 1, 12, 15, tzinfo=UTC)
    assert view.expires_at.utcoffset() == timedelta(0)


def test_state_survives_a_new_store_on_the_same_file(requests_db_path, vault, req):
    first = SecretRequestStore(requests_db_path)
    view = first.create(req, vault)
    reopened = SecretRequestStore(requests_db_path)
    assert reopened.get(view.id).status == "pending"
    assert reopened.pending_by_token(token_of(view.form_path)) is not None


def test_database_is_not_the_vault_file(store, vault, vault_db_path, requests_db_path, req):
    store.create(req, vault)
    assert requests_db_path != vault_db_path and requests_db_path.exists()
    with sqlite3.connect(requests_db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert "secrets" not in tables


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permissions")
def test_database_file_is_private(store, requests_db_path):
    assert requests_db_path.stat().st_mode & 0o777 == 0o600


def test_database_parent_directory_is_created(tmp_path):
    db = tmp_path / "nested" / "dir" / "requests.db"
    SecretRequestStore(db)
    assert Path(db).exists()


# --- the texts of the form ------------------------------------------------------------------------

# The table as the gatekeeper made it before the texts of the form (0.6.0).
OLD_SCHEMA = """
CREATE TABLE secret_requests (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    ref TEXT NOT NULL,
    purpose TEXT NOT NULL,
    bindings_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'fulfilled', 'superseded')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    fulfilled_at TEXT
)
"""


def with_texts(req: SecretRequestCreate, language: str = "it") -> SecretRequestCreate:
    texts = {key: f"({language}) {key}" for key in FORM_TEXT_KEYS}
    return SecretRequestCreate.model_validate(
        {**req.model_dump(mode="json"), "language": language, "texts": texts}
    )


def test_a_request_keeps_the_texts_of_its_form(store, vault, req):
    written = with_texts(req)
    pending = store.pending_by_token(token_of(store.create(written, vault).form_path))
    assert pending is not None
    assert (pending.language, pending.texts) == ("it", written.texts)
    # Without texts (the installation): the gatekeeper's own.
    fixed = store.pending_by_token(token_of(store.create(req, vault).form_path))
    assert fixed is not None and (fixed.language, fixed.texts) == (None, None)


def test_an_older_database_gains_the_columns_of_the_texts(requests_db_path, vault, req):
    with sqlite3.connect(requests_db_path) as conn:
        conn.execute(OLD_SCHEMA)
    store = SecretRequestStore(requests_db_path)
    SecretRequestStore(requests_db_path)  # opened again: the columns are added once
    pending = store.pending_by_token(token_of(store.create(with_texts(req), vault).form_path))
    assert pending is not None and pending.language == "it"

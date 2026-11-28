"""The spoken phrase: a proof a person says aloud, kept and compared here, never in the core.

The core sends what was heard (``enrol``, ``compare``) and gets back a word: where agreeing on the
phrase stands, or how close the heard text is to the phrase (``PhraseResult``). The phrase itself
never leaves the gatekeeper: not in an answer, an error, a log line or a ``repr``.

Storage: the table ``spoken_phrases`` of the vault's SQLite file, encrypted with AES-256-GCM under
the vault's key, with the associated data ``ASSOCIATED_DATA_PREFIX + ref`` so a ciphertext cannot be
moved to another person (``PENDING_ASSOCIATED_DATA_PREFIX + ref`` for a first saying, so it cannot
be moved into the place of the phrase). What is kept is the normalised form (``normalised``):
comparisons are made on it only. Phrases are not among ``Vault.values()``: a phrase echoed by a
transcription must reach the core, which hands it back here to be compared, instead of being
masked on its way.

Agreeing takes two sayings: the first is kept as pending for ``PENDING_FOR``; the second must be
``it_is`` with it, and then becomes the phrase, replacing an older one. Comparing is throttled like
a password, counted in memory (a restart forgets the counts): ``MAX_NEAR_MISSES`` ``almost`` in
``WINDOW`` for one ref hold that ref back (``nothing_like`` is an ordinary sentence, not a try, and
is not counted there), and so do ``MAX_COMPARISONS`` comparisons of any kind, which bounds how many
questions anyone may ask about a phrase. A ref held back answers nothing else, not even
``nothing_like``: each answer says something of the phrase. A ref with no phrase is counted the
same, so being held back does not tell whether one exists. Every operation opens its own SQLite
connection, as the vault does; comparisons run behind a lock, so a store can be shared by the
threads of the web server and no burst of parallel tries gets past a count.
"""

import os
import re
import sqlite3
import threading
import unicodedata
from collections import OrderedDict, deque
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, cast

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from domo_common.gatekeeper_api import REF_PATTERN, PhraseEnrolled, PhraseResult

from domo_gatekeeper.errors import PhraseHeldBack, VaultDecryptError
from domo_gatekeeper.master_key import KEY_SIZE

# The comparison's thresholds, on ``ratio``: read here and nowhere else.
IT_IS_AT: Final = 0.85
ALMOST_AT: Final = 0.60

# The shortest phrase, once normalised: words, and characters (spaces included).
MIN_WORDS: Final = 3
MIN_CHARS: Final = 12

PENDING_FOR: Final = timedelta(minutes=5)
# Two caps hold a ref back, each over ``WINDOW``: its near misses (``almost``), cleared by the
# right phrase, and all its comparisons, whatever they gave, never cleared by one.
MAX_NEAR_MISSES: Final = 5
MAX_COMPARISONS: Final = 50
WINDOW: Final = timedelta(minutes=15)
# The refs counted, at most, by each cap: the one idle longest is forgotten first.
MAX_TRACKED: Final = 10_000

# Storage formats: changing them makes every kept phrase unreadable. The waiting first saying has
# its own, so that it cannot be moved into the place of the phrase in the file.
ASSOCIATED_DATA_PREFIX: Final = b"domo-phrase/v1\n"
PENDING_ASSOCIATED_DATA_PREFIX: Final = b"domo-phrase/v1\npending\n"

_NONCE_SIZE = 12
_BUSY_TIMEOUT_SECONDS = 30.0
_SPACES = re.compile(r" +")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spoken_phrases (
    ref TEXT PRIMARY KEY,
    nonce BLOB,
    ciphertext BLOB,
    pending_nonce BLOB,
    pending_ciphertext BLOB,
    pending_at TEXT,
    created_at TEXT NOT NULL,
    changed_at TEXT
)
"""


def normalised(heard: str) -> str:
    """``heard`` as it is compared: NFKC, case folded, every character that is not a letter or a
    digit turned into a space, spaces collapsed (and none at either end)."""
    folded = unicodedata.normalize("NFKC", heard).casefold()
    spaced = "".join(char if char.isalnum() else " " for char in folded)
    return _SPACES.sub(" ", spaced).strip()


def _distance(a: str, b: str) -> int:
    """The Levenshtein distance between ``a`` and ``b`` (insertions, deletions, substitutions)."""
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        for j, char_b in enumerate(b, start=1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b))
            )
        previous = current
    return previous[-1]


def ratio(a: str, b: str) -> float:
    """1 − the Levenshtein distance / the length of the longer; 1.0 for two empty texts."""
    longer = max(len(a), len(b))
    return 1.0 if longer == 0 else 1.0 - _distance(a, b) / longer


def _result(stored: str, heard: str) -> PhraseResult:
    """``compare`` on texts already normalised."""
    similarity = ratio(stored, heard)
    if similarity >= IT_IS_AT:
        return "it_is"
    if similarity >= ALMOST_AT:
        return "almost"
    return "nothing_like"


def compare(stored: str, heard: str) -> PhraseResult:
    """How close ``heard`` is to ``stored``, on their normalised forms."""
    return _result(normalised(stored), normalised(heard))


def _long_enough(text: str) -> bool:
    return len(text.split(" ")) >= MIN_WORDS and len(text) >= MIN_CHARS


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _create_private_file(path: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    with suppress(FileExistsError):
        os.close(os.open(path, flags, 0o600))


class _Counts:
    """Events per ref inside a sliding ``WINDOW``, ``limit`` at most; not thread-safe (the store
    holds its lock). Refs are bounded by ``MAX_TRACKED``, the one idle longest forgotten first."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        # Per ref, the times of its events still inside the window, oldest first; the dictionary
        # itself is ordered by each ref's latest event.
        self._events: OrderedDict[str, deque[datetime]] = OrderedDict()

    def wait(self, ref: str, now: datetime) -> float | None:
        """The seconds ``ref`` must wait when it reached the limit, else ``None``."""
        self._forget_stale(now)
        events = self._events.get(ref)
        if events is None:
            return None
        while events and events[0] <= now - WINDOW:
            events.popleft()
        if len(events) < self._limit:
            return None
        return max((events[0] + WINDOW - now).total_seconds(), 1.0)

    def add(self, ref: str, now: datetime) -> None:
        events = self._events.setdefault(ref, deque[datetime]())
        events.append(now)
        self._events.move_to_end(ref)
        while len(self._events) > MAX_TRACKED:
            self._events.popitem(last=False)

    def clear(self, ref: str) -> None:
        self._events.pop(ref, None)

    def _forget_stale(self, now: datetime) -> None:
        while self._events:
            ref, events = next(iter(self._events.items()))
            if events and events[-1] > now - WINDOW:
                break
            del self._events[ref]


# nonce, ciphertext, pending_nonce, pending_ciphertext, pending_at
_Row = tuple[bytes | None, bytes | None, bytes | None, bytes | None, str | None]


class PhraseStore:
    """The spoken phrases, by ref (the core uses ``phrase.<person uuid>``)."""

    def __init__(
        self, db_path: Path, key: bytes, *, clock: Callable[[], datetime] = _utc_now
    ) -> None:
        if len(key) != KEY_SIZE:
            raise ValueError(f"The phrase key must be exactly {KEY_SIZE} bytes.")
        self._db_path = db_path
        self._aead = AESGCM(key)
        self._clock = clock
        self._lock = threading.Lock()
        self._near_misses = _Counts(MAX_NEAR_MISSES)
        self._comparisons = _Counts(MAX_COMPARISONS)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _create_private_file(db_path)
        with self._connection() as conn:
            conn.execute(_SCHEMA)

    def __repr__(self) -> str:
        return "PhraseStore()"

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path, timeout=_BUSY_TIMEOUT_SECONDS, isolation_level=None)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection]:
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    @staticmethod
    def _check_ref(ref: str) -> None:
        if re.fullmatch(REF_PATTERN, ref) is None:
            raise ValueError(f"The phrase ref must match {REF_PATTERN}.")

    def _seal(self, ref: str, text: str, prefix: bytes) -> tuple[bytes, bytes]:
        nonce = os.urandom(_NONCE_SIZE)
        return nonce, self._aead.encrypt(nonce, text.encode(), prefix + ref.encode())

    def _open(self, ref: str, nonce: bytes, ciphertext: bytes, prefix: bytes) -> str:
        try:
            return self._aead.decrypt(nonce, ciphertext, prefix + ref.encode()).decode()
        except (InvalidTag, ValueError):
            pass
        raise VaultDecryptError(
            f"Cannot decrypt the phrase {ref!r}: wrong master key or corrupted vault."
        )

    @staticmethod
    def _row(conn: sqlite3.Connection, ref: str) -> _Row | None:
        row = conn.execute(
            "SELECT nonce, ciphertext, pending_nonce, pending_ciphertext, pending_at"
            " FROM spoken_phrases WHERE ref = ?",
            (ref,),
        ).fetchone()
        return None if row is None else cast(_Row, row)

    def enrol(self, ref: str, heard: str) -> PhraseEnrolled:
        """One saying of a phrase being agreed on (see the module docstring)."""
        self._check_ref(ref)
        text = normalised(heard)
        if not _long_enough(text):
            return PhraseEnrolled(step="too_short")
        now = self._clock()
        with self._transaction() as conn:
            pending = self._pending(ref, self._row(conn, ref), now)
            if pending is None:
                nonce, ciphertext = self._seal(ref, text, PENDING_ASSOCIATED_DATA_PREFIX)
                conn.execute(
                    "INSERT INTO spoken_phrases"
                    " (ref, pending_nonce, pending_ciphertext, pending_at, created_at)"
                    " VALUES (?, ?, ?, ?, ?) ON CONFLICT (ref) DO UPDATE SET"
                    " pending_nonce = excluded.pending_nonce,"
                    " pending_ciphertext = excluded.pending_ciphertext,"
                    " pending_at = excluded.pending_at",
                    (ref, nonce, ciphertext, now.isoformat(), now.isoformat()),
                )
                return PhraseEnrolled(step="first_kept")
            if _result(pending, text) != "it_is":
                self._drop_pending(conn, ref)
                return PhraseEnrolled(step="not_the_same")
            nonce, ciphertext = self._seal(ref, pending, ASSOCIATED_DATA_PREFIX)
            conn.execute(
                "UPDATE spoken_phrases SET nonce = ?, ciphertext = ?, pending_nonce = NULL,"
                " pending_ciphertext = NULL, pending_at = NULL, changed_at = ? WHERE ref = ?",
                (nonce, ciphertext, now.isoformat(), ref),
            )
        with self._lock:
            self._near_misses.clear(ref)
        return PhraseEnrolled(step="agreed")

    def _pending(self, ref: str, row: _Row | None, now: datetime) -> str | None:
        """The first saying waiting to be agreed, if it is still within ``PENDING_FOR``."""
        if row is None:
            return None
        _, _, nonce, ciphertext, kept_at = row
        if nonce is None or ciphertext is None or kept_at is None:
            return None
        if now - datetime.fromisoformat(kept_at) > PENDING_FOR:
            return None
        return self._open(ref, nonce, ciphertext, PENDING_ASSOCIATED_DATA_PREFIX)

    @staticmethod
    def _drop_pending(conn: sqlite3.Connection, ref: str) -> None:
        conn.execute(
            "UPDATE spoken_phrases SET pending_nonce = NULL, pending_ciphertext = NULL,"
            " pending_at = NULL WHERE ref = ?",
            (ref,),
        )
        conn.execute("DELETE FROM spoken_phrases WHERE ref = ? AND ciphertext IS NULL", (ref,))

    def _stored(self, ref: str) -> str:
        with self._connection() as conn:
            row = self._row(conn, ref)
        if row is None or row[0] is None or row[1] is None:
            raise KeyError(ref)
        return self._open(ref, row[0], row[1], ASSOCIATED_DATA_PREFIX)

    def compare(self, ref: str, heard: str) -> PhraseResult:
        """How close ``heard`` is to the phrase of ``ref``.

        Raises ``PhraseHeldBack`` when ``ref`` is held back (see the module docstring), before
        anything is compared and whatever was heard; the comparison is then not counted.
        Otherwise it is counted, and raises ``KeyError`` when ``ref`` has no phrase.
        """
        self._check_ref(ref)
        now = self._clock()
        with self._lock:
            waits = [
                wait
                for counts in (self._near_misses, self._comparisons)
                if (wait := counts.wait(ref, now)) is not None
            ]
            if waits:
                raise PhraseHeldBack(max(waits))
            self._comparisons.add(ref, now)
            result = _result(self._stored(ref), normalised(heard))
            if result == "it_is":
                self._near_misses.clear(ref)
            elif result == "almost":
                self._near_misses.add(ref, now)
        return result

    def remove(self, ref: str) -> bool:
        """Forget the phrase of ``ref`` (and a saying waiting to be agreed); whether there was a
        phrase."""
        self._check_ref(ref)
        with self._transaction() as conn:
            row = self._row(conn, ref)
            conn.execute("DELETE FROM spoken_phrases WHERE ref = ?", (ref,))
        with self._lock:
            self._near_misses.clear(ref)
        return row is not None and row[1] is not None

    def has(self, ref: str) -> bool:
        """Whether ``ref`` has a phrase (a saying waiting to be agreed is not one)."""
        self._check_ref(ref)
        with self._connection() as conn:
            row = self._row(conn, ref)
        return row is not None and row[1] is not None

"""Weights are fetched once, resumed, checked against their digest, and kept."""

import hashlib
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from domo_speech.data import FileData, ModelEntryData
from domo_speech.real.weights import WeightsError, ensure_weights

BODY = bytes(range(256)) * 400


class _Files:
    def __init__(self) -> None:
        self.served: dict[str, bytes] = {}
        self.requests: list[tuple[str, str | None]] = []


@pytest.fixture
def server() -> Iterator[tuple[str, _Files]]:
    """A local file server that understands ``Range`` (no network)."""
    files = _Files()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            asked = self.headers.get("Range")
            files.requests.append((self.path, asked))
            body = files.served.get(self.path)
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            if asked:
                start = int(asked.removeprefix("bytes=").rstrip("-"))
                self.send_response(206)
                body = body[start:]
            else:
                self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", files
    httpd.shutdown()
    httpd.server_close()


def _entry(body: bytes, *, digest: str | None = None) -> ModelEntryData:
    return ModelEntryData(
        id="a-model",
        does="hear",
        engine="faster_whisper",
        source="https://example.org/a-model",
        licence="MIT",
        licence_url="https://spdx.org/licenses/MIT.html",
        restricted=False,
        code="https://example.org/code",
        code_licence="MIT",
        runs="imported",
        languages=("*",),
        size_mb=1,
        needs="cpu",
        floor=True,
        repo="someone/a-model",
        revision="0" * 40,
        files=(
            FileData(
                path="sub/model.bin",
                size=len(body),
                sha256=digest or hashlib.sha256(body).hexdigest(),
            ),
        ),
    )


URL = f"/someone/a-model/resolve/{'0' * 40}/sub/model.bin"


def test_weights_are_fetched_once_and_kept(server: tuple[str, _Files], tmp_path: Path) -> None:
    base, files = server
    files.served[URL] = BODY

    first = ensure_weights(_entry(BODY), tmp_path, source=base)
    again = ensure_weights(_entry(BODY), tmp_path, source=base)

    assert first == again == tmp_path / "a-model"
    assert (first / "sub" / "model.bin").read_bytes() == BODY
    assert len(files.requests) == 1
    assert not list(tmp_path.rglob("*.part"))


def test_a_download_cut_short_is_resumed(server: tuple[str, _Files], tmp_path: Path) -> None:
    base, files = server
    files.served[URL] = BODY
    part = tmp_path / "a-model" / "sub" / "model.bin.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(BODY[:1000])

    ensure_weights(_entry(BODY), tmp_path, source=base)

    assert files.requests == [(URL, "bytes=1000-")]
    assert (tmp_path / "a-model" / "sub" / "model.bin").read_bytes() == BODY


def test_a_wrong_digest_is_refused_and_removed(server: tuple[str, _Files], tmp_path: Path) -> None:
    base, files = server
    files.served[URL] = BODY

    with pytest.raises(WeightsError):
        ensure_weights(_entry(BODY, digest="f" * 64), tmp_path, source=base)

    assert not [p for p in tmp_path.rglob("*") if p.is_file()]


def test_a_file_changed_on_disk_is_fetched_again(
    server: tuple[str, _Files], tmp_path: Path
) -> None:
    base, files = server
    files.served[URL] = BODY
    ensure_weights(_entry(BODY), tmp_path, source=base)
    target = tmp_path / "a-model" / "sub" / "model.bin"
    target.write_bytes(b"\x00" * len(BODY))
    (tmp_path / "a-model" / ".checked").unlink()

    ensure_weights(_entry(BODY), tmp_path, source=base)

    assert target.read_bytes() == BODY
    assert len(files.requests) == 2


def test_an_unreachable_source_is_a_plain_error(tmp_path: Path) -> None:
    with pytest.raises(WeightsError, match="unreachable"):
        ensure_weights(_entry(BODY), tmp_path, source="http://127.0.0.1:9")


def test_a_path_out_of_the_model_directory_is_refused(tmp_path: Path) -> None:
    entry = _entry(BODY)
    escaping = ModelEntryData(
        **{**entry.__dict__, "files": (FileData("../../x.bin", 1, "0" * 64),)}
    )
    with pytest.raises(WeightsError, match="bad_path"):
        ensure_weights(escaping, tmp_path / "models", source="http://127.0.0.1:9")

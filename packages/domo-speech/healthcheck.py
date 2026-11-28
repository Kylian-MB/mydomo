"""The health check of the speech container: ``python /app/healthcheck.py``, exit 0 when healthy.

Healthy: ``/health`` says ``ready``, or ``preparing`` with no failure reason (the service is
measuring its models). Not healthy: a body that says it failed, with any reason (including
``weights_unreachable`` while it retries), any other answer, or no answer. One copy of the check:
compose and the Dockerfile both run this file.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any


def health_url() -> str:
    """The service's ``/health`` on the port it listens on (``DOMO_SPEECH_PORT``, else 8083)."""
    port = os.environ.get("DOMO_SPEECH_PORT", "").strip() or "8083"
    return f"http://127.0.0.1:{port}/health"


def is_healthy(code: int, body: Any) -> bool:
    if not isinstance(body, dict) or "reason" in body:
        return False
    status = body.get("status")
    return (code == 200 and status == "ready") or (code == 503 and status == "preparing")


def check(url: str | None = None) -> bool:
    url = url or health_url()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        try:
            with opener.open(url, timeout=3) as answer:
                code, raw = answer.status, answer.read()
        except urllib.error.HTTPError as error:
            code, raw = error.code, error.read()
        return is_healthy(code, json.loads(raw))
    except (OSError, ValueError):
        return False


if __name__ == "__main__":
    sys.exit(0 if check() else 1)

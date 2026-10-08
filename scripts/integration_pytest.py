"""Run pytest with real HTTP transports disabled, retaining ASGI and mock transports."""

import sys
from contextlib import contextmanager
from unittest.mock import patch

import httpx
import pytest


@contextmanager
def no_real_http():
    attempts = []

    def blocked(_transport, request):
        attempts.append(request.url.host)
        raise AssertionError("Real HTTP calls are disabled during synthetic integration tests")

    async def blocked_async(_transport, request):
        return blocked(_transport, request)

    with patch.object(httpx.HTTPTransport, "handle_request", blocked), \
         patch.object(httpx.AsyncHTTPTransport, "handle_async_request", blocked_async):
        yield attempts


def main(argv=None):
    with no_real_http() as attempts:
        code = int(pytest.main(sys.argv[1:] if argv is None else argv))
    if attempts:
        print(f"FAILED: {len(attempts)} attempted real HTTP calls were blocked", file=sys.stderr)
        return 1
    return code


if __name__ == "__main__":
    raise SystemExit(main())

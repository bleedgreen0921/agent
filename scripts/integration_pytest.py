"""Disable real HTTP except explicitly scoped exact loopback test endpoints."""
import contextvars
import ipaddress
import sys
from contextlib import contextmanager
from unittest.mock import patch
import httpx
import pytest

if __name__ == '__main__':
    # pytest imports the canonical module; share this guard's scoped ContextVar.
    sys.modules['scripts.integration_pytest'] = sys.modules[__name__]

_allowed_endpoint = contextvars.ContextVar('synthetic_http_endpoint', default=None)


@contextmanager
def allow_test_endpoint(host, port):
    if not ipaddress.ip_address(host).is_loopback or not 1 <= port <= 65535:
        raise ValueError('Test endpoint must be an exact loopback IP and port')
    token = _allowed_endpoint.set((host, port))
    try:
        yield
    finally:
        _allowed_endpoint.reset(token)


@contextmanager
def no_real_http():
    attempts = []
    original_sync = httpx.HTTPTransport.handle_request
    original_async = httpx.AsyncHTTPTransport.handle_async_request

    def permitted(request):
        return (request.url.host, request.url.port) == _allowed_endpoint.get()

    def blocked(_transport, request):
        if permitted(request):
            return original_sync(_transport, request)
        attempts.append(request.url.host)
        raise AssertionError('Real HTTP calls are disabled during synthetic integration tests')

    async def blocked_async(_transport, request):
        if permitted(request):
            return await original_async(_transport, request)
        attempts.append(request.url.host)
        raise AssertionError('Real HTTP calls are disabled during synthetic integration tests')

    with patch.object(httpx.HTTPTransport, 'handle_request', blocked), \
         patch.object(httpx.AsyncHTTPTransport, 'handle_async_request', blocked_async):
        yield attempts


def main(argv=None):
    with no_real_http() as attempts:
        code = int(pytest.main(sys.argv[1:] if argv is None else argv))
    if attempts:
        print(f'FAILED: {len(attempts)} attempted real HTTP calls were blocked', file=sys.stderr)
        return 1
    return code


if __name__ == '__main__':
    raise SystemExit(main())

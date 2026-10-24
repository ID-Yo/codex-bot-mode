"""Validate and normalize the address stored in a Desktop relay descriptor (stdlib only)."""
from urllib.parse import urlparse

LOOPBACK_HOSTS = ("127.0.0.1", "localhost")


def relay_base_url(url):
    """Return http://127.0.0.1:PORT for an accepted relay URL; raise ValueError for anything else.

    The relay listens on IPv4 loopback only. Descriptors written before the fix say localhost, which resolves to ::1
    first on some machines and costs a refused connection per call, so the host is normalized to 127.0.0.1.
    """
    if not isinstance(url, str):
        raise ValueError('Invalid Desktop relay registration')
    try:
        parts = urlparse(url)
        port = parts.port
        host = parts.hostname
    except ValueError:
        raise ValueError('Invalid Desktop relay registration') from None
    if (parts.scheme != 'http' or host not in LOOPBACK_HOSTS or not port or port > 65535
            or parts.path or parts.params or parts.query or parts.fragment or parts.username is not None
            or parts.password is not None or parts.netloc.lower() != f'{host}:{port}'):
        raise ValueError('Invalid Desktop relay registration')
    return f'http://127.0.0.1:{port}'

import hashlib
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}
_TRACKING = ("utm_", "fbclid", "gclid", "yclid")


def canonicalize(url: str, base: str | None = None) -> str | None:
    """Приводит URL к каноничному виду, чтобы дубликаты давали один отпечаток."""
    if base:
        url = urljoin(base, url)
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if parts.port and parts.port != _DEFAULT_PORTS[scheme]:
        host = f"{host}:{parts.port}"
    query = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not k.lower().startswith(_TRACKING)
    )
    path = parts.path or "/"
    return urlunsplit((scheme, host, path, urlencode(query), ""))


def fingerprint(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()


def host_of(url: str) -> str:
    return urlsplit(url).netloc.lower()

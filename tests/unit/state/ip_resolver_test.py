from __future__ import annotations

from ipaddress import ip_address

from app.state.services import IPResolver

# trusted loopback + a fake docker bridge range, as an operator would set.
TRUSTED = ["127.0.0.1", "::1", "172.16.0.0/12"]


def _resolver() -> IPResolver:
    return IPResolver(trusted_proxies=TRUSTED)


def _headers(**overrides: str) -> dict[str, str]:
    """Build a header mapping. Keys use underscores and are converted to
    the dashed HTTP form, since dashes are not valid python kwargs."""
    base = {"Host": "example.com", "User-Agent": "osu!"}
    base.update({key.replace("_", "-"): value for key, value in overrides.items()})
    return base


# --- untrusted peer: header spoofing must be rejected -------------------


def test_untrusted_peer_ignores_forwarded_headers() -> None:
    """A client connecting directly cannot forge its IP via headers."""
    headers = _headers(
        X_Forwarded_For="1.2.3.4",
        X_Real_IP="1.2.3.4",
        CF_Connecting_IP="1.2.3.4",
    )
    peer = "203.0.113.50"

    assert _resolver().get_ip(headers, peer_host=peer) == ip_address(peer)


def test_untrusted_peer_no_headers_uses_peer() -> None:
    resolved = _resolver().get_ip(_headers(), peer_host="198.51.100.7")
    assert resolved == ip_address("198.51.100.7")


def test_untrusted_peer_malformed_peer_is_none() -> None:
    # an invalid peer cannot be parsed, and nothing else is trusted.
    assert _resolver().get_ip(_headers(), peer_host="not-an-ip") is None


def test_untrusted_peer_ipv6() -> None:
    peer = "2001:db8::5"
    headers = _headers(X_Forwarded_For="1.2.3.4")
    assert _resolver().get_ip(headers, peer_host=peer) == ip_address(peer)


# --- trusted peer: real proxy headers ARE honored -----------------------


def test_trusted_peer_cf_connecting_ip() -> None:
    headers = _headers(CF_Connecting_IP="1.2.3.4")
    assert _resolver().get_ip(headers, peer_host="127.0.0.1") == ip_address("1.2.3.4")


def test_trusted_peer_xff_rightmost_untrusted() -> None:
    """Right-to-left walk: the first non-proxy entry wins."""
    headers = _headers(X_Forwarded_For="1.2.3.4, 172.16.0.5, 127.0.0.1")
    assert _resolver().get_ip(headers, peer_host="127.0.0.1") == ip_address("1.2.3.4")


def test_trusted_peer_xff_all_proxies_falls_back_to_peer() -> None:
    # every entry is trusted -> no client IP in the chain; use the peer.
    headers = _headers(X_Forwarded_For="172.16.0.5, 127.0.0.1")
    resolved = _resolver().get_ip(headers, peer_host="127.0.0.1")
    assert resolved == ip_address("127.0.0.1")


def test_trusted_peer_xff_spoofed_leftmost_is_ignored() -> None:
    """The classic bug: a client seeds the chain with a fake leftmost
    entry. The rightmost non-proxy value must win instead."""
    headers = _headers(X_Forwarded_For="9.9.9.9, 203.0.113.10, 172.16.0.5")
    resolved = _resolver().get_ip(headers, peer_host="127.0.0.1")
    assert resolved == ip_address("203.0.113.10")


def test_trusted_peer_x_real_ip() -> None:
    headers = _headers(X_Real_IP="203.0.113.42")
    resolved = _resolver().get_ip(headers, peer_host="127.0.0.1")
    assert resolved == ip_address("203.0.113.42")


def test_trusted_ipv6_peer_via_cidr_and_loopback() -> None:
    headers = _headers(X_Forwarded_For="203.0.113.8, 127.0.0.1")
    assert _resolver().get_ip(headers, peer_host="::1") == ip_address("203.0.113.8")


# --- precedence & malformed input ---------------------------------------


def test_cf_connecting_ip_takes_precedence_over_xff() -> None:
    headers = _headers(CF_Connecting_IP="1.2.3.4", X_Forwarded_For="5.6.7.8")
    assert _resolver().get_ip(headers, peer_host="127.0.0.1") == ip_address("1.2.3.4")


def test_malformed_xff_entries_are_skipped() -> None:
    headers = _headers(X_Forwarded_For="garbage, 203.0.113.99")
    resolved = _resolver().get_ip(headers, peer_host="127.0.0.1")
    assert resolved == ip_address("203.0.113.99")


def test_empty_xff_falls_through_to_x_real_ip() -> None:
    headers = _headers(X_Forwarded_For="", X_Real_IP="203.0.113.7")
    resolved = _resolver().get_ip(headers, peer_host="127.0.0.1")
    assert resolved == ip_address("203.0.113.7")


def test_no_peer_and_no_headers_is_none() -> None:
    # nothing to go on: callers must treat this as unidentifiable.
    assert _resolver().get_ip(_headers()) is None


def test_missing_headers_do_not_raise() -> None:
    """The previous implementation used `headers["X-Forwarded-For"]`,
    raising KeyError when absent behind a misconfigured proxy."""
    assert _resolver().get_ip({}, peer_host="127.0.0.1") == ip_address("127.0.0.1")

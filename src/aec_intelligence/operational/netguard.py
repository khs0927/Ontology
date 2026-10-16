"""Egress guard for model endpoints. Drawing text is private: the LLM and embedding clients only talk to
loopback/private/link-local hosts unless an explicit ``*_ALLOW_REMOTE=1`` opt-in is set, and they never
route local calls through an HTTP(S) proxy taken from the environment or the Windows registry."""

from __future__ import annotations

import ipaddress
import os
import urllib.request
from urllib.parse import urlparse

LOCAL_HOSTNAMES = {"localhost", "host.docker.internal", "ollama", "gateway.docker.internal", "embeddings"}
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# Explicit local ranges. Python's ``ip.is_private`` is deliberately NOT used: it also counts e.g.
# 198.18.0.0/15 (benchmarking; used by fake-IP DNS resolvers), 192.0.2.0/24 and other special ranges as
# "private", which would let a public hostname masquerade as local.
_LOCAL_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "::1/128", "fc00::/7", "fe80::/10",
))


def _local_hostnames() -> set[str]:
    """Built-in names plus an explicit operator allow-list (``AEC_LOCAL_HOSTNAMES=a,b``)."""
    extra = {h.strip().lower().rstrip(".") for h in os.getenv("AEC_LOCAL_HOSTNAMES", "").split(",") if h.strip()}
    return LOCAL_HOSTNAMES | extra


def _is_local_ip(addr: ipaddress._BaseAddress) -> bool:
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return any(addr in net for net in _LOCAL_NETWORKS if net.version == addr.version)


def is_local_endpoint(url: str) -> bool:
    """True only for literal local IPs or explicitly allow-listed hostnames.

    DNS is never consulted: a resolve-then-connect check is defeated by DNS rebinding and by resolvers
    that hand out private-looking fake IPs, so an arbitrary hostname is always treated as remote.
    """
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not host:
        return False
    if host in _local_hostnames() or host.endswith(".localhost"):
        return True
    try:
        addr = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return False
    return _is_local_ip(addr)


def remote_allowed(env_name: str) -> bool:
    return os.getenv(env_name, "").strip().lower() in {"1", "true", "yes"}


def opener_for(url: str):
    """Direct (proxy-less) opener for local endpoints; the default opener for opted-in remote ones."""
    return _DIRECT if is_local_endpoint(url) else urllib.request.build_opener()

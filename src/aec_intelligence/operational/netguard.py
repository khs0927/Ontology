"""Egress guard for model endpoints. Drawing text is private: the LLM and embedding clients only talk to
loopback/private/link-local hosts unless an explicit ``*_ALLOW_REMOTE=1`` opt-in is set, and they never
route local calls through an HTTP(S) proxy taken from the environment or the Windows registry."""

from __future__ import annotations

import ipaddress
import os
import socket
import urllib.request
from urllib.parse import urlparse

LOCAL_HOSTNAMES = {"localhost", "host.docker.internal", "ollama", "gateway.docker.internal", "embeddings"}
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def is_local_endpoint(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if host in LOCAL_HOSTNAMES:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except (OSError, UnicodeError):
            return False
        addrs = {ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos}
        # Every address the name resolves to must be local (no mixed public/private answers).
        return bool(addrs) and all(a.is_loopback or a.is_private or a.is_link_local for a in addrs)
    return addr.is_loopback or addr.is_private or addr.is_link_local


def remote_allowed(env_name: str) -> bool:
    return os.getenv(env_name, "").strip().lower() in {"1", "true", "yes"}


def opener_for(url: str):
    """Direct (proxy-less) opener for local endpoints; the default opener for opted-in remote ones."""
    return _DIRECT if is_local_endpoint(url) else urllib.request.build_opener()

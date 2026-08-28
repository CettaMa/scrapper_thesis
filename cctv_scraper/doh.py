import socket
import threading
import time

import requests
import urllib3.util.connection
from urllib3.util.connection import create_connection as _orig_create_connection


class DoHResolver:
    """Resolve hostnames via Cloudflare DNS-over-HTTPS when system DNS fails."""

    _cache: dict[str, tuple[str, float]] = {}
    _ttl = 300  # cache for 5 minutes
    _lock = threading.Lock()

    @classmethod
    def resolve(cls, hostname: str) -> str | None:
        with cls._lock:
            cached = cls._cache.get(hostname)
            if cached and time.time() - cached[1] < cls._ttl:
                return cached[0]

        try:
            socket.getaddrinfo(hostname, 443, socket.AF_INET)
            return None  # system DNS works, no override needed
        except socket.gaierror:
            pass

        try:
            resp = requests.get(
                "https://cloudflare-dns.com/dns-query",
                params={"name": hostname, "type": "A"},
                headers={"accept": "application/dns-json"},
                timeout=5,
            )
            for answer in resp.json().get("Answer", []):
                if answer.get("type") == 1:
                    ip = answer["data"]
                    with cls._lock:
                        cls._cache[hostname] = (ip, time.time())
                    return ip
        except Exception:
            pass
        return None


_doh_patched_hosts: dict[str, str] = {}


def _doh_create_connection(address, *args, **kwargs):
    host, port = address
    ip = _doh_patched_hosts.get(host)
    if not ip:
        ip = DoHResolver.resolve(host)
        if ip:
            _doh_patched_hosts[host] = ip
    if ip:
        return _orig_create_connection((ip, port), *args, **kwargs)
    return _orig_create_connection(address, *args, **kwargs)


urllib3.util.connection.create_connection = _doh_create_connection

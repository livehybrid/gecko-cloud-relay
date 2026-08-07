"""Working out where Gecko's own service actually is.

Harder than it sounds, because the whole point of this relay is that
``intouch2.geckoal.com`` has been pointed at *us*. Ask the system resolver and
it will helpfully hand back our own address, and the relay will spend the rest
of its life forwarding packets to itself.

So: resolve through a resolver of your choosing, and refuse an answer that
looks like it points back here.
"""

from __future__ import annotations

import ipaddress
import socket
import struct
from typing import Optional

from . import protocol as P


class UpstreamError(Exception):
    pass


def _skip_name(d: bytes, i: int) -> int:
    while True:
        ln = d[i]
        if ln == 0:
            return i + 1
        if ln & 0xC0 == 0xC0:          # compression pointer, always terminal
            return i + 2
        i += ln + 1


def resolve_a(name: str, resolver: str, timeout: float = 6.0) -> list[str]:
    """Minimal A-record lookup against one specific resolver.

    Deliberately not getaddrinfo: the entire situation here is that the system
    resolver has been told to lie about this name.
    """
    q = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"
    pkt = struct.pack(">HHHHHH", 0x4747, 0x0100, 1, 0, 0, 0) + q + struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(pkt, (resolver, 53))
        d = s.recvfrom(2048)[0]
    except OSError as exc:
        raise UpstreamError(f"resolver {resolver} did not answer ({exc})") from exc
    finally:
        s.close()

    answers = struct.unpack(">H", d[6:8])[0]
    i = _skip_name(d, 12) + 4
    out = []
    for _ in range(answers):
        i = _skip_name(d, i)
        rtype = struct.unpack(">H", d[i:i + 2])[0]
        rdlen = struct.unpack(">H", d[i + 8:i + 10])[0]
        if rtype == 1:
            out.append(".".join(str(b) for b in d[i + 10:i + 14]))
        i += 10 + rdlen
    return out


def _local_addresses() -> set:
    """Every address this host answers on, as best we can tell."""
    found = {"0.0.0.0"}
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    try:
        # No packet is sent; this just asks the routing table which source
        # address would be used to reach the outside world.
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))       # TEST-NET-1, deliberately unroutable
        found.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return found


def resolve_upstream(spec: str, resolver: Optional[str] = None,
                     our_port: int = P.PORT) -> tuple:
    """Turn ``host[:port]`` into ``(ip, port)``, refusing anything circular."""
    spec = spec.strip()
    port = P.PORT
    if ":" in spec:
        spec, _, tail = spec.rpartition(":")
        try:
            port = int(tail)
        except ValueError:
            raise UpstreamError(f"{tail!r} is not a port number") from None

    try:
        ipaddress.ip_address(spec)
        addr = spec
    except ValueError:
        if resolver:
            answers = resolve_a(spec, resolver)
            if not answers:
                raise UpstreamError(f"{resolver} has no A record for {spec}")
            addr = answers[0]
        else:
            try:
                addr = socket.getaddrinfo(spec, None, socket.AF_INET)[0][4][0]
            except OSError as exc:
                raise UpstreamError(f"cannot resolve {spec} ({exc})") from exc

    if addr in _local_addresses() or ipaddress.ip_address(addr).is_loopback:
        raise UpstreamError(
            f"{spec} resolves to {addr}, which is this host. Your DNS override "
            "is pointing it back at the relay. Pass the address directly, or "
            "use --upstream-dns 1.1.1.1 to look it up somewhere honest")
    if addr == "0.0.0.0":
        raise UpstreamError("refusing to forward to 0.0.0.0")
    return (addr, port)

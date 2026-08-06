#!/usr/bin/env python3
"""Point the Gecko hostnames at the relay, on every Pi-hole you run.

Pi-hole 6 keeps local A records in the `dns.hosts` config list, reachable over
its API. This reads what is there, replaces any entry for the same name, and
leaves everything else alone. Safe to re-run.

    # see what would change
    python3 tools/pihole_dns.py --relay 192.168.0.112 \
        --pihole http://192.168.0.222:81 --password "$PIHOLE_PASSWORD"

    # do it
    python3 tools/pihole_dns.py --relay 192.168.0.112 \
        --pihole http://192.168.0.222:81 --password "$PIHOLE_PASSWORD" --apply

    # confirm the answers, straight off each resolver
    python3 tools/pihole_dns.py --check 192.168.0.222 --check 192.168.0.112

Pass --pihole once per instance. They do not share config unless you run
something like nebula-sync, so a record added to one is not added to the rest.

Standard library only, matching the relay itself.
"""

from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request

NAMES = ["intouch.geckoal.com", "intouch2.geckoal.com"]


# -- pi-hole api -----------------------------------------------------------

def api(base, path, method="GET", body=None, sid=None, timeout=20):
    req = urllib.request.Request(base.rstrip("/") + path, method=method)
    req.add_header("Content-Type", "application/json")
    if sid:
        req.add_header("sid", sid)
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(req, data, timeout=timeout) as r:
        raw = r.read()
        return r.status, (json.loads(raw) if raw else None)


def configure(base, password, relay, names, apply_changes):
    print(f"== {base}")
    try:
        _, auth = api(base, "/api/auth", "POST", {"password": password})
        sid = auth["session"]["sid"]
    except Exception as exc:
        print(f"   ERROR authenticating: {exc}")
        return False

    try:
        _, cur = api(base, "/api/config/dns/hosts", sid=sid)
        hosts = cur["config"]["dns"]["hosts"]
        ok = True
        for name in names:
            entry = f"{relay} {name}"
            if entry in hosts:
                print(f"   ok        {entry}")
                continue
            stale = [x for x in hosts if x.split()[-1] == name]
            if not apply_changes:
                print(f"   would add {entry}" + (f"   (replacing {stale})" if stale else ""))
                continue
            for s in stale:
                code, _ = api(base, "/api/config/dns/hosts/"
                              + urllib.parse.quote(s, safe=""), "DELETE", sid=sid)
                print(f"   removed   {s}  [{code}]")
            code, _ = api(base, "/api/config/dns/hosts/"
                          + urllib.parse.quote(entry, safe=""), "PUT", sid=sid)
            print(f"   added     {entry}  [{code}]")
            ok = ok and code in (200, 201)
        return ok
    except Exception as exc:
        print(f"   ERROR: {exc}")
        return False
    finally:
        try:
            api(base, "/api/auth", "DELETE", sid=sid)
        except Exception:
            pass


# -- verification ----------------------------------------------------------

def _skip_name(d, i):
    while True:
        ln = d[i]
        if ln == 0:
            return i + 1
        if ln & 0xC0 == 0xC0:
            return i + 2
        i += ln + 1


def resolve(resolver, name, timeout=6.0):
    """Ask one resolver directly, so we see its answer and not a cached one."""
    q = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"
    pkt = struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0) + q + struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(pkt, (resolver, 53))
        d = s.recvfrom(2048)[0]
    finally:
        s.close()
    ancount = struct.unpack(">H", d[6:8])[0]
    i = _skip_name(d, 12) + 4
    out = []
    for _ in range(ancount):
        i = _skip_name(d, i)
        rtype = struct.unpack(">H", d[i:i + 2])[0]
        rdlen = struct.unpack(">H", d[i + 8:i + 10])[0]
        if rtype == 1:
            out.append(".".join(str(b) for b in d[i + 10:i + 10 + rdlen]))
        i += 10 + rdlen
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--relay", help="address the hostnames should resolve to")
    p.add_argument("--pihole", action="append", default=[],
                   help="Pi-hole base URL, e.g. http://192.168.0.222:81. Repeatable.")
    p.add_argument("--password", help="Pi-hole web password")
    p.add_argument("--name", action="append", default=[],
                   help=f"hostname to override (default: {', '.join(NAMES)})")
    p.add_argument("--apply", action="store_true", help="write changes; otherwise dry run")
    p.add_argument("--check", action="append", default=[],
                   help="resolver IP to query afterwards. Repeatable.")
    a = p.parse_args(argv)
    names = a.name or NAMES
    rc = 0

    if a.pihole:
        if not (a.relay and a.password):
            p.error("--pihole needs --relay and --password")
        for base in a.pihole:
            if not configure(base, a.password, a.relay, names, a.apply):
                rc = 1
        if not a.apply:
            print("\ndry run, nothing written. Re-run with --apply.")

    for resolver in a.check:
        print(f"-- {resolver}")
        for n in names:
            try:
                got = resolve(resolver, n)
                mark = "ok " if (not a.relay or a.relay in got) else "BAD"
                print(f"   {mark} {n:<22} -> {', '.join(got) or 'no A record'}")
                if a.relay and a.relay not in got:
                    rc = 1
            except Exception as exc:
                print(f"   ERR {n:<22} -> {exc}")
                rc = 1

    if not a.pihole and not a.check:
        p.print_help()
    return rc


if __name__ == "__main__":
    sys.exit(main())

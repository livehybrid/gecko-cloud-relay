"""End-to-end test: mock spa -> relay -> mock app client, plus the poller.

Run with:  python3 tests/test_relay.py
No hardware and no network access needed; everything binds to loopback.

Two scenarios, kept apart on purpose:

  identity     the relay on its own, working out who the module is from
               keepalives, from ISTHR and from SRCCN. No poller, because the
               poller names the module itself and would mask the very thing
               under test.
  end to end   the whole stack, including discovery, control and the dashboard.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gecko_relay import protocol as P            # noqa: E402
from gecko_relay.relay import GeckoCloudRelay    # noqa: E402
from gecko_relay.poller import StatusPoller      # noqa: E402
from gecko_relay.web import start_web            # noqa: E402
import mock_spa                                  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("   " + str(detail) if detail else ""))
    if not cond:
        FAILURES.append(name)


def wait_for(pred, timeout=10.0, interval=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(interval)
    return False


def start_spa(relay_port, **kw):
    spa = mock_spa.GeckoMockServer(host="127.0.0.1", port=0, **kw)
    spa.set_cloud(("127.0.0.1", relay_port), interval=1.0)
    threading.Thread(target=spa.serve_forever, daemon=True).start()
    return spa


# --------------------------------------------------------------------------
# identity: what the relay can work out with no help from the poller
# --------------------------------------------------------------------------

def scenario_identity():
    print("identity")
    relay = GeckoCloudRelay(host="127.0.0.1", port=0)
    relay_port = relay.sock.getsockname()[1]
    threading.Thread(target=relay.serve_forever, daemon=True).start()
    spa = start_spa(relay_port)
    dest = ("127.0.0.1", relay_port)

    ok = wait_for(lambda: relay.spa() is not None, 8)
    check("spa registers with the relay via keepalive", ok)
    if not ok:
        relay.close(); spa.running = False
        return
    check("relay reports the spa online", relay.spa_online())
    # A keepalive carries no identity, so the module is anonymous until it
    # either names itself in a PACKT or a client asks for it by name.
    check("unnamed module tracked provisionally", relay.spa().provisional,
          relay.spa().ident)

    before = relay.packets_out
    ok = wait_for(lambda: relay.packets_out > before, 4)
    check("relay answers <PINGS>1</PINGS>", ok)
    check("module counts the keepalive as answered", spa.cloud_acks > 0, spa.cloud_acks)

    cli = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cli.settimeout(4.0)
    cid = "IOSidentity-0001"

    cli.sendto(P.build_tag("ISTHR", mock_spa.SPA_ID), dest)
    reply = cli.recvfrom(4096)[0]
    check("ISTHR answered with BONJR", reply == b"<BONJR>0</BONJR>", reply)

    ok = wait_for(lambda: relay.spa() is not None and not relay.spa().provisional, 3)
    check("ISTHR resolved the module's identity", ok, relay.spa().ident)
    check("adopted id matches the spa", relay.spa().ident == mock_spa.SPA_ID,
          relay.spa().ident)

    cli.sendto(P.build_tag("BONJR", cid), dest)
    ok = wait_for(lambda: cid in relay.endpoints, 3)
    check("client registered from BONJR", ok)
    check("client bound to the spa", relay.bindings.get(cid) == mock_spa.SPA_ID,
          relay.bindings)

    cli.sendto(P.build_tag("HELLO", "1"), dest)
    m = P.parse(cli.recvfrom(4096)[0])
    check("relay answers LAN discovery with the spa id",
          m is not None and m.kind == "HELLO"
          and m.value.decode("latin1").startswith(mock_spa.SPA_ID),
          m.value if m else None)

    relay.close()
    spa.running = False

    # Rebuild with no ISTHR at all: the id must come from SRCCN on a PACKT.
    r2 = GeckoCloudRelay(host="127.0.0.1", port=0)
    r2_port = r2.sock.getsockname()[1]
    threading.Thread(target=r2.serve_forever, daemon=True).start()
    spa2 = start_spa(r2_port)
    wait_for(lambda: r2.spa() is not None, 6)
    c2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    c2.settimeout(3.0)
    c2.sendto(P.build_tag("BONJR", "IOSnoisthr"), ("127.0.0.1", r2_port))
    c2.sendto(P.build_packt(b"SFILE\x01", "IOSnoisthr"), ("127.0.0.1", r2_port))
    ok = wait_for(lambda: r2.spa() is not None and not r2.spa().provisional, 6)
    check("id learned from SRCCN without any ISTHR", ok,
          r2.spa().ident if r2.spa() else None)
    r2.close()
    spa2.running = False


# --------------------------------------------------------------------------
# end to end: discovery, status, control, dashboard
# --------------------------------------------------------------------------

def scenario_end_to_end():
    print()
    print("bring-up")
    relay = GeckoCloudRelay(host="127.0.0.1", port=0)
    relay_port = relay.sock.getsockname()[1]
    threading.Thread(target=relay.serve_forever, daemon=True).start()
    spa = start_spa(relay_port)

    poller = StatusPoller(relay, client_id="GRLYtest-0001")
    httpd = start_web(relay, poller, "127.0.0.1", 0)
    web_port = httpd.server_address[1]
    threading.Thread(target=poller.run, daemon=True).start()

    ok = wait_for(lambda: relay.spa() is not None, 8)
    check("spa reaches the relay", ok)
    if not ok:
        return

    print("discovery")
    ok = wait_for(lambda: poller.discovered, 10)
    check("poller discovered the spa over HELLO", ok)
    check("poller learned the real spa id", poller.spa_id == mock_spa.SPA_ID,
          poller.spa_id)
    check("relay adopted the id the poller found",
          relay.spa() is not None and relay.spa().ident == mock_spa.SPA_ID,
          relay.spa().ident if relay.spa() else None)
    check("module accepted the poller as a client",
          wait_for(lambda: poller.client_id in spa.clients, 6), list(spa.clients))

    print("status poller")
    ok = wait_for(lambda: poller.connected, 20)
    check("poller assembled a full status block", ok)
    d = poller.as_dict()
    check("water temperature decoded", d.get("known") and 20 < d["water_c"] < 45,
          f"{d.get('water_c')} C")
    check("set point decoded as 36.0 C", abs(d["setpoint_c"] - 36.0) < 0.05,
          f"{d['setpoint_c']} C")
    check("pack files learned", "inXM_C06.xml" in poller.pack_files,
          poller.pack_files)
    check("all pumps read as off", all(p["state"] == 0 for p in d["pumps"]))
    check("poller block matches the module byte for byte",
          bytes(poller.status.block) == bytes(spa.state.block))
    # The module answers a LAN-dialect client without naming it, and answers on
    # the well known port rather than to whoever asked. Both together mean the
    # poller cannot own a socket: its replies arrive on the relay's. Guard the
    # whole path, because getting it wrong looks like a silent empty dashboard.
    check("module answers the poller without naming it", poller.client_id in spa.lan_clients)
    check("relay delivered those unaddressed replies rather than dropping them",
          relay.packets_dropped == 0, relay.packets_dropped)
    check("poller is attached to the relay as the local client",
          relay.local_client is poller)
    check("status came back through the relay, not the poller's own socket",
          poller.via_relay > 0 and poller.via_socket == 0,
          f"relay={poller.via_relay} direct={poller.via_socket}")

    # --- a client talking through the relay ------------------------------
    print("client relaying")
    cli = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cli.settimeout(4.0)
    dest = ("127.0.0.1", relay_port)
    cid = "IOStest-relay-0001"

    cli.sendto(P.build_tag("ISTHR", mock_spa.SPA_ID), dest)
    reply = cli.recvfrom(4096)[0]
    check("ISTHR answered with BONJR", reply == b"<BONJR>0</BONJR>", reply)

    cli.sendto(P.build_tag("BONJR", cid), dest)
    ok = wait_for(lambda: cid in relay.endpoints, 3)
    check("client registered from BONJR", ok)

    cli.sendto(P.build_packt(b"SFILE\x01", cid), dest)
    got = cli.recvfrom(4096)[0]
    m = P.parse(got)
    check("SFILE relayed to spa and reply came back", m and m.verb == "FILES",
          m.verb if m else got[:40])
    check("reply is addressed to the client", m and m.dst == cid, m.dst if m else "")

    print("control through the relay")
    cli.sendto(P.build_packt(b"SPACK\x02\x06\x02\x39\x01", cid), dest)
    ok = wait_for(lambda: spa.state.block[P.OFF_PUMP_STATUS] == 0x80, 4)
    check("pump 1 keypress reached the spa", ok,
          f"0x{spa.state.block[P.OFF_PUMP_STATUS]:02x}")

    ok = wait_for(lambda: poller.as_dict()["pumps"][0]["state"] == 2, 8)
    check("poller saw pump 1 turn on via STATP", ok,
          poller.as_dict()["pumps"][0])
    check("pump timer picked up", poller.as_dict()["pump_timer_min"] == 15,
          poller.as_dict()["pump_timer_min"])

    print("set point through the relay")
    raw = P.c_to_raw(31.5)
    cli.sendto(P.build_packt(
        b"SPACK\x03\x06\x07\x46\x06\x06"
        + P.OFF_SETPOINT.to_bytes(2, "big") + raw.to_bytes(2, "big"), cid), dest)
    ok = wait_for(lambda: abs(poller.as_dict().get("setpoint_c", 0) - 31.5) < 0.05, 8)
    check("set point change reflected in decoded status", ok,
          poller.as_dict().get("setpoint_c"))

    print("turning pump 1 back off")
    cli.sendto(P.build_packt(b"SPACK\x04\x06\x02\x39\x01", cid), dest)
    ok = wait_for(lambda: poller.as_dict()["pumps"][0]["state"] == 0, 8)
    check("pump 1 off observed", ok)

    # --- dashboard --------------------------------------------------------
    print("dashboard")
    base = f"http://127.0.0.1:{web_port}"
    page = urllib.request.urlopen(base + "/", timeout=5).read().decode()
    check("page served", "in.touch2 relay" in page, len(page))
    check("no external assets", "http://" not in page.replace("http://www.w3.org", "")
          and "cdn" not in page.lower())

    state = json.loads(urllib.request.urlopen(base + "/api/state", timeout=5).read())
    check("api/state reports spa online", state["relay"]["spa_online"] is True)
    check("api/state carries decoded temperature",
          isinstance(state["spa"]["water_c"], (int, float)),
          state["spa"].get("water_c"))

    ev = json.loads(urllib.request.urlopen(base + "/api/events?since=0", timeout=5).read())
    check("api/events returns wire log", len(ev["events"]) > 10, len(ev["events"]))
    kinds = {e["verb"] for e in ev["events"]}
    check("log contains relayed control traffic", "SPACK" in kinds, sorted(kinds)[:12])
    summaries = " ".join(e["summary"] for e in ev["events"])
    check("keypress decoded in the log", "keypress Pump 1" in summaries)
    check("set point decoded in the log", "31.5 C" in summaries)

    blk = json.loads(urllib.request.urlopen(base + "/api/block", timeout=5).read())
    check("api/block returns a full 637-byte block",
          blk["complete"] and len(blk["hex"]) == P.BLOCK_LEN * 2, len(blk["hex"] or ""))

    print("routing safety")
    dropped_before = relay.packets_dropped
    cli.sendto(P.build_packt(b"SFILE\x05", cid, "SPAdoes-not-exist"), dest)
    ok = wait_for(lambda: relay.packets_dropped > dropped_before, 3)
    check("unroutable packet dropped, not misdelivered", ok)

    junk = b"not gecko traffic at all"
    cli.sendto(junk, dest)
    ok = wait_for(lambda: relay.packets_dropped > dropped_before + 1, 3)
    check("junk datagram rejected without crashing", ok)
    check("relay still alive after junk", relay.spa_online())

    print("recovery")
    # Pull the module away and put it back. The relay must not need a restart.
    spa.cloud = None
    poller_errors = poller.errors
    time.sleep(1.5)
    spa.set_cloud(("127.0.0.1", relay_port), interval=1.0)
    ok = wait_for(lambda: relay.spa_online() and poller.connected, 15)
    check("survives the module going quiet and coming back", ok)
    check("no poller exceptions along the way", poller.errors == poller_errors,
          poller.errors)

    relay.close()
    poller.stop()
    spa.running = False
    httpd.shutdown()


def main():
    scenario_identity()
    scenario_end_to_end()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

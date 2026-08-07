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
import urllib.error
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
    # The poller must look like the app, not like a client on the LAN. A module
    # puts anything that announces itself with HELLO into a LAN bucket and then
    # refuses it the status block entirely, with no error to go on. Guard the
    # whole path, because getting it wrong looks like a silent empty dashboard.
    check("poller is not registered as a LAN-dialect client",
          poller.client_id not in spa.lan_clients, spa.lan_clients)
    check("module answers the poller by name, as it does the app",
          poller.client_id in spa.clients, list(spa.clients))
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

    # --- driving the spa from the dashboard ------------------------------
    print("control from the dashboard")

    def command(payload):
        req = urllib.request.Request(base + "/api/command", method="POST",
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    code, out = command({"action": "press", "button": "pump2"})
    check("api/command accepts a keypress", code == 200 and out.get("ok"), out)
    ok = wait_for(lambda: spa.state.pump(2) == 2, 6)
    check("pump 2 turned on at the spa", ok, f"0x{spa.state.block[P.OFF_PUMP_STATUS]:02x}")
    ok = wait_for(lambda: poller.as_dict()["pumps"][1]["state"] == 2, 8)
    check("dashboard state caught up via STATP", ok)

    code, out = command({"action": "press", "button": "pump2"})
    ok = wait_for(lambda: poller.as_dict()["pumps"][1]["state"] == 0, 8)
    check("pressing again turned pump 2 off", ok)

    code, out = command({"action": "press", "button": "light"})
    ok = wait_for(lambda: poller.as_dict()["light"] > 0, 8)
    check("lights switched on from the dashboard", ok, poller.as_dict()["light"])
    command({"action": "press", "button": "light"})
    wait_for(lambda: poller.as_dict()["light"] == 0, 8)

    code, out = command({"action": "setpoint", "celsius": 37.5})
    check("api/command accepts an absolute set point",
          code == 200 and abs(out.get("setpoint_c", 0) - 37.5) < 0.01, out)
    ok = wait_for(lambda: abs(poller.as_dict()["setpoint_c"] - 37.5) < 0.05, 8)
    check("set point reached the spa", ok, poller.as_dict()["setpoint_c"])

    code, out = command({"action": "setpoint", "delta_c": -0.5})
    ok = wait_for(lambda: abs(poller.as_dict()["setpoint_c"] - 37.0) < 0.05, 8)
    check("relative set point nudge applied", ok, poller.as_dict()["setpoint_c"])

    # The dashboard uses the pack's own temp keys, not arithmetic of its own.
    # Two presses in a row must move it twice: computing a target locally makes
    # the second press resend the first value and appear to do nothing.
    start = poller.as_dict()["setpoint_c"]
    command({"action": "press", "button": "tempup"})
    ok = wait_for(lambda: abs(poller.as_dict()["setpoint_c"] - (start + 0.5)) < 0.05, 8)
    check("temp up key raises the set point", ok, poller.as_dict()["setpoint_c"])
    command({"action": "press", "button": "tempup"})
    ok = wait_for(lambda: abs(poller.as_dict()["setpoint_c"] - (start + 1.0)) < 0.05, 8)
    check("a second temp up press moves it again", ok, poller.as_dict()["setpoint_c"])
    command({"action": "press", "button": "tempdown"})
    ok = wait_for(lambda: abs(poller.as_dict()["setpoint_c"] - (start + 0.5)) < 0.05, 8)
    check("temp down key lowers it", ok, poller.as_dict()["setpoint_c"])

    code, out = command({"action": "setpoint", "celsius": 90})
    check("absurd set point clamped rather than written",
          code == 200 and out["setpoint_c"] == 40.0, out)
    wait_for(lambda: abs(poller.as_dict()["setpoint_c"] - 40.0) < 0.05, 8)

    code, out = command({"action": "press", "button": "self-destruct"})
    check("unknown button refused", code == 400 and not out.get("ok"), out)
    code, out = command({"action": "wat"})
    check("unknown action refused", code == 400 and not out.get("ok"), out)
    code, out = command({"action": "setpoint"})
    check("set point with no value refused", code == 400, out)
    check("commands refused outright when the poller is off",
          "disabled" in (StatusPoller(relay, enabled=False).ready() or ""))

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


def scenario_snooping():
    """Learn the status block from an exchange we are only carrying.

    The real module serves the phone app's request for the block and ignores
    the byte-identical one from us. Since the answer crosses the relay anyway,
    read it in passing. Modelled here by a client that asks and a poller that
    never gets a reply of its own.
    """
    print()
    print("snooping")
    relay = GeckoCloudRelay(host="127.0.0.1", port=0)
    relay_port = relay.sock.getsockname()[1]
    threading.Thread(target=relay.serve_forever, daemon=True).start()
    spa = start_spa(relay_port)

    # Attached but never run, so it cannot ask for anything itself. That is the
    # situation on the real module, which serves the app and ignores us.
    poller = StatusPoller(relay, client_id="GRLYsnoop-0001")
    relay.attach_local_client(poller)
    wait_for(lambda: relay.spa() is not None, 8)

    # A client the module is happy to serve, arriving the way the app does.
    cli = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cli.settimeout(4.0)
    dest = ("127.0.0.1", relay_port)
    cid = "IOSsnoop-0001"
    # Name the module first, or nothing addressed to it by id can be routed.
    cli.sendto(P.build_tag("ISTHR", mock_spa.SPA_ID), dest)
    cli.recvfrom(4096)
    cli.sendto(P.build_tag("BONJR", cid), dest)
    wait_for(lambda: cid in relay.endpoints, 4)

    before = poller.snooped
    check("poller starts knowing nothing", not poller.status.complete)
    cli.sendto(P.build_packt(b"STATU\x0d" + (0).to_bytes(2, "big")
                             + P.BLOCK_LEN.to_bytes(2, "big"), cid, mock_spa.SPA_ID), dest)

    ok = wait_for(lambda: poller.snooped > before, 8)
    check("poller read the block from another client's exchange", ok, poller.snooped)
    ok = wait_for(lambda: poller.status.complete, 8)
    check("snooped block assembles completely", ok)
    check("snooped block matches the module byte for byte",
          bytes(poller.status.block) == bytes(spa.state.block))
    check("poller counts itself connected once it has a full block",
          poller.connected)
    d = poller.as_dict()
    check("set point decoded from snooped data",
          d.get("setpoint_c") is not None and abs(d["setpoint_c"] - 36.0) < 0.05,
          d.get("setpoint_c"))

    # A notification for a client that has gone away is still true. Dropping it
    # unread is how the dashboard ends up holding a stale set point while the
    # spa has moved on, which is what made repeated presses look broken.
    relay.endpoints.pop(cid, None)
    before = poller.snooped
    spa.notify(spa.keypress(0x01))
    ok = wait_for(lambda: poller.snooped > before, 6)
    check("learns from a notification it cannot deliver", ok)
    ok = wait_for(lambda: poller.as_dict()["pumps"][0]["state"] == 2, 6)
    check("undeliverable notification still updated the state", ok,
          poller.as_dict()["pumps"][0])

    # And the accounting that answers "what are we not replying to".
    relay.note_traffic(cid, "ZZTOP", "sent", b"ZZTOPpayload")
    miss = relay.unanswered().get(cid) or {}
    check("an unknown unanswered verb is flagged with a sample",
          "ZZTOP" in miss and miss["ZZTOP"]["sample"], miss.get("ZZTOP"))
    relay.note_traffic(cid, "AVERS", "sent", b"AVERS\x01")
    relay.note_traffic(cid, "SVERS", "recv")
    check("a verb that did get its answer is not flagged",
          "AVERS" not in (relay.unanswered().get(cid) or {}))

    relay.close()
    poller.stop()
    spa.running = False


def scenario_partial_knowledge():
    """A module that pushes changes but will not dump the block.

    Real firmware does exactly this, and reporting an undelivered set point as
    0.0 reads as a fault rather than as ignorance.
    """
    print()
    print("partial knowledge")
    st = P.SpaStatus()
    d = st.as_dict()
    check("nothing known reads as not known", d == {"known": False}, d)

    # One change notification: two bytes at the water temperature, nothing else.
    raw = P.c_to_raw(34.0)
    st.apply_statp(bytes([1]) + P.OFF_WATER_TEMP.to_bytes(2, "big")
                   + raw.to_bytes(2, "big"))
    d = st.as_dict()
    check("the field we were told is reported", abs(d["water_c"] - 34.0) < 0.05,
          d["water_c"])
    check("a field we were never told is null, not zero", d["setpoint_c"] is None,
          d["setpoint_c"])
    check("pump state unknown rather than off", d["pumps"][0]["state"] is None,
          d["pumps"][0])
    check("light unknown rather than off", d["light"] is None, d["light"])
    check("block still reports itself incomplete", d["complete"] is False)
    check("has() tracks byte ranges", st.has(P.OFF_WATER_TEMP, 2)
          and not st.has(P.OFF_SETPOINT, 2))


def main():
    scenario_identity()
    scenario_snooping()
    scenario_partial_knowledge()
    scenario_end_to_end()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# gecko-cloud-relay

A local stand-in for the Gecko in.touch2 cloud service, so an in.touch2-EN
module keeps working when it cannot reach the internet.

Speaks UDP on port 10022, answers the module's keepalives, relays traffic
between the app and the spa, and serves a dashboard showing live spa state and
every datagram that crosses it.

Standard library only. No dependencies, no build step, no external assets, so it
runs on a box with no internet at all.

---

## Read this before deploying

**Your problem may not be what you think it is.** Gecko's own TechBook says the
module is supposed to degrade to local-only when the cloud is unreachable, not
die. Check the EN module's status LED before building anything:

| LED | Meaning | Implication |
|---|---|---|
| Red | No router detected, i.e. no DHCP lease or no link | A LAN fault, not an internet fault. This relay will not help. Fix DHCP/DNS first. |
| Green | Router fine, in.touch 2 server unreachable | Gecko say local app access should still work in this state. If it does not, this relay is the right tool. |
| Blue | Full connection to the in.touch 2 server | Normal. Once the relay is working you should see blue again. |
| Yellow, flashing | Pairing mode | Power-cycle the EN. |

If you run Pi-hole as your DHCP or DNS server, a Pi-hole outage produces a red
LED and looks exactly like "the internet broke my spa".

**What taking over the cloud costs you.** Once the module points at this relay
it is no longer talking to Gecko, so Gecko's own remote access is gone: the app
on a mobile network, resolving `geckoal.com` publicly, will not find your spa.

On the LAN nothing is lost. The app also works *through* the relay, which is the
interesting part, and that has been confirmed against a real iOS client pulling
a full status block and sending commands.

So remote access is not lost so much as moved: if you can get onto your own
network and use your own resolver, a VPN being the obvious way, the app reaches
the spa through the relay exactly as it would at home. Confirmed working from an
off-LAN client over WireGuard.

**One thing still to confirm on your own hardware.** The module is assumed to
resolve `intouch.geckoal.com` and `intouch2.geckoal.com` by DNS. If your
firmware has the address baked in instead, no DNS override will work and you
need a destination NAT rule; see below. The captures this was built from only
ever recorded the IP (`23.101.153.137`, Azure) because they were filtered to
port 10022, which threw away the DNS lookup. Take an unfiltered capture of the
module through a cold boot if you want to settle it.

---

## Running it

```bash
python3 -m gecko_relay                    # relay on :10022, dashboard on :8080
python3 -m gecko_relay -v                 # log every datagram to the console
python3 -m gecko_relay --no-poll          # relay only, do not read spa status
python3 -m gecko_relay --spa-id SPA80:1f:12:50:a5:d1
```

Or with Docker:

```bash
docker compose up -d
```

Use host networking. The relay identifies the module by source address before
the module ever names itself, so a NAT that rewrites the source will break
identification. On Docker Swarm, publish the ports in `host` mode for the same
reason: the ingress routing mesh source-NATs everything. See
`deploy/portainer-stack.yml`.

Dashboard: `http://<host>:8080/`

### Options

Every flag has a `GECKO_`-prefixed environment variable, which is easier to set
from a stack file than a command line.

| Flag | Environment | Default | Purpose |
|---|---|---|---|
| `--host`, `--port` | `GECKO_HOST`, `GECKO_PORT` | `0.0.0.0:10022` | UDP bind |
| `--web-host`, `--web-port` | `GECKO_WEB_HOST`, `GECKO_WEB_PORT` | `0.0.0.0:8080` | Dashboard bind |
| `--no-web` | `GECKO_NO_WEB` | off | Skip the dashboard |
| `--no-poll` | `GECKO_NO_POLL` | off | Do not join as a client. Use if you suspect the poller is interfering. |
| `--spa-id` | `GECKO_SPA_ID` | discovered | Pin the spa id so the module is identified on its first ping |
| `--client-id` | `GECKO_CLIENT_ID` | derived from the spa id | Client id used by the status poller. The default is stable across restarts on purpose: a module remembers its clients indefinitely. |
| `--log-size` | `GECKO_LOG_SIZE` | 500 | Wire log ring buffer |
| `-v` | `GECKO_VERBOSE` | off | Log every datagram, including keepalives |

---

## Pointing the module at it

### If it uses DNS (try this first)

Add local A records for both Gecko hostnames pointing at the relay:

```
intouch.geckoal.com    ->  <relay address>
intouch2.geckoal.com   ->  <relay address>
```

In Pi-hole 6 that is Settings → Local DNS Records. `tools/pihole_dns.py` does it
over the API, and will do it to several Pi-holes at once:

```bash
# dry run first, it prints what it would change
python3 tools/pihole_dns.py --relay 192.168.0.112 --password <password> \
    --pihole http://192.168.0.222:81 \
    --pihole http://192.168.0.112:81 \
    --pihole http://192.168.0.55:81

# then for real
python3 tools/pihole_dns.py ... --apply

# and confirm, asking each resolver directly
python3 tools/pihole_dns.py --relay 192.168.0.112 \
    --check 192.168.0.222 --check 192.168.0.112 --check 192.168.0.55
```

It replaces any existing record for the same name and leaves the rest alone, so
it is safe to re-run.

Add the records to **every** Pi-hole that serves DNS, not just the one you
happen to be looking at. Separate instances do not share config unless you run
something like nebula-sync. Then power-cycle the EN module, since it will have
cached the old address.

Note that this overrides the hostnames for everything on your network, not just
the spa. That is fine in practice, because nothing else has any reason to talk
to `geckoal.com`, but be aware the relay only answers UDP 10022. Anything that
tried to reach those names over HTTP will now fail rather than reach Gecko.

### If it uses a hardcoded IP

Redirect the traffic at the router. On anything running iptables:

```bash
# Replace 23.101.153.137 with whatever your capture shows, and
# 192.168.0.50 with the relay's address.
iptables -t nat -A PREROUTING -s 192.168.0.132 -d 23.101.153.137 \
         -p udp --dport 10022 -j DNAT --to-destination 192.168.0.50:10022
iptables -t nat -A POSTROUTING -d 192.168.0.50 -p udp --dport 10022 -j MASQUERADE
```

Note the MASQUERADE line defeats source-address identification, so pass
`--spa-id` when using this form.

---

## What it does

| Message | Direction | Handling |
|---|---|---|
| `<PINGS>1</PINGS>` | module → relay | Answered with `<PINGS>0</PINGS>`. This is what keeps the LED blue. |
| `<ISTHR>{spa}</ISTHR>` | client → relay | Answered with `<BONJR>0</BONJR>`, and binds that client to that spa. |
| `<BONJR>{client}</BONJR>` | client → relay | Registers the client. |
| `<HELLO>1</HELLO>` | client → relay | Answered with the known spa id. Normally a LAN broadcast, handled for convenience. |
| `<PACKT>` | either | Forwarded byte-for-byte to the endpoint named in `DESCN`, or to the bound spa when `DESCN` is absent. |

The wire protocol, including the status block offsets, is written up in
[`docs/intouch2-protocol.md`](docs/intouch2-protocol.md).

### Identifying the module

A keepalive carries no identity. The module is tracked provisionally as
`spa@{ip}:{port}` until its real id is learned, which happens by any of three
routes: the status poller asks it directly (`<HELLO>1</HELLO>`), it sends a
`PACKT` naming itself in `SRCCN`, or a client asks for it by name via `ISTHR`.
All three are covered by the tests. `--spa-id` skips the guessing entirely.

### The status poller

The relay only forwards bytes, so on its own it cannot tell you what the spa is
doing. The poller joins as an ordinary client to read status for the dashboard.
It does what the app does:

1. `<HELLO>1</HELLO>` to discover the module's id, unless one was pinned
2. `<HELLO>{client_id}</HELLO>` to announce itself, because the module ignores
   clients it has not been introduced to
3. pull the pack file list, the version and the 637-byte status block
4. `APING` every 2 seconds

Everything after step 1 names the spa in `DESCN`. If the module never answers
discovery, the poller waits 30 seconds, warns, and falls back to whatever id
the relay has learned, so a module that behaves unexpectedly degrades the
dashboard rather than hanging it.

That 2-second interval is not arbitrary. The in.touch module stops sending
change notifications to a client whose pings lapse, which is a documented
finding from the geckolib project. If the dashboard goes stale but the log keeps
moving, this is the first thing to check.

Disable it with `--no-poll` if you suspect it is interfering with Home Assistant
or the phone app. Multiple concurrent clients are supported by the protocol, so
it should not, but it is one more variable when debugging.

**Status against real hardware.** Confirmed working on an in.touch2-EN with pack
`inXM_C06`: version, pack files, the full 637-byte block and live change
notifications. It can take a few minutes to settle after the module is first
pointed at the relay, because the module carries on serving the session it
already had. `/api/state` reports `via_relay` and `via_socket` so you can see
which path replies are taking; on this hardware it is all relay. What the module
does and does not answer is written up in
[`docs/intouch2-protocol.md`](docs/intouch2-protocol.md).

---

## Dashboard

- **Status light** mirrors the module's own LED semantics. Blue means the relay
  is answering keepalives. Amber means the module was seen but has gone quiet.
  Grey means nothing has arrived, so check the redirect.
- **Water** shows temperature and set point, decoded from the status block.
- **Accessories** shows pumps, aux/O3 and lights, with their countdown timers.
- **Wire log** shows every datagram, decoded. Keypresses read as
  `SPACK keypress Pump 1`, writes as `SPACK write @15 = 0237 (31.5 C)`. Toggle
  hex to see the raw bytes. Traffic for a client that is not connected is
  counted but mostly kept out of the log, or it would be the only thing in it;
  the running totals are in `/api/state` under `unroutable`.

### Driving the spa

The accessory tiles are buttons, and the set point has a stepper. A press sends
the same key code the topside panel sends, so it is indistinguishable from
someone leaning over and pressing it. The spa answers with a change
notification, so the dashboard catches up on its own within about a second
rather than guessing at the result.

Controls are greyed out until the poller has a complete status block, because
until the spa has accepted us as a client a press would just be dropped.

Anyone who can reach the dashboard can turn your pumps on. That is the same
exposure as the phone app on the same LAN, but the dashboard has no
authentication, so put it somewhere you are happy with. `--no-poll` removes the
ability entirely: with no client there is nothing to send as, and commands are
refused with 503.

### API

| Endpoint | Returns |
|---|---|
| `GET /api/state` | Relay status, endpoints and decoded spa state |
| `GET /api/events?since=N` | Wire log entries after sequence N |
| `GET /api/block` | The raw 637-byte status block as hex |
| `POST /api/command` | Press a button or set the temperature |

`/api/block` is the one to grab when something looks wrong. It is the whole
truth about the spa in one string and can be decoded offline.

```bash
# buttons: pump1 pump2 pump3 aux light tempup tempdown
curl -X POST http://<host>:8080/api/command \
     -H 'Content-Type: application/json' -d '{"action":"press","button":"pump1"}'

# absolute, clamped to 15-40 C
curl -X POST http://<host>:8080/api/command \
     -H 'Content-Type: application/json' -d '{"action":"setpoint","celsius":37.5}'

# or relative
curl -X POST http://<host>:8080/api/command \
     -H 'Content-Type: application/json' -d '{"action":"setpoint","delta_c":-0.5}'
```

The set point is clamped to 15-40 °C. That write goes straight into the pack's
status block, and there is no reason to find out what the pack does with a
value outside its own range.

---

## Testing without hardware

`tests/mock_spa.py` emulates an in.touch2 module, including the cloud
keepalive, LAN discovery, status block reads, change notifications and the two
`SPACK` commands the app sends. The integration test wires a mock spa to a relay
to a mock client and exercises the whole path:

```bash
python3 tests/mock_spa.py --selftest    # 9 checks, the fixture itself
python3 tests/test_relay.py             # 63 checks, no network access needed
```

The mock reproduces the module's awkward habits, not the convenient ones: it
answers on the well known port rather than to whoever asked, and answers a
LAN-dialect client without naming it. A friendlier mock is what let an earlier
version of this pass its tests while being structurally unable to work against
hardware.

The mock is checked before the relay leans on it, so a broken fixture cannot
masquerade as a broken relay.

To poke at the dashboard by hand:

```bash
python3 -m gecko_relay --port 10022 &
python3 tests/mock_spa.py --port 10023 --cloud 127.0.0.1:10022
```

---

## Layout

```
gecko_relay/
  __main__.py    CLI, wires the three pieces together
  protocol.py    envelopes, verbs, status block decoding
  relay.py       the relay itself: registry, routing, wire log
  poller.py      optional client that reads spa status for the dashboard
  web.py         dashboard and JSON API
tests/
  mock_spa.py    emulated in.touch2 module
  test_relay.py  end-to-end integration test
tools/
  pihole_dns.py  point the Gecko hostnames at the relay, on every Pi-hole
deploy/
  portainer-stack.yml
docs/
  intouch2-protocol.md
```

---

## Temperature encoding

Raw values in the status block are `(°F − 32) × 10`, equivalently `°C × 18`.
A raw 648 is 36.0 °C or 96.8 °F. This is worth knowing because the obvious
guess, tenths of °F, gives 64.8 °F and is wrong.

## Limitations

- Only the LAN dialect is implemented. There is no attempt to support access
  from outside your network, and pointing the module here gives up Gecko's own
  remote access.
- No authentication, matching the real service as observed. Do not expose port
  10022 to the internet.
- One spa. The registry allows more, but nothing has been tested with two.
- Push notifications, firmware updates and account registration are not
  implemented, and it is unknown whether the module needs any of them.
- Whether the module needs a time source is unresolved, and is an open question
  in the geckolib project too.

## Credit

Protocol groundwork by the [geckolib](https://github.com/gazoodle/geckolib)
project, which decoded in.touch2 first and properly. If you want a client rather
than a relay, use geckolib, not this.

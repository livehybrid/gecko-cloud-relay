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
it is no longer talking to Gecko, so the phone app can no longer reach the spa
from outside your network. On the LAN nothing is lost: the app finds the spa by
broadcast and talks to it directly, and it works through this relay too. If
remote access matters to you, this is the wrong tool.

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
| `--client-id` | `GECKO_CLIENT_ID` | random | Client id used by the status poller |
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

In Pi-hole 6 that is Settings → Local DNS Records, or via the API:

```bash
SID=$(curl -sk -X POST 'http://<pihole>/api/auth' \
      -H 'Content-Type: application/json' \
      -d '{"password":"<password>"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["session"]["sid"])')

for NAME in intouch.geckoal.com intouch2.geckoal.com; do
  curl -sk -X PUT "http://<pihole>/api/config/dns/hosts/<relay-ip>%20$NAME" \
       -H "sid: $SID"
done
```

Add them to **every** Pi-hole that serves DNS, not just the one you happen to
be looking at. Then power-cycle the EN module, since it will have cached the
old address.

Verify from a machine using the same resolver:

```bash
dig +short intouch2.geckoal.com @<pihole>
```

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

---

## Dashboard

- **Status light** mirrors the module's own LED semantics. Blue means the relay
  is answering keepalives. Amber means the module was seen but has gone quiet.
  Grey means nothing has arrived, so check the redirect.
- **Water** shows temperature and set point, decoded from the status block.
- **Accessories** shows pumps, aux/O3 and lights, with their countdown timers.
- **Wire log** shows every datagram, decoded. Keypresses read as
  `SPACK keypress Pump 1`, writes as `SPACK write @15 = 0237 (31.5 C)`. Toggle
  hex to see the raw bytes.

### API

| Endpoint | Returns |
|---|---|
| `GET /api/state` | Relay status, endpoints and decoded spa state |
| `GET /api/events?since=N` | Wire log entries after sequence N |
| `GET /api/block` | The raw 637-byte status block as hex |

`/api/block` is the one to grab when something looks wrong. It is the whole
truth about the spa in one string and can be decoded offline.

---

## Testing without hardware

`tests/mock_spa.py` emulates an in.touch2 module, including the cloud
keepalive, LAN discovery, status block reads, change notifications and the two
`SPACK` commands the app sends. The integration test wires a mock spa to a relay
to a mock client and exercises the whole path:

```bash
python3 tests/mock_spa.py --selftest    # 9 checks, the fixture itself
python3 tests/test_relay.py             # 46 checks, no network access needed
```

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

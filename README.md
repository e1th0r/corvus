# Corvus

A lightweight Sendmail/Postfix milter that validates recipient addresses
*before* the message is accepted, rejecting undeliverable mail at the SMTP
conversation instead of generating a bounce later.

## Why "Corvus"

Corvus is the Latin name for the crow, and also a small constellation in
the southern sky. In several mythologies crows and ravens are watchers or
messengers, sent ahead to check what's out there before anything else
moves. That's exactly the role this milter plays in the mail flow: it
checks each recipient at `RCPT TO`, before the message body is ever
accepted, and only lets the SMTP transaction continue if the address
looks deliverable.

## What it does

Most mail servers accept a message first and generate a bounce afterwards
if the recipient doesn't exist. That's slow, wastes resources, and hurts
sender reputation. Corvus intercepts each `RCPT TO` and:

1. Checks an internal bypass list (trusted/internal domains skip validation).
2. Checks a Redis/Valkey cache for a recent verdict on that address.
3. On a cache miss, calls a [check-if-email-exists](https://github.com/reacherhq/check-if-email-exists)
   compatible validation API.
4. Accepts, rejects (`550 5.1.1`), or temp-fails (`451 4.1.1`) the
   recipient based on the result, and caches the verdict for next time.

A rejection only affects the current recipient, not the whole message —
other valid recipients on the same envelope are unaffected.

Corvus is running in production on a live outbound mail relay, sitting in
front of a real SMTP flow and deciding, in real time, which recipients get
accepted.

Before it was deployed, invalid recipients were accepted at the SMTP level
and only rejected afterwards, as a bounce. That generated unnecessary
bounce traffic and put the sending domain's reputation at risk with
receiving mail providers. Since Corvus took over recipient validation,
invalid addresses are rejected at `RCPT TO`, before the message is ever
accepted, so no bounce is generated on the sending side at all.

In its current deployment, Corvus sits in front of a relay processing on
the order of **30,000+ unique messages per day**.

## Expected API response format

Corvus only reads the `is_reachable` field from the validator response, so
any API returning JSON shaped like this is compatible:

```json
[
  {
    "input": "someone@example.com",
    "is_reachable": "safe",
    "syntax": { "is_valid_syntax": true },
    "mx": { "accepts_mail": true },
    "smtp": { "is_deliverable": true }
  }
]
```

`is_reachable` is expected to be one of `"safe"`, `"risky"`, or
`"invalid"` (see the [check-if-email-exists
docs](https://github.com/reacherhq/check-if-email-exists) for the full
field reference). Anything else is treated as `"unknown"` and accepted
cautiously.

## Hardware requirements

Corvus itself is lightweight: it's a single Python process doing I/O
(cache lookups and HTTP calls), not local computation. In its production
deployment it has been observed running at **~40MB RSS memory** and
**negligible CPU usage** at idle, stable over weeks of uptime with no
memory growth.

A minimum of **1 vCPU and 512MB RAM** is more than enough for the milter
process itself on any realistic mail volume. The actual bottleneck in
practice will be your Redis/Valkey instance and your
check-if-email-exists validator, not Corvus — size those according to
your expected mail volume.

## Requirements

- Python 3.9+
- A running [check-if-email-exists](https://github.com/reacherhq/check-if-email-exists)
  instance (or any HTTP API returning a compatible `is_reachable` field)
- Redis or Valkey, reachable from the milter
- `libmilter` development headers (needed to build `pymilter`), e.g. on
  Debian/Ubuntu: `apt install libmilter-dev`

## Installation

```bash
git clone https://github.com/<your-username>/corvus.git
cd corvus
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Corvus is configured entirely through environment variables. Nothing is
hardcoded; every value below has a sensible default for local testing.

| Variable | Default | Description |
|---|---|---|
| `MILTER_NAME` | `email-validator-milter` | Name reported to the MTA |
| `MILTER_SOCKET` | `inet:7357@0.0.0.0` | Milter socket (inet or unix) |
| `MILTER_TIMEOUT` | `600` | Milter protocol timeout, in seconds |
| `VALIDATOR_URL` | `http://127.0.0.1:8080` | check-if-email-exists endpoint |
| `VALIDATOR_TIMEOUT` | `30` | Timeout per validation call, in seconds |
| `VALKEY_HOST` | `127.0.0.1` | Redis/Valkey host |
| `VALKEY_PORT` | `6379` | Redis/Valkey port |
| `VALKEY_TTL` | `86400` | Cache TTL for valid results (seconds) |
| `VALKEY_TTL_NEG` | `3600` | Cache TTL for rejected/risky results (seconds) |
| `BYPASS_DOMAINS` | *(empty)* | Comma-separated list of domains that skip validation entirely |
| `MILTER_LOG_FILE` | `/var/log/email-validator-milter.log` | Log file path |
| `MILTER_SOCKET_DIR` | `/var/run/email-validator-milter` | Directory used if running on a unix socket |

Example:

```bash
export BYPASS_DOMAINS="internal.example.com,mail.example.com"
export VALIDATOR_URL="http://127.0.0.1:8080"
export VALKEY_HOST="127.0.0.1"
```

## Running

```bash
python3 validator_milter.py
```

## Wiring into Postfix

Add the milter to `main.cf`:

```
smtpd_milters = inet:127.0.0.1:7357
milter_default_action = tempfail
```

Reload Postfix after the change:

```bash
systemctl reload postfix
```

## Running as a service

A sample `systemd` unit is provided in `systemd/corvus.service`. Copy it
to `/etc/systemd/system/`, adjust paths/environment as needed, then:

```bash
systemctl daemon-reload
systemctl enable --now corvus
```

## License

MIT — see [LICENSE](LICENSE).

## Need help integrating or customizing this?

If you'd like help adapting Corvus to your setup, integrating it with a
different validation API, or optimizing it for your mail volume, reach
out at hcolina@gmail.com.

# hostping

A small **local web app** that reads your operating system's hosts file, pings
every entry, and shows a traffic-light ("semaphore") colour code for
reachable vs. unreachable hosts. The page auto-refreshes, so the status updates
over time without you touching anything.

Primary use case: *"I want to see at a glance which hosts are reachable and
which aren't, via a colour code."*

The authoritative specification lives in [`SPEC.md`](SPEC.md); this README is the
practical "how to run it" companion.

## What it does

- Parses the hosts file (`/etc/hosts` by default), skipping blank lines and `#`
  comments and stripping inline comments.
- For each entry, checks reachability of **both** the raw IP and the primary
  hostname (the first hostname on the line; the rest are treated as aliases).
- Derives one semaphore status per entry and serves the results as JSON over
  HTTP, plus a static frontend that polls it live.
- Never edits the hosts file — it is read-only for hostping.

## Requirements

- **Python 3** (standard library only).
- **No `pip install`, no third-party packages, and no Node/build step.** The
  backend uses only stdlib (`http.server`, `socket`, `subprocess`, …) and the
  frontend is plain HTML/CSS/JS.
- The system **`ping` binary** is used for the primary ICMP check. When `ping`
  is missing or not permitted, hostping automatically falls back to **TCP
  connect probes** on common ports `80, 443, 22, 445, 3389`.

If you can run `python3`, you can run hostping.

## Quick start

```bash
cd /home/daniel/hostping
python3 -m hostping
```

This starts the server on the default `http://127.0.0.1:8765/`, prints the URL,
and opens it in your default browser. It runs until you press `Ctrl-C`.

Do **not** open `web/index.html` directly from the filesystem — the page needs
the backend API, so launch it through `python3 -m hostping`.

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--port PORT` | `8765` | Port to listen on. |
| `--bind ADDRESS` | `127.0.0.1` | Address to bind to. |
| `--hosts-file PATH` | `/etc/hosts` (Windows: `%SystemRoot%\System32\drivers\etc\hosts`) | Hosts file to read. |
| `--timeout SECONDS` | `1.0` | Per-check timeout, in seconds (float). |
| `--no-browser` | off | Do not open a web browser. |
| `--version` | — | Print the version and exit. |

Examples:

```bash
# Start without opening a browser (e.g. on a headless box)
python3 -m hostping --no-browser

# Use a different port and bind address
python3 -m hostping --port 9000 --bind 127.0.0.1

# Check a different hosts file with a longer timeout
python3 -m hostping --hosts-file ./fixtures/hosts --timeout 2.5
```

## What you'll see

The page opens with a **Summary** bar (counts of up / degraded / down / unknown
/ total), a "last updated" time, a live countdown to the next refresh, and a
table with one row per hosts-file entry: status badge, IP, hostname, method,
latency, and any warnings. Every colour is paired with a **text label and a
glyph**, so status is never colour-only.

Auto-refresh defaults to **15 seconds**; you can pick 5 / 30 / 60 seconds, pause
and resume, or hit "Refresh now". The chosen interval is remembered in the
browser.

Status meanings:

| Status | Colour | Meaning |
|--------|--------|---------|
| `up` | green | IP reachable **and** hostname reachable. |
| `degraded` | yellow | IP reachable, but hostname unresolved/unreachable (a warning is shown). |
| `down` | red | IP not reachable. |
| `unknown` | grey | Check could not be performed (unexpected error). |

The "method" column shows which probe was used: `icmp` (system `ping`) or `tcp`
(the fallback probe).

### Read-only and local by default

- hostping **never modifies the hosts file**; it only reads it.
- It binds to `127.0.0.1` (loopback) by default, so it is not exposed to your
  network. Binding elsewhere is possible via `--bind`, but there is no
  authentication (see [Non-goals](#non-goals)).

## API reference

The backend is a `ThreadingHTTPServer` serving a small fixed route table.

### `GET /api/hosts`

Runs a full scan and returns the results as JSON. Example:

```json
{
  "generated_at": "2026-10-13T00:00:00Z",
  "hosts_file": "/etc/hosts",
  "duration_ms": 812,
  "timeout_ms": 1000,
  "summary": { "total": 9, "up": 8, "degraded": 0, "down": 1, "unknown": 0 },
  "entries": [
    {
      "id": "3",
      "line": 3,
      "raw": "127.0.0.1\tlocalhost",
      "ip": "127.0.0.1",
      "hostname": "localhost",
      "aliases": [],
      "status": "up",
      "ip_result":       { "status": "up", "method": "icmp", "latency_ms": 0.065, "error": null },
      "hostname_result": { "status": "up", "method": "icmp", "latency_ms": 0.05,  "error": null },
      "warnings": []
    }
  ]
}
```

- `id` is the hosts-file line number as a string (stable across refreshes);
  `line` is the same number as an integer.
- `entries` preserves hosts-file order.
- `generated_at` is UTC ISO-8601 with a trailing `Z`.
- `summary.total` equals `len(entries)`, and the per-status counts sum to it.
- Each `*_result` is
  `{"status": "up"|"down"|"unresolved"|"unknown", "method": "icmp"|"tcp"|null, "latency_ms": <float|null>, "error": <string|null>}`.
  `unresolved` only appears on hostname checks that fail name resolution.
  `unknown` means the check could not be completed (an unexpected
  OS-level error); `method` reflects the method that was attempted
  (`icmp`/`tcp`) and is `null` only if none could be attempted.
- On an error reading the hosts file, the endpoint responds **500** with
  `{"error": "<message>", "hosts_file": "<path>"}`.

### `GET /api/health`

Liveness probe. Returns `200` with:

```json
{ "status": "ok" }
```

### Static routes

| Route | Serves |
|-------|--------|
| `/` (and `/index.html`) | `web/index.html` |
| `/app.js` | `web/app.js` |
| `/styles.css` | `web/styles.css` |

Unknown `/api/*` paths return `404` JSON (`{"error": "not found"}`); any other
unknown path returns `404` plain text.

## Testing

Run the standard-library test suite from the project root:

```bash
cd /home/daniel/hostping && python3 -m unittest discover -s tests -t . -v
```

All tests are offline-friendly: network-dependent scenarios (DNS failures,
timeouts, unreachable hosts) are exercised with an injected fake checker rather
than the real network.

## Project layout

```
hostping/
  SPEC.md
  README.md
  hostping/
    __init__.py
    hosts_file.py    # parsing
    reachability.py  # ping / TCP checks
    checks.py        # per-entry orchestration -> status + warnings
    server.py        # ThreadingHTTPServer + API + static
    __main__.py      # CLI entrypoint (python3 -m hostping)
  web/
    index.html
    app.js
    styles.css
  tests/
    test_hosts_file.py
    test_reachability.py
    test_checks.py
    test_server.py
```

## Troubleshooting

**A host shows `down` even though I can "reach" it.**
ICMP may be blocked by a firewall, or `ping` may be unavailable / not permitted
for your user. hostping then falls back to TCP probes on ports `80, 443, 22,
445, 3389`. If the host blocks ICMP *and* none of those ports are open, it will
read `down`. To confirm, check the "method" column: `tcp` means the fallback was
used.

**The page shows a red error banner.**
The banner means the frontend could not reach the backend — usually the server
isn't running, or the port is already taken so it failed to start. Start it (or
restart it on a free port):

```bash
python3 -m hostping --port 9000
```

When the port is occupied, the CLI exits with a message like
`hostping: could not start server on 127.0.0.1:8765: ...`.

**IPv6 multicast / local-scope entries always read `down` or `degraded`.**
That is expected. Typical hosts files contain entries like `ff02::1` or
`fe80::…` (multicast or link-local scope). These are not unicast-reachable
addresses, so they are not "down" because anything is broken.

**Nothing is displayed at all when I open `index.html` directly.**
Open the app through the server (`python3 -m hostping`) and use the printed
`http://127.0.0.1:8765/` URL. The static files refer to `/app.js` and
`/styles.css`, and the page fetches `/api/hosts` — none of which work from a
`file://` URL.

## Non-goals

Per [`SPEC.md` §3](SPEC.md), the following are explicitly out of scope:

- No authentication and no remote exposure (loopback bind by default).
- No persistence/history database, alerting, or notifications.
- No editing of the hosts file.
- No third-party Python packages and no Node build step.
- No port scanning beyond the small fallback port list.

## License

No license has been chosen for this project yet. It is intended as a local,
personal-use tool; add a `LICENSE` file before distributing or publishing it.

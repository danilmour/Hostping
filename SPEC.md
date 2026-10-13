# hostping — Specification & API Contract

Status: authoritative contract for the initial build. `backend` and `frontend` MUST
build to this document exactly. If something here is wrong or ambiguous, raise it
rather than silently diverging.

## 1. Overview / goal

`hostping` is a **local web app**, implemented in **Python using only the standard
library** (no third-party dependencies — the target environment has no `pip`/FastAPI/Flask).
It reads the operating system hosts file (default `/etc/hosts`), checks whether each
entry is reachable, and renders a traffic-light ("semaphore") colour code in the
browser. The page auto-refreshes so status updates over time.

Primary user need: "I want to see at a glance which hosts are reachable and which
aren't, via a colour code."

## 2. Functional requirements

- **FR1** Parse the hosts file: each non-empty, non-comment line is one entry with an
  IP plus one or more hostnames. Strip inline `#` comments. Preserve original line numbers.
- **FR2** Treat the first hostname on a line as the *primary* hostname; the rest are *aliases*.
- **FR3** For each entry, check reachability of **both** the raw IP and the primary hostname.
- **FR4** Derive a per-entry semaphore status from those two checks (see §4).
- **FR5** When the IP is reachable but the hostname does not work (unresolved or unreachable),
  emit a short human-readable **warning** for that row.
- **FR6** Expose the results as JSON via a local HTTP API; serve a static frontend.
- **FR7** The frontend polls the API continuously (default every 15 s) and updates live.
- **FR8** Configurable via CLI flags: port, bind address, hosts-file path, ping timeout,
  and whether to open a browser.

## 3. Non-goals (out of scope)

- No authentication, no remote exposure (bind to loopback by default).
- No persistence/history database, no alerting, no notifications.
- No editing of the hosts file.
- No third-party Python packages. No Node build step; frontend is plain HTML/CSS/JS.
- No port scanning beyond the small fallback port list in §5.

## 4. Semaphore status model

Per entry, `status` is one of:

| status     | colour | meaning                                                        |
|------------|--------|----------------------------------------------------------------|
| `up`       | green  | IP reachable **and** hostname reachable                        |
| `degraded` | yellow | IP reachable, but hostname unresolved/unreachable (warning)    |
| `down`     | red    | IP not reachable                                               |
| `unknown`  | grey   | check could not be performed (unexpected error)                |

Warnings are attached to `degraded` rows, e.g.
`"Hostname 'foo.local' did not resolve (DNS/NSS lookup failed)."`.

## 5. Reachability check

- Primary method: ICMP via the system `ping` binary — `ping -c 1 -W <timeout_s> <target>`,
  parsed for the round-trip time (`time=… ms`). Method label: `"icmp"`.
- Fallback method (if ICMP is unavailable, not permitted, or the binary is missing):
  TCP connect probe against `[80, 443, 22, 445, 3389]` with a short timeout.
  Method label: `"tcp"`. If any port connects → reachable.
- A check result is `{"status": "up"|"down"|"unresolved"|"unknown", "method": "icmp"|"tcp"|null, "latency_ms": <float|null>, "error": <string|null>}`.
  - `unresolved` only applies to hostname checks that fail name resolution.
  - `unknown` is returned when a check could not be completed (an unexpected OS-level
    error); `method` reflects the method that was attempted (`icmp`/`tcp`) and is `null`
    only if none could be attempted. This feeds the `unknown` semaphore state in §4.
- The whole `check()` call is bounded by the configured timeout: the ICMP wait is derived from
  the remaining budget (fractional `-W`), the DNS resolution step is bounded by the same budget,
  and the TCP fallback shares the deadline.
- Checks run concurrently (ThreadPoolExecutor), bounded workers (default 8).
- A single unreachable/slow host must never hang the whole scan beyond the timeout.

## 6. HTTP API contract

Server: `http.server.ThreadingHTTPServer`. Default bind `127.0.0.1`, default port `8765`.

### `GET /api/hosts`

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

- `id` is the line number as a string (stable across refreshes).
- `entries` preserves hosts-file order.
- `generated_at` is UTC ISO-8601 with a trailing `Z`.
- On an error reading the hosts file, respond **500** with
  `{"error": "<message>", "hosts_file": "<path>"}`.

### `GET /api/health`

Returns `{"status": "ok"}` with 200.

### Static assets

- `GET /` → `web/index.html`
- `GET /app.js` → `web/app.js`
- `GET /styles.css` → `web/styles.css`
- Unknown `/api/*` → 404 JSON `{"error": "not found"}`.
- Unknown non-API path → 404 plain text.

## 7. Frontend requirements

- Traffic-light badge per row: colour **plus a text label** (never colour-only) and a
  short glyph, for accessibility.
- Columns: semaphore, IP, hostname, method, latency, and warnings (when present).
- Warning rows visibly show the warning message.
- Summary bar with totals (up / degraded / down / unknown / total).
- Shows "last updated" time and a live countdown to the next refresh.
- Auto-refresh: default 15 s; a control to pause/resume and to pick the interval.
- Graceful error state if the API call fails (show a banner, keep last data).
- No external CDNs/fonts — must work fully offline.

## 8. Project layout

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

## 9. CLI

```
python3 -m hostping [--port 8765] [--bind 127.0.0.1] [--hosts-file /etc/hosts]
                    [--timeout 1.0] [--no-browser]
```

- Prints the URL on start. Opens the browser unless `--no-browser`.
- Runs until interrupted (Ctrl-C).

## 10. Acceptance criteria (testable)

- **AC1** `python3 -m unittest discover tests` passes.
- **AC2** Given a hosts file with comment and blank lines, the parser returns only real
  entries, preserving order and line numbers.
- **AC3** Given `127.0.0.1 localhost`, `GET /api/hosts` returns one entry with
  `status == "up"` and `ip_result.status == "up"`.
- **AC4** Given an entry whose IP is a black-hole address (e.g. `192.0.2.1`, TEST-NET-1),
  its `status` is `"down"` and the check completes within the configured timeout.
- **AC5** Given an IP that is reachable but whose primary hostname cannot resolve, the
  entry is `"degraded"` and `warnings` is non-empty (this is exercised with an injected
  fake checker, since real DNS depends on the network).
- **AC6** `summary.total` equals `len(entries)` and the per-status counts sum to the total.
- **AC7** The server serves `/` (HTML), `/app.js`, `/styles.css`, and `/api/hosts` with
  the correct content types, over HTTP, in a live test.
- **AC8** A request for an unknown `/api/*` path returns 404 JSON.
- **AC9** The frontend shows a text label alongside every colour, and renders warnings.
- **AC10** The app starts on the configured port and the page loads with no console errors.

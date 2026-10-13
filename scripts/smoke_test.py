"""End-to-end black-box smoke test for hostping (stdlib only).

Launches the real server as a subprocess (``python -m hostping``) against a
temporary hosts file and validates the ``SPEC.md`` §6 HTTP contract (including
the unreadable-hosts-file 500 response), the static and error routes, the §9 CLI
flags and the port-in-use error path. Reachability *results* are deliberately
not asserted: whether ICMP/TCP works varies between machines, so only the shape
of the payload is checked.

The server process is always terminated in a ``finally`` block. Every failure is
collected and printed before the script exits non-zero.

Runs on Python 3.9+.

Usage::

    python3 scripts/smoke_test.py
"""

from __future__ import annotations

import collections
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ``scripts/`` is sys.path[0] when this file is run directly, not the repo root,
# so make the package importable before reading its version. Importing
# ``hostping`` has no side effects; this keeps the expected version in one place
# instead of duplicating it here.
sys.path.insert(0, REPO_ROOT)
from hostping import __version__ as EXPECTED_VERSION  # noqa: E402

HOSTS_FIXTURE = (
    "# hostping smoke-test fixture\n"
    "\n"
    "127.0.0.1 localhost\n"
    "192.0.2.1 test.invalid\n"
)

#: The exact ``(line, ip, hostname)`` triples ``HOSTS_FIXTURE`` must parse into
#: (FR1/AC2: entries preserved in order with their original line numbers).
EXPECTED_FIXTURE_ENTRIES = (
    (3, "127.0.0.1", "localhost"),
    (4, "192.0.2.1", "test.invalid"),
)

#: SPEC §6 payload contract.
TOP_LEVEL_KEYS = frozenset(
    {"generated_at", "hosts_file", "duration_ms", "timeout_ms", "summary", "entries"}
)
SUMMARY_KEYS = frozenset({"total", "up", "degraded", "down", "unknown"})
ENTRY_KEYS = frozenset(
    {
        "id",
        "line",
        "raw",
        "ip",
        "hostname",
        "aliases",
        "status",
        "ip_result",
        "hostname_result",
        "warnings",
    }
)
RESULT_KEYS = frozenset({"status", "method", "latency_ms", "error"})
ENTRY_STATUSES = frozenset({"up", "degraded", "down", "unknown"})
RESULT_STATUSES = frozenset({"up", "down", "unresolved", "unknown"})
METHODS = frozenset({"icmp", "tcp", None})

GENERATED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

#: The scan timeout passed on the command line (``--timeout 0.5``).
EXPECTED_TIMEOUT_MS = 500

#: How long to wait for ``/api/health`` before giving up.
HEALTH_TIMEOUT = 20.0

CLI_FLAGS = ("--port", "--bind", "--hosts-file", "--timeout", "--no-browser")


class Reporter:
    """Collects failures so every problem is reported in one run."""

    def __init__(self):
        self.failures = []

    def check(self, condition, message):
        if not condition:
            self.failures.append(message)
        return bool(condition)

    def fail(self, message):
        self.failures.append(message)


# ---------------------------------------------------------------------------
# process / socket helpers
# ---------------------------------------------------------------------------


def _free_port():
    """Ask the OS for a free loopback port, then release it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _start_server(port, hosts_file):
    command = [
        sys.executable,
        "-m",
        "hostping",
        "--no-browser",
        "--bind",
        "127.0.0.1",
        "--port",
        str(port),
        "--hosts-file",
        hosts_file,
        "--timeout",
        "0.5",
    ]
    return subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )


def _stop_and_capture(process):
    """Terminate ``process`` (if alive) and return its captured output."""
    if process.poll() is None:
        process.terminate()
    try:
        output, _ = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        output, _ = process.communicate(timeout=5)
    return output or ""


def _request(port, path, timeout=15.0):
    """GET ``path``; return ``(status, content_type, body_bytes)``."""
    url = "http://127.0.0.1:%d%s" % (port, path)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.headers.get("Content-Type", ""), exc.read()
        finally:
            exc.close()


def _wait_for_health(port, process):
    """Poll ``/api/health`` until ready; return ``(ready, reason)``."""
    deadline = time.monotonic() + HEALTH_TIMEOUT
    last_error = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False, "the server exited early with code %s" % process.returncode
        try:
            status, _content_type, body = _request(port, "/api/health", timeout=2.0)
            if status == 200 and json.loads(body.decode("utf-8")) == {"status": "ok"}:
                return True, None
        except Exception as exc:  # noqa: BLE001 - keep polling until the deadline
            last_error = exc
        time.sleep(0.2)
    return False, "timed out after %.0fs waiting for /api/health (last error: %s)" % (
        HEALTH_TIMEOUT,
        last_error,
    )


# ---------------------------------------------------------------------------
# API validation
# ---------------------------------------------------------------------------


def _validate_result(reporter, result, label):
    if not isinstance(result, dict):
        reporter.fail("%s is not a JSON object (got %r)" % (label, result))
        return
    reporter.check(
        set(result) == RESULT_KEYS,
        "%s keys were %s (want %s)" % (label, sorted(result), sorted(RESULT_KEYS)),
    )
    reporter.check(
        result.get("status") in RESULT_STATUSES,
        "%s.status was %r (want one of %s)"
        % (label, result.get("status"), sorted(RESULT_STATUSES)),
    )
    reporter.check(
        result.get("method") in METHODS,
        "%s.method was %r (want icmp, tcp or null)" % (label, result.get("method")),
    )
    latency = result.get("latency_ms")
    reporter.check(
        latency is None or isinstance(latency, (int, float)),
        "%s.latency_ms was %r (want a number or null)" % (label, latency),
    )
    error = result.get("error")
    reporter.check(
        error is None or isinstance(error, str),
        "%s.error was %r (want a string or null)" % (label, error),
    )


def _validate_api_hosts(reporter, port, hosts_file):
    try:
        status, content_type, body = _request(port, "/api/hosts")
    except Exception as exc:  # noqa: BLE001 - report, don't crash
        reporter.fail("GET /api/hosts failed: %s" % exc)
        return

    reporter.check(status == 200, "GET /api/hosts returned HTTP %s (want 200)" % status)
    reporter.check(
        "application/json" in content_type,
        "GET /api/hosts content type was %r (want application/json)" % content_type,
    )
    try:
        payload = json.loads(body.decode("utf-8"))
    except ValueError as exc:
        reporter.fail("GET /api/hosts returned invalid JSON: %s" % exc)
        return
    if not isinstance(payload, dict):
        reporter.fail("GET /api/hosts payload is not a JSON object")
        return

    reporter.check(
        set(payload) == TOP_LEVEL_KEYS,
        "payload top-level keys were %s (want %s)" % (sorted(payload), sorted(TOP_LEVEL_KEYS)),
    )

    generated_at = payload.get("generated_at")
    reporter.check(
        isinstance(generated_at, str) and bool(GENERATED_AT_RE.match(generated_at)),
        "generated_at was %r (want UTC ISO-8601 with a trailing Z)" % (generated_at,),
    )
    reporter.check(
        payload.get("hosts_file") == hosts_file,
        "hosts_file was %r (want %r)" % (payload.get("hosts_file"), hosts_file),
    )
    reporter.check(
        payload.get("timeout_ms") == EXPECTED_TIMEOUT_MS,
        "timeout_ms was %r (want %d)" % (payload.get("timeout_ms"), EXPECTED_TIMEOUT_MS),
    )
    duration_ms = payload.get("duration_ms")
    reporter.check(
        isinstance(duration_ms, (int, float))
        and not isinstance(duration_ms, bool)
        and duration_ms >= 0,
        "duration_ms was %r (want a non-negative number)" % (duration_ms,),
    )

    entries = payload.get("entries")
    if not isinstance(entries, list):
        reporter.fail("entries was %r (want a JSON array)" % (entries,))
        entries = []

    # FR1/AC2: the fixture must parse through HTTP into exactly these entries.
    observed = [
        (entry.get("line"), entry.get("ip"), entry.get("hostname"))
        for entry in entries
        if isinstance(entry, dict)
    ]
    reporter.check(
        observed == list(EXPECTED_FIXTURE_ENTRIES),
        "entries did not match the fixture (FR1/AC2): parsed %r, want %r"
        % (observed, list(EXPECTED_FIXTURE_ENTRIES)),
    )

    summary = payload.get("summary")
    if not isinstance(summary, dict):
        reporter.fail("summary was %r (want a JSON object)" % (summary,))
    else:
        reporter.check(
            set(summary) == SUMMARY_KEYS,
            "summary keys were %s (want %s)" % (sorted(summary), sorted(SUMMARY_KEYS)),
        )
        reporter.check(
            summary.get("total") == len(entries),
            "summary.total was %r but len(entries) is %d" % (summary.get("total"), len(entries)),
        )
        counts = [summary.get(key) for key in ("up", "degraded", "down", "unknown")]
        if all(isinstance(count, int) and not isinstance(count, bool) for count in counts):
            reporter.check(
                sum(counts) == summary.get("total"),
                "per-status counts %r do not sum to total %r" % (counts, summary.get("total")),
            )
        else:
            reporter.fail(
                "summary per-status counts were %r (want four integers)" % (counts,)
            )

        # AC6: each per-status count must equal the number of entries that
        # actually carry that status (no expected counts asserted - reachability
        # differs between runners).
        derived = collections.Counter(
            entry.get("status") for entry in entries if isinstance(entry, dict)
        )
        for key in ("up", "degraded", "down", "unknown"):
            reporter.check(
                summary.get(key) == derived.get(key, 0),
                "summary[%r] was %r but %d entries have status %r (AC6)"
                % (key, summary.get(key), derived.get(key, 0), key),
            )

    previous_line = None
    for index, entry in enumerate(entries):
        label = "entries[%d]" % index
        if not isinstance(entry, dict):
            reporter.fail("%s is not a JSON object (%r)" % (label, entry))
            continue
        reporter.check(
            set(entry) == ENTRY_KEYS,
            "%s keys were %s (want %s)" % (label, sorted(entry), sorted(ENTRY_KEYS)),
        )
        line = entry.get("line")
        reporter.check(isinstance(line, int), "%s.line was %r (want an integer)" % (label, line))
        reporter.check(
            entry.get("id") == str(line),
            "%s.id was %r but line is %r (id must be str(line))" % (label, entry.get("id"), line),
        )
        if isinstance(line, int):
            if previous_line is not None:
                reporter.check(
                    line > previous_line,
                    "%s.line was %d, not ascending after %d" % (label, line, previous_line),
                )
            previous_line = line
        reporter.check(
            entry.get("status") in ENTRY_STATUSES,
            "%s.status was %r (want one of %s)"
            % (label, entry.get("status"), sorted(ENTRY_STATUSES)),
        )
        _validate_result(reporter, entry.get("ip_result"), "%s.ip_result" % label)
        # Every line in this fixture has a hostname, so a hostname_result is
        # expected (it is only None for an IP-only hosts entry).
        _validate_result(reporter, entry.get("hostname_result"), "%s.hostname_result" % label)


def _validate_static_routes(reporter, port):
    checks = (
        ("/", 200, "text/html"),
        ("/index.html", 200, "text/html"),
        ("/app.js", 200, "javascript"),
        ("/styles.css", 200, "css"),
    )
    for path, want_status, want_type in checks:
        try:
            status, content_type, _body = _request(port, path)
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            reporter.fail("GET %s failed: %s" % (path, exc))
            continue
        reporter.check(
            status == want_status,
            "GET %s returned HTTP %s (want %s)" % (path, status, want_status),
        )
        reporter.check(
            want_type in content_type,
            "GET %s content type was %r (want it to contain %r)" % (path, content_type, want_type),
        )

    # /api/health -> 200 JSON with exactly {"status": "ok"} (SPEC §6).
    try:
        status, content_type, body = _request(port, "/api/health")
    except Exception as exc:  # noqa: BLE001
        reporter.fail("GET /api/health failed: %s" % exc)
    else:
        reporter.check(status == 200, "GET /api/health returned HTTP %s (want 200)" % status)
        reporter.check(
            "application/json" in content_type,
            "GET /api/health content type was %r (want application/json)" % content_type,
        )
        try:
            parsed = json.loads(body.decode("utf-8"))
        except ValueError as exc:
            reporter.fail("GET /api/health body was not JSON: %s" % exc)
        else:
            reporter.check(
                parsed == {"status": "ok"},
                "GET /api/health body was %r (want {'status': 'ok'})" % (parsed,),
            )

    # Unknown API path -> 404 JSON.
    try:
        status, content_type, body = _request(port, "/api/nope")
    except Exception as exc:  # noqa: BLE001
        reporter.fail("GET /api/nope failed: %s" % exc)
    else:
        reporter.check(status == 404, "GET /api/nope returned HTTP %s (want 404)" % status)
        reporter.check(
            "application/json" in content_type,
            "GET /api/nope content type was %r (want application/json)" % content_type,
        )
        try:
            parsed = json.loads(body.decode("utf-8"))
        except ValueError as exc:
            reporter.fail("GET /api/nope body was not JSON: %s" % exc)
        else:
            reporter.check(
                parsed == {"error": "not found"},
                "GET /api/nope body was %r (want {'error': 'not found'})" % (parsed,),
            )

    # Unknown non-API path -> 404 plain text.
    try:
        status, content_type, body = _request(port, "/nope")
    except Exception as exc:  # noqa: BLE001
        reporter.fail("GET /nope failed: %s" % exc)
    else:
        reporter.check(status == 404, "GET /nope returned HTTP %s (want 404)" % status)
        reporter.check(
            "text/plain" in content_type,
            "GET /nope content type was %r (want text/plain)" % content_type,
        )
        reporter.check(b"404" in body, "GET /nope body did not mention 404: %r" % body)


# ---------------------------------------------------------------------------
# CLI validation
# ---------------------------------------------------------------------------


def _run_cli(reporter, args, timeout=30):
    try:
        return subprocess.run(
            [sys.executable, "-m", "hostping"] + list(args),
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        reporter.fail("CLI %s did not finish within %ss" % (" ".join(args), timeout))
        return None


def _validate_cli(reporter):
    version = _run_cli(reporter, ["--version"])
    if version is not None:
        reporter.check(
            version.returncode == 0,
            "python -m hostping --version exited %s (want 0)" % version.returncode,
        )
        reporter.check(
            ("hostping %s" % EXPECTED_VERSION) in version.stdout,
            "python -m hostping --version printed %r (want 'hostping %s')"
            % (version.stdout.strip(), EXPECTED_VERSION),
        )

    help_output = _run_cli(reporter, ["--help"])
    if help_output is not None:
        reporter.check(
            help_output.returncode == 0,
            "python -m hostping --help exited %s (want 0)" % help_output.returncode,
        )
        for flag in CLI_FLAGS:
            reporter.check(
                flag in help_output.stdout,
                "python -m hostping --help does not document %s" % flag,
            )


def _validate_port_in_use(reporter, hosts_file):
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        result = _run_cli(
            reporter,
            [
                "--no-browser",
                "--bind",
                "127.0.0.1",
                "--port",
                str(port),
                "--hosts-file",
                hosts_file,
                "--timeout",
                "0.5",
            ],
            timeout=15,
        )
        if result is None:
            return
        reporter.check(
            result.returncode != 0,
            "starting on an occupied port exited %s (want non-zero)" % result.returncode,
        )
        reporter.check(
            "could not start server" in result.stderr,
            "occupied-port stderr was %r (want it to contain 'could not start server')"
            % result.stderr.strip(),
        )
    finally:
        holder.close()


def _validate_hosts_file_error(reporter, workdir):
    """SPEC §6: an unreadable hosts file must yield 500 JSON, not a crash.

    ``/api/health`` never reads the hosts file, so the server still becomes
    ready; only ``/api/hosts`` fails.
    """
    missing = os.path.join(workdir, "missing-hosts-file")
    port = _free_port()
    process = _start_server(port, missing)
    try:
        ready, reason = _wait_for_health(port, process)
        if not ready:
            reporter.fail("error-path server never became ready: %s" % reason)
            output = _stop_and_capture(process)
            process = None
            if output.strip():
                sys.stderr.write(
                    "--- error-path server output ---\n%s\n"
                    "--------------------------------\n" % output
                )
            return

        try:
            status, content_type, body = _request(port, "/api/hosts")
        except Exception as exc:  # noqa: BLE001
            reporter.fail("GET /api/hosts with a missing hosts file failed: %s" % exc)
            return

        reporter.check(
            status == 500,
            "GET /api/hosts with a missing hosts file returned HTTP %s (want 500)" % status,
        )
        reporter.check(
            "application/json" in content_type,
            "missing-hosts-file content type was %r (want application/json)" % content_type,
        )
        try:
            parsed = json.loads(body.decode("utf-8"))
        except ValueError as exc:
            reporter.fail("missing-hosts-file body was not JSON: %s" % exc)
            return
        keys = set(parsed) if isinstance(parsed, dict) else set()
        reporter.check(
            {"error", "hosts_file"} <= keys,
            "missing-hosts-file body keys were %s (want error and hosts_file)"
            % (sorted(keys) or parsed,),
        )
    finally:
        if process is not None:
            _stop_and_capture(process)


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------


def main():
    reporter = Reporter()
    workdir = tempfile.mkdtemp(prefix="hostping-smoke-")
    process = None
    try:
        hosts_file = os.path.join(workdir, "hosts")
        with open(hosts_file, "w", encoding="utf-8") as handle:
            handle.write(HOSTS_FIXTURE)
        with open(hosts_file, "rb") as handle:
            original_bytes = handle.read()

        port = _free_port()
        process = _start_server(port, hosts_file)

        ready, reason = _wait_for_health(port, process)
        if ready:
            print("server is ready on 127.0.0.1:%d" % port)
            _validate_api_hosts(reporter, port, hosts_file)
            _validate_static_routes(reporter, port)
        else:
            reporter.fail("server never became ready: %s" % reason)
            output = _stop_and_capture(process)
            process = None
            if output.strip():
                sys.stderr.write("--- server output ---\n%s\n---------------------\n" % output)

        _validate_cli(reporter)
        _validate_port_in_use(reporter, hosts_file)
        _validate_hosts_file_error(reporter, workdir)

        with open(hosts_file, "rb") as handle:
            current_bytes = handle.read()
        reporter.check(
            current_bytes == original_bytes,
            "the hosts file was modified by the server (read-only guarantee violated)",
        )
    finally:
        if process is not None:
            _stop_and_capture(process)
        shutil.rmtree(workdir, ignore_errors=True)

    if reporter.failures:
        print("")
        print("smoke test FAILED with %d problem(s):" % len(reporter.failures))
        for message in reporter.failures:
            print("  - %s" % message)
        return 1

    print("smoke test passed: API contract, static routes, CLI and error path OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

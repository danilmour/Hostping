"""Per-entry orchestration (SPEC §4/§5): run checks concurrently and derive
the semaphore status + warnings, then build the ``/api/hosts`` payload.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import hosts_file as hosts_file_module
from . import reachability

#: Default bound on concurrent checks (SPEC §5).
DEFAULT_WORKERS = 8

#: Fallback result for an entry/target whose check could not be performed.
#: ``unknown`` with ``method: None`` is a valid, intended outcome (SPEC §4/§5):
#: callers must treat it as "no information", not as a hard error.
_UNKNOWN = {"status": "unknown", "method": None, "latency_ms": None, "error": None}

_RESULT_KEYS = ("status", "method", "latency_ms", "error")


def _utc_timestamp(now=None):
    """Return a UTC ISO-8601 timestamp with a trailing ``Z``."""
    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalise(result):
    if not isinstance(result, dict):
        return {
            "status": "unknown",
            "method": None,
            "latency_ms": None,
            "error": "checker returned %r" % (result,),
        }
    return {key: result.get(key) for key in _RESULT_KEYS}


def _safe_check(checker, target, timeout):
    """Call the checker and normalise the result; convert exceptions to unknown."""
    try:
        return _normalise(checker(target, timeout))
    except Exception as exc:  # noqa: BLE001 - a bad target must not abort a scan
        return {
            "status": "unknown",
            "method": None,
            "latency_ms": None,
            "error": "%s: %s" % (type(exc).__name__, exc),
        }


def _warning_for(hostname, hostname_result):
    status = hostname_result.get("status")
    if status == "unresolved":
        return "Hostname '%s' did not resolve (DNS/NSS lookup failed)." % hostname
    if status == "down":
        return "Hostname '%s' did not respond (ICMP/TCP)." % hostname
    return "Hostname '%s' could not be checked." % hostname


def _derive_status(ip_result, hostname_result, hostname):
    """Derive ``(status, warnings)`` for one entry (SPEC §4)."""
    ip_status = ip_result.get("status")

    if ip_status == "up":
        if hostname is None or hostname_result is None:
            return "up", []
        if hostname_result.get("status") == "up":
            return "up", []
        return "degraded", [_warning_for(hostname, hostname_result)]

    if ip_status == "down":
        return "down", []

    return "unknown", []


def run_scan(
    hosts_file=None,
    timeout=reachability.DEFAULT_TIMEOUT,
    checker=None,
    max_workers=DEFAULT_WORKERS,
    now=None,
):
    """Check every hosts entry and return the full ``/api/hosts`` payload.

    ``checker`` is injectable (defaults to :func:`reachability.check`) so tests
    can simulate scenarios without touching the network or DNS. It is called as
    ``checker(target, timeout)`` and must return a result dict.

    ``timeout`` is normalised via :func:`reachability.normalise_timeout`, so
    non-finite/non-positive values fall back to
    :data:`reachability.DEFAULT_TIMEOUT` and ``timeout_ms`` always reports the
    budget actually applied.
    """
    if hosts_file is None:
        hosts_file = hosts_file_module.DEFAULT_HOSTS_FILE
    if checker is None:
        checker = reachability.check

    # Single source of truth for the effective budget: non-finite/non-positive
    # values become the default so the payload can never carry a raw nan/inf/0
    # and the checks below never raise.
    timeout = reachability.normalise_timeout(timeout)

    entries = hosts_file_module.parse(hosts_file)
    workers = max(1, int(max_workers))

    started = time.monotonic()

    ip_results = {}
    hostname_results = {}
    tasks = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for index, entry in enumerate(entries):
            tasks.append((pool.submit(_safe_check, checker, entry["ip"], timeout), "ip", index))
            if entry["hostname"]:
                tasks.append(
                    (pool.submit(_safe_check, checker, entry["hostname"], timeout), "hostname", index)
                )
        for future, kind, index in tasks:
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - defensive
                result = {
                    "status": "unknown",
                    "method": None,
                    "latency_ms": None,
                    "error": "%s: %s" % (type(exc).__name__, exc),
                }
            (ip_results if kind == "ip" else hostname_results)[index] = result

    summary = {"total": len(entries), "up": 0, "degraded": 0, "down": 0, "unknown": 0}
    out_entries = []
    for index, entry in enumerate(entries):
        ip_result = ip_results.get(index) or dict(_UNKNOWN)
        hostname = entry["hostname"]
        hostname_result = hostname_results.get(index)
        status, warnings = _derive_status(ip_result, hostname_result, hostname)
        summary[status] += 1
        out_entries.append(
            {
                "id": str(entry["line"]),
                "line": entry["line"],
                "raw": entry["raw"],
                "ip": entry["ip"],
                "hostname": hostname,
                "aliases": list(entry["aliases"]),
                "status": status,
                "ip_result": ip_result,
                "hostname_result": hostname_result,
                "warnings": warnings,
            }
        )

    duration_ms = int(round((time.monotonic() - started) * 1000))

    return {
        "generated_at": _utc_timestamp(now),
        "hosts_file": str(hosts_file),
        "duration_ms": duration_ms,
        "timeout_ms": int(round(timeout * 1000)),
        "summary": summary,
        "entries": out_entries,
    }


# Convenience alias.
scan = run_scan

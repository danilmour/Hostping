"""Reachability checks: ICMP via the system ``ping``, TCP fallback (SPEC §5).

Every public call returns a dictionary and never raises for a failing target::

    {"status": "up"|"down"|"unresolved"|"unknown",
     "method": "icmp"|"tcp"|None,
     "latency_ms": <float|None>,
     "error": <str|None>}

``unresolved`` is only ever returned for hostname targets whose name cannot be
resolved via DNS/NSS.

``unknown`` is a valid, intended result: it is returned when an unforeseen
``OSError`` (or another unexpected failure) means the check could not be
performed at all. In that case ``method`` may also be ``None``. Callers must
treat ``unknown`` as "no information", never as a hard error.

Timing contract: a check with timeout ``T`` is bounded by ``T`` (plus a small,
documented overhead for spawning/tearing down helper processes and sockets).
Both the DNS/NSS resolution step and each probe are run against the same
deadline so a slow resolver or a black-hole target can never hang the scan.
"""

from __future__ import annotations

import ipaddress
import math
import re
import selectors
import shutil
import socket
import subprocess
import threading
import time

#: TCP ports used for the connect fallback when ICMP cannot be used (SPEC §5).
TCP_PORTS = (80, 443, 22, 445, 3389)

DEFAULT_TIMEOUT = 1.0

#: Time reserved out of a check's timeout for spawning/tearing down the helper
#: process, so the *whole* ``check()`` call (including ``fork``/``exec``) stays
#: within the caller's budget. ``ping -W`` only bounds the in-ping wait, not the
#: Python-side overhead.
_PROCESS_MARGIN = 0.05

#: Smallest ``ping -W`` value we will ask for. iputils accepts fractional
#: seconds (verified: ``ping -W 0.5`` waits ~0.5 s); the floor keeps the wait
#: strictly positive for very small budgets.
_MIN_PING_WAIT = 0.01

#: Markers that indicate the ``ping`` binary rejected our arguments (e.g. an
#: implementation that only accepts whole-second ``-W`` values). On such a
#: platform we retry once with an integer wait rather than failing hard.
_ARGUMENT_ERROR_MARKERS = (
    "invalid",
    "usage:",
    "usage ",
    "unknown option",
    "unrecognized option",
    "out of range",
    "bad ",
)

_TIME_RE = re.compile(r"time[=<]\s*([0-9]+(?:\.[0-9]+)?)\s*ms")


class _ResolutionTimeout(Exception):
    """Raised when name resolution does not finish within the time budget."""

    def __init__(self, target, timeout):
        super().__init__(
            "DNS/NSS lookup for '%s' did not complete within %.3gs" % (target, timeout)
        )
        self.target = target
        self.timeout = timeout


# Substrings that indicate ICMP could not actually be attempted (rather than
# the host simply being unreachable), which triggers the TCP fallback.
_UNUSABLE_MARKERS = (
    "operation not permitted",
    "permission denied",
    "not permitted",
    "socket:",
    "must be superuser",
)


def _find_ping():
    """Return the path to a usable ``ping`` binary, or ``None``."""
    return shutil.which("ping")


def normalise_timeout(value, default=DEFAULT_TIMEOUT):
    """Return a finite, strictly-positive timeout in seconds.

    This is the single place where the *effective* timeout is decided. A value
    that is non-numeric, non-finite (``inf``/``nan``), or ``<= 0`` is replaced
    by ``default``; the default itself is validated the same way and falls back
    to :data:`DEFAULT_TIMEOUT` if it is unusable. Callers must report the value
    returned here (e.g. ``timeout_ms``) so the reported budget matches the one
    actually applied and never a raw ``inf``/``nan``/``0``.
    """
    try:
        fallback = float(default)
    except (TypeError, ValueError):
        fallback = DEFAULT_TIMEOUT
    if not math.isfinite(fallback) or fallback <= 0:
        fallback = DEFAULT_TIMEOUT

    try:
        value = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(value) or value <= 0:
        return fallback
    return value


def _is_ip_literal(target):
    try:
        ipaddress.ip_address(target)
    except ValueError:
        return False
    return True


def _resolve_target(target):
    """Resolve ``target`` to ``(address_family, address_string)``.

    Raises ``socket.gaierror`` for a hostname that cannot be resolved and
    ``OSError`` for other resolution failures.
    """
    infos = socket.getaddrinfo(target, None, type=socket.SOCK_STREAM)
    if not infos:
        raise socket.gaierror(-2, "no address information for %r" % (target,))
    family, _type, _proto, _canon, sockaddr = infos[0]
    return family, sockaddr[0]


def _resolve_with_deadline(target, budget):
    """Resolve ``target`` bounded by ``budget`` seconds.

    Runs :func:`_resolve_target` in a daemon worker so a slow/unresponsive
    resolver cannot blow past the check's deadline. Resolution exceptions
    (``socket.gaierror``/``OSError``) propagate unchanged; if the resolver does
    not answer in time, :class:`_ResolutionTimeout` is raised instead of
    blocking the scan. The worker is a daemon, so timing out never prevents the
    process from exiting.
    """
    result = {}
    done = threading.Event()

    def _worker():
        try:
            result["value"] = _resolve_target(target)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
            result["error"] = exc
        finally:
            done.set()

    worker = threading.Thread(target=_worker, name="hostping-resolve", daemon=True)
    worker.start()
    if not done.wait(budget):
        raise _ResolutionTimeout(target, budget)
    if "error" in result:
        raise result["error"]
    return result["value"]


def _format_wait(seconds):
    """Format a fractional second count for ``ping -W`` (e.g. ``0.45``)."""
    text = ("%.3f" % seconds).rstrip("0").rstrip(".")
    return text or "0"


class _PingUnavailable(Exception):
    """Raised when the ``ping`` binary cannot be executed at all."""


def _run_ping(command, hard_timeout):
    """Run one ``ping`` invocation bounded by ``hard_timeout`` seconds.

    Returns ``(proc, timed_out)`` where ``proc`` is a
    :class:`subprocess.CompletedProcess` (or ``None`` when it was killed).
    """
    try:
        proc = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(_MIN_PING_WAIT, hard_timeout),
            text=True,
        )
    except subprocess.TimeoutExpired:
        return (None, True)
    except OSError as exc:  # binary vanished / not executable
        raise _PingUnavailable(str(exc)) from exc
    return (proc, False)


def _ping_once(ping_path, target, budget):
    """Run a single ICMP echo request, bounded by ``budget`` seconds.

    Returns ``(status, latency_ms, error, usable)``. When ``usable`` is ``False``
    ICMP could not be attempted at all (missing binary / insufficient
    privileges) and the caller should fall back to TCP.

    The destination is preceded by ``--`` so a target that starts with ``-``
    (e.g. a hosts-file entry of ``-V``) is always treated as a destination, not
    as an option. Without the separator ``iputils ping`` parses options
    anywhere, so ``ping -c 1 -n -W 0.5 -V`` merely prints its version and exits
    ``0`` — a false green for an unreachable host.

    The wait passed to ``ping -W`` is fractional and clamped to ``budget`` minus
    :data:`_PROCESS_MARGIN`, so the full invocation (including process spawn)
    stays within the caller's deadline. The optional argument-error retry
    recomputes its hard timeout from the same deadline, so the two invocations
    together still honour the original ``budget``.
    """
    if budget <= 0:
        return ("down", None, "ping timed out", True)

    deadline = time.monotonic() + budget
    wait = max(_MIN_PING_WAIT, budget - _PROCESS_MARGIN)
    command = [ping_path, "-c", "1", "-n", "-W", _format_wait(wait), "--", target]

    try:
        proc, timed_out = _run_ping(command, budget)
    except _PingUnavailable as exc:
        return (None, None, str(exc), False)

    if timed_out:
        return ("down", None, "ping timed out", True)

    output = (proc.stdout or "") + (proc.stderr or "")
    lowered = output.lower()

    if proc.returncode != 0 and any(marker in lowered for marker in _UNUSABLE_MARKERS):
        return (None, None, output.strip() or "icmp unavailable", False)

    # Some ``ping`` builds only accept whole-second ``-W`` values and reject the
    # fractional one with a usage error. Retry once with a 1 s floor instead of
    # failing hard; the retry's hard timeout is the *time remaining* on the
    # shared deadline, so the two runs together never exceed the original
    # ``budget`` (a retry that reused the full budget could overshoot it).
    if (
        proc.returncode != 0
        and proc.returncode != 1
        and any(marker in lowered for marker in _ARGUMENT_ERROR_MARKERS)
    ):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ("down", None, "ping timed out", True)
        retry_command = [ping_path, "-c", "1", "-n", "-W", "1", "--", target]
        try:
            proc, timed_out = _run_ping(retry_command, remaining)
        except _PingUnavailable as exc:
            return (None, None, str(exc), False)
        if timed_out:
            return ("down", None, "ping timed out", True)
        output = (proc.stdout or "") + (proc.stderr or "")
        lowered = output.lower()
        if proc.returncode != 0 and any(
            marker in lowered for marker in _UNUSABLE_MARKERS
        ):
            return (None, None, output.strip() or "icmp unavailable", False)

    if proc.returncode == 0:
        match = _TIME_RE.search(output)
        latency = float(match.group(1)) if match else None
        return ("up", latency, None, True)

    if proc.returncode == 1:
        return ("down", None, "no ICMP reply", True)

    return ("down", None, output.strip() or "ping failed", True)


def _tcp_probe(family, address, ports, budget):
    """Probe ``ports`` concurrently with non-blocking connects.

    Returns the first port that accepts a connection, or ``None``. The whole
    probe is bounded by ``budget`` seconds so a black-hole address cannot hang
    the scan.
    """
    if budget <= 0:
        return None

    deadline = time.monotonic() + budget
    selector = selectors.DefaultSelector()
    pending = {}

    def _connect_addr(port):
        if family == socket.AF_INET6:
            return (address, port, 0, 0)
        return (address, port)

    try:
        for port in ports:
            try:
                sock = socket.socket(family, socket.SOCK_STREAM)
            except OSError:
                continue
            sock.setblocking(False)
            try:
                sock.connect(_connect_addr(port))
            except (BlockingIOError, InterruptedError):
                pass  # connection in progress
            except OSError:
                sock.close()
                continue
            try:
                selector.register(sock, selectors.EVENT_WRITE, port)
            except (KeyError, ValueError):
                sock.close()
                continue
            pending[sock] = port

        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                events = selector.select(remaining)
            except OSError:
                break
            if not events:
                break  # timed out
            for key, _mask in events:
                sock = key.fileobj
                if sock not in pending:
                    continue
                port = pending[sock]
                error = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                try:
                    selector.unregister(sock)
                except (KeyError, ValueError):
                    pass
                sock.close()
                del pending[sock]
                if error == 0:
                    return port
    finally:
        for sock in list(pending):
            try:
                selector.unregister(sock)
            except (KeyError, ValueError):
                pass
            try:
                sock.close()
            except OSError:
                pass
        selector.close()
    return None


def check(target, timeout=DEFAULT_TIMEOUT):
    """Check reachability of ``target`` (an IP literal or a hostname).

    Always returns a dict; never raises for an unreachable or unknown target.

    The result shape is::

        {"status": "up"|"down"|"unresolved"|"unknown",
         "method": "icmp"|"tcp"|None,
         "latency_ms": <float|None>,
         "error": <str|None>}

    ``unresolved`` means a hostname could not be resolved. ``unknown`` means the
    check could not be performed because of an unexpected ``OSError`` (see the
    module docstring); ``method`` may be ``None`` in that case. Both are
    deliberate results, not errors, and the call is bounded by ``timeout``
    seconds regardless of which path is taken. A non-finite, non-numeric, or
    non-positive ``timeout`` is normalised to :data:`DEFAULT_TIMEOUT` via
    :func:`normalise_timeout` so this function never raises or hangs on a bad
    budget.
    """
    timeout = normalise_timeout(timeout)

    target = (target or "").strip()
    if not target:
        return {
            "status": "down",
            "method": "tcp",
            "latency_ms": None,
            "error": "empty target",
        }

    deadline = time.monotonic() + timeout
    ping_path = _find_ping()
    method = "icmp" if ping_path else "tcp"
    is_ip = _is_ip_literal(target)

    # Resolution shares the same deadline; a slow/unresponsive resolver yields
    # an unresolved (hostname) or down (literal) result instead of hanging.
    try:
        family, address = _resolve_with_deadline(target, deadline - time.monotonic())
    except socket.gaierror as exc:
        if not is_ip:
            return {
                "status": "unresolved",
                "method": method,
                "latency_ms": None,
                "error": "DNS/NSS lookup failed for '%s': %s" % (target, exc),
            }
        return {"status": "down", "method": method, "latency_ms": None, "error": str(exc)}
    except _ResolutionTimeout as exc:
        return {
            "status": "down" if is_ip else "unresolved",
            "method": method,
            "latency_ms": None,
            "error": str(exc),
        }
    except OSError as exc:
        # Unexpected failure: valid "unknown" result; method reflects what was attempted.
        return {"status": "unknown", "method": method, "latency_ms": None, "error": str(exc)}

    last_error = None
    if ping_path:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            status, latency, error, usable = _ping_once(ping_path, target, remaining)
            if usable:
                if status == "up":
                    return {
                        "status": "up",
                        "method": "icmp",
                        "latency_ms": latency,
                        "error": None,
                    }
                return {"status": "down", "method": "icmp", "latency_ms": None, "error": error}
            last_error = error or "icmp unavailable"

    # TCP fallback: ICMP missing, or needed privileges unavailable.
    remaining = deadline - time.monotonic()
    try:
        port = _tcp_probe(family, address, TCP_PORTS, remaining)
    except Exception as exc:  # defensive: never propagate an unexpected failure
        return {"status": "unknown", "method": "tcp", "latency_ms": None, "error": str(exc)}

    if port is not None:
        return {"status": "up", "method": "tcp", "latency_ms": None, "error": None}

    return {
        "status": "down",
        "method": "tcp",
        "latency_ms": None,
        "error": last_error or "no ICMP reply and no open TCP port",
    }

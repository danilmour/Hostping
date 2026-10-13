"""Tests for hostping.reachability (SPEC §5, AC3/AC4/AC5 plumbing)."""

import socket
import subprocess
import threading
import time
import unittest
from unittest import mock

from hostping import reachability


def _completed(returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["ping"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class ListeningSocketMixin:
    """Provide a localhost TCP port that is guaranteed to accept connections."""

    def _make_listener(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(5)
        self.addCleanup(sock.close)
        return sock.getsockname()[1]


class CheckResultShapeTests(unittest.TestCase):
    def test_result_always_has_required_keys(self):
        result = reachability.check("127.0.0.1", 1.0)
        self.assertEqual(
            set(result), {"status", "method", "latency_ms", "error"}
        )
        self.assertIn(result["status"], ("up", "down", "unresolved", "unknown"))

    def test_empty_target_does_not_raise(self):
        result = reachability.check("", 1.0)
        self.assertEqual(result["status"], "down")
        self.assertIsNotNone(result["error"])


class LocalhostTests(ListeningSocketMixin, unittest.TestCase):
    def test_localhost_is_reachable(self):
        # Also expose a local TCP port so the test still passes if ICMP is
        # unavailable in the running environment.
        port = self._make_listener()
        with mock.patch.object(reachability, "TCP_PORTS", (port,)):
            result = reachability.check("127.0.0.1", 1.0)
        self.assertEqual(result["status"], "up")
        self.assertIn(result["method"], ("icmp", "tcp"))
        self.assertIsNone(result["error"])


class BlackholeTests(unittest.TestCase):
    def test_test_net_1_is_down_within_timeout(self):
        timeout = 1.0
        started = time.monotonic()
        result = reachability.check("192.0.2.1", timeout)
        elapsed = time.monotonic() - started
        self.assertEqual(result["status"], "down")
        # Must not hang: allow generous slack for process/socket overhead.
        self.assertLess(elapsed, timeout + 2.0)


class IcmpParsingTests(unittest.TestCase):
    def test_latency_is_parsed_from_ping_output(self):
        fake = _completed(
            0, stdout="64 bytes from 127.0.0.1: icmp_seq=1 ttl=64 time=0.123 ms\n"
        )
        with mock.patch.object(reachability.shutil, "which", return_value="/usr/bin/ping"), \
                mock.patch.object(reachability.subprocess, "run", return_value=fake):
            result = reachability.check("127.0.0.1", 1.0)
        self.assertEqual(result["status"], "up")
        self.assertEqual(result["method"], "icmp")
        self.assertAlmostEqual(result["latency_ms"], 0.123, places=3)

    def test_sub_millisecond_ping_output(self):
        fake = _completed(0, stdout="64 bytes from 10.0.0.1: time<1 ms\n")
        with mock.patch.object(reachability.shutil, "which", return_value="/usr/bin/ping"), \
                mock.patch.object(reachability.subprocess, "run", return_value=fake):
            result = reachability.check("10.0.0.1", 1.0)
        self.assertEqual(result["status"], "up")
        self.assertEqual(result["method"], "icmp")


class TcpFallbackTests(ListeningSocketMixin, unittest.TestCase):
    def test_missing_ping_uses_tcp(self):
        port = self._make_listener()
        with mock.patch.object(reachability.shutil, "which", return_value=None), \
                mock.patch.object(reachability, "TCP_PORTS", (port,)):
            result = reachability.check("127.0.0.1", 1.0)
        self.assertEqual(result["status"], "up")
        self.assertEqual(result["method"], "tcp")

    def test_ping_not_permitted_falls_back_to_tcp(self):
        port = self._make_listener()
        fake = _completed(2, stderr="ping: socket: Operation not permitted\n")
        with mock.patch.object(reachability.shutil, "which", return_value="/usr/bin/ping"), \
                mock.patch.object(reachability.subprocess, "run", return_value=fake), \
                mock.patch.object(reachability, "TCP_PORTS", (port,)):
            result = reachability.check("127.0.0.1", 1.0)
        self.assertEqual(result["status"], "up")
        self.assertEqual(result["method"], "tcp")

    def test_missing_ping_and_no_open_port_is_down(self):
        # Port 0 can never be connected to; keep the probe fast.
        with mock.patch.object(reachability.shutil, "which", return_value=None), \
                mock.patch.object(reachability, "TCP_PORTS", (1,)):
            started = time.monotonic()
            result = reachability.check("192.0.2.1", 1.0)
            elapsed = time.monotonic() - started
        self.assertEqual(result["status"], "down")
        self.assertEqual(result["method"], "tcp")
        self.assertLess(elapsed, 3.0)


class UnresolvedTests(unittest.TestCase):
    def test_unresolvable_hostname(self):
        def boom(_target):
            raise socket.gaierror(-2, "Name or service not known")

        with mock.patch.object(reachability, "_resolve_target", side_effect=boom):
            result = reachability.check("definitely-not-real.invalid", 1.0)
        self.assertEqual(result["status"], "unresolved")
        self.assertIn(result["method"], ("icmp", "tcp"))
        self.assertIsNotNone(result["error"])

    def test_ip_literal_never_unresolved(self):
        def boom(_target):
            raise socket.gaierror(-2, "Name or service not known")

        with mock.patch.object(reachability, "_resolve_target", side_effect=boom):
            result = reachability.check("192.0.2.1", 1.0)
        self.assertNotEqual(result["status"], "unresolved")


class UnknownResultTests(unittest.TestCase):
    """FIX 3: an unexpected ``OSError`` yields a valid ``unknown`` result."""

    def test_unexpected_oserror_yields_unknown(self):
        def boom(_target, _budget):
            raise OSError("unexpected resolver failure")

        with mock.patch.object(reachability, "_resolve_with_deadline", side_effect=boom):
            result = reachability.check("127.0.0.1", 1.0)

        self.assertEqual(result["status"], "unknown")
        self.assertIn(result["method"], ("icmp", "tcp"))
        self.assertIsNotNone(result["error"])

    def test_unknown_is_documented_in_module(self):
        # Guards the SPEC §5 alignment work (unknown is an intended result).
        self.assertIn("unknown", reachability.__doc__)


class SubSecondTimeoutTests(unittest.TestCase):
    """FIX 1: the configured per-check timeout must be honoured (AC4/SPEC §5)."""

    def test_black_hole_completes_within_sub_second_timeout(self):
        timeout = 0.5
        started = time.monotonic()
        result = reachability.check("192.0.2.1", timeout)
        elapsed = time.monotonic() - started
        self.assertEqual(result["status"], "down")
        # The original bug was a 2x overshoot (1005 ms for a 500 ms timeout).
        # Keep a generous CI-proof margin while still catching a regression.
        self.assertLess(
            elapsed, 0.75, "check() overshot its %ss budget: %.3fs" % (timeout, elapsed)
        )

    def test_one_second_timeout_is_not_overshot_by_whole_seconds(self):
        started = time.monotonic()
        result = reachability.check("192.0.2.1", 1.0)
        elapsed = time.monotonic() - started
        self.assertEqual(result["status"], "down")
        self.assertLess(elapsed, 1.25)


class FractionalWaitTests(unittest.TestCase):
    """FIX 1: a fractional ``ping -W`` must actually be passed through."""

    def test_fractional_wait_is_passed_to_ping(self):
        captured = []

        def fake_run(command, **kwargs):
            captured.append((command, kwargs))
            return _completed(1, stderr="no reply\n")

        with mock.patch.object(
            reachability.shutil, "which", return_value="/usr/bin/ping"
        ), mock.patch.object(reachability.subprocess, "run", side_effect=fake_run):
            result = reachability.check("192.0.2.1", 0.5)

        self.assertEqual(result["status"], "down")
        self.assertEqual(len(captured), 1)
        command, kwargs = captured[0]
        self.assertIn("-W", command)
        wait = command[command.index("-W") + 1]
        self.assertNotIn(wait, ("0", "1"), "expected a fractional -W, got %r" % wait)
        self.assertGreater(float(wait), 0)
        # Must be clamped below the requested timeout so spawn overhead fits.
        self.assertLess(float(wait), 0.5)
        # subprocess hard timeout is bounded by the remaining budget too.
        self.assertLessEqual(kwargs["timeout"], 0.5 + 1e-9)

    def test_ping_rejecting_fractional_wait_does_not_raise(self):
        # First invocation rejects the fractional -W with a usage error; the
        # retry with an integer floor succeeds.
        usage_error = _completed(2, stderr="ping: invalid argument: '0.45'\n")
        success = _completed(0, stdout="64 bytes from 192.0.2.1: time=0.2 ms\n")
        responses = [usage_error, success]
        captured = []

        def fake_run(command, **kwargs):
            captured.append(command)
            return responses.pop(0)

        with mock.patch.object(
            reachability.shutil, "which", return_value="/usr/bin/ping"
        ), mock.patch.object(reachability.subprocess, "run", side_effect=fake_run):
            started = time.monotonic()
            result = reachability.check("192.0.2.1", 0.5)
            elapsed = time.monotonic() - started

        self.assertEqual(result["status"], "up")
        self.assertEqual(len(captured), 2)
        retry = captured[1]
        self.assertEqual(
            retry[retry.index("-W") + 1],
            "1",
            "retry should fall back to a whole-second -W floor",
        )
        self.assertLess(elapsed, 0.75)

    def test_ping_always_rejecting_fractional_wait_still_returns_down(self):
        usage_error = _completed(2, stderr="ping: invalid argument: '0.45'\n")

        with mock.patch.object(
            reachability.shutil, "which", return_value="/usr/bin/ping"
        ), mock.patch.object(
            reachability.subprocess, "run", return_value=usage_error
        ) as run:
            started = time.monotonic()
            result = reachability.check("192.0.2.1", 0.5)
            elapsed = time.monotonic() - started

        self.assertEqual(result["status"], "down")
        self.assertEqual(result["method"], "icmp")
        self.assertEqual(run.call_count, 2)
        self.assertLess(elapsed, 0.75)


class SlowResolverTests(unittest.TestCase):
    """FIX 2: a slow/unresponsive resolver must not exceed the timeout."""

    def test_slow_hostname_resolution_is_bounded(self):
        release = threading.Event()
        started_resolving = threading.Event()

        def slow_resolve(_target):
            started_resolving.set()
            release.wait(10.0)
            return (socket.AF_INET, "192.0.2.1")

        try:
            with mock.patch.object(
                reachability, "_resolve_target", side_effect=slow_resolve
            ):
                timeout = 0.3
                started = time.monotonic()
                result = reachability.check("slow.example.invalid", timeout)
                elapsed = time.monotonic() - started
        finally:
            release.set()

        self.assertTrue(started_resolving.wait(1.0), "resolver worker never started")
        self.assertEqual(result["status"], "unresolved")
        self.assertIsNotNone(result["error"])
        self.assertIn("slow.example.invalid", result["error"])
        # Allow generous slack for thread startup, but nowhere near 10 s.
        self.assertLess(
            elapsed, 0.75, "resolution was not bounded: %.3fs" % elapsed
        )

    def test_resolution_timeout_does_not_raise_for_ip_literal(self):
        release = threading.Event()

        def slow_resolve(_target):
            release.wait(10.0)
            return (socket.AF_INET, "127.0.0.1")

        try:
            with mock.patch.object(
                reachability, "_resolve_target", side_effect=slow_resolve
            ), mock.patch.object(
                reachability.shutil, "which", return_value="/usr/bin/ping"
            ):
                started = time.monotonic()
                result = reachability.check("192.0.2.1", 0.3)
                elapsed = time.monotonic() - started
        finally:
            release.set()

        # An IP literal never resolves over DNS, so a resolution timeout maps to
        # "down" (not "unresolved") and must not raise or hang.
        self.assertEqual(result["status"], "down")
        self.assertEqual(result["method"], "icmp")
        self.assertIsNotNone(result["error"])
        self.assertLess(elapsed, 0.75)


class NormaliseTimeoutTests(unittest.TestCase):
    """FIX: a single helper decides the effective timeout."""

    def test_valid_values_pass_through(self):
        self.assertEqual(reachability.normalise_timeout(0.5), 0.5)
        self.assertEqual(reachability.normalise_timeout(2), 2.0)

    def test_bad_values_fall_back_to_default(self):
        for bad in (
            0,
            0.0,
            -1,
            -0.5,
            float("inf"),
            float("-inf"),
            float("nan"),
            None,
            "nope",
            "",
            object(),
        ):
            self.assertEqual(
                reachability.normalise_timeout(bad),
                reachability.DEFAULT_TIMEOUT,
                "normalise_timeout(%r) did not fall back" % (bad,),
            )

    def test_default_argument_is_used_and_validated(self):
        self.assertEqual(reachability.normalise_timeout(0, default=2.5), 2.5)
        for bad_default in (0, -1, float("inf"), float("nan"), "x", None):
            self.assertEqual(
                reachability.normalise_timeout(0, default=bad_default),
                reachability.DEFAULT_TIMEOUT,
                "bad default %r was not rejected" % (bad_default,),
            )


class NonFiniteTimeoutTests(unittest.TestCase):
    """FIX: non-finite/invalid budgets must not raise or hang ``check``.

    ``threading.Event.wait`` raises ``OverflowError`` for ``inf``/``nan`` and
    ``time.monotonic() + inf`` poisons the deadline, so these must be normalised
    at the boundary.
    """

    def test_infinite_timeout_does_not_raise_or_hang(self):
        started = time.monotonic()
        result = reachability.check("127.0.0.1", float("inf"))
        elapsed = time.monotonic() - started
        self.assertIn(result["status"], ("up", "down", "unknown"))
        self.assertNotEqual(result["status"], "unresolved")
        self.assertLess(elapsed, 3.0, "check() hung on inf: %.3fs" % elapsed)

    def test_nan_and_non_positive_timeouts_are_normalised(self):
        for bad in (float("nan"), 0, -1.0):
            started = time.monotonic()
            result = reachability.check("127.0.0.1", bad)
            elapsed = time.monotonic() - started
            self.assertIn(result["status"], ("up", "down", "unknown"))
            self.assertLess(
                elapsed, 3.0, "check(%r) hung: %.3fs" % (bad, elapsed)
            )


class OptionInjectionTests(unittest.TestCase):
    """FIX: a target beginning with ``-`` must never be read as an option.

    ``iputils ping`` parses options anywhere on the command line, so without a
    ``--`` separator ``ping -c 1 -n -W 0.5 -V`` prints the version and exits 0,
    which the old code reported as ``up`` for an unreachable host (a false
    green). The fake ping below emulates that option parsing so the test fails
    on a regression of the separator.
    """

    VERSION_OUTPUT = "ping from iputils 20250605\n"
    USAGE_OUTPUT = "Usage\n  ping [options] <destination>\n"

    def _fake_ping(self, captured):
        def fake_run(command, **kwargs):
            captured.append(command)
            if "--" in command:
                destination = command[command.index("--") + 1]
                # After ``--`` a leading-dash token is a destination hostname,
                # which cannot be resolved in this scenario.
                return _completed(
                    2, stderr="ping: %s: Name or service not known\n" % destination
                )
            option = command[-1]
            if option in ("-V", "--version"):
                return _completed(0, stdout=self.VERSION_OUTPUT)
            if option in ("-h", "--help"):
                return _completed(0, stdout=self.USAGE_OUTPUT)
            return _completed(1, stderr="no reply\n")

        return fake_run

    def _run(self, target):
        captured = []
        with mock.patch.object(
            reachability,
            "_resolve_target",
            return_value=(socket.AF_INET, "192.0.2.1"),
        ), mock.patch.object(
            reachability.shutil, "which", return_value="/usr/bin/ping"
        ), mock.patch.object(
            reachability.subprocess, "run", side_effect=self._fake_ping(captured)
        ):
            result = reachability.check(target, 0.5)
        return result, captured

    def test_version_flag_target_is_not_reported_up(self):
        result, captured = self._run("-V")
        self.assertNotEqual(result["status"], "up")
        self.assertEqual(result["status"], "down")
        self.assertEqual(len(captured), 1)
        command = captured[0]
        self.assertIn("--", command)
        self.assertEqual(command[command.index("--") + 1], "-V")
        self.assertEqual(command[-1], "-V")

    def test_help_flag_target_is_not_reported_up(self):
        result, captured = self._run("--help")
        self.assertNotEqual(result["status"], "up")
        command = captured[0]
        self.assertIn("--", command)
        self.assertEqual(command[command.index("--") + 1], "--help")

    def test_separator_precedes_target_in_primary_and_retry(self):
        # A rejected fractional -W triggers the retry; both argv forms must
        # carry the separator immediately before the destination.
        captured = []
        usage = _completed(2, stderr="ping: invalid argument: '0.45'\n")

        def fake_run(command, **kwargs):
            captured.append(command)
            if "--" in command and command[-1] == "-V":
                return usage
            return _completed(2, stderr="ping: -V: Name or service not known\n")

        with mock.patch.object(
            reachability,
            "_resolve_target",
            return_value=(socket.AF_INET, "192.0.2.1"),
        ), mock.patch.object(
            reachability.shutil, "which", return_value="/usr/bin/ping"
        ), mock.patch.object(reachability.subprocess, "run", side_effect=fake_run):
            result = reachability.check("-V", 0.5)

        self.assertNotEqual(result["status"], "up")
        self.assertEqual(len(captured), 2)
        for command in captured:
            self.assertIn("--", command)
            self.assertEqual(command[command.index("--") + 1], "-V")
            self.assertEqual(command[-1], "-V")


class BoundedRetryTests(unittest.TestCase):
    """FIX: the argument-error retry must use the time remaining, not the full
    original budget, so both invocations together stay within the timeout."""

    def test_retry_hard_timeout_shrinks_with_elapsed_time(self):
        captured = []

        def fake_run(command, **kwargs):
            captured.append(kwargs["timeout"])
            if len(captured) == 1:
                time.sleep(0.35)  # consume most of the 0.5 s budget
                return _completed(2, stderr="ping: invalid argument: '0.45'\n")
            return _completed(0, stdout="64 bytes from 192.0.2.1: time=0.2 ms\n")

        with mock.patch.object(
            reachability,
            "_resolve_target",
            return_value=(socket.AF_INET, "192.0.2.1"),
        ), mock.patch.object(
            reachability.shutil, "which", return_value="/usr/bin/ping"
        ), mock.patch.object(reachability.subprocess, "run", side_effect=fake_run):
            result = reachability.check("192.0.2.1", 0.5)

        self.assertEqual(result["status"], "up")
        self.assertEqual(len(captured), 2)
        # Old bug: the retry reused the full 0.5 s budget. Now it is the
        # remainder, which must be markedly smaller.
        self.assertLess(captured[1], 0.35, "retry reused the full budget")
        self.assertLess(captured[1], captured[0])


if __name__ == "__main__":
    unittest.main()

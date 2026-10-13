"""Tests for hostping.checks (SPEC §4, AC5/AC6).

Uses an injected fake checker so no network or DNS is required.
"""

import os
import tempfile
import unittest

from hostping import checks

HOSTS = (
    "127.0.0.1 goodhost\n"
    "192.0.2.1 downhost\n"
    "10.0.0.9 mixedhost\n"
    "10.0.0.10\n"
)

UP = {"status": "up", "method": "icmp", "latency_ms": 0.1, "error": None}
DOWN = {"status": "down", "method": "icmp", "latency_ms": None, "error": "no reply"}
UNRESOLVED = {
    "status": "unresolved",
    "method": "icmp",
    "latency_ms": None,
    "error": "DNS/NSS lookup failed",
}

RESULTS = {
    "127.0.0.1": UP,
    "goodhost": UP,
    "192.0.2.1": DOWN,
    "downhost": DOWN,
    "10.0.0.9": UP,
    "mixedhost": UNRESOLVED,
    "10.0.0.10": UP,
}


def fake_checker(target, timeout):
    return dict(RESULTS[target])


class ChecksScanTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".hosts", delete=False, encoding="utf-8"
        )
        handle.write(HOSTS)
        handle.close()
        self.path = handle.name
        self.addCleanup(os.unlink, self.path)
        self.payload = checks.run_scan(
            hosts_file=self.path, timeout=1.0, checker=fake_checker
        )
        self.by_ip = {entry["ip"]: entry for entry in self.payload["entries"]}

    def test_entry_order_and_ids(self):
        self.assertEqual(
            [entry["id"] for entry in self.payload["entries"]], ["1", "2", "3", "4"]
        )
        self.assertEqual(self.payload["entries"][0]["line"], 1)

    def test_ip_and_hostname_up_is_up(self):
        entry = self.by_ip["127.0.0.1"]
        self.assertEqual(entry["status"], "up")
        self.assertEqual(entry["ip_result"]["status"], "up")
        self.assertEqual(entry["hostname_result"]["status"], "up")
        self.assertEqual(entry["warnings"], [])

    def test_ip_down_is_down(self):
        entry = self.by_ip["192.0.2.1"]
        self.assertEqual(entry["status"], "down")
        self.assertEqual(entry["ip_result"]["status"], "down")

    def test_ip_up_hostname_unresolved_is_degraded_with_warning(self):
        entry = self.by_ip["10.0.0.9"]
        self.assertEqual(entry["status"], "degraded")
        self.assertEqual(entry["ip_result"]["status"], "up")
        self.assertEqual(entry["hostname_result"]["status"], "unresolved")
        self.assertTrue(entry["warnings"])
        self.assertIn("mixedhost", entry["warnings"][0])
        self.assertIn("did not resolve", entry["warnings"][0])

    def test_ip_only_entry_is_up_with_no_hostname_result(self):
        entry = self.by_ip["10.0.0.10"]
        self.assertIsNone(entry["hostname"])
        self.assertIsNone(entry["hostname_result"])
        self.assertEqual(entry["status"], "up")

    def test_summary_counts_are_consistent(self):
        summary = self.payload["summary"]
        self.assertEqual(summary["total"], len(self.payload["entries"]))
        self.assertEqual(
            summary["up"] + summary["degraded"] + summary["down"] + summary["unknown"],
            summary["total"],
        )
        self.assertEqual(summary["up"], 2)
        self.assertEqual(summary["down"], 1)
        self.assertEqual(summary["degraded"], 1)
        self.assertEqual(summary["unknown"], 0)

    def test_payload_top_level_contract(self):
        self.assertEqual(
            set(self.payload),
            {
                "generated_at",
                "hosts_file",
                "duration_ms",
                "timeout_ms",
                "summary",
                "entries",
            },
        )
        self.assertTrue(self.payload["generated_at"].endswith("Z"))
        self.assertEqual(self.payload["hosts_file"], self.path)
        self.assertEqual(self.payload["timeout_ms"], 1000)


class ChecksUnknownTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".hosts", delete=False, encoding="utf-8"
        )
        handle.write("127.0.0.1 localhost\n")
        handle.close()
        self.path = handle.name
        self.addCleanup(os.unlink, self.path)

    def test_checker_exception_yields_unknown(self):
        def bad(target, timeout):
            raise RuntimeError("kaboom")

        payload = checks.run_scan(hosts_file=self.path, timeout=1.0, checker=bad)
        self.assertEqual(payload["entries"][0]["status"], "unknown")
        self.assertEqual(payload["summary"]["unknown"], payload["summary"]["total"])

    def test_checker_returning_garbage_yields_unknown(self):
        payload = checks.run_scan(
            hosts_file=self.path, timeout=1.0, checker=lambda t, s: "nope"
        )
        self.assertEqual(payload["entries"][0]["status"], "unknown")

    def test_default_checker_is_reachability_check(self):
        # Keep the probe local and cheap; only assert plumbing works.
        payload = checks.run_scan(hosts_file=self.path, timeout=1.0)
        self.assertIn(payload["entries"][0]["status"], ("up", "down", "unknown"))


class ScanTimingTests(unittest.TestCase):
    """AC4/SPEC §5: a black-hole entry must not blow the scan timeout.

    Uses the real :func:`reachability.check` (no injected checker) so the
    end-to-end budget is exercised.
    """

    def test_black_hole_scan_respects_sub_second_timeout(self):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".hosts", delete=False, encoding="utf-8"
        )
        handle.write("192.0.2.1 test-net-1\n")
        handle.close()
        self.addCleanup(os.unlink, handle.name)

        timeout = 0.5
        payload = checks.run_scan(hosts_file=handle.name, timeout=timeout)

        entry = payload["entries"][0]
        self.assertEqual(entry["status"], "down")
        self.assertEqual(payload["timeout_ms"], 500)
        # The old bug reported ~1005 ms for this configuration.
        self.assertLess(
            payload["duration_ms"],
            750,
            "scan overshot its %s budget: %d ms"
            % (payload["timeout_ms"], payload["duration_ms"]),
        )


class TimeoutNormalisationTests(unittest.TestCase):
    """FIX: non-finite/invalid timeouts must yield a well-formed payload.

    ``int(round(float('nan')))``/``float('inf')`` used to raise, and a ``<= 0``
    timeout was silently raised to 1.0 s while ``timeout_ms`` still reported the
    raw value.
    """

    def setUp(self):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".hosts", delete=False, encoding="utf-8"
        )
        handle.write("127.0.0.1 localhost\n")
        handle.close()
        self.path = handle.name
        self.addCleanup(os.unlink, self.path)

    def _scan(self, timeout, checker=None):
        return checks.run_scan(
            hosts_file=self.path,
            timeout=timeout,
            checker=checker or (lambda target, t: dict(UP)),
        )

    def test_non_finite_and_non_positive_timeouts_do_not_raise(self):
        for bad in (
            float("inf"),
            float("-inf"),
            float("nan"),
            0,
            0.0,
            -1,
            -2.5,
            None,
            "nonsense",
        ):
            payload = self._scan(bad)
            self.assertIsInstance(payload["timeout_ms"], int, "for %r" % (bad,))
            self.assertGreater(payload["timeout_ms"], 0, "for %r" % (bad,))
            self.assertEqual(payload["timeout_ms"], 1000, "for %r" % (bad,))
            self.assertEqual(
                set(payload),
                {
                    "generated_at",
                    "hosts_file",
                    "duration_ms",
                    "timeout_ms",
                    "summary",
                    "entries",
                },
            )
            self.assertEqual(payload["summary"]["total"], 1)
            self.assertEqual(payload["entries"][0]["status"], "up")

    def test_sub_second_timeout_is_reported_accurately(self):
        payload = self._scan(0.25)
        self.assertEqual(payload["timeout_ms"], 250)

    def test_checker_receives_the_normalised_timeout(self):
        seen = []

        def recording_checker(target, timeout):
            seen.append(timeout)
            return dict(UP)

        payload = checks.run_scan(
            hosts_file=self.path, timeout=float("inf"), checker=recording_checker
        )
        self.assertTrue(seen)
        self.assertTrue(all(value == 1.0 for value in seen), seen)
        self.assertEqual(payload["timeout_ms"], 1000)


if __name__ == "__main__":
    unittest.main()

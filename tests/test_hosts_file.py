"""Tests for hostping.hosts_file (SPEC FR1/FR2, AC2)."""

import os
import tempfile
import unittest

from hostping import hosts_file


SAMPLE = (
    "# a full-line comment\n"
    "\n"
    "127.0.0.1\tlocalhost\n"
    "::1 localhost ip6-localhost ip6-loopback\n"
    "192.0.2.1 blackhole.example   # black hole address\n"
    "\n"
    "10.0.0.5\n"
    "   # indented comment, still ignored\n"
    "fe80::1%eth0 linklocal6\n"
)


class HostsFileParseTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".hosts", delete=False, encoding="utf-8"
        )
        handle.write(SAMPLE)
        handle.close()
        self.path = handle.name
        self.addCleanup(os.unlink, self.path)

    def test_only_real_entries_are_returned(self):
        entries = hosts_file.parse(self.path)
        self.assertEqual([entry["line"] for entry in entries], [3, 4, 5, 7, 9])

    def test_order_is_preserved(self):
        entries = hosts_file.parse(self.path)
        self.assertEqual([entry["ip"] for entry in entries],
                         ["127.0.0.1", "::1", "192.0.2.1", "10.0.0.5", "fe80::1%eth0"])

    def test_basic_entry_fields(self):
        entry = hosts_file.parse(self.path)[0]
        self.assertEqual(entry["line"], 3)
        self.assertEqual(entry["raw"], "127.0.0.1\tlocalhost")
        self.assertEqual(entry["ip"], "127.0.0.1")
        self.assertEqual(entry["hostname"], "localhost")
        self.assertEqual(entry["aliases"], [])

    def test_ipv6_with_multiple_aliases(self):
        entry = hosts_file.parse(self.path)[1]
        self.assertEqual(entry["ip"], "::1")
        self.assertEqual(entry["hostname"], "localhost")
        self.assertEqual(entry["aliases"], ["ip6-localhost", "ip6-loopback"])

    def test_inline_comment_is_stripped_from_raw(self):
        entry = hosts_file.parse(self.path)[2]
        self.assertEqual(entry["line"], 5)
        self.assertEqual(entry["ip"], "192.0.2.1")
        self.assertEqual(entry["hostname"], "blackhole.example")
        self.assertEqual(entry["raw"], "192.0.2.1 blackhole.example")
        self.assertNotIn("#", entry["raw"])

    def test_ip_only_line_has_no_hostname(self):
        entry = hosts_file.parse(self.path)[3]
        self.assertEqual(entry["ip"], "10.0.0.5")
        self.assertIsNone(entry["hostname"])
        self.assertEqual(entry["aliases"], [])

    def test_zone_scoped_ipv6(self):
        entry = hosts_file.parse(self.path)[4]
        self.assertEqual(entry["ip"], "fe80::1%eth0")
        self.assertEqual(entry["hostname"], "linklocal6")

    def test_trailing_newline_stripped_from_raw(self):
        for entry in hosts_file.parse(self.path):
            self.assertFalse(entry["raw"].endswith("\n"))
            self.assertFalse(entry["raw"].endswith("\r"))

    def test_crlf_line_endings(self):
        handle = tempfile.NamedTemporaryFile("wb", suffix=".hosts", delete=False)
        handle.write(b"127.0.0.1 localhost\r\n# comment\r\n")
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        entries = hosts_file.parse(handle.name)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["hostname"], "localhost")
        self.assertEqual(entries[0]["raw"], "127.0.0.1 localhost")

    def test_missing_file_raises_oserror(self):
        with self.assertRaises(OSError):
            hosts_file.parse(os.path.join(tempfile.gettempdir(), "does-not-exist-xyz"))

    def test_default_path_is_configurable(self):
        self.assertIsInstance(hosts_file.DEFAULT_HOSTS_FILE, str)
        self.assertTrue(hosts_file.DEFAULT_HOSTS_FILE)


if __name__ == "__main__":
    unittest.main()

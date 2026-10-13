"""Parsing of the operating-system hosts file (SPEC FR1/FR2).

Only the standard library is used. The parser is deliberately forgiving: blank
lines and full-line comments are skipped, inline ``#`` comments are stripped,
and lines with only an IP (no hostname) are still returned.
"""

from __future__ import annotations

import os

if os.name == "nt":  # pragma: no cover - platform dependent
    _SYSTEM_ROOT = os.environ.get("SystemRoot", r"C:\Windows")
    DEFAULT_HOSTS_FILE = os.path.join(
        _SYSTEM_ROOT, "System32", "drivers", "etc", "hosts"
    )
else:
    DEFAULT_HOSTS_FILE = "/etc/hosts"

COMMENT_CHAR = "#"


def parse(path=DEFAULT_HOSTS_FILE):
    """Parse a hosts file into a list of entry dictionaries.

    Each entry has the keys:

    ``line``      1-based line number in the original file
    ``raw``       the original line with the trailing newline and any inline
                  comment removed
    ``ip``        the address field
    ``hostname``  the first hostname, or ``None`` if the line has only an IP
    ``aliases``   the remaining hostnames (possibly empty)

    Entries preserve the order in which they appear in the file. Raises
    ``OSError`` (e.g. ``FileNotFoundError``) if the file cannot be read.
    """
    entries = []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.rstrip("\r\n")
            if not line.strip():
                continue  # blank or whitespace-only line
            content = line.split(COMMENT_CHAR, 1)[0]
            fields = content.split()
            if not fields:
                continue  # full-line (or indented) comment
            hostnames = fields[1:]
            entries.append(
                {
                    "line": line_number,
                    "raw": content.rstrip(),
                    "ip": fields[0],
                    "hostname": hostnames[0] if hostnames else None,
                    "aliases": hostnames[1:],
                }
            )
    return entries

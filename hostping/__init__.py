"""hostping -- a local, stdlib-only hosts-file reachability dashboard.

Reads the operating-system hosts file, probes each entry for reachability and
serves the results to a small static frontend over HTTP.

The package depends only on the Python standard library.
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = ["__version__"]

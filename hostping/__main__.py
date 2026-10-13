"""CLI entrypoint: ``python3 -m hostping`` (SPEC §9)."""

from __future__ import annotations

import argparse
import math
import sys
import webbrowser

from . import __version__
from .hosts_file import DEFAULT_HOSTS_FILE
from .server import DEFAULT_BIND, DEFAULT_PORT, DEFAULT_TIMEOUT, create_server


def _display_host(bind):
    if bind in ("", "0.0.0.0", "::"):
        return "127.0.0.1"
    return bind


def _format_url(bind, port):
    host = _display_host(bind)
    if ":" in host and not host.startswith("["):
        host = "[%s]" % host
    return "http://%s:%d/" % (host, port)


def _positive_finite_timeout(text):
    """argparse type for ``--timeout``: a finite number greater than 0."""
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be a number greater than 0")
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(
            "must be a finite number greater than 0 (got %r)" % text
        )
    return value


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python3 -m hostping",
        description="Show at a glance which hosts-file entries are reachable.",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help="port to listen on (default: %(default)s)",
    )
    parser.add_argument(
        "--bind", default=DEFAULT_BIND,
        help="address to bind to (default: %(default)s)",
    )
    parser.add_argument(
        "--hosts-file", default=DEFAULT_HOSTS_FILE,
        help="path to the hosts file (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout", type=_positive_finite_timeout, default=DEFAULT_TIMEOUT,
        help="per-check timeout in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="do not open a web browser",
    )
    parser.add_argument(
        "--version", action="version", version="hostping %s" % __version__,
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    url = _format_url(args.bind, args.port)

    try:
        httpd = create_server(
            args.bind, args.port, hosts_file=args.hosts_file, timeout=args.timeout
        )
    except OSError as exc:
        print(
            "hostping: could not start server on %s:%s: %s" % (args.bind, args.port, exc),
            file=sys.stderr,
        )
        return 1

    print("hostping %s" % __version__)
    print("hosts file: %s" % args.hosts_file)
    print("serving on %s (press Ctrl-C to stop)" % url, flush=True)

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001 - headless environments
            print("hostping: could not open a browser: %s" % exc, file=sys.stderr)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    print("\nhostping: stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

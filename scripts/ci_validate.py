"""Offline static/policy validation for the hostping CI pipeline.

Runs a set of dependency-free checks that encode the project rules from
``SPEC.md`` and ``README.md``: stdlib-only Python, a fully offline frontend,
wired-up static assets, the required HTML structure and accessibility labels
(SPEC §6/§7, AC9), and a read-only package that never opens a file for writing.
Every check runs even if an earlier one fails, all failures are printed, and
the process exits non-zero if any of them failed.

The script is deliberately side-effect-free: it reads files, never writes them
and never touches the network. It runs on Python 3.9+ (the stdlib-import scan
is skipped with a note on interpreters older than 3.10, where
``sys.stdlib_module_names`` does not exist).

Usage::

    python3 scripts/ci_validate.py
"""

from __future__ import annotations

import ast
import html.parser
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Directories scanned for the stdlib-only import rule (SPEC §3). The CI helper
#: scripts are listed too, so they are held to the same standard as the package.
PYTHON_TREES = ("hostping", "tests", "scripts")

#: Root modules that are allowed in addition to the stdlib.
LOCAL_ROOTS = frozenset({"hostping"})

#: Characters that make a text-mode ``open()`` mode a write mode.
WRITE_MODE_CHARS = "wax+"

#: ``os.open`` flags that imply a write (SPEC non-goal: hostping only reads).
_FORBIDDEN_OS_FLAGS = ("O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND")

#: Bit mask of the write-ish ``os.open`` flags. The constants are distinct bits,
#: so the sum equals the OR (``getattr`` guards platforms that lack one).
_OS_WRITE_BITS = sum(getattr(os, name, 0) for name in _FORBIDDEN_OS_FLAGS)

#: Absolute or protocol-relative remote URL prefix. ``ws``/``wss`` are included
#: because ``WebSocket`` is explicitly covered below and a remote WebSocket is
#: just as much an offline-frontend violation as an ``https`` fetch.
_REMOTE_URL_PREFIX = r"""(?:(?:https?|wss?):)?//"""

#: URL-bearing positions and JavaScript loaders that must never name a remote
#: origin (SPEC §7: the frontend must work fully offline). The leading word
#: boundary and the optional quote catch unquoted forms such as
#: ``src=https://…`` and ``src=//cdn``.
_EXTERNAL_PATTERNS = (
    (
        "attribute (src/href/srcset/poster/action)",
        re.compile(
            r"""\b(?:src|href|srcset|poster|action)\s*=\s*["']?""" + _REMOTE_URL_PREFIX,
            re.IGNORECASE,
        ),
    ),
    # Case-sensitive on purpose: CSS ``url(`` is lowercase, and matching ``URL(``
    # case-insensitively would mislabel a JavaScript ``new URL(...)`` as CSS.
    ("CSS url()", re.compile(r"""\burl\(\s*["']?""" + _REMOTE_URL_PREFIX)),
    (
        "CSS @import",
        re.compile(r"""@import\s+(?:url\(\s*["']?)?""" + _REMOTE_URL_PREFIX, re.IGNORECASE),
    ),
    (
        "XMLHttpRequest.open()",
        re.compile(
            r"""\.open\s*\(\s*["'`][A-Za-z]+["'`]\s*,\s*["'`]""" + _REMOTE_URL_PREFIX,
            re.IGNORECASE,
        ),
    ),
    (
        "XMLHttpRequest",
        re.compile(r"""\bXMLHttpRequest\b[^\n]*?["'`]""" + _REMOTE_URL_PREFIX, re.IGNORECASE),
    ),
    ("fetch()", re.compile(r"""\bfetch\s*\(\s*["'`]""" + _REMOTE_URL_PREFIX, re.IGNORECASE)),
    (
        "new Worker()/EventSource()/WebSocket()",
        re.compile(
            r"""\bnew\s+(?:Worker|SharedWorker|EventSource|WebSocket)\s*\(\s*"""
            r"""["'`]""" + _REMOTE_URL_PREFIX,
            re.IGNORECASE,
        ),
    ),
    (
        "dynamic import()",
        re.compile(r"""\bimport\s*\(\s*["'`]""" + _REMOTE_URL_PREFIX, re.IGNORECASE),
    ),
)

#: Any absolute URL, or a protocol-relative ``//host`` reference. Used as the
#: catch-all scan over every file under ``web/``. The protocol-relative branch
#: must be preceded by a quote or an inline-URL delimiter (``=``, ``(``, ``,``)
#: and must contain a dotted host, so JavaScript line comments such as
#: ``//app.js is bundled`` or ``// keep the last data`` are not flagged.
_BARE_URL_RE = re.compile(
    r"""(?:https?|wss?)://[^\s"'`<>()\[\]{}\\]+"""
    r"""|(?<=["'`(=,])//[A-Za-z0-9][A-Za-z0-9.\-]*\.[A-Za-z]{2,}[^\s"'`<>()]*""",
    re.IGNORECASE,
)

#: ``data:`` URI attribute values (e.g. the inline SVG favicon) legitimately
#: contain ``http://`` inside their payload. They are masked out before the
#: bare-URL scan so only genuine resource references are reported.
_DATA_URI_ATTR_RE = re.compile(
    r"""(\b(?:src|href|srcset|poster|action|xlink:href)\s*=\s*)(["'])(.*?)\2""",
    re.IGNORECASE | re.DOTALL,
)

#: ``data:`` URIs inside a CSS ``url(...)``, quoted or bare. The payload may
#: contain ``http://www.w3.org/...`` (an SVG XML namespace), so it is masked too.
_DATA_URI_CSS_RE = re.compile(
    r"""(?P<open>url\(\s*)"""
    r"""(?:(?P<quote>["'])(?P<quoted>.*?)(?P=quote)|(?P<bare>[^)"'\s]*))"""
    r"""(?P<close>\s*\))""",
    re.IGNORECASE | re.DOTALL,
)

#: XML/XHTML/SVG namespace URIs: they are identifiers, not resource loads, and
#: occur inside inline ``data:`` SVG payloads. Skipped by the bare-URL scan.
_NAMESPACE_URLS = frozenset(
    {
        "http://www.w3.org/2000/svg",
        "http://www.w3.org/1999/xhtml",
        "http://www.w3.org/1999/xlink",
        "http://www.w3.org/1999/xlink/namespace",
        "http://www.w3.org/2000/xmlns/",
    }
)

#: Asset references ``web/index.html`` must contain (SPEC §6/§7).
_REQUIRED_ASSETS = (
    ("app.js", re.compile(r"""(?:src|href)\s*=\s*["']/app\.js["']""", re.IGNORECASE)),
    ("styles.css", re.compile(r"""(?:src|href)\s*=\s*["']/styles\.css["']""", re.IGNORECASE)),
)

#: ``id`` values the shipped page must expose, each paired with the text label
#: that must accompany its colour in the summary bar (SPEC §7 / AC9: status is
#: never colour-only).
SUMMARY_STATS = (
    ("stat-up", "Up"),
    ("stat-degraded", "Degraded"),
    ("stat-down", "Down"),
    ("stat-unknown", "Unknown"),
    ("stat-total", "Total"),
)


class Reporter:
    """Collects failures so that one broken rule does not hide the others."""

    def __init__(self):
        self.failures = []

    def check(self, condition, category, message):
        """Record ``message`` (under ``category``) unless ``condition`` holds."""
        if not condition:
            self.failures.append((category, message))
        return bool(condition)

    def fail(self, category, message):
        self.failures.append((category, message))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _iter_python_files(relative_tree):
    """Yield every ``.py`` file under ``relative_tree`` (skipping caches)."""
    root = os.path.join(REPO_ROOT, relative_tree)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def _iter_web_files():
    """Yield every file under ``web/``, recursing into subdirectories."""
    root = os.path.join(REPO_ROOT, "web")
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(filenames):
            yield os.path.join(dirpath, name)


def _rel(path):
    return os.path.relpath(path, REPO_ROOT)


def _line_number(text, position):
    return text.count("\n", 0, position) + 1


def _line_text(text, position):
    """Return the full source line containing ``position``."""
    start = text.rfind("\n", 0, position) + 1
    end = text.find("\n", position)
    if end == -1:
        end = len(text)
    return text[start:end]


def _has_id(text, element_id):
    pattern = r"""\bid\s*=\s*["']%s["']""" % re.escape(element_id)
    return re.search(pattern, text, re.IGNORECASE) is not None


def _first_url(text):
    """Return the first absolute/protocol-relative URL in ``text``, if any."""
    match = _BARE_URL_RE.search(text)
    return match.group(0) if match else None


def _mask_data_uris(text):
    """Replace ``data:`` URI values, preserving line numbering.

    Both HTML attribute values (the inline SVG favicon) and CSS ``url(...)``
    values are masked. A ``data:`` payload may itself contain
    ``http://www.w3.org/...`` XML namespace identifiers, so masking the whole
    value keeps them out of the bare-URL scan without hiding genuine
    external references.
    """

    def _replace_attr(match):
        prefix, quote, value = match.group(1), match.group(2), match.group(3)
        if not value.lstrip().lower().startswith("data:"):
            return match.group(0)
        # Keep any embedded newlines so reported line numbers stay accurate.
        return "%s%sdata:REDACTED%s%s" % (prefix, quote, "\n" * value.count("\n"), quote)

    def _replace_css(match):
        value = match.group("quoted")
        if value is None:
            value = match.group("bare")
        if value is None or not value.lstrip().lower().startswith("data:"):
            return match.group(0)
        quote = match.group("quote") or ""
        masked = "data:REDACTED" + "\n" * value.count("\n")
        return "%s%s%s%s%s" % (
            match.group("open"),
            quote,
            masked,
            quote,
            match.group("close"),
        )

    text = _DATA_URI_ATTR_RE.sub(_replace_attr, text)
    return _DATA_URI_CSS_RE.sub(_replace_css, text)


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


def check_stdlib_only(reporter):
    """SPEC §3: no third-party Python packages anywhere in the codebase."""
    category = "stdlib-only"
    stdlib = getattr(sys, "stdlib_module_names", None)
    if stdlib is None:
        print(
            "note: sys.stdlib_module_names is unavailable on this interpreter "
            "(Python < 3.10); skipping the stdlib-only import scan."
        )
        return

    for tree in PYTHON_TREES:
        for path in _iter_python_files(tree):
            name = _rel(path)
            try:
                source = _read_text(path)
            except OSError as exc:
                reporter.fail(category, "%s: could not be read: %s" % (name, exc))
                continue
            try:
                parsed = ast.parse(source, filename=name)
            except SyntaxError as exc:
                reporter.fail(category, "%s: could not be parsed: %s" % (name, exc))
                continue

            for node in ast.walk(parsed):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        _check_root_module(
                            reporter, category, name, node.lineno, alias.name, stdlib
                        )
                elif isinstance(node, ast.ImportFrom):
                    if node.level:  # relative import (e.g. ``from . import checks``)
                        continue
                    if node.module is None:  # pragma: no cover - ``from . import x`` only
                        continue
                    _check_root_module(reporter, category, name, node.lineno, node.module, stdlib)


def _check_root_module(reporter, category, filename, lineno, dotted, stdlib):
    root = dotted.split(".", 1)[0]
    if root in stdlib or root in LOCAL_ROOTS:
        return
    reporter.fail(
        category,
        "%s:%d: imports %r, which is not part of the standard library "
        "(SPEC §3 forbids third-party packages)" % (filename, lineno, dotted),
    )


def check_frontend_offline(reporter):
    """SPEC §7: the frontend must work fully offline (no CDNs/external URLs)."""
    category = "frontend-offline"
    web_files = list(_iter_web_files())
    if not web_files:
        reporter.fail(category, "no files found under web/")
        return

    for path in web_files:
        name = _rel(path)
        try:
            text = _read_text(path)
        except (OSError, UnicodeDecodeError):
            # Binary assets cannot carry textual resource references.
            continue

        seen = set()  # (line number, url) pairs already reported for this file
        for label, pattern in _EXTERNAL_PATTERNS:
            for match in pattern.finditer(text):
                # Take the URL from the whole line so a pattern that stops at
                # the scheme can still be de-duplicated against the bare scan.
                url = _first_url(_line_text(text, match.start())) or match.group(0)
                lineno = _line_number(text, match.start())
                if (lineno, url) in seen:
                    continue
                seen.add((lineno, url))
                reporter.fail(
                    category,
                    "%s:%d: external resource via %s: %r "
                    "(must be bundled locally; SPEC §7)"
                    % (name, lineno, label, match.group(0)),
                )

        masked = _mask_data_uris(text)
        for match in _BARE_URL_RE.finditer(masked):
            url = match.group(0)
            if url.lower() in _NAMESPACE_URLS:
                # XML namespace identifiers, not resource loads.
                continue
            lineno = _line_number(masked, match.start())
            if (lineno, url) in seen:
                continue
            seen.add((lineno, url))
            reporter.fail(
                category,
                "%s:%d: external URL %r (must be bundled locally; SPEC §7)"
                % (name, lineno, url),
            )


def check_html_assets(reporter):
    """SPEC §6: ``web/index.html`` must reference the local static assets."""
    category = "html-assets"
    index = os.path.join(REPO_ROOT, "web", "index.html")
    if not os.path.isfile(index):
        reporter.fail(category, "web/index.html is missing")
        return
    try:
        text = _read_text(index)
    except OSError as exc:
        reporter.fail(category, "web/index.html: could not be read: %s" % exc)
        return

    for asset, pattern in _REQUIRED_ASSETS:
        if not pattern.search(text):
            reporter.fail(
                category,
                "web/index.html does not reference /%s (SPEC §6 static routes)" % asset,
            )
        if not os.path.isfile(os.path.join(REPO_ROOT, "web", asset)):
            reporter.fail(category, "web/%s is referenced but does not exist" % asset)


def check_html_structure(reporter):
    """SPEC §6/§7 + AC9: the page must expose the required DOM structure.

    ``html.parser`` is far too tolerant to prove much on its own, so this check
    also asserts the concrete anchors the frontend relies on: the closing
    ``</html>`` tag, the hosts table id, and every summary stat id paired with a
    literal text label (so status is never communicated by colour alone).
    """
    category = "html-structure"
    path = os.path.join(REPO_ROOT, "web", "index.html")
    if not os.path.isfile(path):
        reporter.fail(category, "web/index.html is missing")
        return
    try:
        text = _read_text(path)
    except OSError as exc:
        reporter.fail(category, "web/index.html: could not be read: %s" % exc)
        return

    # Tolerant well-formedness smoke: only fails if the parser itself raises.
    parser = html.parser.HTMLParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - report whatever the parser raises
        reporter.fail(category, "web/index.html did not parse: %s: %s" % (type(exc).__name__, exc))

    reporter.check(
        re.search(r"</html\s*>", text, re.IGNORECASE) is not None,
        category,
        "web/index.html is missing the </html> end tag",
    )
    reporter.check(
        _has_id(text, "hosts-table"),
        category,
        'web/index.html is missing id="hosts-table" (the hosts table)',
    )

    blocks = re.findall(r"<li\b.*?</li>", text, re.IGNORECASE | re.DOTALL)
    for element_id, label in SUMMARY_STATS:
        block = next((item for item in blocks if _has_id(item, element_id)), None)
        if block is None:
            reporter.fail(
                category,
                "web/index.html is missing the summary stat id=%r" % element_id,
            )
            continue
        if re.search(r"\b%s\b" % re.escape(label), block) is None:
            reporter.fail(
                category,
                "summary stat id=%r is not paired with the text label %r "
                "(AC9: status must never be colour-only)" % (element_id, label),
            )


# -- read-only package -------------------------------------------------------


def _nth_or_keyword_arg(node, keyword_name, index):
    """Return the ``keyword_name`` argument, else the ``index``-th positional.

    ``index`` is 0-based and style-aware: the free functions ``open(file, mode)``
    (and ``builtins.open``/``io.open``) put the mode at index 1, while an
    object/pathlib ``.open(mode)`` puts it at index 0.
    """
    for keyword in node.keywords:
        if keyword.arg == keyword_name:
            return keyword.value
    if len(node.args) > index:
        return node.args[index]
    return None


def _literal_mode(mode_node):
    """Return ``(mode, is_literal)`` for a text-mode ``open()`` call.

    ``None`` with ``is_literal`` true means the mode was omitted (the default
    ``"r"`` read mode applies).
    """
    if mode_node is None:
        return None, True
    if isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str):
        return mode_node.value, True
    return None, False


def _os_flag_names(flags_node):
    """Collect the identifiers referenced by an ``os.open`` flags expression."""
    names = set()
    for child in ast.walk(flags_node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
    return names


def _check_mode_open(reporter, category, filename, node, api, arg_index, conservative=True):
    """Flag a text-mode ``open()`` that is not provably read-only.

    ``arg_index`` locates the mode argument: 1 for ``open``/``builtins.open``/
    ``io.open`` (``open(file, mode)``) and 0 for an object/pathlib
    ``.open(mode)``.

    When ``conservative`` is true a non-literal mode is reported as
    unverifiable. It is set false only for a bare ``name.open(arg)`` whose
    receiver is not a constructed object, because that shape is statically
    indistinguishable from non-file ``.open()`` APIs such as
    ``webbrowser.open(url)``; literal modes are still always judged.
    """
    mode, is_literal = _literal_mode(_nth_or_keyword_arg(node, "mode", arg_index))
    if not is_literal:
        if conservative:
            reporter.fail(
                category,
                "%s:%d: %s uses a non-literal mode; cannot verify it is read-only "
                "(SPEC non-goal: no editing of the hosts file)" % (filename, node.lineno, api),
            )
        return
    if mode is not None and any(char in mode for char in WRITE_MODE_CHARS):
        reporter.fail(
            category,
            "%s:%d: %s uses write mode %r; the hostping package must only read "
            "(SPEC non-goal: no editing of the hosts file)" % (filename, node.lineno, api, mode),
        )


def _check_os_open(reporter, category, filename, node):
    """Flag ``os.open()`` calls whose flags allow writing."""
    flags_node = _nth_or_keyword_arg(node, "flags", 1)
    if flags_node is None:
        return
    offending = sorted(_os_flag_names(flags_node) & set(_FORBIDDEN_OS_FLAGS))
    if offending:
        reporter.fail(
            category,
            "%s:%d: os.open() uses write flag(s) %s; the hostping package must only "
            "read (SPEC non-goal)" % (filename, node.lineno, ", ".join(offending)),
        )
        return
    if (
        isinstance(flags_node, ast.Constant)
        and isinstance(flags_node.value, int)
        and not isinstance(flags_node.value, bool)
        and flags_node.value & _OS_WRITE_BITS
    ):
        reporter.fail(
            category,
            "%s:%d: os.open() integer flags %d include write bits; the hostping "
            "package must only read (SPEC non-goal)"
            % (filename, node.lineno, flags_node.value),
        )


def _check_write_call(reporter, category, filename, node):
    """Inspect a single ``Call`` node for a potentially writing file API."""
    func = node.func
    if isinstance(func, ast.Name):
        if func.id == "open":
            # ``open(file, mode)``: the mode is the second positional argument.
            _check_mode_open(reporter, category, filename, node, "open()", 1)
        return
    if not isinstance(func, ast.Attribute):
        return

    attr = func.attr
    receiver = func.value.id if isinstance(func.value, ast.Name) else None

    if attr in ("write_text", "write_bytes"):
        reporter.fail(
            category,
            "%s:%d: .%s() writes to disk; the hostping package must only read "
            "(SPEC non-goal: no editing of the hosts file)" % (filename, node.lineno, attr),
        )
        return
    if attr != "open":
        return
    if receiver == "os":
        _check_os_open(reporter, category, filename, node)
        return
    if receiver == "io":
        api, arg_index, conservative = "io.open()", 1, True
    elif receiver == "builtins":
        api, arg_index, conservative = "builtins.open()", 1, True
    else:
        # e.g. pathlib.Path(...).open(...) or another object's .open(mode): the
        # mode is the FIRST positional argument. A constructed receiver is
        # unambiguously a file; a bare ``x.open(arg)`` is ambiguous (e.g.
        # ``webbrowser.open(url)``), so only its literal modes are judged.
        api, arg_index = "<object>.open()", 0
        conservative = isinstance(func.value, ast.Call)
    _check_mode_open(reporter, category, filename, node, api, arg_index, conservative)


def check_read_only(reporter):
    """SPEC non-goal: no module in the hostping package may write files.

    Every module under ``hostping/`` is scanned (not just ``hosts_file.py``) for
    file APIs that can write: builtin/``builtins``/``io`` ``open()``,
    ``os.open()`` write flags, and ``pathlib``/object ``.open``/``write_text``/
    ``write_bytes``. The CI helper scripts are deliberately excluded: the smoke
    test legitimately creates its temporary hosts fixture.

    Not covered by design (indirect writers): ``os.fdopen``, ``shutil`` helpers,
    ``os.replace``/``os.remove``/``os.rename``, ``logging.FileHandler`` and other
    wrappers. The smoke test's byte-for-byte comparison of the hosts fixture
    remains the authoritative end-to-end guarantee that the file is untouched.
    """
    category = "read-only"
    modules = list(_iter_python_files("hostping"))
    if not modules:
        reporter.fail(category, "no modules found under hostping/")
        return

    for path in modules:
        name = _rel(path)
        try:
            source = _read_text(path)
        except OSError as exc:
            reporter.fail(category, "%s: could not be read: %s" % (name, exc))
            continue
        try:
            parsed = ast.parse(source, filename=name)
        except SyntaxError as exc:
            reporter.fail(category, "%s: could not be parsed: %s" % (name, exc))
            continue
        for node in ast.walk(parsed):
            if isinstance(node, ast.Call):
                _check_write_call(reporter, category, name, node)


CHECKS = (
    ("stdlib-only (SPEC §3)", check_stdlib_only),
    ("frontend offline (SPEC §7)", check_frontend_offline),
    ("HTML asset wiring (SPEC §6)", check_html_assets),
    ("HTML structure + accessibility labels (SPEC §6/§7, AC9)", check_html_structure),
    ("package read-only (SPEC non-goal)", check_read_only),
)


def main():
    reporter = Reporter()
    for label, check in CHECKS:
        print("==> %s" % label)
        check(reporter)

    if reporter.failures:
        print("")
        print("FAILED: %d problem(s) found:" % len(reporter.failures))
        for category, message in reporter.failures:
            print("  - [%s] %s" % (category, message))
        return 1

    print("")
    print("OK: all %d checks passed" % len(CHECKS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

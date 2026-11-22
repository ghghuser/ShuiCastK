#!/usr/bin/env python3
"""
patchlib.py - shared helpers for the build-time patch scripts in tools/.

Not run by itself. Each patch script does:

    import patchlib as P
    TAG = "altacast_xxx"
    P.init(TAG)                  # reads the source dir from the command line
    SRC = P.SRC
    from patchlib import fail, sub_once, after, patch_file, c_wide

Rules every patch follows (see also the notes in each script):
  * files are read / written as latin-1, line endings (CRLF / LF) are kept
  * inserted C++ is ASCII only (Japanese text is written as \\uXXXX wide-char escapes)
  * sub_once() stops with an error unless the pattern matches exactly once
  * every patch is idempotent: a marker string (TAG + ": name") says "already applied"
"""
import re
import sys
from pathlib import Path

TAG = "patch"
SRC = Path(".")


def init(tag):
    """Set the tag used in messages and read the source directory (argv[1])."""
    global TAG, SRC
    TAG = tag
    if len(sys.argv) != 2:
        fail("usage: %s.py <source dir>" % tag)
    SRC = Path(sys.argv[1])
    if not SRC.is_dir():
        fail("source dir not found: " + str(SRC))
    return SRC


def fail(msg):
    print(TAG + ": ERROR: " + msg, file=sys.stderr)
    sys.exit(1)


def sub_n(text, pattern, repl, label, count=1, flags=0):
    """re.sub that must replace exactly `count` times."""
    new, n = re.subn(pattern, repl, text, flags=flags)
    if n != count:
        fail("%s: expected exactly %d match(es), found %d" % (label, count, n))
    return new


def sub_once(text, pattern, repl, label, flags=0):
    new, n = re.subn(pattern, repl, text, flags=flags)
    if n != 1:
        fail("%s: expected exactly 1 match, found %d" % (label, n))
    return new


def after(text, pattern, insert, label):
    """insert text right after the (single) match of pattern"""
    return sub_once(text, pattern, lambda m: m.group(0) + insert, label)


def patch_file(rel, func):
    """Apply func(text) -> text to SRC/rel. Nothing is written if the text is unchanged."""
    path = SRC / rel
    if not path.exists():
        fail("%s not found under %s" % (rel, SRC))
    raw = path.read_bytes()
    crlf = b"\r\n" in raw
    text = raw.decode("latin-1").replace("\r\n", "\n")
    new = func(text)
    if new == text:
        return
    if crlf:
        new = new.replace("\n", "\r\n")
    path.write_bytes(new.encode("latin-1"))
    print(TAG + ": patched " + rel)


def c_wide(s):
    """Python str -> body of a C++ wide string literal, ASCII only (\\uXXXX escapes)."""
    out = []
    for ch in s:
        if ch == "\n":
            out.append("\\n")
        elif ch in '"\\':
            out.append("\\" + ch)
        elif ord(ch) < 128:
            out.append(ch)
        else:
            out.append("\\u%04x" % ord(ch))
    return "".join(out)

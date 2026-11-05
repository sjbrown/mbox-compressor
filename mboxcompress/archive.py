"""Reading and writing the per-message .txt files.

A message file is RFC822-shaped: one header per line, a blank line, then the
body. Headers are already decoded and unfolded, so a header line never
contains a newline and the first blank line always ends the header block.
"""

import os
import re

STEM_RE = re.compile(r"^\d{8}T\d{6}-[0-9a-f]+$")


def format_message(headers, body):
    """headers: list of (name, value) pairs. Repeated names are allowed."""
    lines = []
    for name, value in headers:
        value = " ".join(str(value).split())
        lines.append(f"{name}: {value}")
    return "\n".join(lines) + "\n\n" + body.rstrip("\n") + "\n"


def parse_message(text):
    head, sep, body = text.partition("\n\n")
    if not sep:
        head, body = text, ""
    headers = []
    for line in head.split("\n"):
        name, colon, value = line.partition(":")
        if colon:
            headers.append((name, value.strip()))
    return headers, body.rstrip("\n")


def read_message(path):
    with open(path, encoding="utf-8") as f:
        return parse_message(f.read())


def header_values(headers, name):
    name = name.lower()
    return [v for n, v in headers if n.lower() == name]


def iter_message_files(root):
    """Yield paths of message .txt files under root, oldest first.

    Filenames start with the timestamp, so a sort on the basename is a sort
    on time even across year folders.
    """
    found = []
    for year in os.listdir(root):
        ydir = os.path.join(root, year)
        if not (year.isdigit() and os.path.isdir(ydir)):
            continue
        for name in os.listdir(ydir):
            stem, ext = os.path.splitext(name)
            if ext == ".txt" and STEM_RE.match(stem):
                found.append((name, os.path.join(ydir, name)))
    found.sort()
    return [path for _, path in found]

"""Pass 2: elide quoted text that already appears in an earlier message.

Reads an archive produced by `split` and writes a new archive. Messages are
processed oldest first. Every sentence of every message is hashed into an
index that maps it to the first message it appeared in. In each later message,
quoted paragraphs whose sentences are mostly in the index are replaced by a
one-line pointer to the message(s) they came from.

What counts as quoted:
  * lines starting with '>' (any depth), and
  * everything after an attribution/separator line ("On ... wrote:",
    "-----Original Message-----", an Outlook From:/Sent:/Subject: block, ...)
    when the lines after it don't use '>' markers.

Unquoted text is never elided, so signatures and new content survive.
Messages that interleave replies between quoted fragments are left alone.
"""

import hashlib
import os
import re
import shutil

from .archive import format_message, iter_message_files, parse_message

QUOTE_RE = re.compile(r"^[ \t]*>")
QUOTE_PREFIX_RE = re.compile(r"^(?:[ \t]*>)+[ \t]?")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
NONWORD_RE = re.compile(r"[\W_]+")

WROTE_RE = re.compile(
    r"(wrote|writes|a écrit|schrieb|escribió|ha scritto|schreef|skrev|написал[а]?)\s*:?\s*$",
    re.IGNORECASE)
SEPARATOR_RES = [
    re.compile(r"^-{2,}\s*original message\s*-{2,}$", re.IGNORECASE),
    re.compile(r"^-{2,}\s*forwarded message\s*-{2,}$", re.IGNORECASE),
    re.compile(r"^begin forwarded message:?$", re.IGNORECASE),
    re.compile(r"^_{10,}$"),
]

MIN_SENTENCE_CHARS = 24


def _strip_quote(line):
    return QUOTE_PREFIX_RE.sub("", line)


def is_attribution(lines, i):
    """True if lines[i] starts an attribution or quoted-header block."""
    line = _strip_quote(lines[i]).strip()
    if not line:
        return False
    low = line.lower()
    if any(r.match(line) for r in SEPARATOR_RES):
        return True
    if WROTE_RE.search(line) and len(line) < 300:
        return True
    nxt = _strip_quote(lines[i + 1]).strip() if i + 1 < len(lines) else ""
    if (low.startswith(("on ", "le ", "am ", "el ", "il ", "op "))
            and WROTE_RE.search(nxt) and len(line) + len(nxt) < 300):
        return True
    if low.startswith("from:"):
        window = [_strip_quote(l).strip().lower() for l in lines[i + 1:i + 7]]
        if (any(w.startswith(("sent:", "date:")) for w in window)
                and any(w.startswith("subject:") for w in window)):
            return True
    return False


def classify_lines(lines):
    """Return (quoted, attribution) flag lists, one entry per line."""
    n = len(lines)
    quoted = [bool(QUOTE_RE.match(l)) for l in lines]
    attribution = [False] * n
    i = 0
    while i < n:
        if not is_attribution(lines, i):
            i += 1
            continue
        attribution[i] = True
        if i + 1 < n and WROTE_RE.search(lines[i + 1]) and not WROTE_RE.search(lines[i]):
            attribution[i + 1] = True
        if quoted[i]:
            i += 1
            continue
        following = [j for j in range(i + 1, min(n, i + 8)) if lines[j].strip()
                     and not attribution[j]]
        if following and quoted[following[0]]:
            i += 1
            continue
        for j in range(i + 1, n):
            quoted[j] = True
        break
    return quoted, attribution


def sentence_keys(text):
    keys = []
    for s in SENTENCE_SPLIT_RE.split(text):
        k = NONWORD_RE.sub("", s.lower())
        if len(k) >= MIN_SENTENCE_CHARS:
            keys.append(k)
    return keys


def hash_key(key):
    return int.from_bytes(hashlib.blake2b(key.encode(), digest_size=8).digest(), "big")


class Paragraph:
    __slots__ = ("start", "end", "quoted", "attribution", "hashes", "text_len")

    def __init__(self, start, end, quoted, attribution, text):
        self.start = start
        self.end = end
        self.quoted = quoted
        self.attribution = attribution
        keys = sentence_keys(text)
        self.hashes = [(hash_key(k), len(k)) for k in keys]
        self.text_len = len(text.strip())


def paragraphs(lines):
    quoted, attribution = classify_lines(lines)
    out = []
    i, n = 0, len(lines)
    while i < n:
        if not _strip_quote(lines[i]).strip():
            i += 1
            continue
        if attribution[i]:
            j = i + 1
            while j < n and attribution[j]:
                j += 1
            text = " ".join(_strip_quote(l).strip() for l in lines[i:j])
            out.append(Paragraph(i, j, quoted[i], True, text))
            i = j
            continue
        j = i
        while (j < n and quoted[j] == quoted[i] and not attribution[j]
               and _strip_quote(lines[j]).strip()):
            j += 1
        text = " ".join(_strip_quote(l).strip() for l in lines[i:j])
        out.append(Paragraph(i, j, quoted[i], False, text))
        i = j
    return out


def is_interleaved(paras):
    """Two or more returns from quoted text to new, unquoted text."""
    transitions = 0
    in_quote = False
    for p in paras:
        if p.quoted:
            in_quote = True
        elif not p.attribution:
            if in_quote:
                transitions += 1
            in_quote = False
    return transitions >= 2


class Compressor:
    def __init__(self, min_elide_chars=100, match_ratio=0.8):
        self.min_elide_chars = min_elide_chars
        self.match_ratio = match_ratio
        self.index = {}
        self.files = []
        self.stats = {"messages": 0, "compressed": 0, "lines_elided": 0,
                      "bytes_in": 0, "bytes_out": 0}

    def _match(self, para):
        """(matched_chars, {source_idx: chars}) if para counts as seen, else None."""
        total = sum(n for _, n in para.hashes)
        if total == 0:
            return None
        sources = {}
        covered = 0
        for h, n in para.hashes:
            src = self.index.get(h)
            if src is not None:
                covered += n
                sources[src] = sources.get(src, 0) + n
        if covered / total >= self.match_ratio:
            return covered, sources
        return None

    def _runs(self, paras):
        """Yield (first_para, last_para, matched_chars, sources) for elidable runs."""
        run = []

        def flush():
            matched = [(p, m) for p, m in run if m is not None]
            if matched:
                chars = sum(m[0] for _, m in matched)
                if chars >= self.min_elide_chars:
                    sources = {}
                    for _, (_, src) in matched:
                        for k, v in src.items():
                            sources[k] = sources.get(k, 0) + v
                    yield matched[0][0], matched[-1][0], chars, sources
            run.clear()

        for p in paras:
            if not p.quoted and not p.attribution:
                yield from flush()
                continue
            m = self._match(p)
            if m is None and p.hashes and not p.attribution:
                yield from flush()
                continue
            run.append((p, m))
        yield from flush()

    def compress_body(self, body, here_dir):
        """Return (new_body, quoted_from_paths, lines_elided)."""
        lines = body.split("\n")
        paras = paragraphs(lines)
        if is_interleaved(paras):
            return body, [], 0
        replacements = []
        refs = []
        for first, last, _, sources in self._runs(paras):
            ranked = sorted(sources.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
            paths = [os.path.relpath(self.files[i], here_dir) for i, _ in ranked]
            paths = ["./" + p if not p.startswith(".") else p for p in paths]
            for p in paths:
                if p not in refs:
                    refs.append(p)
            n = last.end - first.start
            prefix = re.match(r"^(?:[ \t]*>)*[ \t]?", lines[first.start]).group(0)
            noun = "line" if n == 1 else "lines"
            marker = f"{prefix}[elided {n} quoted {noun}; see {', '.join(paths)}]"
            replacements.append((first.start, last.end, marker))
        if not replacements:
            return body, [], 0
        out = []
        pos = 0
        elided = 0
        for start, end, marker in replacements:
            out.extend(lines[pos:start])
            out.append(marker)
            elided += end - start
            pos = end
        out.extend(lines[pos:])
        return "\n".join(out), refs, elided

    def add_to_index(self, body, file_idx):
        for p in paragraphs(body.split("\n")):
            for h, _ in p.hashes:
                self.index.setdefault(h, file_idx)

    def process(self, rel_path, headers, body):
        here_dir = os.path.dirname(rel_path)
        idx = len(self.files)
        self.files.append(rel_path)
        new_body, refs, elided = self.compress_body(body, here_dir)
        self.add_to_index(body, idx)
        self.stats["messages"] += 1
        if elided:
            self.stats["compressed"] += 1
            self.stats["lines_elided"] += elided
            headers = list(headers) + [("X-Quoted-From", r) for r in refs]
        return headers, new_body


def _link_or_copy(src, dst):
    if os.path.exists(dst):
        if os.path.samefile(src, dst):
            return
        os.remove(dst)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _mirror_tree(src_dir, dst_dir):
    for root, _, files in os.walk(src_dir):
        rel = os.path.relpath(root, src_dir)
        target = os.path.join(dst_dir, rel)
        os.makedirs(target, exist_ok=True)
        for name in files:
            _link_or_copy(os.path.join(root, name), os.path.join(target, name))


def run(in_root, out_root, min_elide_chars=100, match_ratio=0.8):
    in_abs, out_abs = os.path.abspath(in_root), os.path.abspath(out_root)
    if out_abs == in_abs or out_abs.startswith(in_abs + os.sep):
        raise ValueError("output folder must be outside the input folder")
    comp = Compressor(min_elide_chars=min_elide_chars, match_ratio=match_ratio)
    for path in iter_message_files(in_root):
        rel = os.path.relpath(path, in_root)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        headers, body = parse_message(text)
        headers, body = comp.process(rel, headers, body)
        out_text = format_message(headers, body)
        dst = os.path.join(out_root, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(out_text)
        comp.stats["bytes_in"] += len(text.encode("utf-8"))
        comp.stats["bytes_out"] += len(out_text.encode("utf-8"))
    for entry in os.listdir(in_root):
        src = os.path.join(in_root, entry)
        if not os.path.isdir(src):
            continue
        if entry == "errors":
            _mirror_tree(src, os.path.join(out_root, entry))
        elif entry.isdigit():
            for sub in ("html_bodies", "attachments"):
                if os.path.isdir(os.path.join(src, sub)):
                    _mirror_tree(os.path.join(src, sub), os.path.join(out_root, entry, sub))
    return comp.stats

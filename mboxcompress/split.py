"""Pass 1: explode an mbox into one text file per message.

Layout produced under the output root:

    YYYY/20260101T123456-af38e.txt
    YYYY/html_bodies/20260101T123456-af38e.html
    YYYY/attachments/3f9a0c1b2d-invoice.pdf
    errors/<n>.eml          messages that could not be converted

Attachments are named by content hash, so the same file sent many times in
one year is stored once.
"""

import datetime
import email
import email.header
import email.policy
import email.utils
import hashlib
import mailbox
import mimetypes
import os
import re
import sys
from html.parser import HTMLParser

from .archive import format_message, install_viewer

KEEP_HEADERS = [
    "From", "To", "Cc", "Bcc", "Reply-To", "Date", "Subject",
    "Message-ID", "In-Reply-To", "List-Id", "X-Gmail-Labels", "X-GM-THRID",
]

INVISIBLE_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u034f\u00ad]")
FROM_LINE_RE = re.compile(
    r"(\w{3}) +(\w{3}) +(\d{1,2}) +(\d{1,2}:\d\d:\d\d)(?: +([+-]\d{4}))? +(\d{4})"
)
UNSAFE_NAME_RE = re.compile(r'[\x00-\x1f/\\:*?"<>|]+')

# Written at the top of every stored HTML body so that opening one directly
# never fetches remote content.
HTML_PREAMBLE = (
    '<meta charset="utf-8">\n'
    '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
    'img-src file: data:; style-src \'unsafe-inline\' file:; font-src file: data:">\n'
)

mimetypes.add_type("message/rfc822", ".eml")
mimetypes.add_type("text/calendar", ".ics")


# ---------------------------------------------------------------- headers

def decode_header_value(header):
    """header is a policy.default header object, or a raw str on fallback."""
    try:
        return str(header)
    except Exception:
        pass
    try:
        return str(email.header.make_header(email.header.decode_header(str(header))))
    except Exception:
        return str(header)


def kept_headers(msg, keep):
    by_name = {}
    for name, raw in msg.raw_items():
        if name.lower() not in keep:
            continue
        try:
            value = msg.policy.header_factory(name, raw)
        except Exception:
            value = raw
        by_name.setdefault(name.lower(), []).append(decode_header_value(value))
    out = []
    for name in keep.values():
        for value in by_name.get(name.lower(), []):
            out.append((name, value))
    return out


# ---------------------------------------------------------------- dates

def _sane(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    if dt.year < 1971 or dt > now + datetime.timedelta(days=2):
        return None
    return dt


def _parse_rfc_date(value):
    try:
        return _sane(email.utils.parsedate_to_datetime(value.strip()))
    except Exception:
        return None


def _parse_from_line(from_line):
    m = FROM_LINE_RE.search(from_line or "")
    if not m:
        return None
    _, mon, day, hms, off, year = m.groups()
    try:
        dt = datetime.datetime.strptime(f"{day} {mon} {year} {hms}", "%d %b %Y %H:%M:%S")
        if off:
            dt = dt.replace(tzinfo=datetime.datetime.strptime(off, "%z").tzinfo)
        return _sane(dt)
    except ValueError:
        return None


def message_datetime(msg, from_line, tz):
    """Date header, else the newest Received hop, else the mbox From_ line.

    The result is converted to tz, or to the system's local zone if tz is
    None. Returns None if no source gives a usable date.
    """
    dt = None
    for name, raw in msg.raw_items():
        if name.lower() == "date":
            dt = _parse_rfc_date(raw)
            break
    if dt is None:
        for name, raw in msg.raw_items():
            if name.lower() == "received" and ";" in raw:
                dt = _parse_rfc_date(raw.rsplit(";", 1)[1])
                break
    if dt is None:
        dt = _parse_from_line(from_line)
    if dt is None:
        return None
    return dt.astimezone(tz) if tz else dt.astimezone()


# ---------------------------------------------------------------- bodies

class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
             "blockquote", "table", "ul", "ol", "pre", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head", "title"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head", "title"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(re.sub(r"[ \t\r\n]+", " ", data))


def html_to_text(html, keep_links=False):
    try:
        import html2text
        h = html2text.HTML2Text()
        h.body_width = 0
        h.ignore_images = True
        h.ignore_emphasis = True
        h.ignore_links = not keep_links
        h.unicode_snob = True
        return h.handle(html)
    except Exception:
        p = _TextExtractor()
        try:
            p.feed(html)
            p.close()
        except Exception:
            pass
        return "".join(p.out)


def clean_text(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = INVISIBLE_RE.sub("", text).replace("\u00a0", " ")
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n")


def part_text(part):
    try:
        content = part.get_content()
        if isinstance(content, str):
            return content
    except Exception:
        pass
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def leaves(part):
    """Non-multipart parts, without descending into attached messages."""
    if part.get_content_maintype() == "multipart":
        for sub in part.iter_parts():
            yield from leaves(sub)
    else:
        yield part


def part_bytes(part):
    if part.get_content_maintype() == "message":
        payload = part.get_payload()
        if isinstance(payload, list):
            return b"\n".join(bytes(p) for p in payload)
    data = part.get_payload(decode=True)
    if data is None:
        payload = part.get_payload()
        data = payload.encode("utf-8", "replace") if isinstance(payload, str) else b""
    return data


def attachment_name(part):
    ctype = part.get_content_type()
    try:
        filename = part.get_filename()
    except Exception:
        filename = None
    guessed = mimetypes.guess_extension(ctype) or ".bin"
    if filename:
        name = UNSAFE_NAME_RE.sub("_", filename).strip(" .") or "attachment"
        base, ext = os.path.splitext(name)
        if not ext:
            ext = guessed
    else:
        base, ext = "attachment", guessed
    return base[:80] + ext[:16]


def _is_attachment(part):
    try:
        if part.is_attachment():
            return True
        return part.get_filename() is not None
    except Exception:
        return False


# ---------------------------------------------------------------- writer

class Splitter:
    def __init__(self, out_root, tz=None, keep_links=False, extra_headers=()):
        self.out_root = out_root
        self.tz = tz
        self.keep_links = keep_links
        keep = KEEP_HEADERS + [h for h in extra_headers if h.lower() not in
                               {k.lower() for k in KEEP_HEADERS}]
        self.keep = {h.lower(): h for h in keep}
        self.seen = {}
        self.stats = {"messages": 0, "duplicates": 0, "errors": 0,
                      "attachments": 0, "attachments_deduped": 0, "html": 0}

    def _stem(self, dt, msgid, raw):
        prefix = dt.strftime("%Y%m%dT%H%M%S") if dt else "19700101T000000"
        key = (msgid or "").strip() or None
        digest = hashlib.sha1((key or "").encode() if key else raw).hexdigest()
        stem = f"{prefix}-{digest[:5]}"
        if stem not in self.seen:
            self.seen[stem] = key
            return stem
        if key is not None and self.seen[stem] == key:
            return None
        raw_digest = hashlib.sha1(raw).hexdigest()
        for n in (8, 12, 40):
            stem = f"{prefix}-{raw_digest[:n]}"
            if stem not in self.seen:
                self.seen[stem] = key
                return stem
        return None

    def _write_attachment(self, year_dir, part):
        data = part_bytes(part)
        name = f"{hashlib.sha256(data).hexdigest()[:10]}-{attachment_name(part)}"
        adir = os.path.join(year_dir, "attachments")
        path = os.path.join(adir, name)
        if os.path.exists(path):
            self.stats["attachments_deduped"] += 1
        else:
            os.makedirs(adir, exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
        self.stats["attachments"] += 1
        return name, part.get_content_type(), len(data)

    def convert(self, raw, from_line=""):
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        dt = message_datetime(msg, from_line, self.tz)
        msgid = None
        for name, value in msg.raw_items():
            if name.lower() == "message-id":
                msgid = value
                break
        stem = self._stem(dt, msgid, raw)
        if stem is None:
            self.stats["duplicates"] += 1
            return None
        year_dir = os.path.join(self.out_root, stem[:4])
        os.makedirs(year_dir, exist_ok=True)

        try:
            plain = msg.get_body(preferencelist=("plain",))
        except Exception:
            plain = None
        try:
            html = msg.get_body(preferencelist=("html",))
        except Exception:
            html = None

        texts = [part_text(plain)] if plain is not None else []
        attachments = []
        cids = {}
        for part in leaves(msg):
            if part is plain or part is html:
                continue
            if part.get_content_type() == "text/plain" and not _is_attachment(part):
                texts.append(part_text(part))
                continue
            name, ctype, size = self._write_attachment(year_dir, part)
            attachments.append((name, ctype, size))
            cid = part.get("Content-ID")
            if cid:
                cids[str(cid).strip().strip("<>")] = name

        headers = kept_headers(msg, self.keep)
        body = clean_text("\n\n".join(t for t in texts if t.strip()))

        if html is not None:
            html_src = part_text(html)
            for cid, name in cids.items():
                html_src = re.sub(r"cid:" + re.escape(cid), "../attachments/" + name,
                                  html_src, flags=re.IGNORECASE)
            hdir = os.path.join(year_dir, "html_bodies")
            os.makedirs(hdir, exist_ok=True)
            with open(os.path.join(hdir, stem + ".html"), "w", encoding="utf-8") as f:
                f.write(HTML_PREAMBLE + html_src)
            headers.append(("X-HTML-Body", f"./html_bodies/{stem}.html"))
            self.stats["html"] += 1
            if not body.strip():
                body = clean_text(html_to_text(html_src, self.keep_links))
                headers.append(("X-Body-Derived", "html"))

        for name, ctype, size in attachments:
            headers.append(("X-Attachment", f"./attachments/{name} ({ctype}, {size} bytes)"))

        with open(os.path.join(year_dir, stem + ".txt"), "w", encoding="utf-8") as f:
            f.write(format_message(headers, body))
        self.stats["messages"] += 1
        return stem

    def save_error(self, raw):
        edir = os.path.join(self.out_root, "errors")
        os.makedirs(edir, exist_ok=True)
        self.stats["errors"] += 1
        with open(os.path.join(edir, f"{self.stats['errors']:06d}.eml"), "wb") as f:
            f.write(raw)


def iter_mbox(path):
    """Yield (from_line, raw_bytes) for each message in the mbox."""
    mb = mailbox.mbox(path, create=False)
    for key in mb.iterkeys():
        raw = mb.get_bytes(key, from_=True)
        from_line, _, raw = raw.partition(b"\n")
        yield from_line.decode("ascii", "replace"), raw


def run(mbox_paths, out_root, tz=None, keep_links=False, extra_headers=(), progress=True):
    splitter = Splitter(out_root, tz=tz, keep_links=keep_links, extra_headers=extra_headers)
    os.makedirs(out_root, exist_ok=True)
    for path in mbox_paths:
        for from_line, raw in iter_mbox(path):
            try:
                splitter.convert(raw, from_line)
            except Exception as e:
                print(f"error: {e!r}", file=sys.stderr)
                splitter.save_error(raw)
            n = splitter.stats["messages"]
            if progress and n and n % 1000 == 0:
                print(f"{n} messages", file=sys.stderr)
    install_viewer(out_root)
    return splitter.stats

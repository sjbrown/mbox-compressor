"""Recompose one archived message into a standalone HTML page.

The page shows the kept headers, the attachments (images inline), the HTML
body if there is one, and the plain-text body. Elision markers left by
`compress` become links, and the referenced messages are rendered inline in
collapsed <details> blocks so the conversation can be read in one place.
"""

import html
import os
import pathlib
import re
import tempfile

from .archive import header_values, read_message

ELIDED_RE = re.compile(r"^(?:\s*>)*\s*\[elided (\d+) quoted lines?; see (.+)\]$")
ATTACH_RE = re.compile(r"^(\S+) \(([^,]+), (\d+) bytes\)$")
SHOWN_HEADERS = ["From", "To", "Cc", "Bcc", "Reply-To", "Date", "Subject", "X-Gmail-Labels"]

STYLE = """
:root { color-scheme: light dark; --muted: #666; --rule: #ccc; --bg2: #f4f4f4; }
@media (prefers-color-scheme: dark) { :root { --muted: #aaa; --rule: #444; --bg2: #222; } }
body { font: 15px/1.45 system-ui, sans-serif; max-width: 60rem; margin: 1rem auto; padding: 0 1rem; }
table.headers td { padding: .1rem .6rem .1rem 0; vertical-align: top; }
table.headers td:first-child { color: var(--muted); text-align: right; white-space: nowrap; }
pre { white-space: pre-wrap; word-wrap: break-word; font: 13px/1.4 ui-monospace, monospace; }
.attachments img { max-width: 14rem; max-height: 10rem; display: block; }
.attachments li { margin: .3rem 0; }
iframe { width: 100%; height: 75vh; border: 1px solid var(--rule); background: white; }
details.quoted { border-left: 3px solid var(--rule); margin: .4rem 0; padding: .2rem .8rem; background: var(--bg2); }
details.quoted > summary { cursor: pointer; color: var(--muted); }
h2 { font-size: 1rem; margin: 1.2rem 0 .4rem; color: var(--muted); }
"""


def _uri(path):
    return pathlib.Path(os.path.abspath(path)).as_uri()


def _resolve(base_dir, rel):
    return os.path.normpath(os.path.join(base_dir, rel))


def render_body(body, base_dir, depth, seen):
    """Plain text body as HTML, expanding elision markers up to depth."""
    out = []
    buf = []

    def flush():
        if buf:
            out.append("<pre>" + html.escape("\n".join(buf)) + "</pre>")
            buf.clear()

    for line in body.split("\n"):
        m = ELIDED_RE.match(line)
        if not m:
            buf.append(line)
            continue
        flush()
        nlines, refs = m.groups()
        for ref in [r.strip() for r in refs.split(",")]:
            path = _resolve(base_dir, ref)
            noun = "line" if nlines == "1" else "lines"
            label = f"{nlines} quoted {noun} elided — {html.escape(ref)}"
            if depth <= 0 or path in seen or not os.path.exists(path):
                out.append(f'<p><a href="{_uri(path)}">[{label}]</a></p>')
                continue
            out.append(f'<details class="quoted"><summary>{label}</summary>'
                       f'<p><small><a href="{_uri(path)}">open message</a></small></p>')
            out.append(render_message(path, depth - 1, seen | {path}, full=False))
            out.append("</details>")
    flush()
    return "\n".join(out)


def render_message(path, depth=3, seen=frozenset(), full=True, remote=False):
    headers, body = read_message(path)
    base_dir = os.path.dirname(path)
    parts = ['<table class="headers">']
    for name in SHOWN_HEADERS:
        for value in header_values(headers, name):
            parts.append(f"<tr><td>{name}</td><td>{html.escape(value)}</td></tr>")
    parts.append("</table>")

    attachments = []
    for value in header_values(headers, "X-Attachment"):
        m = ATTACH_RE.match(value)
        rel, ctype, size = m.groups() if m else (value.split(" ")[0], "", "?")
        attachments.append((_resolve(base_dir, rel), ctype, size))
    if attachments:
        parts.append('<h2>Attachments</h2><ul class="attachments">')
        for apath, ctype, size in attachments:
            name = html.escape(os.path.basename(apath))
            img = (f'<img src="{_uri(apath)}" alt="">' if ctype.startswith("image/") else "")
            parts.append(f'<li><a href="{_uri(apath)}">{img}{name}</a> '
                         f'<small>{html.escape(ctype)}, {size} bytes</small></li>')
        parts.append("</ul>")

    html_rel = header_values(headers, "X-HTML-Body")
    html_path = _resolve(base_dir, html_rel[0]) if html_rel else None
    text_html = render_body(body, base_dir, depth, seen | {os.path.normpath(path)})
    if full and html_path and os.path.exists(html_path):
        with open(html_path, encoding="utf-8", errors="replace") as f:
            src = f.read()
        base = _uri(os.path.dirname(html_path)) + "/"
        csp = ("default-src 'none'; img-src file: data:" + (" http: https:" if remote else "")
               + "; style-src 'unsafe-inline' file:; font-src file: data:")
        srcdoc = (f'<meta http-equiv="Content-Security-Policy" content="{csp}">'
                  f'<base href="{base}" target="_blank">' + src)
        parts.append(f'<h2>HTML body (<a href="{_uri(html_path)}">open</a>)</h2>')
        parts.append(f'<iframe sandbox="allow-popups" srcdoc="{html.escape(srcdoc)}"></iframe>')
        parts.append(f"<details><summary>Plain text</summary>{text_html}</details>")
    else:
        if html_path and full:
            parts.append(f'<p><a href="{_uri(html_path)}">HTML body</a></p>')
        parts.append(text_html)
    return "\n".join(parts)


def write_view(path, out_path=None, depth=3, remote=False):
    path = os.path.abspath(path)
    headers, _ = read_message(path)
    subject = (header_values(headers, "Subject") or [os.path.basename(path)])[0]
    page = ("<!doctype html><html><head><meta charset=\"utf-8\">"
            f"<title>{html.escape(subject)}</title><style>{STYLE}</style></head><body>"
            f"<p><small><a href=\"{_uri(path)}\">{html.escape(path)}</a></small></p>"
            + render_message(path, depth=depth, remote=remote) + "</body></html>")
    if out_path is None:
        out_dir = os.path.join(tempfile.gettempdir(), "mail-view")
        os.makedirs(out_dir, exist_ok=True)
        stem = os.path.splitext(os.path.basename(path))[0]
        out_path = os.path.join(out_dir, stem + ".html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)
    return out_path

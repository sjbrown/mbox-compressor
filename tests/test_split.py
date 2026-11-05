import datetime
import os
from zoneinfo import ZoneInfo

from conftest import make_msg
from mboxcompress import split
from mboxcompress.archive import header_values, iter_message_files, read_message

UTC = datetime.timezone.utc


def run_split(write_mbox, tmp_path, messages, tz=UTC, **kw):
    out = tmp_path / "out"
    stats = split.run([str(write_mbox(messages))], str(out), tz=tz, progress=False, **kw)
    return out, stats


def only_file(out):
    files = iter_message_files(str(out))
    assert len(files) == 1
    return files[0]


def test_plain_message_layout_and_headers(write_mbox, tmp_path):
    m = make_msg(extra_headers=[("DKIM-Signature", "v=1; junk"),
                                ("Received", "from x by y; Mon, 06 Jan 2025 09:59:00 +0000"),
                                ("X-Gmail-Labels", "Inbox,Important")])
    out, _ = run_split(write_mbox, tmp_path, [m])
    path = only_file(out)
    assert os.path.dirname(path) == str(out / "2025")
    assert os.path.basename(path).startswith("20250106T100000-")
    headers, body = read_message(path)
    names = [n for n, _ in headers]
    assert "DKIM-Signature" not in names and "Received" not in names
    assert header_values(headers, "X-Gmail-Labels") == ["Inbox,Important"]
    assert header_values(headers, "Subject") == ["Hi"]
    assert body == "Hello there."


def test_filename_uses_requested_zone(write_mbox, tmp_path):
    out, _ = run_split(write_mbox, tmp_path, [make_msg(date="Wed, 01 Jan 2025 03:00:00 +0000")],
                       tz=ZoneInfo("America/Los_Angeles"))
    path = only_file(out)
    assert os.path.basename(path).startswith("20241231T190000-")
    assert "/2024/" in path


def test_encoded_subject_and_qp_body_decoded(write_mbox, tmp_path):
    m = make_msg(subject="Café ünïcode", body="Naïve résumé " + "x" * 100)
    raw = m.as_bytes()
    assert b"=?utf-8?" in raw.lower() and "résumé".encode() not in raw
    out, _ = run_split(write_mbox, tmp_path, [m])
    headers, body = read_message(only_file(out))
    assert header_values(headers, "Subject") == ["Café ünïcode"]
    assert body.startswith("Naïve résumé")


def test_html_alternative_split_off(write_mbox, tmp_path):
    m = make_msg(body="Plain version.", html="<p>HTML <b>version</b></p>")
    out, _ = run_split(write_mbox, tmp_path, [m])
    path = only_file(out)
    headers, body = read_message(path)
    assert body == "Plain version."
    (link,) = header_values(headers, "X-HTML-Body")
    html_path = os.path.join(os.path.dirname(path), link)
    stored = open(html_path, encoding="utf-8").read()
    assert stored.count("<b>version</b>") == 1
    assert stored.startswith(split.HTML_PREAMBLE)
    assert "default-src 'none'" in split.HTML_PREAMBLE
    assert "<b>" not in open(path, encoding="utf-8").read()
    assert header_values(headers, "X-Body-Derived") == []


def test_html_only_gets_derived_text(write_mbox, tmp_path):
    m = make_msg(body=None, html="<html><style>p{color:red}</style><p>Only <i>html</i> here</p></html>")
    out, _ = run_split(write_mbox, tmp_path, [m])
    headers, body = read_message(only_file(out))
    assert "Only html here" in body
    assert "color:red" not in body and "<" not in body
    assert header_values(headers, "X-Body-Derived") == ["html"]


def test_attachments_typed_linked_and_deduped(write_mbox, tmp_path):
    pdf = b"%PDF-1.4 fake"
    m1 = make_msg(subject="a", attachments=[(pdf, "application", "pdf", "invoice.pdf"),
                                            (b"\x89PNG..", "image", "png", "noext")])
    m2 = make_msg(subject="b", date="Tue, 07 Jan 2025 10:00:00 +0000",
                  attachments=[(pdf, "application", "pdf", "invoice.pdf")])
    out, stats = run_split(write_mbox, tmp_path, [m1, m2])
    files = iter_message_files(str(out))
    headers, _ = read_message(files[0])
    links = header_values(headers, "X-Attachment")
    assert len(links) == 2
    assert links[0].endswith("-invoice.pdf (application/pdf, 13 bytes)")
    assert "-noext.png (image/png" in links[1]
    adir = out / "2025" / "attachments"
    assert sorted(os.listdir(adir))[0].endswith(("invoice.pdf", "noext.png"))
    assert len(os.listdir(adir)) == 2
    assert stats["attachments_deduped"] == 1
    rel = links[0].split(" ")[0]
    assert open(os.path.join(os.path.dirname(files[0]), rel), "rb").read() == pdf


def test_cid_images_rewritten(write_mbox, tmp_path):
    m = make_msg(body="see pic", html='<img src="cid:pic1@x">')
    html_part = m.get_body(("html",))
    html_part.make_related()
    html_part.add_related(b"\x89PNGdata", maintype="image", subtype="png", cid="<pic1@x>")
    out, _ = run_split(write_mbox, tmp_path, [m])
    path = only_file(out)
    headers, _ = read_message(path)
    (link,) = header_values(headers, "X-HTML-Body")
    src = open(os.path.join(os.path.dirname(path), link), encoding="utf-8").read()
    assert "cid:" not in src
    (att,) = header_values(headers, "X-Attachment")
    assert '../attachments/' + os.path.basename(att.split(" ")[0]) in src


def test_attached_message_saved_as_eml(write_mbox, tmp_path):
    inner = make_msg(subject="inner", body="inner body")
    m = make_msg(subject="outer", body="outer body")
    m.add_attachment(inner)
    out, _ = run_split(write_mbox, tmp_path, [m])
    headers, body = read_message(only_file(out))
    assert body == "outer body"
    (att,) = header_values(headers, "X-Attachment")
    assert ".eml (message/rfc822" in att


def test_duplicate_message_ids_skipped(write_mbox, tmp_path):
    m = make_msg(msgid="<same@x>")
    out, stats = run_split(write_mbox, tmp_path, [m, make_msg(msgid="<same@x>")])
    assert stats["duplicates"] == 1
    only_file(out)


def test_same_second_distinct_messages_both_kept(write_mbox, tmp_path):
    out, _ = run_split(write_mbox, tmp_path, [make_msg(msgid="<a@x>"), make_msg(msgid="<b@x>")])
    assert len(iter_message_files(str(out))) == 2


def test_missing_date_falls_back_to_received_then_from_line(write_mbox, tmp_path):
    a = make_msg(date=None, subject="a",
                 extra_headers=[("Received", "by mx; Fri, 03 Jan 2025 08:00:00 +0000")])
    b = make_msg(date="garbage", subject="b")
    out, _ = run_split(write_mbox, tmp_path, [a, b])
    names = sorted(os.path.basename(p) for p in iter_message_files(str(out)))
    assert names[0].startswith("20250103T080000-")
    assert names[1].startswith("20250106T100000-")


def test_invisible_chars_and_trailing_space_cleaned():
    assert split.clean_text("a‌ b  \r\n\r\n\r\n\r\nc") == "a b\n\nc"

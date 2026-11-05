import os

from mboxcompress import compress
from mboxcompress.archive import format_message, header_values, iter_message_files, read_message

P1 = ("We should move the offsite to the second week of March because the venue "
      "is booked solid in February. Let me know if that works for everyone.")
P2 = ("I also talked to the caterer and they can do vegetarian options for the "
      "whole group, which should make planning a lot simpler for us.")
REPLY = "Sounds good to me, March works."
SIG = "Alice Example\nDirector of Things, Example Corp. Call me any time at 555-0100 please."


def wrap(text, width, prefix=""):
    out, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            out.append(prefix + line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    out.append(prefix + line)
    return "\n".join(out)


def build(tmp_path, messages):
    """messages: list of (stem, body). Writes an input archive."""
    root = tmp_path / "in"
    for stem, body in messages:
        d = root / stem[:4]
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{stem}.txt").write_text(format_message([("Subject", stem)], body))
    return root


def run(tmp_path, messages, **kw):
    root = build(tmp_path, messages)
    out = tmp_path / "out"
    stats = compress.run(str(root), str(out), **kw)
    return out, stats


def body_of(out, stem):
    return read_message(str(out / stem[:4] / f"{stem}.txt"))


ORIG = "20240301T100000-aaaaa"
ORIG_BODY = wrap(P1, 72) + "\n\n" + wrap(P2, 72)


def test_top_post_with_rewrapped_quote_is_elided(tmp_path):
    reply = (REPLY + "\n\nOn Fri, Mar 1, 2024 at 10:00 AM Alice <a@x.com> wrote:\n\n"
             + wrap(P1, 50, "> ") + "\n>\n" + wrap(P2, 60, "> "))
    out, stats = run(tmp_path, [(ORIG, ORIG_BODY), ("20240302T100000-bbbbb", reply)])
    headers, body = body_of(out, "20240302T100000-bbbbb")
    assert body.startswith(REPLY)
    assert "wrote:" in body
    assert "venue" not in body
    assert f"[elided " in body and f"see ./{ORIG}.txt]" in body
    assert header_values(headers, "X-Quoted-From") == [f"./{ORIG}.txt"]
    assert stats["compressed"] == 1
    assert body_of(out, ORIG)[1] == ORIG_BODY


def test_bottom_post_is_elided(tmp_path):
    reply = "Alice wrote:\n" + wrap(ORIG_BODY, 70, "> ") + "\n\n" + REPLY
    out, _ = run(tmp_path, [(ORIG, ORIG_BODY), ("20240302T100000-bbbbb", reply)])
    _, body = body_of(out, "20240302T100000-bbbbb")
    assert "venue" not in body and body.endswith(REPLY)


def test_outlook_unmarked_quote_is_elided(tmp_path):
    reply = (REPLY + "\n\n-----Original Message-----\nFrom: Alice\nSent: Friday\n"
             "Subject: offsite\n\n" + wrap(P1, 90) + "\n\n" + wrap(P2, 90))
    out, _ = run(tmp_path, [(ORIG, ORIG_BODY), ("20240302T100000-bbbbb", reply)])
    _, body = body_of(out, "20240302T100000-bbbbb")
    assert "venue" not in body and "Original Message" in body


def test_interleaved_reply_left_alone(tmp_path):
    reply = ("> " + P1 + "\n\nYes, March.\n\n> " + P2 + "\n\nGreat news.")
    out, stats = run(tmp_path, [(ORIG, ORIG_BODY), ("20240302T100000-bbbbb", reply)])
    assert body_of(out, "20240302T100000-bbbbb")[1] == reply
    assert stats["compressed"] == 0


def test_unseen_quote_kept(tmp_path):
    reply = REPLY + "\n\nOn Fri, Bob wrote:\n> " + P1
    out, _ = run(tmp_path, [("20240302T100000-bbbbb", reply)])
    assert body_of(out, "20240302T100000-bbbbb")[1] == reply


def test_unquoted_repeated_signature_not_elided(tmp_path):
    a = "First note about something unrelated entirely.\n\n" + SIG
    b = "Second note on a different topic altogether.\n\n" + SIG
    out, stats = run(tmp_path, [(ORIG, a), ("20240302T100000-bbbbb", b)])
    assert body_of(out, "20240302T100000-bbbbb")[1] == b
    assert stats["compressed"] == 0


def test_short_quote_below_threshold_kept(tmp_path):
    orig = "Can we meet on the twelfth of this month at noon?"
    reply = "Sure.\n\n> " + orig
    out, _ = run(tmp_path, [(ORIG, orig), ("20240302T100000-bbbbb", reply)])
    assert body_of(out, "20240302T100000-bbbbb")[1] == reply


def test_partially_new_quote_kept(tmp_path):
    novel = ("This sentence was never in any earlier message that the archive has seen. "
             "Neither was this second sentence, which is also completely new to the index.")
    reply = REPLY + "\n\n> " + P1 + "\n>\n> " + novel
    out, _ = run(tmp_path, [(ORIG, ORIG_BODY), ("20240302T100000-bbbbb", reply)])
    _, body = body_of(out, "20240302T100000-bbbbb")
    assert f"> [elided 1 quoted line; see ./{ORIG}.txt]" in body
    assert novel in body.replace("> ", "")


def test_long_chain_points_at_original_across_years(tmp_path):
    first = "20231231T230000-aaaaa"
    second = ("Agreed.\n\nOn Sun, Alice wrote:\n" + wrap(ORIG_BODY, 72, "> "))
    third = ("Me too.\n\nOn Mon, Bob wrote:\n> Agreed.\n>\n> On Sun, Alice wrote:\n"
             + wrap(ORIG_BODY, 66, "> > "))
    out, _ = run(tmp_path, [(first, ORIG_BODY), ("20240101T100000-bbbbb", second),
                            ("20240102T100000-ccccc", third)])
    _, body = body_of(out, "20240102T100000-ccccc")
    assert "venue" not in body
    assert f"\n> > [elided 5 quoted lines; see ../2023/{first}.txt]" in body


def test_assets_mirrored_and_output_rerunnable(tmp_path):
    root = build(tmp_path, [(ORIG, ORIG_BODY)])
    (root / "2024" / "attachments").mkdir()
    (root / "2024" / "attachments" / "abc-x.pdf").write_bytes(b"pdf")
    (root / "2024" / "html_bodies").mkdir()
    (root / "2024" / "html_bodies" / f"{ORIG}.html").write_text("<p>x</p>")
    out = tmp_path / "out"
    for _ in range(2):
        compress.run(str(root), str(out))
    assert (out / "2024" / "attachments" / "abc-x.pdf").read_bytes() == b"pdf"
    assert (out / "2024" / "html_bodies" / f"{ORIG}.html").exists()
    assert len(iter_message_files(str(out))) == 1


def test_refuses_output_inside_input(tmp_path):
    root = build(tmp_path, [(ORIG, ORIG_BODY)])
    try:
        compress.run(str(root), str(root / "sub"))
    except ValueError:
        return
    raise AssertionError("expected ValueError")

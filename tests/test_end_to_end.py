import os

from conftest import make_msg
from mboxcompress.__main__ import main
from mboxcompress.archive import iter_message_files

ORIG = ("We should move the offsite to the second week of March because the venue is "
        "booked solid in February. Let me know if that works for everyone, thanks.")


def test_split_compress_view(write_mbox, tmp_path):
    first = make_msg(subject="Offsite", body=ORIG, html=f"<p>{ORIG}</p>", msgid="<1@x>",
                     attachments=[(b"\x89PNGfake", "image", "png", "map.png")])
    reply = make_msg(subject="Re: Offsite", msgid="<2@x>", date="Tue, 07 Jan 2025 10:00:00 +0000",
                     body="Works for me.\n\nOn Mon, Alice wrote:\n> " + ORIG)
    mbox = write_mbox([first, reply])
    split_dir, comp_dir = tmp_path / "split", tmp_path / "comp"

    assert main(["split", str(mbox), str(split_dir), "--tz", "UTC"]) == 0
    assert main(["compress", str(split_dir), str(comp_dir)]) == 0
    files = iter_message_files(str(comp_dir))
    assert len(files) == 2
    reply_txt = open(files[1], encoding="utf-8").read()
    assert "venue" not in reply_txt and "[elided" in reply_txt

    out = tmp_path / "view.html"
    assert main(["view", files[1], "-o", str(out)]) == 0
    page = out.read_text()
    assert "Re: Offsite" in page
    assert '<details class="quoted">' in page
    assert "venue is booked" in page

    out1 = tmp_path / "view1.html"
    main(["view", files[0], "-o", str(out1)])
    page1 = out1.read_text()
    assert "<iframe" in page1 and "map.png" in page1
    assert "Content-Security-Policy" in page1
    assert (comp_dir / "2025" / "attachments").is_dir()
    assert os.listdir(comp_dir / "2025" / "html_bodies")

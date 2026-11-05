import argparse
import sys
import webbrowser


def _tz(name):
    if not name:
        return None
    from zoneinfo import ZoneInfo
    return ZoneInfo(name)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mbox-compressor")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("split", help="explode mbox file(s) into an archive folder")
    s.add_argument("mbox", nargs="+")
    s.add_argument("out")
    s.add_argument("--tz", help="IANA zone for filenames (default: this machine's local zone)")
    s.add_argument("--keep-links", action="store_true",
                   help="keep URLs when deriving text from HTML-only messages")
    s.add_argument("--keep-header", action="append", default=[], metavar="NAME",
                   help="keep an extra header (repeatable)")

    c = sub.add_parser("compress", help="write a copy of an archive with known quotes elided")
    c.add_argument("archive")
    c.add_argument("out")
    c.add_argument("--min-elide-chars", type=int, default=100)
    c.add_argument("--match-ratio", type=float, default=0.8)

    v = sub.add_parser("view", help="render one message .txt to an HTML page")
    v.add_argument("message")
    v.add_argument("-o", "--out")
    v.add_argument("--depth", type=int, default=3, help="levels of elided quotes to expand")
    v.add_argument("--remote-images", action="store_true")
    v.add_argument("--open", action="store_true", help="open in a browser")

    a = ap.parse_args(argv)
    if a.cmd == "split":
        from . import split
        stats = split.run(a.mbox, a.out, tz=_tz(a.tz), keep_links=a.keep_links,
                          extra_headers=a.keep_header)
    elif a.cmd == "compress":
        from . import compress
        stats = compress.run(a.archive, a.out, min_elide_chars=a.min_elide_chars,
                             match_ratio=a.match_ratio)
    else:
        from . import view
        path = view.write_view(a.message, a.out, depth=a.depth, remote=a.remote_images)
        print(path)
        if a.open:
            webbrowser.open("file://" + path)
        return 0
    for k, val in stats.items():
        print(f"{k}: {val}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

# mbox-compressor

Turn a Gmail Takeout mbox into a greppable, one-file-per-message archive, then
optionally squeeze out quoted reply chains.

Requires Python 3.9+. No required dependencies; `html2text` gives better text for
HTML-only messages (a stdlib fallback is used without it).

```bash
pip install -e '.[html]'
```

## 1. split

```bash
mbox-compressor split "Takeout/Mail/All mail Including Spam and Trash.mbox" archive/
```

Produces:

```
archive/
  2025/
    20250106T100000-0ca79.txt
    html_bodies/20250106T100000-0ca79.html
    attachments/0703319ff5-map.png
  errors/000001.eml        # anything that failed to convert, saved raw
```

A message file looks like:

```
From: Alice <alice@example.com>
To: Bob <bob@example.com>
Date: Mon, 06 Jan 2025 10:00:00 +0000
Subject: Offsite
Message-ID: <1@x>
X-Gmail-Labels: Inbox,Important
X-GM-THRID: 1820000000000000000
X-HTML-Body: ./html_bodies/20250106T100000-0ca79.html
X-Attachment: ./attachments/0703319ff5-map.png (image/png, 18234 bytes)

We should move the offsite to ...
```

- Headers and bodies are decoded to UTF-8. Only From, To, Cc, Bcc, Reply-To,
  Date, Subject, Message-ID, In-Reply-To, List-Id, X-Gmail-Labels and
  X-GM-THRID are kept; add more with `--keep-header NAME`.
- Filenames use the Date header (falling back to the newest `Received:` hop,
  then the mbox `From ` line) in this machine's local zone, or `--tz Zone/Name`.
  The suffix is a hash of the Message-ID; repeated Message-IDs are skipped.
- HTML-only messages get a plaintext body derived from the HTML, marked
  `X-Body-Derived: html`. Links are dropped from it unless `--keep-links`.
- Attachments are named by content hash, so a file sent many times in a year is
  stored once. Inline `cid:` images in the HTML are rewritten to point at them.
  Extensions are guessed from the MIME type when the filename has none.

## 2. compress (optional, re-runnable)

```bash
mbox-compressor compress archive/ archive-compact/
```

Writes a new archive. Message files are rewritten; HTML bodies and attachments
are hard-linked (copied if hard links aren't possible).

Every sentence is normalized (quote markers, whitespace, punctuation and case
removed) and hashed, oldest message first. In later messages, quoted runs whose
sentences are at least `--match-ratio` (0.8) already seen, and that total at
least `--min-elide-chars` (100), become a pointer:

```
On Mon, Alice wrote:
> [elided 12 quoted lines; see ./20250106T100000-0ca79.txt]
```

and the message gets an `X-Quoted-From:` header for each source. Pointers go to
the first message a sentence appeared in, so they never form cycles.

"Quoted" means `>`-prefixed lines, or everything after an attribution/separator
line (`On ... wrote:`, `-----Original Message-----`, an Outlook
`From:/Sent:/Subject:` block, forwarded-message markers) when what follows isn't
`>`-prefixed. Unquoted text — new content, signatures — is never elided.
Messages that interleave replies between quoted fragments are left untouched.

The sentence index lives in memory: expect a few hundred MB for a large archive.

## 3. view

```bash
mbox-compressor view archive-compact/2025/20250107T100000-995fe.txt --open
```

Writes `/tmp/mail-view/<stem>.html` (or `-o PATH`): headers, attachments
(images inline), the HTML body in a sandboxed frame, and the text body. Elided
quotes expand in place from the messages they point to, up to `--depth` (3)
levels. Remote images in HTML bodies are blocked unless `--remote-images`.

## Tests

```bash
pip install -e '.[test]'
python -m pytest
```

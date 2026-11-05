import mailbox
import time
from email.message import EmailMessage

import pytest


def make_msg(subject="Hi", body="Hello there.", html=None, date="Mon, 06 Jan 2025 10:00:00 +0000",
             msgid=None, sender="Alice <alice@example.com>", to="Bob <bob@example.com>",
             attachments=(), extra_headers=()):
    m = EmailMessage()
    m["From"] = sender
    m["To"] = to
    m["Subject"] = subject
    if date:
        m["Date"] = date
    m["Message-ID"] = msgid or f"<{abs(hash((subject, body, date)))}@example.com>"
    for name, value in extra_headers:
        m[name] = value
    if body is not None:
        m.set_content(body)
        if html is not None:
            m.add_alternative(html, subtype="html")
    else:
        m.set_content(html, subtype="html")
    for data, maintype, subtype, filename in attachments:
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return m


@pytest.fixture
def write_mbox(tmp_path):
    def _write(messages, name="mail.mbox"):
        path = tmp_path / name
        mb = mailbox.mbox(str(path))
        for m in messages:
            mm = mailbox.mboxMessage(m)
            mm.set_from("1234567890@xxx", time.gmtime(1736157600))
            mb.add(mm)
        mb.flush()
        mb.close()
        return path
    return _write

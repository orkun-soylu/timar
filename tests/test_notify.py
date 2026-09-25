import re

from timar import notify
from timar.notify import MAX_MESSAGE


def _balanced(chunk: str) -> bool:
    stack = []
    for m in re.finditer(r"<(/?)([a-z]+)[^>]*>", chunk):
        if m.group(1):
            if not stack or stack.pop() != m.group(2):
                return False
        else:
            stack.append(m.group(2))
    return not stack


def _sweep_report(lines: int) -> str:
    body = "\n".join(f"host-{i:03d}  disk  /var is 91% full &amp; growing" for i in range(lines))
    return f"<b>Log sweep</b>\n\n<i>Two hosts need attention.</i>\n\n<pre>{body}</pre>"


def test_a_short_message_is_sent_untouched():
    text = "<b>Timar</b>\nAll clear."
    assert notify._split(text) == [text]


def test_a_preformatted_block_longer_than_one_message_is_closed_in_every_chunk():
    """The findings are one `<pre>` block. When it outgrew 4096 characters the first chunk ended
    inside an open `<pre>` and Telegram rejected the whole report with "Can't find end tag
    corresponding to start tag" -- the 09:30 sweep was never delivered."""
    text = _sweep_report(200)
    chunks = notify._split(text)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= MAX_MESSAGE
        assert _balanced(chunk), chunk[:80]
    # Every chunk after the first continues the block rather than dropping out of it.
    assert all(c.startswith("<pre>") for c in chunks[1:])


def test_splitting_loses_no_line():
    text = _sweep_report(200)
    joined = "\n".join(notify._split(text))
    for i in range(200):
        assert f"host-{i:03d} " in joined


def test_a_truncated_line_does_not_end_in_half_an_entity():
    """An over-long line is cut to fit. Cutting through `&amp;` leaves `&am`, which Telegram
    rejects as malformed markup just like an unclosed tag."""
    line = "x" * (MAX_MESSAGE - notify._TAG_SLACK - 3) + "&amp;tail"
    chunks = notify._split("<pre>short</pre>\n" + line)
    last = chunks[-1]
    assert len(last) <= MAX_MESSAGE
    assert not re.search(r"&[^;\s]*$", last)

import re


_BEGIN_TAG = "<memory-context>"
_END_TAG = "</memory-context>"
_TAG_NAME = "memory-context"
_OPEN_TAG_RE = re.compile(r'<\s*memory-context\s*>', re.IGNORECASE)
_CLOSE_TAG_RE = re.compile(r'</\s*memory-context\s*>', re.IGNORECASE)
_INTERNAL_CONTEXT_RE = re.compile(
    r'<\s*memory-context\s*>[\s\S]*?</\s*memory-context\s*>',
    re.IGNORECASE,
)


def scrub_memory_context(text: str) -> str:
    """One-shot scrub all complete memory-context blocks from text."""
    return _INTERNAL_CONTEXT_RE.sub('', text)


class StreamingContextScrubber:
    """Stateful scrubber for streaming text that may contain split memory-context spans.

    A <memory-context> opened in one delta and closed in another delta leaks
    its payload to the UI if done by one-shot regex because the non-greedy
    needs both tags in the same string. This scrubber runs a small state
    machine across deltas, holding back partial-tag tails and discarding
    everything inside a span.
    """

    def __init__(self):
        self._in_span: bool = False
        self._buf: str = ""

    def feed(self, text: str) -> str:
        """Return the visible portion of text after scrubbing."""
        if not text:
            return ""
        buf = self._buf + text
        self._buf = ""
        out: list[str] = []

        while buf:
            if self._in_span:
                match = _CLOSE_TAG_RE.search(buf)
                if match is None:
                    # Hold back a potential partial closing tag
                    held = self._max_partial_suffix(buf, _END_TAG)
                    self._buf = buf[-held:] if held else ""
                    return "".join(out)
                # Found close — skip span content, continue after close tag
                buf = buf[match.end():]
                self._in_span = False
            else:
                match = _OPEN_TAG_RE.search(buf)
                if match is None:
                    # No open tag — hold back potential partial opening tag
                    held = self._max_partial_suffix(buf, _BEGIN_TAG)
                    out.append(buf[:-held] if held else buf)
                    self._buf = buf[-held:] if held else ""
                    return "".join(out)
                # Found open — emit text before open tag, enter span
                if match.start() > 0:
                    out.append(buf[:match.start()])
                buf = buf[match.end():]
                self._in_span = True

        return "".join(out)

    def flush(self) -> str:
        """Drop any held-back partial tag buffer at end-of-stream."""
        if self._in_span:
            # Still inside span — drop anything remaining
            self._buf = ""
            self._in_span = False
            return ""
        self._buf = ""
        return ""

    @staticmethod
    def _max_partial_suffix(buf: str, tag: str) -> int:
        """Return length of longest buf-suffix that can prefix tag grammar."""
        buf_lower = buf.lower()
        is_closing = tag == _END_TAG
        for index, char in enumerate(buf_lower):
            if char == "<" and StreamingContextScrubber._is_partial_tag_prefix(buf_lower[index:], is_closing):
                return len(buf_lower) - index
        return 0

    @staticmethod
    def _is_partial_tag_prefix(fragment: str, is_closing: bool) -> bool:
        if not fragment or fragment[0] != "<":
            return False

        index = 1
        if is_closing:
            if index == len(fragment):
                return True
            if fragment[index] != "/":
                return False
            index += 1
        elif index < len(fragment) and fragment[index] == "/":
            return False

        while index < len(fragment) and fragment[index].isspace():
            index += 1
        if index == len(fragment):
            return True

        name_fragment = fragment[index:]
        if len(name_fragment) <= len(_TAG_NAME):
            return _TAG_NAME.startswith(name_fragment)
        if not name_fragment.startswith(_TAG_NAME):
            return False

        index += len(_TAG_NAME)
        while index < len(fragment) and fragment[index].isspace():
            index += 1
        return index == len(fragment) or (index == len(fragment) - 1 and fragment[index] == ">")

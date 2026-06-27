import pytest

from app.utils.memory_scrubber import StreamingContextScrubber, scrub_memory_context


# ---------- one-shot scrub_memory_context ----------

def test_scrub_memory_context_removes_complete_block():
    text = "before<memory-context>secret</memory-context>after"
    assert scrub_memory_context(text) == "beforeafter"


def test_scrub_memory_context_removes_multiple_blocks():
    text = "<memory-context>a</memory-context>x<memory-context>b</memory-context>"
    assert scrub_memory_context(text) == "x"


def test_scrub_memory_context_case_insensitive():
    text = "<MEMORY-CONTEXT>secret</Memory-Context>"
    assert scrub_memory_context(text) == ""


def test_scrub_memory_context_tolerates_tag_whitespace():
    # open tag tolerates whitespace after <; close tag tolerates whitespace after /
    text = "< memory-context >secret</ memory-context >"
    assert scrub_memory_context(text) == ""


def test_scrub_memory_context_no_block_unchanged():
    text = "plain text without tags"
    assert scrub_memory_context(text) == text


def test_scrub_memory_context_partial_open_unchanged():
    # one-shot regex cannot remove unclosed span — by design
    text = "<memory-context>secret without close"
    assert scrub_memory_context(text) == text


# ---------- StreamingContextScrubber ----------

def test_streaming_scrubber_single_chunk_complete_block():
    scrubber = StreamingContextScrubber()
    out = scrubber.feed("<memory-context>secret</memory-context>")
    out += scrubber.flush()
    assert out == ""


def test_streaming_scrubber_no_tags_passthrough():
    scrubber = StreamingContextScrubber()
    out = scrubber.feed("hello world")
    out += scrubber.flush()
    assert out == "hello world"


def test_streaming_scrubber_block_split_across_chunks():
    """Acceptance criterion 1: cross-chunk <memory-context>...</memory-context>."""
    scrubber = StreamingContextScrubber()
    out = scrubber.feed("prefix <memory-context>sec")
    out += scrubber.feed("ret</memory-context> suffix")
    out += scrubber.flush()
    assert out == "prefix  suffix"
    assert "<memory-context>" not in out
    assert "</memory-context>" not in out
    assert "secret" not in out


def test_streaming_scrubber_open_tag_split_across_chunks():
    """Open tag itself straddles a chunk boundary."""
    scrubber = StreamingContextScrubber()
    out = scrubber.feed("text <memory")
    out += scrubber.feed("-context>payload</memory-context> tail")
    out += scrubber.flush()
    assert out == "text  tail"
    assert "payload" not in out


def test_streaming_scrubber_close_tag_split_across_chunks():
    """Close tag straddles a chunk boundary."""
    scrubber = StreamingContextScrubber()
    out = scrubber.feed("<memory-context>payload</memory")
    out += scrubber.feed("-context> visible")
    out += scrubber.flush()
    assert out == " visible"
    assert "payload" not in out


def test_streaming_scrubber_unclosed_span_dropped_on_flush():
    """Acceptance criterion 2: unclosed span remaining content discarded."""
    scrubber = StreamingContextScrubber()
    out = scrubber.feed("clean <memory-context>leaked payload that never closes")
    out += scrubber.flush()
    assert out == "clean "
    assert "leaked payload" not in out


def test_streaming_scrubber_unclosed_span_with_partial_close_dropped():
    scrubber = StreamingContextScrubber()
    out = scrubber.feed("<memory-context>secret</memory-context")
    out += scrubber.flush()
    # partial close tag held back, then dropped at flush (in span)
    assert out == ""
    assert "secret" not in out


def test_streaming_scrubber_multiple_blocks_in_stream():
    scrubber = StreamingContextScrubber()
    out = scrubber.feed("a<memory-context>x</memory-context>b")
    out += scrubber.feed("c<memory-context>y</memory-context>d")
    out += scrubber.flush()
    assert out == "abcd"


def test_streaming_scrubber_case_insensitive():
    scrubber = StreamingContextScrubber()
    out = scrubber.feed("<MEMORY-CONTEXT>sec")
    out += scrubber.feed("ret</Memory-Context>done")
    out += scrubber.flush()
    assert out == "done"


def test_streaming_scrubber_preserves_content_before_and_after():
    scrubber = StreamingContextScrubber()
    out = scrubber.feed("line1\n<memory-context>x</memory-context>\nline2")
    out += scrubber.flush()
    assert out == "line1\n\nline2"


def test_streaming_scrubber_empty_feed_returns_empty():
    scrubber = StreamingContextScrubber()
    assert scrubber.feed("") == ""


def test_streaming_scrubber_reuse_after_flush():
    scrubber = StreamingContextScrubber()
    scrubber.feed("<memory-context>x</memory-context>")
    scrubber.flush()
    # second turn after flush
    out = scrubber.feed("fresh start")
    out += scrubber.flush()
    assert out == "fresh start"


def test_streaming_scrubber_no_leak_when_partial_open_then_unrelated_text():
    """Partial open prefix held back, then text that doesn't complete the tag."""
    scrubber = StreamingContextScrubber()
    out = scrubber.feed("hello <memo")
    # next chunk does NOT complete <memory-context> — held buffer should flush as visible
    out += scrubber.feed("ry of cats")
    out += scrubber.flush()
    assert out == "hello <memory of cats"

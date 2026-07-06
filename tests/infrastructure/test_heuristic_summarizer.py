import pytest

from app.infrastructure.memory.heuristic_summarizer import HeuristicSummarizer


@pytest.mark.asyncio
async def test_summarizer_text_and_image_message_summary_excludes_image_data():
    summarizer = HeuristicSummarizer()
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看这张图"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
            ],
        },
        {"role": "assistant", "content": "已分析"},
    ]

    summary = await summarizer.summarize(messages, "")

    assert "data:image" not in summary
    assert "image_url" not in summary
    assert "[" not in summary
    assert "看这张图" in summary


@pytest.mark.asyncio
async def test_summarizer_image_only_message_does_not_write_list_repr():
    summarizer = HeuristicSummarizer()
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
            ],
        },
        {"role": "assistant", "content": "已分析"},
    ]

    summary = await summarizer.summarize(messages, "")

    assert "data:image" not in summary
    assert "image_url" not in summary
    assert "[{'type'" not in summary


@pytest.mark.asyncio
async def test_summarizer_string_content_unchanged():
    summarizer = HeuristicSummarizer()
    messages = [
        {"role": "user", "content": "hello world"},
        {"role": "assistant", "content": "hi"},
    ]

    summary = await summarizer.summarize(messages, "")

    assert "hello world" in summary
    assert "hi" in summary

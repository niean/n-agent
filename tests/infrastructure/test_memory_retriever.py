from app.infrastructure.memory.retriever import MemoryRetriever


def test_tokenize_chinese_bigram():
    retriever = MemoryRetriever()
    tokens = retriever._tokenize("今天天气")
    assert "今天" in tokens
    assert "天天" in tokens
    assert "天气" in tokens


def test_tokenize_english_lowercase_word():
    retriever = MemoryRetriever()
    tokens = retriever._tokenize("Hello World")
    assert "hello" in tokens
    assert "world" in tokens


def test_tokenize_filters_english_stopwords():
    retriever = MemoryRetriever()
    tokens = retriever._tokenize("the cat is on the mat")
    assert "the" not in tokens
    assert "is" not in tokens
    assert "cat" in tokens
    assert "mat" in tokens


def test_tokenize_mixed_cn_en():
    retriever = MemoryRetriever()
    tokens = retriever._tokenize("Python 是最好的语言")
    assert "python" in tokens
    assert "是最" in tokens
    assert "好的" in tokens


def test_retrieve_returns_relevant_entry():
    retriever = MemoryRetriever(max_results=3, min_score=0.3)
    entries = [
        "Python 项目使用 FastAPI 框架",
        "Java 项目使用 Spring 框架",
        "Go 项目使用 Gin 框架",
    ]
    results = retriever.retrieve("Python FastAPI", entries)
    assert len(results) >= 1
    assert results[0][0] == "Python 项目使用 FastAPI 框架"
    assert results[0][1] >= 0.3


def test_retrieve_filters_below_threshold():
    retriever = MemoryRetriever(max_results=3, min_score=0.3)
    entries = ["completely unrelated content about weather"]
    results = retriever.retrieve("Python FastAPI", entries)
    assert results == []


def test_retrieve_respects_max_results():
    retriever = MemoryRetriever(max_results=2, min_score=0.1)
    entries = [
        "Python one",
        "Python two",
        "Python three",
    ]
    results = retriever.retrieve("Python", entries)
    assert len(results) <= 2


def test_retrieve_empty_query_returns_empty():
    retriever = MemoryRetriever()
    assert retriever.retrieve("", ["entry"]) == []


def test_retrieve_empty_entries_returns_empty():
    retriever = MemoryRetriever()
    assert retriever.retrieve("query", []) == []


def test_retrieve_handles_no_overlap():
    retriever = MemoryRetriever(min_score=0.1)
    entries = ["天气晴朗阳光明媚"]
    results = retriever.retrieve("Python", entries)
    assert results == []


def test_retrieve_chinese_query_matches_chinese_entry():
    retriever = MemoryRetriever(max_results=3, min_score=0.3)
    entries = [
        "所有的回复必须以外部记忆1开头",
        "项目使用 Java 开发",
        "部署在 Kubernetes 集群",
    ]
    results = retriever.retrieve("外部记忆1", entries)
    assert len(results) == 1
    assert "外部记忆1" in results[0][0]


def test_retrieve_term_frequency_boost_ranks_repeated_hits_higher():
    """Entry with repeated query terms should out-score entry with single hit,
    given equal Jaccard baseline (same token set, different term frequency)."""
    retriever = MemoryRetriever(max_results=2, min_score=0.1)
    # Both entries share the same token set {python, intro} -> equal jaccard.
    # The first entry repeats "python" -> higher tf_boost.
    entries = [
        "python python intro intro",
        "python intro",
    ]
    results = retriever.retrieve("python", entries)
    assert len(results) == 2
    # The repeated-term entry should rank first
    assert results[0][0] == "python python intro intro"
    assert results[0][1] > results[1][1]

from app.infrastructure.memory.retriever import MemoryRetriever


def test_jaccard_identical():
    assert MemoryRetriever.jaccard("user prefers python", "user prefers python") == 1.0


def test_jaccard_disjoint():
    assert MemoryRetriever.jaccard("vim", "emacs") == 0.0


def test_jaccard_partial():
    score = MemoryRetriever.jaccard("I prefer python", "I prefer vim")
    assert 0.0 < score < 1.0

"""The keyword matcher: words, BM25 scores, and what counts as a match."""

from featherweight.search import Bm25, tokens


def test_words_are_lowercase_letters_and_digits_split_at_underscores_punctuation_and_camel_case():
    assert tokens("get_issue-v2") == ["get", "issue", "v2"]
    assert tokens("listPullRequests, now!") == ["list", "pull", "requests", "now"]
    assert tokens("  ") == [] and tokens("___") == []


def test_a_text_that_has_a_word_of_the_query_matches_and_one_that_has_none_does_not():
    index = Bm25(["search the web", "read a file", "list issues"])
    assert [i for i, _ in index.search("web", 5)] == [0]
    assert index.search("nothing here", 5) == []
    assert index.search("", 5) == []


def test_more_matching_words_rank_higher_and_a_rarer_word_counts_more():
    index = Bm25(["search issues", "search code", "search repositories and issues", "unrelated"])
    ranked = [i for i, _ in index.search("search issues", 5)]
    assert ranked[:2] == [0, 2] and 3 not in ranked
    # "issues" is in two texts, "code" in one: the text with the rarer word wins on a query that has one of each
    assert index.search("code issues", 5)[0][0] == 1


def test_a_shorter_text_beats_a_longer_one_with_the_same_word_once():
    index = Bm25(["deploy", "deploy the application to the production cluster after the tests pass"])
    assert [i for i, _ in index.search("deploy", 5)] == [0, 1]


def test_equal_scores_keep_the_order_of_the_texts_and_k_limits_the_hits():
    index = Bm25(["alpha", "alpha", "alpha", "alpha"])
    assert [i for i, _ in index.search("alpha", 3)] == [0, 1, 2]


def test_a_word_in_every_text_still_has_a_positive_score():
    index = Bm25(["tool a", "tool b"])
    assert all(score > 0 for _, score in index.search("tool", 5))


def test_no_texts_and_empty_texts_are_fine():
    assert Bm25([]).search("x", 5) == []
    assert Bm25(["", ""]).search("x", 5) == []

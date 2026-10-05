"""Edge cases of the deterministic text rules (glossary, language choice, Ask context, time stamps, sentence blocks)
beyond the modules' own self-checks."""
import json

import ask
import blocks
import glossary
import stamps


def test_glossary_longest_match_and_no_rules(tmp_path):
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"corrections": {"purchasing": "Purchasing", "purchasing api": "Purchasing A/P"}}))
    g = glossary.Glossary(str(p))
    assert g.correct("the purchasing api and purchasing") == ("the Purchasing A/P and Purchasing", 2)
    assert g.correct("a/purchasing api") == ("a/purchasing api", 0)           # not a whole word after "/"
    p.write_text("{}")
    assert glossary.Glossary(str(p)).correct("x") == ("x", 0) and glossary.Glossary(str(p)).context_terms() == []


def test_language_rules():
    assert glossary.language_drop("中文", "japanese") is False and glossary.language_drop("", "English") is False
    assert glossary.output_language(["đây là tiếng Việt có dấu"], "German") == "Vietnamese"
    assert ask.answer_language("Ai chịu trách nhiệm?") == "Vietnamese" and ask.answer_language("123?") == "the same language as the question"


def test_context_leak_needs_a_run_of_words():
    assert not glossary.context_leak("grn", "a b")
    assert glossary.context_leak("Use PO21, GRN, APV.", "po21 grn apv")
    assert not glossary.context_leak("PO21, GRN and APV", "po21 grn apv")         # interrupted run


def test_ask_prompt_fills_every_placeholder():
    p, n_rows, n_words = ask.prompt("{{NOTES}}|{{TRANSCRIPT}}|{{QUESTION}}|{{ANSWER_LANGUAGE}}", "", [], "Who?")
    assert p == "(none yet)|(nothing recognised yet)|Who?|English" and (n_rows, n_words) == (0, 0)


def test_stamps_hour_format_and_unknown_section():
    assert stamps.keep_known("at [1:02:10] and [1:02:11]", [3730.0], tol=0.5) == "at [1:02:10] and"
    out, n, k = stamps.stamp("## A\n- x\n## B\n- y", [(1.0, "y z")], sections={3})
    assert (n, k) == (0, 0) and out == "## A\n- x\n## B\n- y"


def test_blocks_join_rules():
    t = blocks.Transcript()
    t.sentence_final("system", 0, 2, 1.8, "first part")
    t.sentence_final("mic", 2.1, 3, 2.9, "other source")                  # a different source never joins
    t.sentence_final("system", 3.5, 4, 3.9, "late")                      # the last block is mic now: new block
    t.sentence_final("system", 6, 7, 6.9, "far")                         # gap >= 1 s: new block
    assert [b["text"] for b in t.blocks] == ["first part", "other source", "late", "far"]
    assert blocks.norm_words("Đơn, hàng_PO!") == ["đơn", "hàng", "po"]
    assert t.refine("system", 100, 200, "nothing there") == []

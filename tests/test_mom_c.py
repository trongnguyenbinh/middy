"""MoM map/reduce with a fake LLM: parts by sentence time, a cut map answer split and re-run, reduce retried once,
Main content built by code."""
import mom_c


def fin(s, text="câu", **kw):
    return {"s": s, "e": s + 5, "text": text, "speaker": "Speaker 1", "dropped_lang": False, **kw}


def llm(map_answer="- ý", reduce_finish=("stop",), cut_over=None):
    calls = []

    def gen(tag, prompt, max_tokens):
        calls.append((tag, prompt, max_tokens))
        if tag.startswith("mcmap"):
            n = prompt.count("\n[")
            finish = "length" if cut_over is not None and n > cut_over else "stop"
            return map_answer, {"finish": finish, "wall_s": 1.0, "gen_tokens": 10}
        k = int(tag[5:])
        return "# Tiêu đề\n## Summary\nS\n## Main content\nbị thay\n## Action items\nNone", \
            {"finish": reduce_finish[min(k, len(reduce_finish) - 1)], "wall_s": 2.0, "gen_tokens": 20}
    gen.calls = calls
    return gen


def test_chunks_by_part_and_skip_empty_or_dropped():
    rows = [fin(599), fin(0), fin(600), fin(30, ""), fin(40, dropped_lang=True), fin(1300)]
    assert [[f["s"] for f in c] for c in mom_c.chunks(rows)] == [[0, 599], [600], [1300]]
    assert mom_c.chunks([]) == []
    assert [[f["s"] for f in c] for c in mom_c.chunks(rows, part_s=300)] == [[0], [599], [600], [1300]]


def test_empty_meeting_makes_no_llm_call():
    gen = llm()
    md, parts, info = mom_c.build_mom([fin(0, "")], "English", gen)
    assert (md, parts, info["parts"], gen.calls) == ("", [], 0, [])


def test_map_prompt_carries_times_and_speakers():
    gen = llm()
    mom_c.build_mom([fin(61, "nhập kho GRN"), fin(130, "xong")], "Vietnamese", gen)
    tag, prompt, mt = gen.calls[0]
    assert tag == "mcmap0" and mt == mom_c.MAP_TOKENS
    assert "[01:01] Speaker 1: nhập kho GRN" in prompt and "01:01–02:15" in prompt and "{{" not in prompt


def test_cut_map_is_split_until_it_fits():
    gen = llm(cut_over=2)
    md, parts, info = mom_c.build_mom([fin(s) for s in range(0, 40, 5)], "English", gen)
    assert info["map_cut"] == 3 and info["map_split"] == 3 and info["parts"] == 4
    assert all(len(p) == 1 for _, _, p in parts)


def test_single_sentence_cut_is_kept_with_a_warning():
    gen = llm(cut_over=0)
    _, parts, info = mom_c.build_mom([fin(0)], "English", gen)
    assert info["map_cut"] == 1 and info["map_split"] == 0 and len(parts) == 1 and info["warn"] == ["part 00:00 cut on a single sentence: kept as is"]


def test_reduce_cut_twice_warns_and_main_content_is_complete():
    gen = llm(map_answer="- (D) chốt dùng GRN\n- (none)\n* (T) Lan gửi file", reduce_finish=("length", "length"))
    md, _, info = mom_c.build_mom([fin(0), fin(700)], "English", gen)
    assert info["reduce_calls"] == 2 and info["reduce_cut"] == 2 and "reduce cut twice" in info["warn"][0]
    assert "bị thay" not in md and md.count("* chốt dùng GRN") == 2 and "(none)" not in md and "(T)" not in md
    assert info["gen_s"] == 2 * 1.0 + 2 * 2.0 and info["gen_tokens"] == 2 * 10 + 2 * 20


def test_vietnamese_headings_fixed_only_for_vietnamese():
    md, _, _ = mom_c.build_mom([fin(0)], "Vietnamese", llm())
    assert "## Tóm tắt" in md and "## Nội dung chính" in md and "## Việc cần làm\nKhông có" in md
    md, _, _ = mom_c.build_mom([fin(0)], "English", llm())
    assert "## Summary" in md and "None" in md


def test_insert_main_and_main_rows_round_trip():
    parts = [(0, 590, ["- a", "- b"]), (600, 650, []), (1200, 1210, ["* c"])]
    main = mom_c.main_from_parts(parts)
    assert main == "### [00:00–09:50]\n* a\n* b\n### [20:00–20:10]\n* c"
    md = mom_c.insert_main("# T\n## S\nx\n## M\nold\n## D\n- d", main)
    assert "old" not in md and md.endswith("## D\n- d")
    assert mom_c.main_rows(md) == [("00:00–09:50", ["a", "b"]), ("20:00–20:10", ["c"])]
    assert mom_c.insert_main("# only title", "M").endswith("## Main content\nM\n")

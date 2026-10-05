"""Lỗi 21: time stamps and coverage of a meeting's notes — NUMBERS ONLY (safe on real meetings, read-only database).
  tools/stamp_check.py <meeting_id> [--db run/midy.db]
live notes: rounds kept, distinct texts, rounds identical to the previous one, rounds cut by the token cap (finish=length);
final live note and MoM: lines stamped / considered, "no time stamp" remarks left, stamps in time order, how far into the
meeting the stamps reach (a note that stops growing shows here as a reach far below the meeting length).
"""
import json
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.realpath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "proto"))
import stamps  # noqa: E402

TS = re.compile(r"\[(\d+):(\d\d)\]")


def shape(md):
    items = [l for l in md.split("\n") if stamps.ITEM.match(l) and stamps.ITEM.match(l).group(2).strip()]
    ts = [int(a) * 60 + int(b) for l in items for a, b in TS.findall(l)[:1]]
    return {"items": len(items), "stamped": len(ts), "placeholders": len(stamps.PLACEHOLDER.findall(md)),
            "in_order_pairs": sum(b >= a for a, b in zip(ts, ts[1:])), "pairs": max(0, len(ts) - 1),
            "reach_min": round(max(ts) / 60, 1) if ts else None, "median_min": round(sorted(ts)[len(ts) // 2] / 60, 1) if ts else None}


def main():
    mid = int(sys.argv[1])
    db_path = sys.argv[sys.argv.index("--db") + 1] if "--db" in sys.argv else os.path.join(ROOT, "run", "midy.db")
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    audio = db.execute("select audio_end_s from meetings where id=?", (mid,)).fetchone()[0] or 0
    live = db.execute("select text, stats from notes where meeting_id=? and kind='live' order by idx", (mid,)).fetchall()
    mom = db.execute("select text from notes where meeting_id=? and kind='mom'", (mid,)).fetchone()
    texts = [stamps.clean(t) for t, _ in live]
    out = {"audio_min": round(audio / 60, 1),
           "live": {"rounds_kept": len(live), "distinct": len(set(texts)), "same_as_previous": sum(a == b for a, b in zip(texts, texts[1:])),
                    "cut_by_token_cap": sum(json.loads(s or "{}").get("finish") == "length" for _, s in live),
                    "final": shape(live[-1][0]) if live else None},
           "mom": shape(mom[0]) if mom else None}
    print(json.dumps(out))


if __name__ == "__main__":
    main()

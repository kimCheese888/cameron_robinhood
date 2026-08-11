#!/usr/bin/env python3
"""Tests for ross_compare core logic. Run: python -m pytest test_ross_compare.py
or plain: python test_ross_compare.py"""

import ross_compare as rc

# Real excerpt from Ross's 2026-08-09 "ONE Stock On Watch for Monday" video.
TRANSCRIPT = """
on Friday, we did have YJ which ended up making a big move from $2 to $14 a
share and then it came all the way back down to 274. It was a Chinese stock
with no news. MB, this was a Hong Kong company, made a big move from $4 to 20.
VAT, this was a US company. NAMI, this was big on Friday, had put in this
squeeze from after hours Thursday. It was the stock I did the best on. Then if
we look at WHG from the other day, big move from $4 up to 12. I just got smoked
on that trade on DSY. So going into Monday, I'm watching ZJYL for continuation,
maybe a break of 5. XHLD was up 46% in after hours. HDI 4.23 million share
float. INHD personally not interested. If I was trading something like Nvidia
or the S&P 500 with a small IRA account, the AI names, that's not my thing.
"""

OUR_WATCHLIST = ["AXTX", "LOFF", "NAMI", "SPCH"]


def test_overlap_finds_nami():
    overlap = rc.find_overlap(TRANSCRIPT, OUR_WATCHLIST)
    assert "NAMI" in overlap, f"expected NAMI in overlap, got {overlap}"
    # AXTX/LOFF/SPCH are not in this transcript
    assert "AXTX" not in overlap
    assert "SPCH" not in overlap


def test_extract_picks_up_real_tickers():
    tickers = rc.extract_ross_tickers(TRANSCRIPT)
    for t in ("NAMI", "YJ", "WHG", "MB", "VAT", "DSY", "XHLD", "HDI", "INHD"):
        assert t in tickers, f"expected {t} in extracted, got {tickers}"


def test_extract_excludes_jargon():
    tickers = rc.extract_ross_tickers(TRANSCRIPT)
    for junk in ("US", "AI", "IRA"):
        assert junk not in tickers, f"{junk} should be stoplisted"


def test_compare_shape():
    r = rc.compare(TRANSCRIPT, "2026-08-10", "l7Z5aqS4zpk", "ONE Stock",
                   symbols=OUR_WATCHLIST)
    assert r["overlap"] == ["NAMI"]
    assert set(r["our_only"]) == {"AXTX", "LOFF", "SPCH"}
    assert "YJ" in r["ross_candidates"]


if __name__ == "__main__":
    n = 0
    for name in sorted(dir()):
        if name.startswith("test_"):
            globals()[name]()
            print(f"  ok  {name}")
            n += 1
    print(f"\n{n} tests passed")

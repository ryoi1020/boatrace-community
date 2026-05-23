"""
backtest.py
福岡競艇場（jcd=22）の過去50レースを自動でバックテストする。

使い方:
    export ANTHROPIC_API_KEY=...
    python3 backtest.py

各レースで:
  1) 公式サイトから出走表（racelist + beforeinfo）を取得
  2) server.py の call_claude_parse / call_claude_predict で予想を生成
  3) 公式サイトから着順 + 払戻金を取得
  4) 予想 kaikata の的中判定 → 投資・払戻を集計
  5) backtest_results.csv に1行追記
"""

import os
import re
import csv
import sys
import time
from datetime import date, timedelta

from server import (
    fetch_html,
    strip_html,
    fetch_all_pages,
    call_claude_parse,
    call_claude_predict,
    check_hit,
    API_KEY,
)

JCD = "22"
VENUE = "福岡"
TARGET_RACES = 50
SLEEP_SEC = 3
LOOKBACK_DAYS = 60
CSV_PATH = os.path.join(os.path.dirname(__file__), "backtest_results.csv")

FIELDNAMES = [
    "race_date", "race_num", "pattern",
    "honmei", "niban", "sanban",
    "first", "second", "third",
    "tan_hit", "niren_hit", "sanren_hit", "sanrenpuku_hit",
    "bet", "return", "profit", "hits",
]


def fetch_race_result_full(rno, jcd, hd):
    """レース結果ページから着順と各券種の払戻金を取得"""
    url = f"https://www.boatrace.jp/owpc/pc/race/raceresult?rno={rno}&jcd={jcd}&hd={hd}"
    try:
        html = fetch_html(url)
    except Exception as e:
        print(f"    結果ページ取得失敗: {e}")
        return None
    text = strip_html(html)

    def find_after(label_pat, kumi_re):
        m = re.search(label_pat, text)
        if not m:
            return None, None
        window = text[m.end():m.end() + 400]
        # kumi の後ろは「&yen;」「¥」など任意の非数字を経て数字に至る
        pat = r"(" + kumi_re + r")\D*?([\d,]+)"
        m2 = re.search(pat, window)
        if not m2:
            return None, None
        kumi = re.sub(r"\s+", "", m2.group(1))
        yen = int(m2.group(2).replace(",", ""))
        return kumi, yen

    # 3連単から着順を読む（1着-2着-3着がそのままkumiawase）
    sanren_kumi, sanren_yen = find_after(
        r"3連単|三連単",
        r"[1-6]\s*[-－→]\s*[1-6]\s*[-－→]\s*[1-6]",
    )
    if not sanren_kumi:
        return None
    nm = re.match(r"([1-6])[-－→]([1-6])[-－→]([1-6])", sanren_kumi)
    if not nm:
        return None
    first, second, third = int(nm.group(1)), int(nm.group(2)), int(nm.group(3))

    payouts = {"3連単": sanren_yen}

    _, v = find_after(r"3連複|三連複", r"[1-6]\s*[-－=]\s*[1-6]\s*[-－=]\s*[1-6]")
    if v: payouts["3連複"] = v
    _, v = find_after(r"2連単|二連単", r"[1-6]\s*[-－→]\s*[1-6]")
    if v: payouts["2連単"] = v
    _, v = find_after(r"2連複|二連複", r"[1-6]\s*[-－=]\s*[1-6]")
    if v: payouts["2連複"] = v
    _, v = find_after(r"単勝", r"[1-6]")
    if v: payouts["単勝"] = v

    return {"first": first, "second": second, "third": third, "payouts": payouts}


def calc_return(kenshu, kumiawase, kin, payouts, first, second, third):
    """的中していれば払戻額（円）を返す。払戻金は ¥100 ベットあたり"""
    if not check_hit(kenshu, kumiawase, first, second, third):
        return 0
    type_key = None
    for key in ("3連単", "3連複", "2連単", "2連複", "単勝"):
        if key in kenshu:
            type_key = key
            break
    if not type_key or type_key not in payouts:
        return 0
    return int(payouts[type_key] * kin / 100)


def run_one_race(rno, hd):
    """1レースのバックテスト。CSV行のdictを返す。失敗時はNone"""
    print(f"\n[ {hd} {rno}R ] 処理中…")

    result = fetch_race_result_full(rno, JCD, hd)
    if not result:
        print("  → 結果未確定/未開催（スキップ）")
        return None

    pages = fetch_all_pages(rno, JCD, hd)
    if not pages.get("racelist"):
        print("  → 出走表取得失敗")
        return None

    try:
        shutsuba = call_claude_parse(pages)
    except Exception as e:
        print(f"  → 出走表パース失敗: {e}")
        return None

    try:
        pred = call_claude_predict(shutsuba, VENUE, "")
    except Exception as e:
        print(f"  → 予想生成失敗: {e}")
        return None

    honmei = pred.get("honmei", [])
    h_num = honmei[0].get("num") if len(honmei) > 0 else None
    o_num = honmei[1].get("num") if len(honmei) > 1 else None
    s_num = honmei[2].get("num") if len(honmei) > 2 else None

    first, second, third = result["first"], result["second"], result["third"]
    payouts = result["payouts"]

    bet_total = 0
    return_total = 0
    hit_count = 0
    for k in pred.get("kaikata", []):
        kenshu = k.get("kenshu", "")
        kumi = k.get("kumiawase", "")
        kin = int(k.get("kin", 0) or 0)
        bet_total += kin
        ret = calc_return(kenshu, kumi, kin, payouts, first, second, third)
        if ret > 0:
            hit_count += 1
        return_total += ret

    profit = return_total - bet_total

    print(
        f"  ◎{h_num} ○{o_num} ▲{s_num} → 結果 {first}-{second}-{third} | "
        f"パターン{pred.get('race_pattern','?')} 投資{bet_total}円 払戻{return_total}円 収支{profit:+d}円"
    )

    def _hit(*args):
        return 1 if all(args) else 0

    return {
        "race_date": hd,
        "race_num": f"{rno}R",
        "pattern": pred.get("race_pattern", ""),
        "honmei": h_num, "niban": o_num, "sanban": s_num,
        "first": first, "second": second, "third": third,
        "tan_hit": _hit(h_num is not None, h_num == first),
        "niren_hit": _hit(h_num == first, o_num == second),
        "sanren_hit": _hit(h_num == first, o_num == second, s_num == third),
        "sanrenpuku_hit": _hit(
            h_num is not None and o_num is not None and s_num is not None,
            {h_num, o_num, s_num} == {first, second, third},
        ),
        "bet": bet_total,
        "return": return_total,
        "profit": profit,
        "hits": hit_count,
    }


def main():
    if not API_KEY:
        print("ERROR: ANTHROPIC_API_KEY が設定されていません")
        sys.exit(1)

    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=FIELDNAMES).writeheader()

    rows = []
    today = date.today()
    d = today - timedelta(days=1)
    end_limit = today - timedelta(days=LOOKBACK_DAYS)

    while len(rows) < TARGET_RACES and d >= end_limit:
        hd = d.strftime("%Y%m%d")
        for rno in range(1, 13):
            if len(rows) >= TARGET_RACES:
                break
            row = run_one_race(rno, hd)
            if row:
                rows.append(row)
                with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
                    csv.DictWriter(f, fieldnames=FIELDNAMES).writerow(row)
                print(f"  ★ 累計 {len(rows)}/{TARGET_RACES} 完了")
            time.sleep(SLEEP_SEC)
        d -= timedelta(days=1)

    if not rows:
        print("\n対象レースが取得できませんでした")
        return

    n = len(rows)
    total_bet = sum(r["bet"] for r in rows)
    total_return = sum(r["return"] for r in rows)
    total_profit = total_return - total_bet
    tan = sum(r["tan_hit"] for r in rows)
    niren = sum(r["niren_hit"] for r in rows)
    sanren = sum(r["sanren_hit"] for r in rows)
    sanpuku = sum(r["sanrenpuku_hit"] for r in rows)

    print("\n" + "=" * 60)
    print(f"  福岡（jcd={JCD}）バックテスト結果  対象 {n} レース")
    print("=" * 60)
    print(f"  単勝的中:   {tan}/{n}  ({tan / n * 100:.1f}%)")
    print(f"  2連単的中:  {niren}/{n}  ({niren / n * 100:.1f}%)")
    print(f"  3連単的中:  {sanren}/{n}  ({sanren / n * 100:.1f}%)")
    print(f"  3連複的中:  {sanpuku}/{n}  ({sanpuku / n * 100:.1f}%)")
    print(f"  投資総額:   {total_bet:,}円")
    print(f"  払戻総額:   {total_return:,}円")
    print(f"  収支:       {total_profit:+,}円")
    if total_bet > 0:
        print(f"  回収率:     {total_return / total_bet * 100:.1f}%")
    print(f"  CSV: {CSV_PATH}")


if __name__ == "__main__":
    main()

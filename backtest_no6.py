"""
backtest_no6.py
6号艇のデータを入力から外した状態で predict を呼び、福岡20レース分でテスト。

CSV: backtest_results_no6.csv
"""

import os
import re
import sys
import csv
import time
from datetime import date, timedelta

from server import fetch_all_pages, call_claude_parse, call_claude_predict, API_KEY
from backtest import fetch_race_result_full, calc_return, FIELDNAMES, JCD, VENUE, SLEEP_SEC

TARGET_RACES = 20
LOOKBACK_DAYS = 60
CSV_PATH = os.path.join(os.path.dirname(__file__), "backtest_results_no6.csv")


def strip_six(shutsuba_text):
    """6号艇の行を削除し、AIへの追加指示を末尾に付ける"""
    lines = shutsuba_text.splitlines()
    filtered = [l for l in lines if not re.match(r"^\s*[6６]\s*号艇", l)]
    note = (
        "\n\n※ 6号艇のデータは入力されていません。"
        "6号艇は本命/対抗/単穴の対象から外し、"
        "kaikata の組み合わせにも 6 を含めないでください。"
        "shutsuba 配列も 1〜5号艇の5件のみ出力してください。"
    )
    return "\n".join(filtered) + note


def run_one_race(rno, hd):
    print(f"\n[ {hd} {rno}R ] 処理中…")
    result = fetch_race_result_full(rno, JCD, hd)
    if not result:
        print("  → 結果未取得（スキップ）")
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

    shutsuba5 = strip_six(shutsuba)
    print("  6号艇行を削除して predict 実行")

    try:
        pred = call_claude_predict(shutsuba5, VENUE, "")
    except Exception as e:
        print(f"  → 予想生成失敗: {e}")
        return None

    honmei = pred.get("honmei", [])
    h_num = honmei[0].get("num") if len(honmei) > 0 else None
    o_num = honmei[1].get("num") if len(honmei) > 1 else None
    s_num = honmei[2].get("num") if len(honmei) > 2 else None

    first, second, third = result["first"], result["second"], result["third"]
    payouts = result["payouts"]

    bet_total = ret_total = hit_count = 0
    for k in pred.get("kaikata", []):
        kenshu = k.get("kenshu", "")
        kumi = k.get("kumiawase", "")
        kin = int(k.get("kin", 0) or 0)
        bet_total += kin
        r = calc_return(kenshu, kumi, kin, payouts, first, second, third)
        if r > 0:
            hit_count += 1
        ret_total += r

    profit = ret_total - bet_total
    print(
        f"  ◎{h_num} ○{o_num} ▲{s_num} → {first}-{second}-{third} | "
        f"パターン{pred.get('race_pattern','?')} 投資{bet_total}円 払戻{ret_total}円 収支{profit:+d}円"
    )

    def _hit(*args):
        return 1 if all(args) else 0

    return {
        "race_date": hd, "race_num": f"{rno}R",
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
        "bet": bet_total, "return": ret_total,
        "profit": profit, "hits": hit_count,
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
    bet = sum(r["bet"] for r in rows)
    ret = sum(r["return"] for r in rows)
    profit = ret - bet
    tan = sum(r["tan_hit"] for r in rows)
    niren = sum(r["niren_hit"] for r in rows)
    sanren = sum(r["sanren_hit"] for r in rows)
    sanpuku = sum(r["sanrenpuku_hit"] for r in rows)

    print("\n" + "=" * 60)
    print(f"  福岡（jcd={JCD}） 6号艇抜き  対象 {n} レース")
    print("=" * 60)
    print(f"  単勝的中:   {tan}/{n}  ({tan/n*100:.1f}%)")
    print(f"  2連単的中:  {niren}/{n}  ({niren/n*100:.1f}%)")
    print(f"  3連単的中:  {sanren}/{n}  ({sanren/n*100:.1f}%)")
    print(f"  3連複的中:  {sanpuku}/{n}  ({sanpuku/n*100:.1f}%)")
    print(f"  投資:       {bet:,}円")
    print(f"  払戻:       {ret:,}円")
    print(f"  収支:       {profit:+,}円")
    if bet > 0:
        print(f"  回収率:     {ret/bet*100:.1f}%")
    print(f"  CSV: {CSV_PATH}")


if __name__ == "__main__":
    main()

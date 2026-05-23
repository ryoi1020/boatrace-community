"""
backtest_week.py
過去7日間（今日から1〜7日前）の全24会場のレースをバックテストする。

使い方:
    export ANTHROPIC_API_KEY=...
    python3 backtest_week.py

仕様:
  - 対象: 過去7日間（今日から1〜7日前）
  - 全24会場 × 全12R を走査
  - 予想は 1〜5号艇のみ（6号艇除外、call_claude_predict 側で実装済み）
  - 各レースで:
      1) 公式サイトから出走表（racelist + beforeinfo）を取得
      2) server.py の call_claude_parse / call_claude_predict で予想を生成
      3) 公式サイトから着順 + 払戻金を取得
      4) 単勝・2連単・3連単の的中判定
      5) backtest_week_YYYYMMDD.csv に1行追記
  - レース間 3秒待機、取得失敗はスキップ
"""

import os
import csv
import sys
import time
from collections import defaultdict
from datetime import date, timedelta

from server import (
    VENUES,
    fetch_all_pages,
    call_claude_parse,
    call_claude_predict,
    API_KEY,
)
from backtest import fetch_race_result_full, calc_return

LOOKBACK_DAYS = 7
SLEEP_SEC = 3

CSV_PATH = os.path.join(
    os.path.dirname(__file__),
    f"backtest_week_{date.today().strftime('%Y%m%d')}.csv",
)

FIELDNAMES = [
    "race_date", "jcd", "venue", "race_num", "pattern",
    "honmei", "niban", "sanban",
    "first", "second", "third",
    "tan_hit", "niren_hit", "sanren_hit",
    "grade_tan", "grade_niren", "grade_sanren",
    "tan_bet", "tan_return",
    "niren_bet", "niren_return",
    "sanren_bet", "sanren_return",
    "bet", "return", "profit",
]


def _bet_type(kenshu):
    if "3連単" in kenshu: return "sanren"
    if "2連単" in kenshu: return "niren"
    if "単勝" in kenshu:   return "tan"
    return None


def run_one_race(hd, jcd, venue, rno, idx, total):
    """1レースのバックテスト。CSV行のdictを返す。失敗時はNone"""
    date_disp = f"{hd[:4]}/{hd[4:6]}/{hd[6:]}"
    print(f"\n{date_disp} {venue} {rno}R 処理中... ({idx}/{total})")

    result = fetch_race_result_full(rno, jcd, hd)
    if not result:
        print("  → 結果未確定/未開催（スキップ）")
        return None

    pages = fetch_all_pages(rno, jcd, hd)
    if not pages.get("racelist"):
        print("  → 出走表取得失敗（スキップ）")
        return None

    try:
        shutsuba = call_claude_parse(pages)
    except Exception as e:
        print(f"  → 出走表パース失敗: {e}")
        return None

    try:
        pred = call_claude_predict(shutsuba, venue, "")
    except Exception as e:
        print(f"  → 予想生成失敗: {e}")
        return None

    honmei = pred.get("honmei", [])
    h_num = honmei[0].get("num") if len(honmei) > 0 else None
    o_num = honmei[1].get("num") if len(honmei) > 1 else None
    s_num = honmei[2].get("num") if len(honmei) > 2 else None

    first, second, third = result["first"], result["second"], result["third"]
    payouts = result["payouts"]

    per_type_bet = {"tan": 0, "niren": 0, "sanren": 0}
    per_type_ret = {"tan": 0, "niren": 0, "sanren": 0}
    for k in pred.get("kaikata", []):
        kenshu = k.get("kenshu", "")
        kumi = k.get("kumiawase", "")
        kin = int(k.get("kin", 0) or 0)
        t = _bet_type(kenshu)
        if t is None:
            continue
        per_type_bet[t] += kin
        per_type_ret[t] += calc_return(kenshu, kumi, kin, payouts, first, second, third)

    bet_total = sum(per_type_bet.values())
    return_total = sum(per_type_ret.values())
    profit = return_total - bet_total
    tan_hit = 1 if (h_num is not None and h_num == first) else 0
    niren_hit = 1 if (h_num == first and o_num == second) else 0
    sanren_hit = 1 if (h_num == first and o_num == second and s_num == third) else 0

    grade = pred.get("grade") or {}
    grade_tan = grade.get("tan", "") or ""
    grade_niren = grade.get("niren", "") or ""
    grade_sanren = grade.get("sanren", "") or ""

    print(
        f"  ◎{h_num} ○{o_num} ▲{s_num} → 結果 {first}-{second}-{third} | "
        f"単{'◯' if tan_hit else '×'} 2単{'◯' if niren_hit else '×'} "
        f"3単{'◯' if sanren_hit else '×'} | 投資{bet_total}円 払戻{return_total}円 "
        f"収支{profit:+d}円"
    )

    return {
        "race_date": hd,
        "jcd": jcd,
        "venue": venue,
        "race_num": f"{rno}R",
        "pattern": pred.get("race_pattern", ""),
        "honmei": h_num, "niban": o_num, "sanban": s_num,
        "first": first, "second": second, "third": third,
        "tan_hit": tan_hit,
        "niren_hit": niren_hit,
        "sanren_hit": sanren_hit,
        "grade_tan": grade_tan,
        "grade_niren": grade_niren,
        "grade_sanren": grade_sanren,
        "tan_bet": per_type_bet["tan"],
        "tan_return": per_type_ret["tan"],
        "niren_bet": per_type_bet["niren"],
        "niren_return": per_type_ret["niren"],
        "sanren_bet": per_type_bet["sanren"],
        "sanren_return": per_type_ret["sanren"],
        "bet": bet_total,
        "return": return_total,
        "profit": profit,
    }


def print_summary(rows):
    if not rows:
        print("\n対象レースが取得できませんでした")
        return

    n = len(rows)
    total_bet = sum(r["bet"] for r in rows)
    total_return = sum(r["return"] for r in rows)
    total_profit = total_return - total_bet

    print("\n" + "=" * 78)
    print(f"  週間バックテスト結果  対象 {n} レース")
    print("=" * 78)

    # 日別
    print("\n【日別の的中率・回収率】")
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["race_date"]].append(r)
    for hd in sorted(by_day.keys()):
        rs = by_day[hd]
        m = len(rs)
        bet = sum(r["bet"] for r in rs)
        ret = sum(r["return"] for r in rs)
        tan = sum(r["tan_hit"] for r in rs)
        niren = sum(r["niren_hit"] for r in rs)
        sanren = sum(r["sanren_hit"] for r in rs)
        roi = (ret / bet * 100) if bet > 0 else 0
        date_disp = f"{hd[:4]}/{hd[4:6]}/{hd[6:]}"
        print(
            f"  {date_disp}  {m:3d}R  単{tan:3d}({tan/m*100:5.1f}%) "
            f"2単{niren:3d}({niren/m*100:5.1f}%) 3単{sanren:3d}({sanren/m*100:5.1f}%) "
            f"投資{bet:>7,}円 払戻{ret:>7,}円 回収率{roi:6.1f}%"
        )

    # 券種別
    print("\n【券種別の的中率・回収率】")
    type_specs = [
        ("単勝",  "tan_hit",    "tan_bet",    "tan_return"),
        ("2連単", "niren_hit",  "niren_bet",  "niren_return"),
        ("3連単", "sanren_hit", "sanren_bet", "sanren_return"),
    ]
    for label, hit_key, bet_key, ret_key in type_specs:
        hits = sum(r[hit_key] for r in rows)
        bet = sum(r[bet_key] for r in rows)
        ret = sum(r[ret_key] for r in rows)
        roi = (ret / bet * 100) if bet > 0 else 0
        print(
            f"  {label:>4s}: {hits:3d}/{n} ({hits/n*100:5.1f}%)  "
            f"投資{bet:>7,}円 払戻{ret:>7,}円 回収率{roi:6.1f}%"
        )

    # 全体
    roi = (total_return / total_bet * 100) if total_bet > 0 else 0
    print("\n【全体】")
    print(f"  対象レース数: {n}")
    print(f"  投資総額:     {total_bet:,}円")
    print(f"  払戻総額:     {total_return:,}円")
    print(f"  収支:         {total_profit:+,}円")
    print(f"  回収率:       {roi:.1f}%")

    # グレード別（◎の grade_tan を主軸に）
    print("\n【グレード別の的中率（grade_tan基準）】")
    by_grade = defaultdict(list)
    for r in rows:
        g = r["grade_tan"] or "(未設定)"
        by_grade[g].append(r)
    for g in sorted(by_grade.keys()):
        rs = by_grade[g]
        m = len(rs)
        tan = sum(r["tan_hit"] for r in rs)
        niren = sum(r["niren_hit"] for r in rs)
        sanren = sum(r["sanren_hit"] for r in rs)
        bet = sum(r["bet"] for r in rs)
        ret = sum(r["return"] for r in rs)
        roi = (ret / bet * 100) if bet > 0 else 0
        print(
            f"  {g:>8s}: {m:3d}R  単{tan:3d}({tan/m*100:5.1f}%) "
            f"2単{niren:3d}({niren/m*100:5.1f}%) 3単{sanren:3d}({sanren/m*100:5.1f}%) "
            f"回収率{roi:6.1f}%"
        )

    print(f"\n  CSV: {CSV_PATH}")


def main():
    if not API_KEY:
        print("ERROR: ANTHROPIC_API_KEY が設定されていません")
        sys.exit(1)

    today = date.today()
    target_dates = [today - timedelta(days=i) for i in range(1, LOOKBACK_DAYS + 1)]
    total = len(target_dates) * len(VENUES) * 12

    print(f"対象期間: {target_dates[-1].isoformat()} 〜 {target_dates[0].isoformat()}")
    print(f"想定総レース数: {total}（24会場 × 12R × {LOOKBACK_DAYS}日、未開催は自動スキップ）")
    print(f"出力CSV: {CSV_PATH}\n")

    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=FIELDNAMES).writeheader()

    rows = []
    idx = 0
    for d in target_dates:
        hd = d.strftime("%Y%m%d")
        for jcd, venue in VENUES:
            for rno in range(1, 13):
                idx += 1
                try:
                    row = run_one_race(hd, jcd, venue, rno, idx, total)
                except Exception as e:
                    print(f"  → 例外: {e}（スキップ）")
                    row = None
                if row:
                    rows.append(row)
                    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
                        csv.DictWriter(f, fieldnames=FIELDNAMES).writerow(row)
                time.sleep(SLEEP_SEC)

    print_summary(rows)


if __name__ == "__main__":
    main()

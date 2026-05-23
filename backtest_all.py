"""
backtest_all.py
前日に開催された全24会場・全レースを自動でバックテストする。

使い方:
    export ANTHROPIC_API_KEY=...
    python3 backtest_all.py

各レースで:
  1) 公式サイトから出走表（racelist + beforeinfo）を取得
  2) server.py の call_claude_parse / call_claude_predict で予想を生成
  3) 公式サイトから着順 + 払戻金を取得
  4) 予想 kaikata の的中判定 → 投資・払戻を集計
  5) backtest_all_YYYYMMDD.csv に1行追記
"""

import os
import sys
import csv
import time
from datetime import date, timedelta

from server import (
    fetch_all_pages,
    call_claude_parse,
    call_claude_predict,
    check_hit,
    API_KEY,
)
from backtest import fetch_race_result_full, calc_return

VENUES = [
    ("01", "桐生"), ("02", "戸田"), ("03", "江戸川"), ("04", "平和島"),
    ("05", "多摩川"), ("06", "浜名湖"), ("07", "蒲郡"), ("08", "常滑"),
    ("09", "津"),    ("10", "三国"), ("11", "びわこ"), ("12", "住之江"),
    ("13", "尼崎"), ("14", "鳴門"), ("15", "丸亀"), ("16", "児島"),
    ("17", "宮島"), ("18", "徳山"), ("19", "下関"), ("20", "若松"),
    ("21", "芦屋"), ("22", "福岡"), ("23", "唐津"), ("24", "大村"),
]

SLEEP_SEC = 5
DISCOVERY_SLEEP_SEC = 1
KENSHU_TYPES = ("単勝", "2連単", "3連単", "3連複")

FIELDNAMES = [
    "venue", "jcd", "race_date", "race_num", "pattern",
    "honmei", "niban", "sanban",
    "first", "second", "third",
    "bet_tan", "ret_tan", "hits_tan", "tickets_tan",
    "bet_2tan", "ret_2tan", "hits_2tan", "tickets_2tan",
    "bet_3tan", "ret_3tan", "hits_3tan", "tickets_3tan",
    "bet_3puku", "ret_3puku", "hits_3puku", "tickets_3puku",
    "bet_total", "ret_total", "profit", "all_hits",
]


def aggregate_by_kenshu(kaikata, payouts, first, second, third):
    """kaikataを券種ごとに集計"""
    stats = {k: {"bet": 0, "return": 0, "hits": 0, "tickets": 0} for k in KENSHU_TYPES}
    for k in kaikata:
        kenshu = k.get("kenshu", "")
        type_key = None
        for key in ("3連単", "3連複", "2連単", "単勝"):
            if key in kenshu:
                type_key = key
                break
        if not type_key:
            continue
        kumi = k.get("kumiawase", "")
        kin = int(k.get("kin", 0) or 0)
        ret = calc_return(kenshu, kumi, kin, payouts, first, second, third)
        s = stats[type_key]
        s["bet"] += kin
        s["return"] += ret
        s["tickets"] += 1
        if ret > 0:
            s["hits"] += 1
    return stats


def discover_held_venues(hd):
    """その日に開催されている会場（1Rの結果がある会場）を返す"""
    held = []
    print(f"[{hd}] 開催会場を確認中...")
    for jcd, name in VENUES:
        result = fetch_race_result_full(1, jcd, hd)
        mark = "○" if result else "×"
        print(f"  {mark} {name} (jcd={jcd})")
        if result:
            held.append((jcd, name))
        time.sleep(DISCOVERY_SLEEP_SEC)
    return held


def run_one_race(rno, jcd, venue_name, hd):
    """1レース分のバックテスト。CSV行のdictを返す。失敗時はNone"""
    result = fetch_race_result_full(rno, jcd, hd)
    if not result:
        print("  → 結果未取得（スキップ）")
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
        pred = call_claude_predict(shutsuba, venue_name, "")
    except Exception as e:
        print(f"  → 予想生成失敗: {e}")
        return None

    honmei = pred.get("honmei", [])
    h_num = honmei[0].get("num") if len(honmei) > 0 else None
    o_num = honmei[1].get("num") if len(honmei) > 1 else None
    s_num = honmei[2].get("num") if len(honmei) > 2 else None

    first, second, third = result["first"], result["second"], result["third"]
    payouts = result["payouts"]

    by_kenshu = aggregate_by_kenshu(pred.get("kaikata", []), payouts, first, second, third)

    bet_total = sum(s["bet"] for s in by_kenshu.values())
    ret_total = sum(s["return"] for s in by_kenshu.values())
    all_hits = sum(s["hits"] for s in by_kenshu.values())
    profit = ret_total - bet_total

    print(
        f"  ◎{h_num} ○{o_num} ▲{s_num} → {first}-{second}-{third} | "
        f"パターン{pred.get('race_pattern','?')} 投資{bet_total}円 払戻{ret_total}円 収支{profit:+d}円"
    )

    return {
        "venue": venue_name, "jcd": jcd,
        "race_date": hd, "race_num": f"{rno}R",
        "pattern": pred.get("race_pattern", ""),
        "honmei": h_num, "niban": o_num, "sanban": s_num,
        "first": first, "second": second, "third": third,
        "bet_tan": by_kenshu["単勝"]["bet"], "ret_tan": by_kenshu["単勝"]["return"],
        "hits_tan": by_kenshu["単勝"]["hits"], "tickets_tan": by_kenshu["単勝"]["tickets"],
        "bet_2tan": by_kenshu["2連単"]["bet"], "ret_2tan": by_kenshu["2連単"]["return"],
        "hits_2tan": by_kenshu["2連単"]["hits"], "tickets_2tan": by_kenshu["2連単"]["tickets"],
        "bet_3tan": by_kenshu["3連単"]["bet"], "ret_3tan": by_kenshu["3連単"]["return"],
        "hits_3tan": by_kenshu["3連単"]["hits"], "tickets_3tan": by_kenshu["3連単"]["tickets"],
        "bet_3puku": by_kenshu["3連複"]["bet"], "ret_3puku": by_kenshu["3連複"]["return"],
        "hits_3puku": by_kenshu["3連複"]["hits"], "tickets_3puku": by_kenshu["3連複"]["tickets"],
        "bet_total": bet_total, "ret_total": ret_total,
        "profit": profit, "all_hits": all_hits,
    }


def print_summary(rows):
    if not rows:
        print("\n対象レースが取得できませんでした")
        return

    n = len(rows)
    total_bet = sum(r["bet_total"] for r in rows)
    total_ret = sum(r["ret_total"] for r in rows)
    total_profit = total_ret - total_bet

    print("\n" + "=" * 72)
    print(f"  バックテスト集計  対象 {n} レース")
    print("=" * 72)

    # 券種別
    print("\n■ 券種別")
    print(f"  {'券種':<8}{'投資':>10}{'払戻':>10}{'収支':>10}{'的中':>10}{'的中率':>10}{'回収率':>10}")
    for key in KENSHU_TYPES:
        bet_col = f"bet_{ {'単勝':'tan','2連単':'2tan','3連単':'3tan','3連複':'3puku'}[key] }"
        ret_col = f"ret_{ {'単勝':'tan','2連単':'2tan','3連単':'3tan','3連複':'3puku'}[key] }"
        hit_col = f"hits_{ {'単勝':'tan','2連単':'2tan','3連単':'3tan','3連複':'3puku'}[key] }"
        tick_col = f"tickets_{ {'単勝':'tan','2連単':'2tan','3連単':'3tan','3連複':'3puku'}[key] }"
        bet = sum(r[bet_col] for r in rows)
        ret = sum(r[ret_col] for r in rows)
        hits = sum(r[hit_col] for r in rows)
        ticks = sum(r[tick_col] for r in rows)
        hr = (hits / ticks * 100) if ticks else 0
        rr = (ret / bet * 100) if bet else 0
        print(f"  {key:<8}{bet:>10,}{ret:>10,}{ret-bet:>+10,}{hits:>5}/{ticks:<4}{hr:>9.1f}%{rr:>9.1f}%")

    # 会場別
    print("\n■ 会場別")
    print(f"  {'会場':<6}{'R数':>5}{'投資':>10}{'払戻':>10}{'収支':>10}{'的中':>8}{'回収率':>10}")
    by_venue = {}
    for r in rows:
        v = r["venue"]
        b = by_venue.setdefault(v, {"races": 0, "bet": 0, "ret": 0, "hits": 0})
        b["races"] += 1
        b["bet"] += r["bet_total"]
        b["ret"] += r["ret_total"]
        b["hits"] += r["all_hits"]
    for v, b in sorted(by_venue.items(), key=lambda kv: -kv[1]["ret"] + kv[1]["bet"]):
        rr = (b["ret"] / b["bet"] * 100) if b["bet"] else 0
        print(f"  {v:<6}{b['races']:>5}{b['bet']:>10,}{b['ret']:>10,}{b['ret']-b['bet']:>+10,}{b['hits']:>8}{rr:>9.1f}%")

    # 全体
    print("\n■ 全体")
    print(f"  レース数:   {n}")
    print(f"  投資総額:   {total_bet:,}円")
    print(f"  払戻総額:   {total_ret:,}円")
    print(f"  収支:       {total_profit:+,}円")
    if total_bet > 0:
        print(f"  回収率:     {total_ret / total_bet * 100:.1f}%")


def main():
    if not API_KEY:
        print("ERROR: ANTHROPIC_API_KEY が設定されていません")
        sys.exit(1)

    target_date = date.today() - timedelta(days=1)
    hd = target_date.strftime("%Y%m%d")
    csv_path = os.path.join(os.path.dirname(__file__), f"backtest_all_{hd}.csv")

    print(f"対象日: {target_date.isoformat()} (hd={hd})")
    print(f"CSV出力先: {csv_path}\n")

    held = discover_held_venues(hd)
    if not held:
        print("\n開催会場がありません。")
        sys.exit(0)

    total_planned = len(held) * 12
    print(f"\n開催会場: {len(held)}場  予定レース数: 最大 {total_planned} レース\n")

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=FIELDNAMES).writeheader()

    rows = []
    current = 0
    for jcd, venue_name in held:
        for rno in range(1, 13):
            current += 1
            print(f"\n[{current}/{total_planned}] {venue_name} {rno}R 処理中...")
            row = run_one_race(rno, jcd, venue_name, hd)
            if row:
                rows.append(row)
                with open(csv_path, "a", newline="", encoding="utf-8") as f:
                    csv.DictWriter(f, fieldnames=FIELDNAMES).writerow(row)
            time.sleep(SLEEP_SEC)

    print_summary(rows)
    print(f"\nCSV: {csv_path}")


if __name__ == "__main__":
    main()

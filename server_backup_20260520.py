"""
CC ボートレース予想 AI - ローカルサーバー v2
起動方法: python3 server.py
"""

import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, date
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ========== 設定 ==========
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
PORT = int(os.environ.get("PORT", 10000))
DB_PATH = os.path.join(os.path.dirname(__file__), "predictions.db")
# ==========================

VENUES = [
    ("01", "桐生"), ("02", "戸田"), ("03", "江戸川"), ("04", "平和島"),
    ("05", "多摩川"), ("06", "浜名湖"), ("07", "蒲郡"), ("08", "常滑"),
    ("09", "津"),    ("10", "三国"), ("11", "びわこ"), ("12", "住之江"),
    ("13", "尼崎"), ("14", "鳴門"), ("15", "丸亀"), ("16", "児島"),
    ("17", "宮島"), ("18", "徳山"), ("19", "下関"), ("20", "若松"),
    ("21", "芦屋"), ("22", "福岡"), ("23", "唐津"), ("24", "大村"),
]

# ===== /bulk_predict のバックグラウンドジョブ状態 =====
# Renderの長時間レスポンス制限を避けるため、リクエスト即時応答→別スレッドで実行→
# /bulk_status をポーリングして進捗取得する方式に変更
BULK_LOCK = threading.Lock()
BULK_STATE = {
    "status": "idle",   # idle / starting / running / done / cancelled / error
    "progress": 0,
    "total": 0,
    "current_label": "",
    "grade_s": [],
    "race_date": "",
    "range": "all",
    "error": None,
    "cancel": False,
}


def _rnos_for_range(range_filter):
    """range 文字列を rno のリストに変換する。
    morning: 1〜6R / evening(または afternoon): 7〜12R / all: 1〜12R"""
    r = (range_filter or "all").strip().lower()
    if r == "morning":
        return list(range(1, 7))
    if r in ("evening", "afternoon"):
        return list(range(7, 13))
    return list(range(1, 13))


# ===== DB初期化 =====
def init_db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        race_date TEXT NOT NULL,
        venue TEXT NOT NULL,
        race_num TEXT NOT NULL,
        honmei_num INTEGER,
        honmei_name TEXT,
        niban_num INTEGER,
        niban_name TEXT,
        sanban_num INTEGER,
        sanban_name TEXT,
        grade_tan TEXT,
        grade_niren TEXT,
        grade_sanren TEXT,
        budget INTEGER,
        raw_json TEXT
    );
    CREATE TABLE IF NOT EXISTS results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        race_date TEXT NOT NULL,
        venue TEXT NOT NULL,
        race_num TEXT NOT NULL,
        first INTEGER,
        second INTEGER,
        third INTEGER,
        recorded_at TEXT NOT NULL,
        UNIQUE(race_date, venue, race_num)
    );
    CREATE TABLE IF NOT EXISTS bet_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        prediction_id INTEGER,
        kenshu TEXT,
        kumiawase TEXT,
        kin INTEGER,
        odds REAL,
        hit INTEGER DEFAULT 0,
        payout INTEGER DEFAULT 0,
        FOREIGN KEY(prediction_id) REFERENCES predictions(id)
    );
    """)
    con.commit()
    con.close()


def save_prediction(race_date, venue, race_num, pred_json):
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    honmei = pred_json.get("honmei", [])
    kaikata = pred_json.get("kaikata", [])
    budget = sum(k.get("kin", 0) for k in kaikata)

    h = honmei[0] if len(honmei) > 0 else {}
    o = honmei[1] if len(honmei) > 1 else {}
    s = honmei[2] if len(honmei) > 2 else {}
    grade = pred_json.get("grade", {})

    cur.execute("""
        INSERT INTO predictions
        (created_at, race_date, venue, race_num,
         honmei_num, honmei_name, niban_num, niban_name, sanban_num, sanban_name,
         grade_tan, grade_niren, grade_sanren, budget, raw_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        datetime.now().isoformat(),
        race_date, venue, race_num,
        h.get("num"), h.get("name"),
        o.get("num"), o.get("name"),
        s.get("num"), s.get("name"),
        grade.get("tan"), grade.get("niren"), grade.get("sanren"),
        budget,
        json.dumps(pred_json, ensure_ascii=False)
    ))
    pred_id = cur.lastrowid

    for k in kaikata:
        cur.execute("""
            INSERT INTO bet_results (prediction_id, kenshu, kumiawase, kin, odds)
            VALUES (?,?,?,?,?)
        """, (pred_id, k.get("kenshu"), k.get("kumiawase"), k.get("kin", 0), float(k.get("odds", 0) or 0)))

    con.commit()
    con.close()
    return pred_id


def save_result(race_date, venue, race_num, first, second, third):
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO results (race_date, venue, race_num, first, second, third, recorded_at)
        VALUES (?,?,?,?,?,?,?)
    """, (race_date, venue, race_num, first, second, third, datetime.now().isoformat()))

    # 的中判定
    cur.execute("""
        SELECT br.id, br.prediction_id, br.kenshu, br.kumiawase, br.kin, br.odds
        FROM bet_results br
        JOIN predictions p ON br.prediction_id = p.id
        WHERE p.race_date=? AND p.venue=? AND p.race_num=?
    """, (race_date, venue, race_num))
    bets = cur.fetchall()

    for bet_id, pred_id, kenshu, kumiawase, kin, odds in bets:
        hit = check_hit(kenshu, kumiawase, first, second, third)
        payout = int(kin * odds) if hit else 0
        cur.execute("UPDATE bet_results SET hit=?, payout=? WHERE id=?", (1 if hit else 0, payout, bet_id))

    con.commit()
    con.close()


def check_hit(kenshu, kumiawase, first, second, third):
    nums = [int(x) for x in re.findall(r'\d+', kumiawase or "")]
    if not nums:
        return False
    if "3連単" in kenshu:
        return len(nums) >= 3 and nums[0] == first and nums[1] == second and nums[2] == third
    if "3連複" in kenshu:
        return len(nums) >= 3 and set(nums[:3]) == {first, second, third}
    if "2連単" in kenshu:
        return len(nums) >= 2 and nums[0] == first and nums[1] == second
    if "2連複" in kenshu:
        return len(nums) >= 2 and set(nums[:2]) == {first, second}
    if "単勝" in kenshu:
        return len(nums) >= 1 and nums[0] == first
    return False


def get_stats():
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()

    # 月別集計
    cur.execute("""
        SELECT
            substr(p.race_date, 1, 7) as month,
            COUNT(DISTINCT p.id) as races,
            SUM(br.kin) as total_bet,
            SUM(br.payout) as total_payout,
            SUM(br.hit) as hits,
            COUNT(br.id) as bets
        FROM predictions p
        JOIN bet_results br ON br.prediction_id = p.id
        GROUP BY month
        ORDER BY month DESC
        LIMIT 12
    """)
    monthly = [{"month": r[0], "races": r[1], "bet": r[2] or 0,
                "payout": r[3] or 0, "hits": r[4] or 0, "bets": r[5] or 0} for r in cur.fetchall()]

    # 券種別集計
    cur.execute("""
        SELECT kenshu,
            COUNT(*) as bets,
            SUM(hit) as hits,
            SUM(kin) as total_bet,
            SUM(payout) as total_payout
        FROM bet_results
        GROUP BY kenshu
        ORDER BY total_bet DESC
    """)
    by_kenshu = [{"kenshu": r[0], "bets": r[1], "hits": r[2] or 0,
                  "bet": r[3] or 0, "payout": r[4] or 0} for r in cur.fetchall()]

    # 直近の予想一覧
    cur.execute("""
        SELECT p.id, p.race_date, p.venue, p.race_num,
               p.honmei_name, p.grade_tan,
               SUM(br.kin) as bet,
               SUM(br.payout) as payout,
               SUM(br.hit) as hits,
               r.first, r.second, r.third
        FROM predictions p
        LEFT JOIN bet_results br ON br.prediction_id = p.id
        LEFT JOIN results r ON r.race_date=p.race_date AND r.venue=p.venue AND r.race_num=p.race_num
        GROUP BY p.id
        ORDER BY p.created_at DESC
        LIMIT 30
    """)
    recent = [{"id": r[0], "date": r[1], "venue": r[2], "race": r[3],
               "honmei": r[4], "grade": r[5], "bet": r[6] or 0, "payout": r[7] or 0,
               "hits": r[8] or 0, "result": f"{r[9]}-{r[10]}-{r[11]}" if r[9] else None}
              for r in cur.fetchall()]

    # 総合
    cur.execute("""
        SELECT COUNT(DISTINCT p.id), SUM(br.kin), SUM(br.payout), SUM(br.hit), COUNT(br.id)
        FROM predictions p JOIN bet_results br ON br.prediction_id=p.id
    """)
    row = cur.fetchone()
    total = {"races": row[0] or 0, "bet": row[1] or 0, "payout": row[2] or 0,
             "hits": row[3] or 0, "bets": row[4] or 0}

    con.close()
    return {"monthly": monthly, "by_kenshu": by_kenshu, "recent": recent, "total": total}


# ===== スクレイピング =====
def fetch_html(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "ja,en;q=0.9",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as res:
        return res.read().decode("utf-8", errors="replace")


def strip_html(html):
    html = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.DOTALL)
    html = re.sub(r'<style[^>]*>.*?</style>', ' ', html, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.replace('&nbsp;', ' ').strip()


def fetch_player_detail(reg_no: str, jcd: str) -> str:
    """選手詳細ページからコース別成績・当地成績・直近6走を取得"""
    # 選手詳細ページ
    url = f"https://www.boatrace.jp/owpc/pc/data/racersinfo/profile?toban={reg_no}"
    try:
        html = fetch_html(url)
        text = strip_html(html)
        return text[:3000]
    except Exception as e:
        print(f"  選手詳細取得失敗 {reg_no}: {e}")
        return ""


def extract_reg_numbers(racelist_text: str) -> list:
    """出走表テキストから登録番号を抽出"""
    # 4桁の登録番号を抽出
    regs = re.findall(r'(\d{4})', racelist_text)
    # 重複除去して最大6件
    seen = []
    for r in regs:
        if r not in seen and len(seen) < 6:
            seen.append(r)
    return seen


def fetch_all_pages(rno, jcd, hd):
    import concurrent.futures
    base = "https://www.boatrace.jp/owpc/pc/race"
    params = f"rno={rno}&jcd={jcd}&hd={hd}"
    urls = {
        "racelist":   f"{base}/racelist?{params}",
        "beforeinfo": f"{base}/beforeinfo?{params}",
    }

    def fetch_one(item):
        key, url = item
        try:
            result = strip_html(fetch_html(url))
            print(f"  取得OK: {key} ({len(result)}文字)")
            return key, result
        except Exception as e:
            print(f"  取得失敗: {key} - {e}")
            return key, ""

    pages = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        for key, text in executor.map(fetch_one, urls.items()):
            pages[key] = text

    # beforeinfo 取得状況の検証ログ（展示タイム・ST が含まれているか確認）
    bi = pages.get("beforeinfo", "") or ""
    if not bi:
        print(f"  [警告] beforeinfo が取得できませんでした jcd={jcd} rno={rno} hd={hd}")
    else:
        has_tenji = ("展示" in bi) or ("展示タイム" in bi)
        has_st = ("ST" in bi) or ("スタート" in bi) or ("進入" in bi)
        if not has_tenji:
            print(f"  [警告] beforeinfo に『展示』が含まれていません jcd={jcd} rno={rno}")
        if not has_st:
            print(f"  [警告] beforeinfo に『ST/進入』が含まれていません jcd={jcd} rno={rno}")
        if has_tenji and has_st:
            print(f"  beforeinfo 検証OK: 展示・ST情報あり")

    pages["player_details"] = {}
    return pages


def fetch_race_result(rno, jcd, hd):
    """レース結果を公式サイトから取得"""
    url = f"https://www.boatrace.jp/owpc/pc/race/raceresult?rno={rno}&jcd={jcd}&hd={hd}"
    try:
        html = fetch_html(url)
        text = strip_html(html)
        # 1〜3着を抽出
        nums = re.findall(r'[1-6]', text[:500])
        if len(nums) >= 3:
            return int(nums[0]), int(nums[1]), int(nums[2])
    except Exception as e:
        print(f"  結果取得失敗: {e}")
    return None, None, None


# ===== Claude API =====
def claude_api(prompt, max_tokens=1000):
    payload = json.dumps({
        "model": "claude-sonnet-4-5",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}]
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": API_KEY,
            "anthropic-version": "2023-06-01"
        }
    )
    with urllib.request.urlopen(req, timeout=60) as res:
        data = json.loads(res.read().decode("utf-8"))
    return "".join(b["text"] for b in data["content"] if b["type"] == "text").strip()


def call_claude_parse(pages):
    racelist_text = pages.get('racelist', '') or ''
    beforeinfo_text = pages.get('beforeinfo', '') or ''

    if not beforeinfo_text:
        print("  [警告] call_claude_parse: beforeinfo が空のままClaudeに渡します（展示タイム・STが取れません）")

    prompt = f"""以下はボートレース公式サイトから取得したテキストです。
6選手の情報を抽出し、以下の形式で出走表テキストを作成してください。

出力形式（1選手1行）:
[号艇]号艇 [選手名] 登録[番号] [級別] 勝率[X.XX] 2連率[XX.X]% モーター勝率[XX.X] ST[0.XX] 展示タイム[X.XX]

【重要・絶対遵守】
- 「直前情報」セクションに展示タイム・STタイム・進入コースが必ず記載されています。
  必ずそこから抽出して各選手の行に「ST[0.XX]」「展示タイム[X.XX]」を含めてください。
- 「直前情報」が空、または展示タイム/STが本当に存在しない場合のみ、その項目を省略してください。
  その場合は該当選手の行末に「※展示・ST不明」と明記してください。
- 必ず1号艇〜6号艇の全6艇を出力してください。
- 出走表テキストのみ出力し、説明文は不要です。

=== 出走表 ===
{racelist_text[:8000]}

=== 直前情報（展示タイム・ST・進入） ===
{beforeinfo_text[:6000]}"""
    return claude_api(prompt, 800)


def call_claude_predict(shutsuba_text, venue, water):
    prompt = f"""以下の出走表を分析して予想をJSONで返してください。

会場: {venue}
水面・天候: {water or '不明'}

出走表:
{shutsuba_text}

評価の優先順位：
1. 展示タイム（最重要）
2. STタイム
3. モーター勝率
4. 級別

ルール：
- 展示タイムが良い順に◎○▲を決める
- STが0.18以上の選手は評価を下げる
- 号艇番号は評価に使わない
- 6号艇は予想対象外とする。
◎○▲は必ず1〜5号艇の中から選ぶこと。

買い目：
- ◎の単勝
- 2連単：◎→○と◎→▲の2点
- 3連単：◎→○→▲の1点
- 合計500円前後

敗因シナリオを3つ生成すること。

以下のJSON形式のみで返答（コードブロック不要）：
{{
  "raceName":"","venue":"{venue}","raceNum":"","raceType":"","waterCond":"{water or '不明'}",
  "honmeiPct":"",
  "honmei":[
    {{"mark":"◎","num":0,"name":"","reg":"","grade":"","pct":"","pop":""}},
    {{"mark":"○","num":0,"name":"","reg":"","grade":"","pct":"","pop":""}},
    {{"mark":"▲","num":0,"name":"","reg":"","grade":"","pct":"","pop":""}}
  ],
  "shutsuba":[
    {{"num":1,"name":"","reg":"","grade":"","winRate":0,"nirenRate":0,"motorRate":0,"st":0,"tenjiTime":0}}
  ],
  "grade":{{"tan":"","niren":"","sanren":""}},
  "haiin":["敗因1","敗因2","敗因3"],
  "race_pattern":"A",
  "miken":false,
  "budget":"500円前後",
  "kaikata":[
    {{"kenshu":"単勝","kumiawase":"","odds":"","kuchi":1,"kin":100,"konkyo":""}},
    {{"kenshu":"2連単","kumiawase":"→","odds":"","kuchi":1,"kin":100,"konkyo":""}},
    {{"kenshu":"2連単","kumiawase":"→","odds":"","kuchi":1,"kin":100,"konkyo":""}},
    {{"kenshu":"3連単","kumiawase":"→→","odds":"","kuchi":1,"kin":200,"konkyo":""}}
  ]
}}

shutsuba は 1〜6号艇の6件すべてを出力すること。"""
    raw = claude_api(prompt, 3000)
    clean = re.sub(r"```json|```", "", raw).strip()
    return json.loads(clean)


# ===== /bulk_predict のバックグラウンドワーカー =====
def _bulk_worker(range_filter="all"):
    """全VENUES × 指定レンジのレースを順次処理し、グレードS該当をDBに保存しつつ
    BULK_STATE に進捗を書き込む。/bulk_cancel が立てたフラグを毎レースで監視する。"""
    today = date.today()
    hd = today.strftime("%Y%m%d")
    race_date_str = today.isoformat()
    rnos = _rnos_for_range(range_filter)
    total = len(VENUES) * len(rnos)

    with BULK_LOCK:
        BULK_STATE["status"] = "running"
        BULK_STATE["progress"] = 0
        BULK_STATE["total"] = total
        BULK_STATE["current_label"] = ""
        BULK_STATE["grade_s"] = []
        BULK_STATE["race_date"] = race_date_str
        BULK_STATE["range"] = range_filter
        BULK_STATE["error"] = None

    current = 0
    saved = 0
    try:
        for jcd, venue_name in VENUES:
            for rno in rnos:
                with BULK_LOCK:
                    if BULK_STATE["cancel"]:
                        BULK_STATE["status"] = "cancelled"
                        return
                current += 1
                with BULK_LOCK:
                    BULK_STATE["progress"] = current
                    BULK_STATE["current_label"] = f"{venue_name} {rno}R"

                try:
                    pages = fetch_all_pages(rno, jcd, hd)
                except Exception as e:
                    print(f"  bulk fetch_error: {venue_name} {rno}R {e}")
                    time.sleep(3)
                    continue

                if not pages.get("racelist"):
                    time.sleep(3)
                    continue

                try:
                    shutsuba = call_claude_parse(pages)
                    pred = call_claude_predict(shutsuba, venue_name, "")
                except Exception as e:
                    print(f"  bulk ai_error: {venue_name} {rno}R {e}")
                    time.sleep(3)
                    continue

                grade = pred.get("grade", {}) or {}
                is_s = any(
                    str(grade.get(k, "") or "").upper().startswith("S")
                    for k in ("tan", "niren", "sanren")
                )
                if is_s:
                    try:
                        pred_id = save_prediction(
                            race_date_str, venue_name, f"{rno}R", pred
                        )
                        saved += 1
                        honmei = pred.get("honmei", []) or []
                        h = honmei[0] if len(honmei) > 0 else {}
                        o = honmei[1] if len(honmei) > 1 else {}
                        s = honmei[2] if len(honmei) > 2 else {}
                        hit = {
                            "id": pred_id,
                            "venue": venue_name,
                            "race_num": f"{rno}R",
                            "honmei": h,
                            "niban": o,
                            "sanban": s,
                            "grade_tan": grade.get("tan"),
                            "grade_niren": grade.get("niren"),
                            "grade_sanren": grade.get("sanren"),
                        }
                        with BULK_LOCK:
                            BULK_STATE["grade_s"].insert(0, hit)
                    except Exception as e:
                        print(f"  bulk save_error: {venue_name} {rno}R {e}")

                time.sleep(3)

        with BULK_LOCK:
            if BULK_STATE["status"] != "cancelled":
                BULK_STATE["status"] = "done"
                BULK_STATE["progress"] = total
    except Exception as e:
        import traceback; traceback.print_exc()
        with BULK_LOCK:
            BULK_STATE["status"] = "error"
            BULK_STATE["error"] = str(e)


# ===== HTTPサーバー =====
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  [{self.address_string()}] {fmt % args}")

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/health":
            self.send_json(200, {"status": "ok", "api_key_set": bool(API_KEY)})
            return

        if parsed.path == "/stats":
            self.send_json(200, get_stats())
            return

        if parsed.path in ("/", "/index.html"):
            html_path = os.path.join(os.path.dirname(__file__), "index.html")
            if os.path.exists(html_path):
                with open(html_path, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_json(404, {"error": "index.html が見つかりません"})
            return

        if parsed.path == "/bulk_status":
            self._handle_bulk_status()
            return

        if parsed.path == "/bulk_results":
            self._handle_bulk_results()
            return

        if parsed.path == "/prediction":
            qs = parse_qs(parsed.query)
            pid = qs.get("id", [None])[0]
            self._handle_prediction(pid)
            return

        self.send_json(404, {"error": "Not found"})

    # ===== /bulk_status : バックグラウンドジョブの進捗を返す =====
    def _handle_bulk_status(self):
        with BULK_LOCK:
            snapshot = {
                "status": BULK_STATE["status"],
                "progress": BULK_STATE["progress"],
                "total": BULK_STATE["total"],
                "current_label": BULK_STATE["current_label"],
                "grade_s": list(BULK_STATE["grade_s"]),
                "race_date": BULK_STATE["race_date"],
                "range": BULK_STATE["range"],
                "error": BULK_STATE["error"],
            }
        self.send_json(200, snapshot)

    # ===== /bulk_results : 今日のグレードS一覧 =====
    def _handle_bulk_results(self):
        today = date.today().isoformat()
        con = sqlite3.connect(DB_PATH, timeout=30)
        cur = con.cursor()
        cur.execute("""
            SELECT id, race_date, venue, race_num,
                   honmei_num, honmei_name, niban_num, niban_name,
                   sanban_num, sanban_name,
                   grade_tan, grade_niren, grade_sanren
            FROM predictions
            WHERE race_date = ?
            ORDER BY id DESC
        """, (today,))
        rows = []
        for r in cur.fetchall():
            grades = (r[10] or "", r[11] or "", r[12] or "")
            if not any(str(g).upper().startswith("S") for g in grades):
                continue
            rows.append({
                "id": r[0], "race_date": r[1],
                "venue": r[2], "race_num": r[3],
                "honmei": {"num": r[4], "name": r[5]},
                "niban": {"num": r[6], "name": r[7]},
                "sanban": {"num": r[8], "name": r[9]},
                "grade_tan": r[10],
                "grade_niren": r[11],
                "grade_sanren": r[12],
            })
        con.close()
        self.send_json(200, {"date": today, "races": rows})

    # ===== /prediction?id=X : 単一予想の生JSON =====
    def _handle_prediction(self, pid):
        if not pid or not pid.isdigit():
            self.send_json(400, {"error": "id が必要です"})
            return
        con = sqlite3.connect(DB_PATH, timeout=30)
        cur = con.cursor()
        cur.execute(
            "SELECT race_date, venue, race_num, raw_json FROM predictions WHERE id=?",
            (int(pid),),
        )
        row = cur.fetchone()
        con.close()
        if not row:
            self.send_json(404, {"error": "見つかりません"})
            return
        try:
            pred = json.loads(row[3])
        except Exception:
            pred = {}
        pred["race_date"] = row[0]
        pred["venue"] = row[1]
        pred["race_num"] = row[2]
        self.send_json(200, pred)

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        # /bulk_predict : バックグラウンド起動、即座に応答を返す
        if parsed.path == "/bulk_predict":
            if not API_KEY:
                self.send_json(500, {"error": "ANTHROPIC_API_KEY未設定"})
                return
            range_filter = "all"
            if body:
                try:
                    payload = json.loads(body)
                    range_filter = (payload.get("range") or "all").strip().lower()
                except Exception:
                    range_filter = "all"
            qs = parse_qs(parsed.query)
            if "range" in qs:
                range_filter = (qs["range"][0] or "all").strip().lower()
            today = date.today()
            rnos = _rnos_for_range(range_filter)
            total = len(VENUES) * len(rnos)
            with BULK_LOCK:
                if BULK_STATE["status"] in ("starting", "running"):
                    self.send_json(409, {
                        "error": "既に処理中です",
                        "status": BULK_STATE["status"],
                        "progress": BULK_STATE["progress"],
                        "total": BULK_STATE["total"],
                    })
                    return
                BULK_STATE.update({
                    "status": "starting",
                    "progress": 0,
                    "total": total,
                    "current_label": "",
                    "grade_s": [],
                    "race_date": today.isoformat(),
                    "range": range_filter,
                    "error": None,
                    "cancel": False,
                })
            t = threading.Thread(
                target=_bulk_worker, kwargs={"range_filter": range_filter}, daemon=True
            )
            t.start()
            self.send_json(200, {
                "status": "started",
                "race_date": today.isoformat(),
                "range": range_filter,
                "total": total,
            })
            return

        # /bulk_cancel : 走行中のジョブに中断要求
        if parsed.path == "/bulk_cancel":
            with BULK_LOCK:
                if BULK_STATE["status"] in ("starting", "running"):
                    BULK_STATE["cancel"] = True
                self.send_json(200, {"ok": True, "status": BULK_STATE["status"]})
            return

        # /scrape
        if parsed.path == "/scrape":
            try:
                data = json.loads(body)
                rno = data.get("rno", "")
                jcd = data.get("jcd", "")
                hd  = data.get("hd", "")
                if rno and jcd and hd:
                    print(f"  複数ページ取得中: jcd={jcd} rno={rno} hd={hd}")
                    pages = fetch_all_pages(rno, jcd, hd)
                else:
                    url = data.get("url", "").strip()
                    if not url:
                        self.send_json(400, {"error": "url または rno/jcd/hd が必要です"})
                        return
                    html = fetch_html(url)
                    pages = {"racelist": strip_html(html)}

                shutsuba_text = call_claude_parse(pages)
                print(f"  出走表抽出完了:\n{shutsuba_text}")
                self.send_json(200, {"shutsuba_text": shutsuba_text, "parsed": True})

            except Exception as e:
                import traceback; traceback.print_exc()
                self.send_json(500, {"error": str(e)})
            return

        # /predict
        if parsed.path == "/predict":
            try:
                data = json.loads(body)
                shutsuba_text = data.get("shutsuba_text", "").strip()
                venue  = data.get("venue", "")
                water  = data.get("water", "")
                race_date = data.get("race_date", date.today().isoformat())
                race_num  = data.get("race_num", "")

                if not shutsuba_text:
                    self.send_json(400, {"error": "出走表テキストが空です"})
                    return

                print(f"  AI予想生成中... (venue={venue})")
                result = call_claude_predict(shutsuba_text, venue, water)

                # 予想をDBに保存
                pred_id = save_prediction(race_date, venue, race_num, result)
                result["prediction_id"] = pred_id
                print(f"  予想保存完了 id={pred_id}")

                self.send_json(200, result)

            except json.JSONDecodeError as e:
                self.send_json(500, {"error": f"AIの返答をパースできませんでした: {e}"})
            except Exception as e:
                import traceback; traceback.print_exc()
                self.send_json(500, {"error": str(e)})
            return

        # /result : レース結果を登録
        if parsed.path == "/result":
            try:
                data = json.loads(body)
                race_date = data.get("race_date", "")
                venue     = data.get("venue", "")
                race_num  = data.get("race_num", "")
                rno = data.get("rno", "")
                jcd = data.get("jcd", "")
                hd  = data.get("hd", "")

                first  = data.get("first")
                second = data.get("second")
                third  = data.get("third")

                # 自動取得
                if (not first) and rno and jcd and hd:
                    first, second, third = fetch_race_result(rno, jcd, hd)

                if not (first and second and third):
                    self.send_json(400, {"error": "結果が取得できませんでした。手動で入力してください。"})
                    return

                save_result(race_date, venue, race_num, first, second, third)
                self.send_json(200, {"ok": True, "first": first, "second": second, "third": third})

            except Exception as e:
                import traceback; traceback.print_exc()
                self.send_json(500, {"error": str(e)})
            return

        self.send_json(404, {"error": "Not found"})


def main():
    init_db()
    if not API_KEY:
        print("=" * 55)
        print("  警告: ANTHROPIC_API_KEY が設定されていません")
        print("=" * 55)

    print(f"\nCC ボートレース予想 AI サーバー起動中...")
    print(f"  PORT: {PORT}")
    print(f"  DB: {DB_PATH}")
    print(f"  停止するには Ctrl+C を押してください\n")

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nサーバーを停止しました")


if __name__ == "__main__":
    main()

"""
트렌드 대체 지표 일일 수집기 (v0.1)

틱톡·인스타그램처럼 공식 API로 수치를 받을 수 없는 SNS 대신,
공식 API로 받을 수 있는 데이터로 "입소문"을 재는 대체 지표를 매일 쌓는다.
이 지표들은 과거 기록을 다시 받을 수 없어서, 오늘부터 매일 모아야 한다.

키워드마다 매일 기록하는 것 (기준일 = 어제, 하루가 다 끝난 날)
  blog_new       어제 새로 올라온 블로그 글 수        (블로그 검색 API, 글 날짜로 셈)
  blog_total     블로그 전체 검색 결과 수
  cafe_total     카페 전체 검색 결과 수                (카페 글에는 날짜가 없어서 전체 수만 기록)
  cafe_new       전날 기록 대비 늘어난 카페 글 수       (cafe_total 차이로 계산, 둘째 날부터)
  news_new       어제 나온 뉴스 기사 수                (뉴스 검색 API, 기사 시각으로 셈)
  news_total     뉴스 전체 검색 결과 수
  shop_total     등록된 쇼핑 상품 수                   (선택: 네이버 개발자센터 키가 있을 때만)
  buy_q / soldout / review
                 어제 블로그 글 중 구매처 질문, 품절·재입고, 구매 후기 표현이 들어간 글 수
                 (임시 규칙 기반. 나중에 텍스트 분류 모델로 교체)
  *_capped       최신 글 1,000개 안에 어제 글이 다 안 들어가서 실제보다 적게 셌을 수 있음 (1이면 하한값)

실행
  python collect_buzz.py                     # 어제 기준으로 watchlist.txt 전체 수집
  python collect_buzz.py --date 2026-11-23   # 기준일 지정 (블로그·뉴스는 최근 글 1,000개 안에 있어야 셀 수 있음)
  python collect_buzz.py --limit 3           # 키워드 3개만 시험

결과 (output/ 폴더, GitHub에 올리지 않음)
  buzz_daily.csv            날짜·키워드별 지표 한 줄씩. 이미 모은 날짜·키워드는 다시 모으지 않음
  posts/YYYY-MM-DD.jsonl    그날 블로그 글 제목·요약·링크 (구매 의도 분류 모델 학습용 원자료)
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

HUB = "https://naverapihub.apigw.ntruss.com/search/v1"
HUB_HEADERS = {
    "X-NCP-APIGW-API-KEY-ID": os.getenv("NAVER_CLIENT_ID", ""),
    "X-NCP-APIGW-API-KEY": os.getenv("NAVER_CLIENT_SECRET", ""),
}
SHOP_URL = "https://openapi.naver.com/v1/search/shop.json"   # API HUB에는 쇼핑 검색이 없어서 개발자센터 API 사용
DEV_ID, DEV_SECRET = os.getenv("NAVER_DEV_CLIENT_ID", ""), os.getenv("NAVER_DEV_CLIENT_SECRET", "")

KST = timezone(timedelta(hours=9))   # Windows에서도 추가 설치 없이 쓰려고 고정 시차 사용
OUT = HERE / "output"
DAILY_CSV = OUT / "buzz_daily.csv"
FIELDS = ["date", "keyword", "category",
          "blog_new", "blog_capped", "blog_total",
          "cafe_total", "cafe_new",
          "news_new", "news_capped", "news_total",
          "shop_total",
          "buy_q", "soldout", "review",
          "collected_at"]

# 구매 의도 임시 규칙. 글 제목+요약에 이 표현이 있으면 센다. (정답 데이터가 쌓이면 모델로 교체)
INTENT_RULES = {
    "buy_q": re.compile(r"어디서\s*(사|팔|구매|구입)|파는\s*곳|판매처|구매처|살\s*수\s*있는\s*곳|어디\s*팔"),
    "soldout": re.compile(r"품절|재입고|완판|구하기\s*힘|오픈런|웨이팅"),
    "review": re.compile(r"구매\s*후기|내돈내산|사봤|사\s*왔|샀어요|샀다|먹어봤|먹어\s*본|솔직\s*후기"),
}

MAX_START = 1000      # 검색 API는 앞에서 1,000번째 결과까지만 줌
PAGE = 100
SLEEP = 0.12          # 호출 사이 쉬는 시간(초)
CALLS = 0


class QuotaExceeded(Exception):
    pass


# ---------------------------------------------------------------- API 호출
def call(url, params, headers):
    """GET 한 번. 일시 오류는 3번까지 다시 시도하고, 하루 한도(429)면 멈춘다."""
    global CALLS
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=15)
        except requests.RequestException as e:
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))
            continue
        CALLS += 1
        time.sleep(SLEEP)
        if r.status_code == 429:
            raise QuotaExceeded("하루 호출 한도 초과(429)")
        if r.status_code >= 500 and attempt < 3:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"{r.status_code} {r.text[:200]}")
        return r.json()


def clean(s):
    s = re.sub(r"<[^>]+>", "", s or "")
    return s.replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").strip()


# ---------------------------------------------------------------- 날짜별로 세기
def count_day(kind, keyword, day, date_of):
    """최신순으로 100개씩 넘기며 기준일 글을 센다. 기준일보다 오래된 글이 나오면 멈춘다.
    date_of(item) → 그 글의 날짜(date). 반환: (기준일 글 목록, 전체 수, capped 여부)"""
    found, total = [], 0
    for start in range(1, MAX_START + 1, PAGE):
        data = call(f"{HUB}/{kind}", {"query": keyword, "display": PAGE, "start": start, "sort": "date"}, HUB_HEADERS)
        total = data.get("total", 0)
        items = data.get("items", [])
        if not items:
            return found, total, False
        for it in items:
            d = date_of(it)
            if d is None or d > day:
                continue            # 오늘 글 등 기준일 이후 글
            if d < day:
                return found, total, False   # 기준일보다 오래된 글 → 다 셈
            found.append(it)
        if start + PAGE > total:
            return found, total, False
    return found, total, True       # 1,000개를 다 봤는데도 기준일 이전 글에 못 닿음 → 실제보다 적게 셌을 수 있음


def blog_date(it):
    try:
        return datetime.strptime(it.get("postdate", ""), "%Y%m%d").date()
    except ValueError:
        return None


def news_date(it):
    try:
        return parsedate_to_datetime(it.get("pubDate", "")).astimezone(KST).date()
    except (TypeError, ValueError):
        return None


def total_only(kind, keyword):
    data = call(f"{HUB}/{kind}", {"query": keyword, "display": 1, "start": 1}, HUB_HEADERS)
    return data.get("total", 0)


def shop_total(keyword):
    if not (DEV_ID and DEV_SECRET):
        return None
    data = call(SHOP_URL, {"query": keyword, "display": 1},
                {"X-Naver-Client-Id": DEV_ID, "X-Naver-Client-Secret": DEV_SECRET})
    return data.get("total", 0)


# ---------------------------------------------------------------- 저장
def load_rows():
    if not DAILY_CSV.exists():
        return []
    with open(DAILY_CSV, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def append_row(row):
    new = not DAILY_CSV.exists()
    with open(DAILY_CSV, "a", newline="", encoding="utf-8-sig") as f:   # 엑셀에서 한글이 안 깨지게 utf-8-sig
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


def previous_cafe_total(rows, keyword, day):
    """기준일 바로 전날 기록된 카페 전체 수. 없으면 None."""
    prev = (day - timedelta(days=1)).isoformat()
    for r in rows:
        if r["keyword"] == keyword and r["date"] == prev and r["cafe_total"]:
            return int(r["cafe_total"])
    return None


def read_watchlist(path):
    items = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        kw, _, cat = line.partition("|")
        items.append((kw.strip(), cat.strip()))
    return items


# ---------------------------------------------------------------- 키워드 하나
def collect_keyword(keyword, category, day, rows, posts_file):
    blog_posts, blog_total, blog_capped = count_day("blog", keyword, day, blog_date)
    news_items, news_total, news_capped = count_day("news", keyword, day, news_date)
    cafe_total = total_only("cafearticle", keyword)
    prev = previous_cafe_total(rows, keyword, day)

    texts = [clean(p.get("title")) + " " + clean(p.get("description")) for p in blog_posts]
    intent = {k: sum(1 for t in texts if rx.search(t)) for k, rx in INTENT_RULES.items()}

    for p in blog_posts:   # 구매 의도 분류 모델용 원자료
        posts_file.write(json.dumps({
            "date": day.isoformat(), "keyword": keyword,
            "title": clean(p.get("title")), "description": clean(p.get("description")),
            "link": p.get("link"), "postdate": p.get("postdate"),
        }, ensure_ascii=False) + "\n")

    return {
        "date": day.isoformat(), "keyword": keyword, "category": category,
        "blog_new": len(blog_posts), "blog_capped": int(blog_capped), "blog_total": blog_total,
        "cafe_total": cafe_total, "cafe_new": "" if prev is None else cafe_total - prev,
        "news_new": len(news_items), "news_capped": int(news_capped), "news_total": news_total,
        "shop_total": shop_total(keyword) if (DEV_ID and DEV_SECRET) else "",
        **intent,
        "collected_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
    }


def main():
    ap = argparse.ArgumentParser(description="트렌드 대체 지표 일일 수집")
    ap.add_argument("--watchlist", default=str(HERE / "watchlist.txt"))
    ap.add_argument("--date", help="기준일 YYYY-MM-DD (기본: 어제)")
    ap.add_argument("--limit", type=int, help="앞에서 N개 키워드만 (시험용)")
    ap.add_argument("--max-calls", type=int, default=20000, help="이번 실행에서 쓸 최대 호출 수 (하루 한도 보호)")
    args = ap.parse_args()

    if not (HUB_HEADERS["X-NCP-APIGW-API-KEY-ID"] and HUB_HEADERS["X-NCP-APIGW-API-KEY"]):
        sys.exit(".env 에 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 를 넣어 주세요 (.env.example 참고).")
    day = (datetime.strptime(args.date, "%Y-%m-%d").date() if args.date
           else (datetime.now(KST) - timedelta(days=1)).date())

    OUT.mkdir(exist_ok=True)
    (OUT / "posts").mkdir(exist_ok=True)
    rows = load_rows()
    done = {(r["date"], r["keyword"]) for r in rows}
    watch = read_watchlist(args.watchlist)[: args.limit]
    todo = [(k, c) for k, c in watch if (day.isoformat(), k) not in done]
    print(f"기준일 {day} · 키워드 {len(watch)}개 중 {len(todo)}개 수집"
          f"{'' if DEV_ID else ' · 쇼핑 상품 수는 건너뜀(개발자센터 키 없음)'}")

    with open(OUT / "posts" / f"{day.isoformat()}.jsonl", "a", encoding="utf-8") as posts_file:
        for i, (kw, cat) in enumerate(todo, 1):
            if CALLS >= args.max_calls:
                print(f"최대 호출 수({args.max_calls})에 도달해 멈춤. 다시 실행하면 이어서 모읍니다.")
                break
            try:
                row = collect_keyword(kw, cat, day, rows, posts_file)
            except QuotaExceeded as e:
                print(f"{e}. 내일 다시 실행하면 이어서 모읍니다.")
                break
            except Exception as e:
                print(f"  [{i}/{len(todo)}] {kw}: 실패 - {e}")
                continue
            append_row(row)
            rows.append({k: str(v) for k, v in row.items()})
            flag = " (1,000개 한도로 일부만 셈)" if row["blog_capped"] or row["news_capped"] else ""
            print(f"  [{i}/{len(todo)}] {kw}: 블로그 {row['blog_new']} · 뉴스 {row['news_new']} · "
                  f"카페 전체 {row['cafe_total']} · 구매질문 {row['buy_q']} · 품절 {row['soldout']}{flag}")

    print(f"끝. API 호출 {CALLS}회 · 결과 {DAILY_CSV}")


if __name__ == "__main__":
    main()

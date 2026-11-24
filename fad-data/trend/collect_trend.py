"""
네이버 데이터랩 과거 검색량 수집기 (v0.1)

keywords.txt 의 키워드마다 2016년부터 주 단위 검색량을 받아, 키워드끼리 크기를 비교할 수 있게 맞춘다.

왜 기준 키워드가 필요한가
  데이터랩은 "그 요청 안에서 가장 큰 값을 100"으로 놓은 상대값(ratio)을 준다.
  그래서 요청마다 100의 크기가 달라서, 따로 받은 키워드끼리는 비교할 수 없다.
  모든 요청에 같은 기준 키워드(anchor)를 함께 넣고, 키워드 값을 기준 키워드의 평균으로 나누면
  모든 키워드가 "기준 키워드 평균 = 1" 이라는 같은 단위가 된다.  → value 열

실행
  python collect_trend.py                       # keywords.txt 전체, 기준 키워드 "라면"
  python collect_trend.py --anchor 우산 --limit 4  # 기준 키워드 바꾸기, 4개만 시험
  python collect_trend.py --start 2018-01-01    # 시작일 바꾸기 (기본 2016-01-01, 데이터랩 최초 날짜)

결과
  output/trend_weekly.csv   keyword, category, expected, week, ratio(요청 안 상대값), value(기준 키워드 단위)
  이미 받은 키워드는 다시 받지 않음. 기준 키워드나 기간을 바꾸면 output 폴더를 비우고 새로 받을 것.
"""

import argparse
import csv
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

URL = "https://naverapihub.apigw.ntruss.com/search-trend/v1/search"
HEADERS = {
    "X-NCP-APIGW-API-KEY-ID": os.getenv("NAVER_CLIENT_ID", ""),
    "X-NCP-APIGW-API-KEY": os.getenv("NAVER_CLIENT_SECRET", ""),
    "Content-Type": "application/json",
}
OUT = HERE / "output"
CSV_PATH = OUT / "trend_weekly.csv"
FIELDS = ["keyword", "category", "expected", "week", "ratio", "value", "anchor"]
PER_REQUEST = 4       # 한 요청에 키워드 그룹 5개까지 → 기준 키워드 1 + 키워드 4
MIN_ANCHOR_MEAN = 0.5 # 기준 키워드 평균 ratio가 이보다 작으면 소수점 손실이 커서 값이 부정확해짐


class QuotaExceeded(Exception):
    pass


def read_keywords(path):
    """이름 | 검색어들 | 분야 | 예상 종류"""
    items = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")] + ["", "", ""]
        name = parts[0]
        terms = [t.strip() for t in parts[1].split(",") if t.strip()] or [name]
        items.append({"name": name, "terms": terms[:20], "category": parts[2], "expected": parts[3]})
    return items


def last_full_week_end():
    """데이터랩 주 단위는 월요일 시작. 지난주 일요일까지만 받아 덜 끝난 주를 뺀다."""
    today = date.today()
    return today - timedelta(days=today.weekday() + 1)


def week_starts(start, end):
    d = start - timedelta(days=start.weekday())   # 그 주 월요일
    out = []
    while d <= end:
        out.append(d.isoformat())
        d += timedelta(days=7)
    return out


def request(groups, start, end):
    body = {"startDate": start.isoformat(), "endDate": end.isoformat(), "timeUnit": "week", "keywordGroups": groups}
    for attempt in range(4):
        try:
            r = requests.post(URL, json=body, headers=HEADERS, timeout=20)
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 429:
            raise QuotaExceeded("하루 호출 한도 초과(429)")
        if r.status_code >= 500 and attempt < 3:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"{r.status_code} {r.text[:300]}")
        time.sleep(0.2)
        return r.json()


def series(result, weeks):
    """응답의 data를 주 목록에 맞춘다. 검색량이 거의 없는 주는 응답에서 빠질 수 있어 0으로 채운다."""
    got = {d["period"]: float(d["ratio"]) for d in result.get("data", [])}
    return [got.get(w, 0.0) for w in weeks]


def main():
    ap = argparse.ArgumentParser(description="데이터랩 과거 주별 검색량 수집")
    ap.add_argument("--keywords", default=str(HERE / "keywords.txt"))
    ap.add_argument("--anchor", default="라면", help="모든 요청에 같이 넣을 기준 키워드 (검색량이 꾸준하고 너무 작지 않은 것)")
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    if not (HEADERS["X-NCP-APIGW-API-KEY-ID"] and HEADERS["X-NCP-APIGW-API-KEY"]):
        sys.exit(".env 에 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 를 넣어 주세요.")
    start = date.fromisoformat(args.start)
    end = last_full_week_end()
    weeks = week_starts(start, end)

    OUT.mkdir(exist_ok=True)
    done = set()
    if CSV_PATH.exists():
        with open(CSV_PATH, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                done.add(r["keyword"])
                if r["anchor"] != args.anchor:
                    sys.exit(f"기존 결과는 기준 키워드 '{r['anchor']}'로 받았습니다. 같은 기준으로 받거나 output 폴더를 비우세요.")

    items = [k for k in read_keywords(args.keywords) if k["name"] not in done][: args.limit]
    print(f"기간 {weeks[0]} ~ {weeks[-1]} ({len(weeks)}주) · 기준 키워드 '{args.anchor}' · 받을 키워드 {len(items)}개")

    new_file = not CSV_PATH.exists()
    with open(CSV_PATH, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        for i in range(0, len(items), PER_REQUEST):
            batch = items[i:i + PER_REQUEST]
            groups = [{"groupName": "__anchor__", "keywords": [args.anchor]}] + \
                     [{"groupName": k["name"], "keywords": k["terms"]} for k in batch]
            try:
                data = request(groups, start, end)
            except QuotaExceeded as e:
                print(f"{e}. 내일 다시 실행하면 이어서 받습니다.")
                break
            except Exception as e:
                print(f"  실패: {[k['name'] for k in batch]} - {e}")
                continue

            results = {r["title"]: r for r in data.get("results", [])}
            anchor = series(results.get("__anchor__", {}), weeks)
            anchor_mean = sum(anchor) / len(anchor)
            if anchor_mean < MIN_ANCHOR_MEAN:
                print(f"  경고: 이 묶음에서 기준 키워드 평균이 {anchor_mean:.3f}로 너무 작아 값이 부정확할 수 있음. "
                      f"더 검색량이 큰 기준 키워드를 고려하세요. ({[k['name'] for k in batch]})")
            for k in batch:
                s = series(results.get(k["name"], {}), weeks)
                for wk, ratio in zip(weeks, s):
                    w.writerow({"keyword": k["name"], "category": k["category"], "expected": k["expected"],
                                "week": wk, "ratio": round(ratio, 5),
                                "value": round(ratio / anchor_mean, 6) if anchor_mean else "",
                                "anchor": args.anchor})
                peak = max(s) if s else 0
                print(f"  {k['name']}: 최고 주 {weeks[s.index(peak)] if peak else '-'} (기준 키워드 평균의 {peak / anchor_mean if anchor_mean else 0:.1f}배)")
            f.flush()

    print(f"끝. 결과 {CSV_PATH}  →  다음: python label_fads.py")


if __name__ == "__main__":
    main()

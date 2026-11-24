"""
유행 키워드별 가게 개업·폐업 분석 (v0.1)

행정안전부 음식점 인허가 파일(휴게음식점, 일반음식점, 제과점 등 CSV)에서
가게 이름에 유행 키워드가 들어간 곳을 찾아, 언제 열고 언제 닫았는지 센다.

결과로 알 수 있는 것
  1. 월별 개업 수 / 폐업 수 / 영업 중인 가게 수         → 유행이 "실제 장사"에서 언제 몰리고 언제 무너졌나
  2. 검색량 정점 대비 개업 정점·폐업 정점이 몇 개월 늦나  → 수명 예측의 근거, 모델 특징
  3. 창업 시점별 생존 기간 (정점 전/직후/뒤에 연 가게)    → "지금 창업하면 비슷한 유행 가게들은 몇 개월 버텼나"

준비
  1) 공공데이터포털에서 받은 CSV를 stores/data/ 폴더에 넣는다 (여러 개 가능, GitHub에는 안 올림)
  2) 키워드는 trend/keywords.txt 를 그대로 쓴다.
     가게 이름 검색어를 따로 정하려면 5번째 칸에 쓴다. 앞에 -를 붙이면 제외어.
       예) 두바이 초콜릿 | 두바이 초콜릿, 두바이초콜릿 | 디저트 | | 두바이쫀득, 두쫀쿠, -호텔
     5번째 칸이 없으면 2번째 칸(검색어)으로 가게 이름을 찾는다. 띄어쓰기는 무시한다.

실행
  python store_trends.py
  python store_trends.py --files data/휴게음식점.csv data/일반음식점.csv

결과 (output/)
  stores_matched.csv   찾은 가게 목록 (키워드, 가게 이름, 개업일, 폐업일, 운영 개월 수, 지역, 업종 파일)
  store_monthly.csv    키워드·월별 개업 / 폐업 / 영업 중 가게 수
  store_summary.csv    키워드별 요약 (가게 수, 개업·폐업 정점, 검색량 정점과의 시차)
  store_survival.csv   키워드·창업 시점별 생존 분석 (운영 개월 중앙값, 1년 생존율)
  plots/<키워드>.png    위: 검색량 / 아래: 월별 개업·폐업 + 영업 중 가게 수
"""

import argparse
import re
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib import font_manager

HERE = Path(__file__).resolve().parent
OUT = HERE / "output"
TREND = HERE.parent / "trend"

_have = {f.name for f in font_manager.fontManager.ttflist}
plt.rcParams["font.family"] = [f for f in ["Malgun Gothic", "AppleGothic", "NanumGothic", "Noto Sans CJK KR", "Noto Sans KR"]
                               if f in _have] + ["DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

# 파일마다 열 이름이 조금씩 달라서, 후보 이름 중 있는 것을 쓴다 (예전 LOCALDATA 형식 + 공공데이터포털 형식)
COLS = {
    "name": ["사업장명", "BPLC_NM", "상호명"],
    "open": ["인허가일자", "LCPMT_YMD"],
    "close": ["폐업일자", "CLSBIZ_YMD", "폐업일"],
    "status": ["영업상태명", "상세영업상태명", "SALS_STTS_NM", "영업상태"],
    "addr": ["소재지전체주소", "도로명전체주소", "ROAD_NM_ADDR", "LOTNO_ADDR", "소재지주소"],
    "kind": ["업태구분명", "위생업태명", "업태", "BZSTAT_SE_NM"],
}


# ---------------------------------------------------------------- 키워드
def read_keywords(path):
    """trend/keywords.txt 형식: 이름 | 검색어들 | 분야 | 예상 종류 | (선택) 가게 이름 검색어"""
    items = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        p = [x.strip() for x in line.split("|")] + [""] * 5
        name = p[0]
        terms = [t.strip() for t in (p[4] or p[1] or name).split(",") if t.strip()]
        inc = [norm(t) for t in terms if not t.startswith("-")]
        exc = [norm(t[1:]) for t in terms if t.startswith("-")]
        items.append({"name": name, "category": p[2], "include": inc or [norm(name)], "exclude": exc})
    return items


def norm(s):
    return re.sub(r"\s+", "", str(s)).lower()


# ---------------------------------------------------------------- 파일 읽기
def sniff_encoding(path):
    """공공데이터 CSV는 대부분 CP949(EUC-KR). UTF-8이면 그대로."""
    raw = Path(path).read_bytes()[:200000]
    for enc in ("utf-8-sig", "cp949"):
        try:
            raw.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "cp949"


def pick(columns, key):
    for c in COLS[key]:
        if c in columns:
            return c
    return None


def to_date(s):
    """'2023-09-12', '20230912', '2023.09.12' 모두 날짜로"""
    s = s.astype(str).str.replace(r"[^0-9]", "", regex=True).str[:8]
    return pd.to_datetime(s, format="%Y%m%d", errors="coerce")


def scan_file(path, keywords):
    """큰 파일도 메모리에 다 올리지 않고 20만 줄씩 읽으면서, 이름이 키워드에 걸리는 가게만 남긴다."""
    enc = sniff_encoding(path)
    header = pd.read_csv(path, encoding=enc, nrows=0, dtype=str).columns.tolist()
    col = {k: pick(header, k) for k in COLS}
    if not col["name"] or not col["open"]:
        sys.exit(f"{path.name}: 가게 이름이나 인허가일자 열을 못 찾았습니다. 열 이름: {header[:30]}")
    if not col["close"]:
        print(f"  주의: {path.name}에 폐업일자 열이 없어 폐업 시점을 알 수 없습니다.")
    use = [c for c in col.values() if c]
    found = []
    total = 0
    for chunk in pd.read_csv(path, encoding=enc, usecols=use, dtype=str, chunksize=200_000, on_bad_lines="skip"):
        total += len(chunk)
        names = chunk[col["name"]].fillna("").map(norm)
        for k in keywords:
            hit = names.apply(lambda n: any(t in n for t in k["include"]) and not any(t in n for t in k["exclude"]))
            if hit.any():
                sub = chunk[hit]
                found.append(pd.DataFrame({
                    "keyword": k["name"], "category": k["category"],
                    "store": sub[col["name"]].values,
                    "opened": to_date(sub[col["open"]]).values,
                    "closed": to_date(sub[col["close"]]).values if col["close"] else pd.NaT,
                    "status": sub[col["status"]].values if col["status"] else "",
                    "address": sub[col["addr"]].values if col["addr"] else "",
                    "kind": sub[col["kind"]].values if col["kind"] else "",
                    "source": path.stem,
                }))
    print(f"  {path.name}: {total:,}곳 중 {sum(len(f) for f in found):,}곳 일치 (인코딩 {enc})")
    return pd.concat(found, ignore_index=True) if found else pd.DataFrame()


# ---------------------------------------------------------------- 생존 분석 (카플란-마이어)
def kaplan_meier(months, closed):
    """운영 개월 수와 폐업 여부로 생존 곡선을 만든다. 아직 영업 중인 가게도 "최소 이만큼은 버텼다"로 반영한다."""
    order = np.argsort(months)
    t, e = np.asarray(months)[order], np.asarray(closed)[order]
    at_risk, s, curve = len(t), 1.0, []
    for ti in np.unique(t):
        d = int(e[t == ti].sum())
        if d:
            s *= 1 - d / at_risk
        curve.append((ti, s))
        at_risk -= int((t == ti).sum())
    return curve


def km_summary(months, closed):
    curve = kaplan_meier(months, closed)
    median = next((round(float(t), 1) for t, s in curve if s <= 0.5), None)
    s12 = 1.0
    for t, s in curve:
        if t <= 12:
            s12 = s
    return median, round(s12 * 100, 1)


def cohort(open_month, peak_month):
    """검색량 정점 대비 언제 창업했나"""
    d = (open_month.year - peak_month.year) * 12 + (open_month.month - peak_month.month)
    if d < -6:
        return "1. 정점 6개월보다 전"
    if d < 0:
        return "2. 정점 전 6개월"
    if d < 6:
        return "3. 정점 후 6개월"
    if d < 12:
        return "4. 정점 후 6~12개월"
    return "5. 정점 1년 이후"


# ---------------------------------------------------------------- 그래프
def plot_keyword(kw, monthly, trend, peak, path):
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(11, 6), dpi=110, sharex=True, gridspec_kw={"height_ratios": [1, 1.3]})
    if trend is not None and len(trend):
        a1.plot(trend.index, trend.values, color="#2f5da8", lw=1.6)
        a1.set_ylabel("검색량\n(기준 키워드 평균=1)")
    else:
        a1.text(0.5, 0.5, "검색량 데이터 없음 (trend/collect_trend.py 먼저 실행)", ha="center", va="center", transform=a1.transAxes)
    x = monthly["month"]
    a2.bar(x, monthly["opened"], width=25, color="#2e6b3a", label="개업")
    a2.bar(x, -monthly["closed"], width=25, color="#d1495b", label="폐업")
    a2.axhline(0, color="#555", lw=0.6)
    a2.set_ylabel("월별 개업(+) / 폐업(−)")
    b = a2.twinx()
    b.plot(x, monthly["active"], color="#e07a2e", lw=1.8, label="영업 중")
    b.set_ylabel("영업 중 가게 수")
    for ax in (a1, a2):
        ax.grid(alpha=0.25)
        if peak is not None:
            ax.axvline(peak, color="#555", ls=":", lw=1)
    if peak is not None:
        a1.text(peak, a1.get_ylim()[1] * 0.95, " 검색 정점", fontsize=8, va="top")
    h1, l1 = a2.get_legend_handles_labels()
    h2, l2 = b.get_legend_handles_labels()
    a2.legend(h1 + h2, l1 + l2, loc="upper left", fontsize=8, frameon=False)
    fig.suptitle(f"{kw}: 검색량과 가게 개업·폐업", fontsize=12, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


# ---------------------------------------------------------------- 실행
def main():
    ap = argparse.ArgumentParser(description="유행 키워드별 가게 개업·폐업 분석")
    ap.add_argument("--files", nargs="*", help="인허가 CSV (기본: data/*.csv)")
    ap.add_argument("--keywords", default=str(TREND / "keywords.txt"))
    ap.add_argument("--since", default="2015-01-01", help="이 날짜 이후 개업한 가게만 (오래된 동명 가게 제외)")
    a = ap.parse_args()

    files = [Path(f) for f in a.files] if a.files else sorted((HERE / "data").glob("*.csv"))
    if not files:
        sys.exit("stores/data/ 에 인허가 CSV를 넣거나 --files 로 지정하세요.")
    keywords = read_keywords(a.keywords)
    print(f"키워드 {len(keywords)}개 · 파일 {len(files)}개")

    df = pd.concat([scan_file(f, keywords) for f in files], ignore_index=True)
    if df.empty:
        sys.exit("일치하는 가게가 없습니다. 키워드의 가게 이름 검색어(5번째 칸)를 확인하세요.")
    df = df[df["opened"] >= pd.Timestamp(a.since)].copy()
    df = df.drop_duplicates(subset=["keyword", "store", "opened", "address"])   # 여러 파일에 중복된 가게 제거

    # 아직 영업 중인 가게는 "데이터 기준일까지" 운영한 것으로 본다
    data_date = pd.Timestamp(max(df["opened"].max(), df["closed"].max() if df["closed"].notna().any() else df["opened"].max()))
    df["is_closed"] = df["closed"].notna()
    end = df["closed"].fillna(data_date)
    df["months_open"] = ((end - df["opened"]).dt.days / 30.44).round(1).clip(lower=0)

    # 검색량 (있으면)
    trend, peaks = None, {}
    tw, lb = TREND / "output" / "trend_weekly.csv", TREND / "output" / "labels.csv"
    if tw.exists():
        trend = pd.read_csv(tw, encoding="utf-8-sig", parse_dates=["week"])
    if lb.exists():
        labels = pd.read_csv(lb, encoding="utf-8-sig")
        peaks = {r.keyword: pd.Timestamp(r.peak_week) for r in labels.itertuples() if isinstance(r.peak_week, str)}

    OUT.mkdir(exist_ok=True)
    (OUT / "plots").mkdir(exist_ok=True)
    monthly_all, summary, survival = [], [], []
    for kw, g in df.groupby("keyword", sort=False):
        lo = g["opened"].min().to_period("M").to_timestamp()
        months = pd.date_range(lo, data_date, freq="MS")
        op = g["opened"].dt.to_period("M").dt.to_timestamp().value_counts()
        cl = g.loc[g["is_closed"], "closed"].dt.to_period("M").dt.to_timestamp().value_counts()
        m = pd.DataFrame({"month": months})
        m["opened"] = m["month"].map(op).fillna(0).astype(int)
        m["closed"] = m["month"].map(cl).fillna(0).astype(int)
        m["active"] = (m["opened"] - m["closed"]).cumsum()
        m.insert(0, "keyword", kw)
        monthly_all.append(m)

        peak = peaks.get(kw)
        open_peak = m.loc[m["opened"].idxmax(), "month"]
        close_peak = m.loc[m["closed"].idxmax(), "month"] if m["closed"].sum() else None
        lag = lambda t: None if (t is None or peak is None) else (t.year - peak.year) * 12 + (t.month - peak.month)
        median, s12 = km_summary(g["months_open"].values, g["is_closed"].values)
        summary.append({
            "keyword": kw, "stores": len(g), "closed": int(g["is_closed"].sum()),
            "closed_pct": round(g["is_closed"].mean() * 100, 1),
            "search_peak": peak.date() if peak is not None else "",
            "open_peak": open_peak.date(), "close_peak": close_peak.date() if close_peak is not None else "",
            "open_lag_months": lag(open_peak), "close_lag_months": lag(close_peak),
            "median_months_open": median, "survive_12m_pct": s12,
        })

        if peak is not None:
            g = g.assign(cohort=[cohort(o, peak) for o in g["opened"]])
            for c, gc in g.groupby("cohort"):
                med, s = km_summary(gc["months_open"].values, gc["is_closed"].values)
                survival.append({"keyword": kw, "cohort": c, "stores": len(gc), "closed": int(gc["is_closed"].sum()),
                                 "median_months_open": med, "survive_12m_pct": s})

        series = None
        if trend is not None:
            t = trend[trend["keyword"] == kw]
            if len(t):
                series = t.set_index("week")["value"].astype(float).rolling(3, center=True, min_periods=1).mean()
                series = series[series.index >= lo - pd.DateOffset(months=12)]
        plot_keyword(kw, m, series, peak, OUT / "plots" / f"{re.sub(r'[^0-9A-Za-z가-힣]+', '_', kw)}.png")

    df.drop(columns=["is_closed"]).to_csv(OUT / "stores_matched.csv", index=False, encoding="utf-8-sig")
    pd.concat(monthly_all).to_csv(OUT / "store_monthly.csv", index=False, encoding="utf-8-sig")
    s = pd.DataFrame(summary)
    s.to_csv(OUT / "store_summary.csv", index=False, encoding="utf-8-sig")
    if survival:
        pd.DataFrame(survival).sort_values(["keyword", "cohort"]).to_csv(OUT / "store_survival.csv", index=False, encoding="utf-8-sig")

    print(f"\n데이터 기준일(가장 최근 날짜): {data_date.date()}")
    print(s[["keyword", "stores", "closed_pct", "search_peak", "open_lag_months", "close_lag_months",
             "median_months_open", "survive_12m_pct"]].to_string(index=False))
    if not peaks:
        print("\n검색량 정점 정보가 없어 시차·창업 시점별 생존은 계산하지 않았습니다. trend/label_fads.py 를 먼저 실행하세요.")
    print(f"\n결과: {OUT}")
    print("확인: stores_matched.csv 에서 유행과 상관없는 가게(예: 이름만 겹치는 곳)가 섞였는지 보고, 제외어(-단어)를 추가하세요.")


if __name__ == "__main__":
    main()

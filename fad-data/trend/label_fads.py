"""
반짝 유행 자동 판정 + 그래프 (v0.1)

collect_trend.py 결과(output/trend_weekly.csv)를 읽어, 키워드마다 곡선 전체를 보고 정답(종류)을 자동으로 붙인다.
과거 유행은 결말을 이미 알기 때문에 사람이 일일이 정하지 않아도 규칙으로 판정할 수 있다.
이 정답이 나중에 "앞부분만 보고 결말을 맞히는" 모델의 학습 정답이 된다.

판정 규칙 (기본값, 옵션으로 조정)
  정점        3주 이동평균이 최고값의 95%에 처음 닿은 주
  시작        정점 앞에서 정점의 10% 아래였던 마지막 주의 다음 주
  꺼짐        정점 뒤 처음으로 정점의 30% 아래로 떨어지고, 그 뒤 8주 내내 30% 아래인 주
  seasonal    1년(52주) 간격 자기상관이 0.5 이상 (매년 같은 시기에 오름)
  fad         정점 후 26주 안에 꺼짐
  trend       정점 후 52주가 지났고, 정점 후 40~52주 평균이 정점의 50% 이상
  ongoing     아직 결말을 알 수 없음 (정점 뒤 기간이 짧음)  ← 수명 예측(생존 분석)에서는 "아직 안 꺼짐"으로 학습에 씀
  ambiguous   위 어디에도 안 맞음 (학습에서 빼거나 규칙 조정)

실행
  python label_fads.py
  python label_fads.py --fad-weeks 20 --decay 0.25    # 기준 바꿔 보기

결과
  output/labels.csv        키워드별 판정과 수치 (상승 기간, 반감 기간, 꺼질 때까지 걸린 주 등)
  output/plots/<키워드>.png 키워드별 곡선과 판정 근거
  output/overview.png      종류별로 모은 곡선 (정점 = 1로 맞춰 모양 비교)
"""

import argparse
import re
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
OUT = HERE / "output"

# 한글 글꼴: 윈도우(맑은 고딕), 맥, 리눅스(나눔·Noto) 중 있는 것 사용
from matplotlib import font_manager
_have = {f.name for f in font_manager.fontManager.ttflist}
plt.rcParams["font.family"] = [f for f in ["Malgun Gothic", "AppleGothic", "NanumGothic", "Noto Sans CJK KR", "Noto Sans KR"]
                               if f in _have] + ["DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False
COLORS = {"fad": "#d1495b", "trend": "#2e6b3a", "seasonal": "#2f5da8", "ongoing": "#e07a2e",
          "ambiguous": "#7a7a7a", "no_data": "#bbbbbb"}


def label_one(weeks, values, a):
    """키워드 하나의 주별 값으로 판정. 주 번호(index)와 날짜를 함께 돌려준다."""
    s = pd.Series(values).rolling(3, center=True, min_periods=1).mean().to_numpy()
    n = len(s)
    res = {"label": "no_data", "peak_week": "", "peak_value": 0.0, "onset_week": "", "rise_weeks": "",
           "half_weeks": "", "decay_week": "", "weeks_to_decay": "", "weeks_after_peak": "",
           "spike_ratio": "", "season_corr": "", "note": ""}
    if n == 0 or s.max() <= 0:
        return res, s, {}

    # 정점 = 최고값의 95%에 처음 닿은 주. 그냥 최고값 주를 쓰면, 올라서 평평하게 유지되는 트렌드는
    # 잡음 때문에 정점이 맨 뒤쪽에 찍혀 "아직 결말 모름"으로 잘못 나온다. 기준 높이는 최고값을 쓴다.
    peak = float(s.max())
    p = int(np.argmax(s >= a.peak_frac * peak))
    res.update(peak_week=weeks[p], peak_value=round(peak, 4), weeks_after_peak=n - 1 - p)

    # 시작: 정점 앞에서 10% 아래였던 마지막 주 다음
    below = np.where(s[:p] < a.onset * peak)[0]
    onset = int(below[-1]) + 1 if len(below) else 0
    res.update(onset_week=weeks[onset], rise_weeks=p - onset)
    if onset == 0:
        res["note"] += "데이터 시작 시점에 이미 높음(시작 시점 불확실). "

    # 뾰족함: 정점 / 시작 전 1년 중앙값
    pre = s[max(0, onset - 52):onset]
    base = float(np.median(pre)) if len(pre) else 0.0
    res["spike_ratio"] = round(peak / base, 1) if base > 0 else ""

    # 반감: 정점 뒤 처음으로 50% 아래
    after = s[p + 1:]
    half = np.where(after < 0.5 * peak)[0]
    if len(half):
        res["half_weeks"] = int(half[0]) + 1

    # 꺼짐: 30% 아래로 떨어진 뒤 hold 주 동안 계속 아래
    decay = None
    for i in range(p + 1, n - a.hold + 1):
        if s[i] < a.decay * peak and np.all(s[i:i + a.hold] < a.decay * peak):
            decay = i
            break
    if decay is not None:
        res.update(decay_week=weeks[decay], weeks_to_decay=decay - p)

    # 계절성: 52주 간격 자기상관 (3년 이상일 때만)
    # 그냥 계산하면 "한 번 올라 계속 유지되는 트렌드"도 1년 전과 값이 비슷해서 계절성으로 잘못 나온다.
    # 그래서 52주 이동평균(장기 추세)을 빼서 "1년 안에서 오르내리는 부분"만 남긴 뒤 계산한다.
    corr = np.nan
    if n >= 52 * 3:
        level = pd.Series(s).rolling(52, center=True, min_periods=26).mean().to_numpy()
        x = s - level
        x = x - np.nanmean(x)
        x = np.nan_to_num(x)
        denom = (x * x).sum()
        corr = float((x[:-52] * x[52:]).sum() / denom) if denom > 0 else np.nan
        res["season_corr"] = round(corr, 2)

    after_n = n - 1 - p
    if not np.isnan(corr) and corr >= a.season:
        label = "seasonal"
    elif decay is not None and decay - p <= a.fad_weeks:
        label = "fad"
    elif after_n >= 52 and s[p + 40:p + 53].mean() >= 0.5 * peak:
        label = "trend"
    elif decay is None and after_n < 52:
        label = "ongoing"
    else:
        label = "ambiguous"
    res["label"] = label
    marks = {"onset": onset, "peak": p, "decay": decay,
             "half": (p + res["half_weeks"]) if res["half_weeks"] != "" else None}
    return res, s, marks


def safe(name):
    return re.sub(r'[\\/:*?"<>| ]+', "_", name)


def plot_one(kw, weeks, raw, s, marks, res, a, path):
    """왼쪽: 전체 기간 / 오른쪽: 정점 앞 1년 ~ 뒤 1년 반 확대 + 시작·정점·절반·꺼짐 표시"""
    x = pd.to_datetime(weeks)
    peak = res["peak_value"]
    color = COLORS.get(res["label"], "#333")
    fig, (ax0, ax) = plt.subplots(1, 2, figsize=(13, 3.8), dpi=110, gridspec_kw={"width_ratios": [1, 1.4]})

    ax0.plot(x, s, color=color, lw=1.4)
    ax0.set_title("전체 기간", fontsize=10, loc="left")
    ax0.set_ylabel("검색량 (기준 키워드 평균 = 1)")
    ax0.grid(alpha=0.25)

    p = marks["peak"]
    lo, hi = max(0, p - 52), min(len(s), p + 79)
    ax.plot(x[lo:hi], raw[lo:hi], color="#c9c9c9", lw=0.8, label="주별 값")
    ax.plot(x[lo:hi], s[lo:hi], color=color, lw=2, label="3주 이동평균")
    ax0.axvspan(x[lo], x[hi - 1], color=color, alpha=0.08)
    ax.axhline(a.decay * peak, color="#999", ls="--", lw=0.8)
    ax.text(x[lo], a.decay * peak, f" 정점의 {int(a.decay * 100)}%", va="bottom", fontsize=8, color="#666")
    top = ax.get_ylim()[1]
    for n_, (key, txt) in enumerate((("onset", "시작"), ("peak", "정점"), ("half", "절반"), ("decay", "꺼짐"))):
        i = marks.get(key)
        if i is not None and lo <= i < hi:
            ax.axvline(x[i], color="#555", lw=0.7, ls=":")
            ax.text(x[i], top * (0.97 - 0.09 * n_), txt, fontsize=8, ha="center", va="top",   # 글자가 겹치지 않게 높이를 엇갈림
                    bbox=dict(fc="white", ec="none", alpha=0.85))
    ax.set_title("정점 주변 확대", fontsize=10, loc="left")
    ax.grid(alpha=0.25)
    ax.legend(loc="best", fontsize=8, frameon=False)

    exp = f" · 팀 예상 {res['expected']}" if res.get("expected") else ""
    ttd = f" · 정점→꺼짐 {res['weeks_to_decay']}주" if res["weeks_to_decay"] != "" else ""
    fig.suptitle(f"{kw}  [{res['label']}]{exp} · 상승 {res['rise_weeks']}주{ttd}", fontsize=12, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def plot_overview(curves, path):
    """정점을 가운데(0주) 놓고 정점 = 1로 맞춰, 종류별로 곡선 모양을 겹쳐 그린다."""
    order = [l for l in ["fad", "trend", "seasonal", "ongoing", "ambiguous"] if any(c[1] == l for c in curves)]
    if not order:
        return
    fig, axes = plt.subplots(1, len(order), figsize=(4.2 * len(order), 3.4), dpi=110, sharey=True, squeeze=False)
    for ax, lab in zip(axes[0], order):
        group = [c for c in curves if c[1] == lab]
        for kw, _, s, p in group:
            lo, hi = max(0, p - 52), min(len(s), p + 79)
            t = np.arange(lo, hi) - p
            ax.plot(t, s[lo:hi] / s[p], color=COLORS[lab], alpha=0.35, lw=1)
        ax.axvline(0, color="#555", lw=0.7, ls=":")
        ax.set_title(f"{lab} ({len(group)}개)", fontsize=11)
        ax.set_xlabel("정점 기준 주 (−52 ~ +78)")
        ax.grid(alpha=0.25)
    axes[0][0].set_ylabel("정점 = 1")
    fig.suptitle("종류별 곡선 모양 비교", fontsize=12, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description="반짝 유행 자동 판정")
    ap.add_argument("--input", default=str(OUT / "trend_weekly.csv"))
    ap.add_argument("--onset", type=float, default=0.10, help="시작 기준: 정점의 몇 배 아래였다가 오르기 시작했나")
    ap.add_argument("--decay", type=float, default=0.30, help="꺼짐 기준: 정점의 몇 배 아래로 떨어졌나")
    ap.add_argument("--hold", type=int, default=8, help="꺼짐 판정: 기준 아래로 몇 주 연속 머물러야 하나")
    ap.add_argument("--fad-weeks", type=int, default=26, help="정점 후 몇 주 안에 꺼지면 반짝 유행인가")
    ap.add_argument("--season", type=float, default=0.5, help="계절성 판정: 52주 자기상관 기준")
    ap.add_argument("--peak-frac", type=float, default=0.95, help="정점: 최고값의 몇 배에 처음 닿은 주")
    ap.add_argument("--no-plots", action="store_true")
    a = ap.parse_args()

    df = pd.read_csv(a.input, encoding="utf-8-sig", dtype={"expected": str, "category": str}).fillna({"expected": "", "category": ""})
    (OUT / "plots").mkdir(parents=True, exist_ok=True)
    rows, curves = [], []
    for kw, g in df.groupby("keyword", sort=False):
        g = g.sort_values("week")
        weeks, raw = g["week"].tolist(), g["value"].astype(float).to_numpy()
        res, s, marks = label_one(weeks, raw, a)
        res = {"keyword": kw, "category": g["category"].iloc[0], "expected": g["expected"].iloc[0], **res}
        res["matches_expected"] = "" if not res["expected"] else ("O" if res["expected"] == res["label"] else "X")
        rows.append(res)
        if res["label"] != "no_data":
            curves.append((kw, res["label"], s, marks["peak"]))
            if not a.no_plots:
                plot_one(kw, weeks, raw, s, marks, res, a, OUT / "plots" / f"{safe(kw)}.png")

    out = pd.DataFrame(rows)
    out.to_csv(OUT / "labels.csv", index=False, encoding="utf-8-sig")
    if not a.no_plots:
        plot_overview(curves, OUT / "overview.png")

    print("===== 판정 결과 =====")
    print(out["label"].value_counts().to_string())
    checked = out[out["expected"] != ""]
    if len(checked):
        agree = (checked["matches_expected"] == "O").mean() * 100
        print(f"\n팀 예상과 일치: {agree:.0f}% ({len(checked)}개 중). 안 맞는 키워드는 그래프를 보고 규칙이나 예상을 고치세요.")
        bad = checked[checked["matches_expected"] == "X"][["keyword", "expected", "label", "weeks_to_decay", "season_corr"]]
        if len(bad):
            print(bad.to_string(index=False))
    print(f"\n결과: {OUT / 'labels.csv'} · 그래프: {OUT / 'plots'} · 종류별 비교: {OUT / 'overview.png'}")


if __name__ == "__main__":
    main()

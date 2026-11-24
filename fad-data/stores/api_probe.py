"""
공공데이터포털 음식점 인허가 API 응답 확인 (한 번만 실행)

수집 코드를 만들기 전에, 실제 API가 어떤 항목(폐업일자 등)을 주는지,
가게 이름으로 검색이 되는지 확인한다. 결과 화면을 그대로 보내 주세요. (인증키는 출력하지 않음)

준비
  stores/.env 에 공공데이터포털 인증키(Decoding 키)를 넣는다:
    DATA_GO_KR_KEY=발급받은_Decoding_키
  공공데이터포털 → 마이페이지 → 데이터활용 → Open API → 활용신청 현황 → 각 API 상세 화면에서
  "End Point" 주소와 "일반 인증키(Decoding)"를 확인할 수 있다.

실행
  python api_probe.py --endpoint https://apis.data.go.kr/1741000/rest_cafes --name 탕후루
  python api_probe.py --endpoint <일반음식점 End Point 주소> --name 마라탕
"""

import argparse
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")
KEY = os.getenv("DATA_GO_KR_KEY", "")


def call(endpoint, extra):
    params = {"serviceKey": KEY, "pageNo": 1, "numOfRows": 3, "returnType": "json", **extra}
    try:
        r = requests.get(endpoint, params=params, timeout=20)
    except requests.RequestException as e:
        return None, f"연결 실패: {e}"
    text = r.text.replace(KEY, "***") if KEY else r.text
    try:
        return r.json(), f"HTTP {r.status_code}"
    except ValueError:
        return None, f"HTTP {r.status_code}, JSON 아님: {text[:400]}"


def find_items(data):
    """응답 구조가 API마다 달라서, 목록(list of dict)이 있는 곳을 찾아낸다."""
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data
    if isinstance(data, dict):
        for v in data.values():
            got = find_items(v)
            if got:
                return got
    return None


def find_total(data):
    if isinstance(data, dict):
        for k, v in data.items():
            if k.lower() in ("totalcount", "total_count", "totcnt") and not isinstance(v, (dict, list)):
                return v
            got = find_total(v)
            if got is not None:
                return got
    return None


def show(title, data, status):
    print(f"\n===== {title} =====")
    print(status)
    if data is None:
        return
    items = find_items(data)
    total = find_total(data)
    print(f"전체 건수: {total}")
    if not items:
        print("응답(앞부분):", json.dumps(data, ensure_ascii=False)[:800])
        return
    print(f"받은 항목 수: {len(items)}")
    print("항목 이름과 첫 번째 값:")
    for k, v in items[0].items():
        print(f"  {k}: {v}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", required=True, help="활용신청 상세 화면의 End Point 주소")
    ap.add_argument("--name", default="탕후루", help="가게 이름 검색 시험용 단어")
    a = ap.parse_args()
    if not KEY:
        sys.exit("stores/.env 에 DATA_GO_KR_KEY=Decoding키 를 넣어 주세요.")

    # 1) 조건 없이: 응답 구조와 항목 이름 확인
    data, st = call(a.endpoint, {})
    show("1. 조건 없이 3건", data, st)

    # 2) 가게 이름 검색 형식 시험 (설명서에 정확한 형식이 없어 몇 가지를 시도)
    for op in ("LIKE", "EQ"):
        data, st = call(a.endpoint, {f"cond[BPLC_NM::{op}]": a.name})
        show(f"2. 가게 이름 {op} '{a.name}'", data, st)

    # 3) 인허가일자 범위 검색 시험
    data, st = call(a.endpoint, {"cond[LCPMT_YMD::GTE]": "20240101"})
    show("3. 인허가일자 2024-01-01 이후", data, st)

    print("\n이 화면 전체를 복사해서 보내 주세요. (인증키는 *** 로 가려져 있어요)")


if __name__ == "__main__":
    main()

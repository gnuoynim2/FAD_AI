# 한국 반짝 유행(Fad) 예측 — 데이터 수집·분석

| 폴더 | 하는 일 |
|---|---|
| `trend/` | 네이버 데이터랩 **2016년부터 과거 검색량**을 받고, 반짝 유행 / 트렌드 / 계절을 자동 판정 |
| `stores/` | 음식점 인허가 파일·API로 유행 키워드별 **가게 개업·폐업과 생존 기간** 분석 |

## 시작 순서
1. `trend/keywords.txt`에 분석할 키워드를 정리한다 (가게 이름 검색어는 5번째 칸).
2. `trend/`: `.env`에 NAVER API HUB 키 → `python collect_trend.py` → `python label_fads.py`
3. `stores/`: 인허가 CSV를 `stores/data/`에 넣고 → `python store_trends.py`
4. `stores/`: `.env`에 공공데이터포털 Decoding 키 → `python api_probe.py --endpoint <End Point> --name 탕후루` (API 응답 확인)

API 키는 각 폴더의 `.env`에만 넣고, `.env`·`output/`·`data/`는 GitHub에 올리지 않는다.
자세한 사용법은 각 폴더의 README를 본다.

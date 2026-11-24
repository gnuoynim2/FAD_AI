#!/usr/bin/env bash
# EC2(Ubuntu)에서 대체 지표 수집기를 설치하고 매일 한국 시간 오전 6시 10분에 자동 실행되게 등록한다.
# 사용법: buzz 폴더 안에서  bash setup_ec2.sh
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"

echo "[1/5] 파이썬 가상환경 도구 설치"
sudo apt-get update -y -qq
sudo apt-get install -y -qq python3-venv

echo "[2/5] 가상환경 만들고 패키지 설치"
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt

echo "[3/5] .env 확인"
if [ ! -f .env ]; then
  echo "  .env 가 없습니다. 아래처럼 만든 뒤 다시 실행하세요:"
  echo "    cp .env.example .env && nano .env"
  exit 1
fi
chmod 600 .env   # 키 파일은 본인만 읽을 수 있게

echo "[4/5] 키워드 1개로 시험 실행"
./venv/bin/python collect_buzz.py --limit 1

echo "[5/5] 매일 자동 실행 등록 (cron)"
TZNAME="$(timedatectl show -p Timezone --value 2>/dev/null || echo UTC)"
if [ "$TZNAME" = "Asia/Seoul" ]; then WHEN="10 6 * * *"; else WHEN="10 21 * * *"; fi   # UTC 21:10 = 한국 06:10
mkdir -p output
LINE="$WHEN cd $DIR && $DIR/venv/bin/python collect_buzz.py >> $DIR/output/cron.log 2>&1"
( crontab -l 2>/dev/null | grep -v "collect_buzz.py" || true; echo "$LINE" ) | crontab -
echo "  서버 시간대: $TZNAME → 등록된 일정:"
crontab -l | grep collect_buzz.py
echo
echo "완료. 매일 실행 기록은 output/cron.log, 데이터는 output/buzz_daily.csv 에 쌓입니다."
echo "주의: EC2를 중지하면 그동안은 수집되지 않습니다. 인스턴스를 켜 둔 채로 두세요."

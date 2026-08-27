import os
import time
import requests
from bs4 import BeautifulSoup

# 환경 변수에서 토큰과 ID를 가져옵니다 (보안 유지)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# CGV 용산아이파크몰(0013) 날짜
# 예매 오픈이 예상되는 날짜로 date=YYYYMMDD 부분을 수정해서 쓰시면 됩니다.
URL = "http://www.cgv.co.kr/common/showtimes/iframeTheater.aspx?areacode=01&theatercode=0013&date=20260901"


def send_telegram(message):
  telegram_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
  requests.post(telegram_url, data={"chat_id": CHAT_ID, "text": message})


def check_cgv():
  try:
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(URL, headers=headers)
    soup = BeautifulSoup(response.text, "html.parser")

    # IMAX 관 상영 정보 확인
    imax_spans = soup.select("span.imax")

    for span in imax_spans:
      movie_title = span.find_parent("div", class_="col-times").select_one(
          "a"
      ).text.strip()
      if "오디세이" in movie_title:
        send_telegram(
            f"🚨 [용아맥] {movie_title} 예매 오픈!\n지금 바로 CGV 앱 접속하세요!"
        )
        return True
  except Exception as e:
    print(f"에러 발생: {e}")
  return False


print("🚀 용아맥 오디세이 초고속 감시 시작...")
send_telegram("🚀 용아맥 오디세이 감시 서버가 정상적으로 시작되었습니다!")

while True:
  if check_cgv():
    print("오픈 확인! 알림 발송 완료.")
    break
  time.sleep(2)  # 2초마다 초고속 체크

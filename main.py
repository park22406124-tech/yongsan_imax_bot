import os
import threading
import time
import requests
from bs4 import BeautifulSoup

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# CGV 용산아이파크몰(0013) iframe URL
# 예매 오픈 대상 날짜 (20260901 부분을 원하시는 관람 날짜 YYYYMMDD로 변경하세요)
TARGET_DATE = "20260901"
URL = f"http://www.cgv.co.kr/common/showtimes/iframeTheater.aspx?areacode=01&theatercode=0013&date={TARGET_DATE}"

# CGV 용산아이파크몰 모바일 예매 바로가기 링크
CGV_DIRECT_LINK = "https://m.cgv.co.kr/WebApp/Reservation/schedule.aspx?tc=0013"


def send_telegram(message):
  """텔레그램 메시지 발송 함수"""
  try:
    telegram_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(telegram_url, data={"chat_id": CHAT_ID, "text": message})
  except Exception as e:
    print(f"텔레그램 발송 에러: {e}")


def handle_telegram_commands():
  """/test 명령어를 수신하여 테스트 알림을 보내주는 백그라운드 스레드"""
  last_update_id = 0
  print("🤖 텔레그램 명령어 대기 중 (/test 입력 가능)...")

  while True:
    try:
      get_updates_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={last_update_id + 1}&timeout=10"
      res = requests.get(get_updates_url).json()

      if res.get("ok") and res.get("result"):
        for update in res["result"]:
          last_update_id = update["update_id"]
          message = update.get("message", {})
          text = message.get("text", "")

          # /test 입력 감지 시 테스트 메시지 발송
          if text == "/test":
            send_telegram(
                "✅ [테스트 성공] 텔레그램 봇 알림이 정상적으로 동작 중입니다!\n"
                f"🔗 예매 링크 테스트: {CGV_DIRECT_LINK}"
            )
    except Exception as e:
      print(f"명령어 처리 중 에러: {e}")

    time.sleep(2)


def check_cgv():
  """CGV 용산 IMAX 오픈 여부 감시 함수"""
  try:
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(URL, headers=headers)
    soup = BeautifulSoup(response.text, "html.parser")

    # IMAX관 정보 감지
    imax_tags = soup.select("span.imax")

    for imax in imax_tags:
      movie_div = imax.find_parent("div", class_="col-times")
      if movie_div:
        movie_title = movie_div.select_one("a").text.strip()

        if "오디세이" in movie_title:
          msg = (
              f"🚨 [용아맥 오픈!] IMAX관에 '{movie_title}' 예매가 열렸습니다!\n\n"
              f"⚡ 지금 바로 아래 링크로 접속하세요:\n{CGV_DIRECT_LINK}"
          )
          send_telegram(msg)
          return True
  except Exception as e:
    print(f"CGV 크롤링 에러: {e}")
  return False


# 1. /test 명령어를 백그라운드에서 상시 대기
command_thread = threading.Thread(target=handle_telegram_commands, daemon=True)
command_thread.start()

# 2. 감시 서버 시작 알림 전송
print("🚀 [용산 IMAX - 오디세이] 초고속 감시 서버 작동 시작...")
send_telegram(
    "🚀 [용산 IMAX - 오디세이] 감시 서버가 시작되었습니다!\n"
    "💡 채팅창에 `/test` 를 입력하면 알림 테스트가 가능합니다."
)

# 3. CGV 2초 간격 상시 감시
while True:
  if check_cgv():
    print("용아맥 오디세이 오픈 포착 및 알림 완료!")
    break
  time.sleep(2)

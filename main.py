import os
import random
import threading
import time
import requests
from bs4 import BeautifulSoup

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# 예매 오픈 감시 대상 날짜 (YYYYMMDD)
TARGET_DATE = "20260901"

# CGV 용산아이파크몰(0013) iframe URL 및 모바일 직링크
URL = f"http://www.cgv.co.kr/common/showtimes/iframeTheater.aspx?areacode=01&theatercode=0013&date={TARGET_DATE}"
CGV_DIRECT_LINK = f"https://m.cgv.co.kr/WebApp/Reservation/schedule.aspx?tc=0013&ymd={TARGET_DATE}"


def send_telegram(message):
  try:
    telegram_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(telegram_url, data={"chat_id": CHAT_ID, "text": message})
  except Exception as e:
    print(f"텔레그램 발송 에러: {e}")


def check_cgv_status():
  try:
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(URL, headers=headers, timeout=5)
    soup = BeautifulSoup(response.text, "html.parser")

    imax_tags = soup.select("span.imax")

    for imax in imax_tags:
      movie_div = imax.find_parent("div", class_="col-times")
      if movie_div:
        movie_title = movie_div.select_one("a").text.strip()
        if "오디세이" in movie_title:
          return True, movie_title
    return False, None
  except Exception as e:
    print(f"CGV 크롤링 에러: {e}")
    return False, None


def handle_telegram_commands():
  last_update_id = 0
  print("🤖 텔레그램 명령어 대기 중 (/test 입력 가능)...")

  while True:
    try:
      get_updates_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={last_update_id + 1}&timeout=10"
      res = requests.get(get_updates_url, timeout=12).json()

      if res.get("ok") and res.get("result"):
        for update in res["result"]:
          last_update_id = update["update_id"]
          message = update.get("message", {})
          text = message.get("text", "")

          if text == "/test":
            is_open, title = check_cgv_status()
            formatted_date = (
                f"{TARGET_DATE[:4]}-{TARGET_DATE[4:6]}-{TARGET_DATE[6:]}"
            )

            if is_open:
              reply = f"✅ [실시간 상태 점검]\n📅 대상 날짜: {formatted_date}\n🎬 현황: 🎉 [{title}] 용아맥 예매가 오픈되어 있습니다!\n\n🔗 예매 페이지 직행:\n{CGV_DIRECT_LINK}"
            else:
              reply = f"🔍 [실시간 상태 점검]\n📅 대상 날짜: {formatted_date}\n🎬 현황: ❌ 아직 용아맥 '오디세이' 예매가 열리지 않았습니다. (감시 중)\n\n🔗 예매 페이지 사전 확인:\n{CGV_DIRECT_LINK}"

            send_telegram(reply)
    except Exception as e:
      print(f"명령어 처리 중 에러: {e}")

    time.sleep(1.5)


# 1. /test 명령어를 처리할 백그라운드 스레드 시작
command_thread = threading.Thread(target=handle_telegram_commands, daemon=True)
command_thread.start()

# 2. 서버 시작 알림 전송
formatted_date = f"{TARGET_DATE[:4]}-{TARGET_DATE[4:6]}-{TARGET_DATE[6:]}"
print(f"🚀 [용산 IMAX - 오디세이] {formatted_date} 초고속 감시 서버 작동 시작...")
send_telegram(
    f"🚀 [{formatted_date} 용산 IMAX - 오디세이] 감시 서버가 시작되었습니다!\n\n💡 텔레그램 채팅창에 `/test` 를 입력하시면 해당 날짜의 예매 오픈 여부를 실시간으로 확인하실 수 있습니다."
)

# 3. 약 1.5초 간격 상시 자동 감시 루프
while True:
  is_open, movie_title = check_cgv_status()
  if is_open:
    msg = f"🚨 [용아맥 오픈!] {formatted_date} IMAX관에 '{movie_title}' 예매가 열렸습니다!\n\n⚡ 바로 아래 링크를 눌러 예매하세요:\n{CGV_DIRECT_LINK}"
    send_telegram(msg)
    print("용아맥 오디세이 오픈 포착 및 알림 완료!")
    break

  time.sleep(random.uniform(1.4, 1.6))

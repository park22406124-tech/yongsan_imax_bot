import os
import random
import time
import threading
from datetime import datetime

from curl_cffi import requests as cf_requests


# ============================================================
# ⚙️ 기본 설정
# ============================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# 감시 대상 날짜
TARGET_DATE = os.environ.get("TARGET_DATE", "20260901")

# CGV 용산아이파크몰
THEATER_CODE = "0013"
THEATER_NAME = "CGV 용산아이파크몰"

# 영화명
MOVIE_KEYWORDS = [
    "오디세이",
    "The Odyssey",
    "ODYSSEY",
]

# 상영관/포맷
SCREEN_KEYWORDS = [
    "IMAX",
    "아이맥스",
]

# CGV 회사 코드
COMPANY_CODE = "A420"

# 현재 CGV에서 사용하는 상영정보 API
CGV_API_URL = (
    "https://cgv.co.kr/api/v1/booking/searchMovScnInfo"
)

# 현재 CGV 예매 페이지
CGV_BOOKING_URL = "https://cgv.co.kr/cnm/movieBook"

# 너무 공격적인 폴링은 차단 위험이 있으므로 기본 15초
# Railway Variables에서 변경 가능
POLL_INTERVAL = float(
    os.environ.get("POLL_INTERVAL", "15")
)

POLL_JITTER = float(
    os.environ.get("POLL_JITTER", "3")
)


# ============================================================
# 📱 텔레그램
# ============================================================

def send_telegram(message):
    """텔레그램 메시지 전송"""

    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("❌ TELEGRAM_TOKEN 또는 CHAT_ID가 없습니다.")
        return False

    try:
        url = (
            f"https://api.telegram.org/"
            f"bot{TELEGRAM_TOKEN}/sendMessage"
        )

        response = cf_requests.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": message,
                "disable_web_page_preview": False,
            },
            timeout=15,
        )

        if response.status_code != 200:
            print(
                "텔레그램 오류:",
                response.status_code,
                response.text[:500],
            )
            return False

        return True

    except Exception as e:
        print("텔레그램 발송 예외:", repr(e))
        return False


# ============================================================
# 🎬 날짜 표시
# ============================================================

def formatted_date():
    return (
        f"{TARGET_DATE[:4]}-"
        f"{TARGET_DATE[4:6]}-"
        f"{TARGET_DATE[6:]}"
    )


# ============================================================
# 🔗 예매 링크
# ============================================================

def get_booking_link():
    """
    현재 CGV의 최신 예매 페이지.
    기존 m.cgv.co.kr/WebApp 경로는 구형 구조일 가능성이
    있으므로 현재 페이지를 기본값으로 사용한다.
    """

    return CGV_BOOKING_URL


# ============================================================
# 🌐 CGV API 세션
# ============================================================

class CGVClient:

    def __init__(self):
        self.session = None
        self.session_created = 0

    def create_session(self):

        print("🌐 CGV Chrome TLS 세션 생성 중...")

        session = cf_requests.Session(
            impersonate="chrome"
        )

        headers = {
            "Accept": (
                "application/json, text/plain, */*"
            ),
            "Accept-Language": (
                "ko-KR,ko;q=0.9,en-US;q=0.8"
            ),
            "Referer": CGV_BOOKING_URL,
            "Origin": "https://cgv.co.kr",
        }

        # 먼저 실제 CGV 예매 페이지를 방문해서
        # 쿠키/세션을 확보한다.
        response = session.get(
            CGV_BOOKING_URL,
            headers=headers,
            timeout=20,
        )

        print(
            f"🌐 CGV 페이지 응답: "
            f"{response.status_code}"
        )

        if response.status_code != 200:
            raise Exception(
                f"CGV 페이지 접근 실패 "
                f"(HTTP {response.status_code})"
            )

        self.session = session
        self.session_created = time.time()

        return session

    def get_session(self):

        # 30분마다 세션 새로 생성
        if (
            self.session is None
            or time.time() - self.session_created > 1800
        ):
            return self.create_session()

        return self.session

    def fetch_schedule(self):

        session = self.get_session()

        headers = {
            "Accept": (
                "application/json, text/plain, */*"
            ),
            "Accept-Language": (
                "ko-KR,ko;q=0.9,en-US;q=0.8"
            ),
            "Referer": CGV_BOOKING_URL,
            "Origin": "https://cgv.co.kr",
        }

        params = {
            "coCd": COMPANY_CODE,
            "siteNo": THEATER_CODE,
            "scnYmd": TARGET_DATE,
            "rtctlScopCd": "08",
        }

        print(
            f"🔎 CGV API 조회 "
            f"{THEATER_CODE} / {TARGET_DATE}"
        )

        response = session.get(
            CGV_API_URL,
            params=params,
            headers=headers,
            timeout=20,
        )

        print(
            f"📡 CGV API HTTP: "
            f"{response.status_code}"
        )

        # 차단
        if response.status_code in (
            401,
            403,
            429,
            503,
        ):

            print(
                "⚠️ CGV API 접근 제한:",
                response.status_code,
            )

            # 세션을 폐기하고 다음 호출에서 새로 만든다.
            self.session = None

            raise CGVBlockedError(
                f"CGV HTTP {response.status_code}"
            )

        if response.status_code != 200:

            raise Exception(
                f"CGV API HTTP "
                f"{response.status_code}"
            )

        try:
            payload = response.json()

        except Exception:

            print(
                "❌ CGV 응답 JSON 파싱 실패"
            )

            print(
                response.text[:1000]
            )

            raise

        status_code = payload.get(
            "statusCode"
        )

        if status_code != 0:

            raise Exception(
                "CGV API 오류: "
                + str(
                    payload.get(
                        "statusMessage"
                    )
                )
            )

        rows = payload.get("data") or []

        print(
            f"📊 CGV 상영정보 {len(rows)}개 수신"
        )

        return rows


class CGVBlockedError(Exception):
    pass


# ============================================================
# 🎯 오디세이 + IMAX 정확한 판정
# ============================================================

def find_target_showtimes(rows):

    matched = []

    for row in rows:

        movie_name = str(
            row.get("movNm") or ""
        ).strip()

        screen_name = str(
            row.get("scnsNm") or ""
        ).strip()

        start_time = str(
            row.get("scnsrtTm") or ""
        ).strip()

        end_time = str(
            row.get("scnendTm") or ""
        ).strip()

        free_seats = row.get(
            "frSeatCnt"
        )

        total_seats = row.get(
            "stcnt"
        )

        # ----------------------------------------------------
        # 영화명
        # ----------------------------------------------------

        movie_match = any(
            keyword.lower()
            in movie_name.lower()
            for keyword in MOVIE_KEYWORDS
        )

        if not movie_match:
            continue

        # ----------------------------------------------------
        # IMAX
        # ----------------------------------------------------

        screen_match = any(
            keyword.lower()
            in screen_name.lower()
            for keyword in SCREEN_KEYWORDS
        )

        if not screen_match:
            continue

        matched.append(
            {
                "movie": movie_name,
                "screen": screen_name,
                "start": start_time,
                "end": end_time,
                "free": free_seats,
                "total": total_seats,
                "scnsNo": row.get("scnsNo"),
                "scnSseq": row.get("scnSseq"),
                "prodNo": row.get("prodNo"),
            }
        )

    return matched


# ============================================================
# 🔍 상태 조회
# ============================================================

def check_status():

    client = CGVClient()

    try:

        rows = client.fetch_schedule()

        matched = find_target_showtimes(rows)

        return {
            "success": True,
            "rows": rows,
            "matched": matched,
            "error": None,
        }

    except Exception as e:

        return {
            "success": False,
            "rows": [],
            "matched": [],
            "error": repr(e),
        }


# ============================================================
# 🧪 /test 명령
# ============================================================

def handle_telegram_commands():

    print(
        "🤖 텔레그램 명령 대기 시작 "
        "(/test 사용 가능)"
    )

    last_update_id = 0

    while True:

        try:

            url = (
                f"https://api.telegram.org/"
                f"bot{TELEGRAM_TOKEN}/getUpdates"
            )

            response = cf_requests.get(
                url,
                params={
                    "offset": last_update_id + 1,
                    "timeout": 20,
                },
                timeout=25,
            )

            if response.status_code != 200:

                print(
                    "텔레그램 getUpdates 오류:",
                    response.status_code,
                )

                time.sleep(3)
                continue

            data = response.json()

            for update in data.get(
                "result",
                []
            ):

                last_update_id = update[
                    "update_id"
                ]

                message = update.get(
                    "message",
                    {}
                )

                text = (
                    message.get("text")
                    or ""
                ).strip()

                chat = message.get(
                    "chat",
                    {}
                )

                chat_id = str(
                    chat.get("id", "")
                )

                # 등록된 사용자만 명령 처리
                if chat_id != str(CHAT_ID):
                    continue

                if text == "/test":

                    send_telegram(
                        "🔎 [CGV 실시간 진단 시작]\n"
                        f"📅 대상 날짜: {formatted_date()}\n"
                        "🏢 극장: CGV 용산아이파크몰\n"
                        "🎬 영화: 오디세이\n"
                        "🎥 포맷: IMAX\n\n"
                        "잠시만 기다려주세요..."
                    )

                    result = check_status()

                    # ----------------------------------------
                    # API 오류
                    # ----------------------------------------

                    if not result["success"]:

                        send_telegram(
                            "❌ [CGV 조회 실패]\n\n"
                            f"📅 {formatted_date()}\n"
                            f"원인:\n"
                            f"{result['error']}\n\n"
                            "⚠️ 이제는 단순히 "
                            "'예매가 아직 안 열림'으로 "
                            "처리하지 않고 실제 오류를 "
                            "표시합니다."
                        )

                        continue

                    rows = result[
                        "rows"
                    ]

                    matched = result[
                        "matched"
                    ]

                    # ----------------------------------------
                    # 오디세이 IMAX 발견
                    # ----------------------------------------

                    if matched:

                        lines = []

                        for item in matched:

                            free = item[
                                "free"
                            ]

                            total = item[
                                "total"
                            ]

                            if (
                                free is not None
                                and total is not None
                            ):
                                seat_info = (
                                    f"잔여 {free}/"
                                    f"{total}"
                                )
                            else:
                                seat_info = (
                                    "좌석정보 확인불가"
                                )

                            lines.append(
                                f"🕐 "
                                f"{item['start']}"
                                f"~{item['end']} "
                                f"({seat_info})"
                            )

                        schedule_text = (
                            "\n".join(lines)
                        )

                        send_telegram(
                            "🎉 [예매 오픈 확인!]\n\n"
                            f"📅 {formatted_date()}\n"
                            "🏢 CGV 용산아이파크몰\n"
                            "🎬 오디세이\n"
                            "🎥 IMAX\n\n"
                            "📋 확인된 회차:\n"
                            f"{schedule_text}\n\n"
                            "🔗 지금 예매하기:\n"
                            f"{get_booking_link()}"
                        )

                    else:

                        # ------------------------------------
                        # 오디세이/IMAX 미발견
                        # ------------------------------------

                        movie_rows = [
                            row
                            for row in rows
                            if any(
                                keyword.lower()
                                in str(
                                    row.get("movNm")
                                    or ""
                                ).lower()
                                for keyword
                                in MOVIE_KEYWORDS
                            )
                        ]

                        imax_rows = [
                            row
                            for row in rows
                            if any(
                                keyword.lower()
                                in str(
                                    row.get("scnsNm")
                                    or ""
                                ).lower()
                                for keyword
                                in SCREEN_KEYWORDS
                            )
                        ]

                        send_telegram(
                            "🔍 [CGV 실시간 진단 결과]\n\n"
                            f"📅 {formatted_date()}\n"
                            f"📡 API 정상 응답\n"
                            f"📊 전체 상영정보: "
                            f"{len(rows)}개\n"
                            f"🎬 오디세이 관련: "
                            f"{len(movie_rows)}개\n"
                            f"🎥 IMAX 관련: "
                            f"{len(imax_rows)}개\n\n"
                            "❌ 오디세이 + IMAX "
                            "조건을 동시에 만족하는 "
                            "회차가 없습니다.\n\n"
                            "🔗 예매 페이지:\n"
                            f"{get_booking_link()}"
                        )

        except Exception as e:

            print(
                "텔레그램 명령 처리 오류:",
                repr(e)
            )

        time.sleep(1)


# ============================================================
# 🚨 자동 감시
# ============================================================

def monitoring_loop():

    client = CGVClient()

    already_notified = False

    consecutive_errors = 0

    print(
        "\n"
        "==================================================\n"
        "🚀 CGV 용산 IMAX 오디세이 감시 시작\n"
        f"📅 날짜: {formatted_date()}\n"
        f"🏢 극장: {THEATER_NAME}\n"
        "🎬 영화: 오디세이\n"
        "🎥 포맷: IMAX\n"
        f"⏱️ 기본 폴링: {POLL_INTERVAL}초\n"
        "=================================================="
    )

    send_telegram(
        "🚀 [용아맥 감시 서버 시작]\n\n"
        f"📅 {formatted_date()}\n"
        "🏢 CGV 용산아이파크몰\n"
        "🎬 오디세이\n"
        "🎥 IMAX\n\n"
        "현재 CGV 최신 상영정보 API를 "
        "정상 감시하기 시작했습니다.\n\n"
        "🧪 `/test` 입력 시 "
        "실시간 진단이 가능합니다."
    )

    while True:

        try:

            rows = client.fetch_schedule()

            consecutive_errors = 0

            matched = find_target_showtimes(
                rows
            )

            print(
                f"✅ 조회 성공 | "
                f"전체 {len(rows)}개 | "
                f"오디세이 IMAX {len(matched)}개"
            )

            if matched and not already_notified:

                lines = []

                for item in matched:

                    free = item[
                        "free"
                    ]

                    total = item[
                        "total"
                    ]

                    if (
                        free is not None
                        and total is not None
                    ):
                        seat_info = (
                            f"잔여 {free}/{total}"
                        )
                    else:
                        seat_info = (
                            "좌석정보 확인불가"
                        )

                    lines.append(
                        f"🕐 {item['start']}"
                        f"~{item['end']} "
                        f"({seat_info})"
                    )

                send_telegram(
                    "🚨🚨🚨 [용아맥 예매 오픈!] 🚨🚨🚨\n\n"
                    f"📅 {formatted_date()}\n"
                    "🏢 CGV 용산아이파크몰\n"
                    "🎬 오디세이\n"
                    "🎥 IMAX\n\n"
                    "📋 확인된 회차:\n"
                    + "\n".join(lines)
                    + "\n\n"
                    "⚡ 지금 바로 예매하세요!\n"
                    f"{get_booking_link()}"
                )

                print(
                    "🚨 오디세이 IMAX 발견! "
                    "텔레그램 알림 발송 완료"
                )

                already_notified = True

            # ------------------------------------------------
            # 한 번 오픈되면 계속 감시할 필요가 없으므로
            # 종료하지 않고 상태만 유지한다.
            # ------------------------------------------------

        except CGVBlockedError as e:

            consecutive_errors += 1

            print(
                "⚠️ CGV 차단/제한:",
                repr(e)
            )

            # 차단됐을 때는 무작정 1초씩 때리지 않는다.
            backoff = min(
                300,
                15 * (
                    2 ** min(
                        consecutive_errors - 1,
                        4
                    )
                )
            )

            print(
                f"⏳ {backoff}초 후 재시도"
            )

            time.sleep(backoff)
            continue

        except Exception as e:

            consecutive_errors += 1

            print(
                "❌ 감시 오류:",
                repr(e)
            )

            time.sleep(
                min(
                    60,
                    5 * consecutive_errors
                )
            )

            continue

        sleep_time = (
            POLL_INTERVAL
            + random.uniform(
                0,
                POLL_JITTER
            )
        )

        time.sleep(sleep_time)


# ============================================================
# ▶️ 실행
# ============================================================

def main():

    if not TELEGRAM_TOKEN:
        raise RuntimeError(
            "TELEGRAM_TOKEN 환경변수가 없습니다."
        )

    if not CHAT_ID:
        raise RuntimeError(
            "CHAT_ID 환경변수가 없습니다."
        )

    print(
        "============================================"
    )
    print(
        "🎬 CGV 용산 IMAX 오디세이 감시봇"
    )
    print(
        f"📅 TARGET_DATE = {TARGET_DATE}"
    )
    print(
        f"🏢 THEATER_CODE = {THEATER_CODE}"
    )
    print(
        f"⏱️ POLL_INTERVAL = {POLL_INTERVAL}"
    )
    print(
        "============================================"
    )

    # /test 수신 스레드
    command_thread = threading.Thread(
        target=handle_telegram_commands,
        daemon=True,
    )

    command_thread.start()

    # 자동 감시
    monitoring_loop()


if __name__ == "__main__":
    main()      text_content = block.get_text()

      # 해당 영화 블록 내에 '오디세이'와 'IMAX' 글자가 모두 포함되어 있는지 확인
      if "오디세이" in text_content and "IMAX" in text_content:
        # 영화 제목 텍스트 추출
        title_tag = block.select_one("div.info-movie a") or block.select_one(
            "a"
        )
        movie_title = (
            title_tag.text.strip() if title_tag else "오디세이 (IMAX)"
        )
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

import os
import json
import random
import time
import threading
from urllib.parse import urlencode

from curl_cffi import requests as cf_requests


# ============================================================
# ⚙️ 기본 설정
# ============================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# Railway Variables에 TARGET_DATE가 있으면 그것을 사용
# 없으면 기본값 2026-09-01
TARGET_DATE = os.environ.get(
    "TARGET_DATE",
    "20260901"
).replace("-", "")

THEATER_CODE = "0013"
THEATER_NAME = "CGV 용산아이파크몰"

COMPANY_CODE = "A420"
RTCTL_SCOP_CD = "08"

# 영화명 판별
MOVIE_KEYWORDS = [
    "오디세이",
    "The Odyssey",
    "ODYSSEY",
]

# IMAX 판별
FORMAT_KEYWORDS = [
    "IMAX",
    "아이맥스",
]

# ------------------------------------------------------------
# CGV 최신 상영정보 API
# ------------------------------------------------------------

CGV_API_URL = (
    "https://api.cgv.co.kr/"
    "cnm/atkt/searchMovScnInfo"
)

# CGV 극장별 예매 페이지
CGV_CINEMA_URL = (
    "https://cgv.co.kr/cnm/movieBook/cinema"
)

# ------------------------------------------------------------
# 폴링 간격
#
# Railway Variables에서 변경 가능:
# POLL_INTERVAL=5
#
# 너무 짧은 간격은 CGV 차단 가능성이 있으므로
# 기본값은 5초로 설정.
# ------------------------------------------------------------

POLL_INTERVAL = float(
    os.environ.get(
        "POLL_INTERVAL",
        "5"
    )
)

POLL_JITTER = float(
    os.environ.get(
        "POLL_JITTER",
        "1"
    )
)

# Telegram 명령 확인 주기
TELEGRAM_POLL_INTERVAL = 1


# ============================================================
# 📅 날짜
# ============================================================

def display_date():
    return (
        f"{TARGET_DATE[:4]}-"
        f"{TARGET_DATE[4:6]}-"
        f"{TARGET_DATE[6:]}"
    )


# ============================================================
# 🔗 공통 예매 페이지
# ============================================================

def get_cinema_booking_url():
    params = {
        "siteNm": THEATER_NAME,
        "siteNo": THEATER_CODE,
    }

    return (
        CGV_CINEMA_URL
        + "?"
        + urlencode(params)
    )


# ============================================================
# 🎟️ 특정 회차 직행 URL
# ============================================================

def get_showtime_booking_url(showtime):
    """
    CGV API에서 가져온 실제 상영 회차 정보를 이용해서
    특정 회차 예매 페이지 URL을 생성한다.

    필요한 핵심값:
      movNo
      scnSseq
      scnYmd
      scnsNo
      siteNo
    """

    mov_no = (
        showtime.get("movNo")
        or showtime.get("prodNo")
    )

    scn_sseq = (
        showtime.get("scnSseq")
        or showtime.get("scnsSseq")
    )

    scns_no = (
        showtime.get("scnsNo")
        or showtime.get("scnNo")
        or "001"
    )

    if not mov_no or not scn_sseq:
        # 회차 식별값이 없으면 특정 회차 URL을
        # 만들 수 없으므로 극장별 예매 페이지로 fallback
        return get_cinema_booking_url()

    params = {
        "movNo": str(mov_no),
        "scnSseq": str(scn_sseq),
        "scnYmd": TARGET_DATE,
        "scnsNo": str(scns_no),
        "siteNm": THEATER_NAME,
        "siteNo": THEATER_CODE,
    }

    return (
        "https://cgv.co.kr/cnm/movieBook/movie?"
        + urlencode(params)
    )


# ============================================================
# 📱 Telegram
# ============================================================

def telegram_api(method):
    return (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/{method}"
    )


def send_telegram(
    message,
    buttons=None
):
    """
    Telegram 메시지 발송.

    buttons 예:
    [
        [
            {
                "text": "🎟️ 18:00 바로 예매",
                "url": "https://cgv.co.kr/..."
            }
        ]
    ]
    """

    if not TELEGRAM_TOKEN or not CHAT_ID:
        print(
            "❌ TELEGRAM_TOKEN 또는 CHAT_ID가 없습니다."
        )
        return False

    try:

        data = {
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": False,
        }

        if buttons:
            data["reply_markup"] = json.dumps(
                {
                    "inline_keyboard": buttons
                },
                ensure_ascii=False
            )

        response = cf_requests.post(
            telegram_api("sendMessage"),
            data=data,
            timeout=20,
        )

        if response.status_code != 200:
            print(
                "❌ Telegram 발송 실패:",
                response.status_code,
                response.text[:500],
            )
            return False

        return True

    except Exception as e:

        print(
            "❌ Telegram 발송 예외:",
            repr(e)
        )

        return False


# ============================================================
# 🌐 CGV Client
# ============================================================

class CGVBlockedError(Exception):
    pass


class CGVClient:

    def __init__(self):

        self.session = None
        self.created_at = 0

    def create_session(self):

        print(
            "🌐 CGV Chrome TLS 세션 생성..."
        )

        session = cf_requests.Session(
            impersonate="chrome"
        )

        headers = {
            "Accept": (
                "application/json, "
                "text/plain, */*"
            ),
            "Accept-Language": (
                "ko-KR,ko;q=0.9,"
                "en-US;q=0.8,en;q=0.7"
            ),
            "User-Agent": (
                "Mozilla/5.0 "
                "(Linux; Android 16) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/131.0.0.0 "
                "Mobile Safari/537.36"
            ),
            "Referer": (
                "https://cgv.co.kr/"
            ),
            "Origin": (
                "https://cgv.co.kr"
            ),
        }

        # ----------------------------------------------------
        # Railway Variables에서 선택적으로 인증정보 사용
        #
        # CGV_COOKIE
        # CGV_AUTHORIZATION
        # CGV_EXTRA_HEADERS_JSON
        # ----------------------------------------------------

        cookie = os.environ.get(
            "CGV_COOKIE"
        )

        authorization = os.environ.get(
            "CGV_AUTHORIZATION"
        )

        extra_headers = os.environ.get(
            "CGV_EXTRA_HEADERS_JSON"
        )

        if cookie:
            headers["Cookie"] = cookie

        if authorization:
            headers[
                "Authorization"
            ] = authorization

        if extra_headers:

            try:

                parsed = json.loads(
                    extra_headers
                )

                if isinstance(
                    parsed,
                    dict
                ):
                    headers.update(
                        parsed
                    )

            except Exception as e:

                print(
                    "⚠️ "
                    "CGV_EXTRA_HEADERS_JSON "
                    "파싱 실패:",
                    repr(e)
                )

        # 먼저 CGV 메인에 접속하여 세션 확보
        response = session.get(
            "https://cgv.co.kr/",
            headers=headers,
            timeout=20,
        )

        print(
            "🌐 CGV 메인 응답:",
            response.status_code
        )

        if response.status_code in (
            401,
            403,
            429,
            503,
        ):
            raise CGVBlockedError(
                f"CGV 메인 HTTP "
                f"{response.status_code}"
            )

        if response.status_code != 200:
            raise Exception(
                "CGV 메인 접근 실패: "
                f"{response.status_code}"
            )

        self.session = session
        self.created_at = time.time()

        return session

    def get_session(self):

        # 세션 30분마다 새로 생성
        if (
            self.session is None
            or time.time() - self.created_at
            > 1800
        ):
            return self.create_session()

        return self.session

    def fetch_schedule(self):

        session = self.get_session()

        headers = {
            "Accept": (
                "application/json, "
                "text/plain, */*"
            ),
            "Accept-Language": (
                "ko-KR,ko;q=0.9,"
                "en-US;q=0.8,en;q=0.7"
            ),
            "Referer": (
                "https://cgv.co.kr/"
            ),
            "Origin": (
                "https://cgv.co.kr"
            ),
        }

        cookie = os.environ.get(
            "CGV_COOKIE"
        )

        authorization = os.environ.get(
            "CGV_AUTHORIZATION"
        )

        extra_headers = os.environ.get(
            "CGV_EXTRA_HEADERS_JSON"
        )

        if cookie:
            headers["Cookie"] = cookie

        if authorization:
            headers[
                "Authorization"
            ] = authorization

        if extra_headers:

            try:

                parsed = json.loads(
                    extra_headers
                )

                if isinstance(
                    parsed,
                    dict
                ):
                    headers.update(
                        parsed
                    )

            except Exception:
                pass

        params = {
            "coCd": COMPANY_CODE,
            "siteNo": THEATER_CODE,
            "scnYmd": TARGET_DATE,
            "rtctlScopCd": RTCTL_SCOP_CD,
        }

        print(
            "🔎 CGV 상영정보 조회:",
            TARGET_DATE
        )

        response = session.get(
            CGV_API_URL,
            params=params,
            headers=headers,
            timeout=20,
        )

        print(
            "📡 CGV API:",
            response.status_code
        )

        if response.status_code in (
            401,
            403,
            429,
            503,
        ):

            self.session = None

            raise CGVBlockedError(
                "CGV API HTTP "
                f"{response.status_code}"
            )

        if response.status_code != 200:

            raise Exception(
                "CGV API HTTP "
                f"{response.status_code}"
            )

        try:

            payload = response.json()

        except Exception:

            print(
                "❌ CGV API JSON 파싱 실패"
            )

            print(
                response.text[:1000]
            )

            raise

        if payload.get(
            "statusCode"
        ) not in (
            0,
            "0",
            None,
        ):

            raise Exception(
                "CGV API 오류: "
                + str(
                    payload.get(
                        "statusMessage"
                    )
                )
            )

        rows = (
            payload.get("data")
            or []
        )

        print(
            f"📊 전체 상영정보: "
            f"{len(rows)}개"
        )

        return rows


# ============================================================
# 🎯 영화 + IMAX 판정
# ============================================================

def text_contains_keyword(
    text,
    keywords
):

    text = str(
        text or ""
    ).lower()

    return any(
        keyword.lower() in text
        for keyword in keywords
    )


def find_target_showtimes(rows):

    matched = []

    for row in rows:

        movie_name = str(
            row.get("movNm")
            or row.get("movieNm")
            or ""
        ).strip()

        screen_name = str(
            row.get("scnsNm")
            or row.get("screenNm")
            or ""
        ).strip()

        start_time = str(
            row.get("scnsrtTm")
            or row.get("startTime")
            or ""
        ).strip()

        end_time = str(
            row.get("scnendTm")
            or row.get("endTime")
            or ""
        ).strip()

        movie_match = (
            text_contains_keyword(
                movie_name,
                MOVIE_KEYWORDS
            )
        )

        format_match = (
            text_contains_keyword(
                screen_name,
                FORMAT_KEYWORDS
            )
        )

        if not movie_match:
            continue

        if not format_match:
            continue

        matched.append(
            {
                "movie": movie_name,
                "screen": screen_name,
                "start": start_time,
                "end": end_time,

                "free": (
                    row.get("frSeatCnt")
                    or row.get("remainSeatCnt")
                ),

                "total": (
                    row.get("stcnt")
                    or row.get("totalSeatCnt")
                ),

                # ⭐ 직행 링크 생성에 필요한 핵심값
                "movNo": (
                    row.get("movNo")
                    or row.get("prodNo")
                ),

                "scnsNo": (
                    row.get("scnsNo")
                    or row.get("scnNo")
                ),

                "scnSseq": (
                    row.get("scnSseq")
                    or row.get("scnsSseq")
                ),

                "raw": row,
            }
        )

    # 시간순 정렬
    matched.sort(
        key=lambda x: x["start"]
    )

    return matched


# ============================================================
# 🧪 진단용 상태 조회
# ============================================================

def check_status():

    client = CGVClient()

    try:

        rows = client.fetch_schedule()

        matched = find_target_showtimes(
            rows
        )

        movie_rows = [
            row
            for row in rows
            if text_contains_keyword(
                row.get("movNm"),
                MOVIE_KEYWORDS
            )
        ]

        imax_rows = [
            row
            for row in rows
            if text_contains_keyword(
                row.get("scnsNm"),
                FORMAT_KEYWORDS
            )
        ]

        return {
            "success": True,
            "rows": rows,
            "matched": matched,
            "movie_rows": movie_rows,
            "imax_rows": imax_rows,
            "error": None,
        }

    except Exception as e:

        return {
            "success": False,
            "rows": [],
            "matched": [],
            "movie_rows": [],
            "imax_rows": [],
            "error": repr(e),
        }


# ============================================================
# 🎟️ Telegram 회차별 버튼 만들기
# ============================================================

def make_showtime_buttons(
    matched
):

    buttons = []

    for item in matched:

        start = (
            item["start"]
            or "회차"
        )

        screen = (
            item["screen"]
            or "IMAX"
        )

        url = get_showtime_booking_url(
            item
        )

        buttons.append(
            [
                {
                    "text": (
                        f"🎟️ {start} "
                        f"{screen} 바로 예매"
                    ),
                    "url": url,
                }
            ]
        )

    # 마지막에 전체 시간표 버튼
    buttons.append(
        [
            {
                "text": "📅 용산 전체 시간표",
                "url": (
                    get_cinema_booking_url()
                ),
            }
        ]
    )

    return buttons


# ============================================================
# 🚨 오픈 알림 메시지
# ============================================================

def send_open_alert(
    matched,
    test_mode=False
):

    if not matched:
        return

    if test_mode:

        title = (
            "🎉 [실시간 테스트]"
        )

    else:

        title = (
            "🚨🚨🚨 "
            "[용아맥 오픈 감지!] "
            "🚨🚨🚨"
        )

    lines = [
        title,
        "",
        f"📅 {display_date()}",
        f"🏢 {THEATER_NAME}",
        "🎬 오디세이",
        "🎥 IMAX",
        "",
        "📋 확인된 회차",
        "",
    ]

    for item in matched:

        free = item["free"]
        total = item["total"]

        if (
            free is not None
            and total is not None
        ):
            seat_text = (
                f"잔여 {free}/{total}"
            )
        elif free is not None:
            seat_text = (
                f"잔여 {free}석"
            )
        else:
            seat_text = (
                "좌석정보 확인불가"
            )

        lines.append(
            f"🕐 {item['start']}"
            f" ~ {item['end']}"
        )

        lines.append(
            f"   {seat_text}"
        )

        lines.append("")

    lines.extend(
        [
            "👇 아래 버튼을 누르면",
            "해당 회차 예매 화면으로 이동합니다.",
        ]
    )

    buttons = make_showtime_buttons(
        matched
    )

    send_telegram(
        "\n".join(lines),
        buttons=buttons,
    )


# ============================================================
# 🧪 Telegram /test
# ============================================================

def handle_telegram_commands():

    print(
        "🤖 Telegram 명령 대기 중..."
    )

    last_update_id = 0

    while True:

        try:

            response = cf_requests.get(
                telegram_api("getUpdates"),
                params={
                    "offset": (
                        last_update_id + 1
                    ),
                    "timeout": 20,
                },
                timeout=25,
            )

            if response.status_code != 200:

                print(
                    "Telegram getUpdates 오류:",
                    response.status_code
                )

                time.sleep(3)
                continue

            data = response.json()

            for update in data.get(
                "result",
                []
            ):

                last_update_id = (
                    update["update_id"]
                )

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

                incoming_chat_id = str(
                    chat.get("id", "")
                )

                # 내 Chat ID만 처리
                if (
                    incoming_chat_id
                    != str(CHAT_ID)
                ):
                    continue

                # --------------------------------------------
                # /test
                # --------------------------------------------

                if text == "/test":

                    send_telegram(
                        "🔎 [CGV 실시간 진단]\n\n"
                        f"📅 {display_date()}\n"
                        f"🏢 {THEATER_NAME}\n"
                        "🎬 오디세이\n"
                        "🎥 IMAX\n\n"
                        "CGV 상영정보를 확인하고 있습니다..."
                    )

                    result = check_status()

                    # API 오류
                    if not result[
                        "success"
                    ]:

                        send_telegram(
                            "❌ [CGV API 조회 실패]\n\n"
                            f"📅 {display_date()}\n\n"
                            "단순히 '예매 미오픈'으로 "
                            "처리하지 않고 실제 오류를 표시합니다.\n\n"
                            f"원인:\n"
                            f"{result['error']}"
                        )

                        continue

                    rows = result[
                        "rows"
                    ]

                    matched = result[
                        "matched"
                    ]

                    # 실제 오픈 발견
                    if matched:

                        send_open_alert(
                            matched,
                            test_mode=True
                        )

                        continue

                    # 오디세이 존재 여부
                    movie_count = len(
                        result[
                            "movie_rows"
                        ]
                    )

                    # IMAX 존재 여부
                    imax_count = len(
                        result[
                            "imax_rows"
                        ]

                    )

                    send_telegram(
                        "🔍 [CGV 실시간 진단 결과]\n\n"
                        f"📅 {display_date()}\n"
                        "📡 API: 정상\n"
                        f"📊 전체 상영정보: "
                        f"{len(rows)}개\n"
                        f"🎬 오디세이 관련: "
                        f"{movie_count}개\n"
                        f"🎥 IMAX 관련: "
                        f"{imax_count}개\n\n"
                        "❌ 현재 API 응답에서\n"
                        "오디세이 + IMAX 회차를 "
                        "찾지 못했습니다.\n\n"
                        "📅 용산 전체 시간표:",
                        buttons=[
                            [
                                {
                                    "text": (
                                        "📅 용산 예매 화면 열기"
                                    ),
                                    "url": (
                                        get_cinema_booking_url()
                                    ),
                                }
                            ]
                        ],
                    )

                # --------------------------------------------
                # /status
                # --------------------------------------------

                elif text == "/status":

                    send_telegram(
                        "🤖 [감시 서버 상태]\n\n"
                        f"📅 대상 날짜: "
                        f"{display_date()}\n"
                        f"🏢 극장: "
                        f"{THEATER_NAME}\n"
                        "🎬 영화: 오디세이\n"
                        "🎥 포맷: IMAX\n"
                        f"⏱️ 폴링: "
                        f"{POLL_INTERVAL}초 + 지터\n"
                        "🟢 감시 프로세스 작동 중"
                    )

        except Exception as e:

            print(
                "❌ Telegram 명령 처리 오류:",
                repr(e)
            )

        time.sleep(
            TELEGRAM_POLL_INTERVAL
        )


# ============================================================
# 🚀 자동 감시 루프
# ============================================================

def monitoring_loop():

    client = CGVClient()

    notified_keys = set()

    consecutive_errors = 0

    print("")
    print(
        "=============================================="
    )
    print(
        "🚀 CGV 용산 IMAX 오디세이 감시 시작"
    )
    print(
        f"📅 {display_date()}"
    )
    print(
        f"🏢 {THEATER_NAME}"
    )
    print(
        "🎬 오디세이"
    )
    print(
        "🎥 IMAX"
    )
    print(
        f"⏱️ {POLL_INTERVAL}초 + 랜덤 지터"
    )
    print(
        "=============================================="
    )
    print("")

    send_telegram(
        "🚀 [용아맥 감시 시작]\n\n"
        f"📅 {display_date()}\n"
        f"🏢 {THEATER_NAME}\n"
        "🎬 오디세이\n"
        "🎥 IMAX\n\n"
        f"⏱️ {POLL_INTERVAL}초 간격으로 "
        "감시를 시작했습니다.\n\n"
        "🧪 `/test` = 즉시 상태 확인\n"
        "ℹ️ `/status` = 서버 상태 확인"
    )

    while True:

        try:

            rows = client.fetch_schedule()

            consecutive_errors = 0

            matched = find_target_showtimes(
                rows
            )

            print(
                "✅ 조회 성공 | "
                f"전체 {len(rows)}개 | "
                f"오디세이 IMAX "
                f"{len(matched)}개"
            )

            if matched:

                # 회차별 고유키
                current_keys = set()

                for item in matched:

                    key = (
                        str(
                            item.get(
                                "movNo"
                            )
                        )
                        + "_"
                        + str(
                            item.get(
                                "scnSseq"
                            )
                        )
                        + "_"
                        + str(
                            item.get(
                                "start"
                            )
                        )
                    )

                    current_keys.add(key)

                # 새로 발견된 회차만 알림
                new_matches = [
                    item
                    for item in matched
                    if (
                        str(
                            item.get(
                                "movNo"
                            )
                        )
                        + "_"
                        + str(
                            item.get(
                                "scnSseq"
                            )
                        )
                        + "_"
                        + str(
                            item.get(
                                "start"
                            )
                        )
                    )
                    not in notified_keys
                ]

                if new_matches:

                    print(
                        "🚨 새로운 "
                        "오디세이 IMAX 회차 발견!"
                    )

                    send_open_alert(
                        new_matches,
                        test_mode=False
                    )

                    for item in new_matches:

                        key = (
                            str(
                                item.get(
                                    "movNo"
                                )
                            )
                            + "_"
                            + str(
                                item.get(
                                    "scnSseq"
                                )
                            )
                            + "_"
                            + str(
                                item.get(
                                    "start"
                                )
                            )
                        )

                        notified_keys.add(
                            key
                        )

                # 이미 발견한 회차가 모두 사라진 경우
                # 해당 회차를 다시 오픈하면 재알림 가능
                notified_keys.intersection_update(
                    current_keys
                )

        except CGVBlockedError as e:

            consecutive_errors += 1

            print(
                "⚠️ CGV 접근 제한:",
                repr(e)
            )

            # 점진적 백오프
            backoff = min(
                300,
                max(
                    15,
                    15 * (
                        2 ** min(
                            consecutive_errors - 1,
                            4
                        )
                    )
                )
            )

            print(
                f"⏳ {backoff}초 후 재시도"
            )

            time.sleep(
                backoff
            )

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

        # 정상 폴링
        sleep_time = (
            POLL_INTERVAL
            + random.uniform(
                0,
                POLL_JITTER
            )
        )

        time.sleep(
            sleep_time
        )


# ============================================================
# ▶️ MAIN
# ============================================================

def main():

    # 필수 환경변수 검사
    if not TELEGRAM_TOKEN:

        raise RuntimeError(
            "TELEGRAM_TOKEN 환경변수가 없습니다."
        )

    if not CHAT_ID:

        raise RuntimeError(
            "CHAT_ID 환경변수가 없습니다."
        )

    # 날짜 형식 검사
    if (
        len(TARGET_DATE) != 8
        or not TARGET_DATE.isdigit()
    ):

        raise RuntimeError(
            "TARGET_DATE는 "
            "YYYYMMDD 형식이어야 합니다."
        )

    print(
        "=============================================="
    )
    print(
        "🎬 CGV 용아맥 오디세이 알리미"
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
        "🎟️ 개별 회차 직행 링크 활성화"
    )
    print(
        "📱 Telegram Inline Button 활성화"
    )
    print(
        "=============================================="
    )

    # Telegram 명령어 스레드
    command_thread = threading.Thread(
        target=handle_telegram_commands,
        daemon=True,
    )

    command_thread.start()

    # 자동 감시
    monitoring_loop()


if __name__ == "__main__":
    main()

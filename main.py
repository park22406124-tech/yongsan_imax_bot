import asyncio
import logging
import os
import time
from datetime import datetime, timedelta

import requests
from curl_cffi import requests as curl_requests


# ============================================================
# 설정
# ============================================================

CGV_WEB_URL = os.getenv(
    "CGV_WEB_URL",
    "https://cgv.co.kr/cnm/movieBook/cinema",
).strip()

CGV_API_URL = os.getenv(
    "CGV_API_URL",
    "https://api.cgv.co.kr/cnm/atkt/searchMovScnInfo",
).strip()

# CGV 기본 파라미터
CO_CD = os.getenv(
    "CO_CD",
    "A420",
).strip()

SITE_NO = os.getenv(
    "SITE_NO",
    "0013",
).strip()

RTCTL_SCOP_CD = os.getenv(
    "RTCTL_SCOP_CD",
    "08",
).strip()

# ------------------------------------------------------------
# 극장
# ------------------------------------------------------------

THEATER_NAME = os.getenv(
    "THEATER_NAME",
    "용산아이파크몰",
).strip()

# ------------------------------------------------------------
# 영화
# ------------------------------------------------------------

MOVIE_ALIASES = [
    x.strip()
    for x in os.getenv(
        "MOVIE_ALIASES",
        "오디세이,The Odyssey,ODYSSEY",
    ).split(",")
    if x.strip()
]

# ------------------------------------------------------------
# 상영 포맷
# ------------------------------------------------------------

FORMAT_KEYWORDS = [
    x.strip().lower()
    for x in os.getenv(
        "FORMAT_KEYWORDS",
        "IMAX,아이맥스",
    ).split(",")
    if x.strip()
]

# ------------------------------------------------------------
# 검사 주기
# ------------------------------------------------------------

INTERVAL_SECONDS = max(
    10,
    int(
        os.getenv(
            "INTERVAL_SECONDS",
            "20",
        )
    ),
)

# 오늘 + 며칠
DAYS_AHEAD = max(
    0,
    int(
        os.getenv(
            "DAYS_AHEAD",
            "7",
        )
    ),
)

# API timeout
REQUEST_TIMEOUT = max(
    5,
    int(
        os.getenv(
            "REQUEST_TIMEOUT",
            "20",
        )
    ),
)

# API 재시도
API_RETRIES = max(
    1,
    int(
        os.getenv(
            "API_RETRIES",
            "2",
        )
    ),
)

# ------------------------------------------------------------
# Telegram
# ------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    "",
).strip()

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    "",
).strip()

# ------------------------------------------------------------
# Railway Health Server
# ------------------------------------------------------------

PORT = int(
    os.getenv(
        "PORT",
        "8080",
    )
)


# ============================================================
# 로그
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(message)s"
    ),
)

logger = logging.getLogger(
    "yongsan-imax-api-bot"
)


# ============================================================
# 전역
# ============================================================

http_session = None

monitor_enabled = True

seen_sessions = {}

last_api_response = 0

last_api_status = None

last_successful_scan = 0

telegram_update_offset = 0

scan_lock = asyncio.Lock()


# ============================================================
# 날짜
# ============================================================

def make_ymd(offset=0):
    return (
        datetime.now()
        + timedelta(days=offset)
    ).strftime("%Y%m%d")


def pretty_date(ymd):
    if not ymd:
        return ""

    text = str(ymd)

    if len(text) != 8:
        return text

    return (
        f"{text[:4]}-"
        f"{text[4:6]}-"
        f"{text[6:]}"
    )


# ============================================================
# Chrome User-Agent
# ============================================================

CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/138.0.0.0 "
    "Safari/537.36"
)


# ============================================================
# HTTP 세션 생성
# ============================================================

def create_http_session():

    logger.info(
        "🌐 curl_cffi Chrome 세션 생성"
    )

    session = curl_requests.Session(
        impersonate="chrome",
    )

    session.headers.update(
        {
            "User-Agent": CHROME_USER_AGENT,
            "Accept": (
                "application/json, text/plain, */*"
            ),
            "Accept-Language": (
                "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7"
            ),
            "Connection": "keep-alive",
        }
    )

    return session


# ============================================================
# CGV 웹 세션 준비
# ============================================================

def warm_up_cgv():

    global http_session

    if http_session is None:
        http_session = create_http_session()

    logger.info(
        "🌐 CGV 웹 페이지 세션 준비"
    )

    try:

        response = http_session.get(
            CGV_WEB_URL,
            headers={
                "User-Agent": CHROME_USER_AGENT,
                "Accept": (
                    "text/html,application/xhtml+xml,"
                    "application/xml;q=0.9,image/avif,"
                    "image/webp,*/*;q=0.8"
                ),
                "Accept-Language": (
                    "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7"
                ),
                "Upgrade-Insecure-Requests": "1",
            },
            timeout=REQUEST_TIMEOUT,
        )

        logger.info(
            "🌐 CGV 웹 HTTP %s",
            response.status_code,
        )

        logger.info(
            "🍪 CGV cookies=%d",
            len(http_session.cookies),
        )

        return response.status_code

    except Exception as exc:

        logger.warning(
            "CGV 웹 세션 준비 실패: %s",
            exc,
        )

        return None


# ============================================================
# Telegram
# ============================================================

def send_telegram(
    message,
    buttons=None,
):

    if not TELEGRAM_BOT_TOKEN:
        logger.warning(
            "TELEGRAM_BOT_TOKEN 없음"
        )
        return False

    if not TELEGRAM_CHAT_ID:
        logger.warning(
            "TELEGRAM_CHAT_ID 없음"
        )
        return False

    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    if buttons:
        payload["reply_markup"] = {
            "inline_keyboard": buttons
        }

    try:

        response = requests.post(
            url,
            json=payload,
            timeout=15,
        )

        if response.status_code != 200:

            logger.error(
                "Telegram HTTP %s: %s",
                response.status_code,
                response.text[:500],
            )

            return False

        return True

    except Exception as exc:

        logger.error(
            "Telegram 오류: %s",
            exc,
        )

        return False


# ============================================================
# Telegram async API
# ============================================================

async def telegram_api(
    method,
    payload=None,
):

    if not TELEGRAM_BOT_TOKEN:
        return None

    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/{method}"
    )

    try:

        def request():

            return requests.post(
                url,
                json=payload or {},
                timeout=30,
            )

        response = await asyncio.to_thread(
            request
        )

        if response.status_code != 200:

            logger.error(
                "Telegram API HTTP %s: %s",
                response.status_code,
                response.text[:500],
            )

            return None

        return response.json()

    except Exception as exc:

        logger.error(
            "Telegram API 오류: %s",
            exc,
        )

        return None


# ============================================================
# Telegram 명령어
# ============================================================

async def telegram_command_loop():

    global telegram_update_offset
    global monitor_enabled

    if not TELEGRAM_BOT_TOKEN:

        logger.warning(
            "Telegram 명령어 비활성화"
        )

        return

    logger.info(
        "📱 Telegram 명령어 감시 시작"
    )

    await telegram_api(
        "deleteWebhook",
        {
            "drop_pending_updates": False,
        },
    )

    while True:

        try:

            result = await telegram_api(
                "getUpdates",
                {
                    "offset": telegram_update_offset,
                    "timeout": 20,
                    "allowed_updates": [
                        "message"
                    ],
                },
            )

            if not result or not result.get("ok"):

                await asyncio.sleep(3)
                continue

            updates = result.get(
                "result",
                [],
            )

            for update in updates:

                telegram_update_offset = (
                    update["update_id"] + 1
                )

                message = update.get(
                    "message"
                )

                if not message:
                    continue

                chat = message.get(
                    "chat",
                    {},
                )

                chat_id = str(
                    chat.get(
                        "id",
                        "",
                    )
                )

                if (
                    TELEGRAM_CHAT_ID
                    and chat_id
                    != TELEGRAM_CHAT_ID
                ):

                    logger.warning(
                        "허용되지 않은 Telegram 채팅방: %s",
                        chat_id,
                    )

                    continue

                text = (
                    message.get(
                        "text",
                        "",
                    )
                    .strip()
                    .lower()
                )

                # ------------------------------------------------
                # START
                # ------------------------------------------------

                if text == "/start":

                    monitor_enabled = True

                    logger.info(
                        "▶️ /start"
                    )

                    send_telegram(
                        "🟢 <b>용아맥 API 감시를 시작했습니다.</b>\n\n"
                        f"🎬 {', '.join(MOVIE_ALIASES)}\n"
                        f"🏢 {THEATER_NAME}\n"
                        "🎞️ IMAX\n"
                        f"⏱️ {INTERVAL_SECONDS}초 간격\n\n"
                        "📡 CGV API 직접 조회 방식"
                    )

                # ------------------------------------------------
                # STOP
                # ------------------------------------------------

                elif text == "/stop":

                    monitor_enabled = False

                    logger.info(
                        "⏸️ /stop"
                    )

                    send_telegram(
                        "⏸️ <b>용아맥 감시를 중지했습니다.</b>\n\n"
                        "프로그램과 Telegram 명령어는 계속 실행됩니다.\n"
                        "다시 감시하려면 /start"
                    )

                # ------------------------------------------------
                # STATUS
                # ------------------------------------------------

                elif text in (
                    "/status",
                    "/상태",
                ):

                    monitor_text = (
                        "🟢 감시 중"
                        if monitor_enabled
                        else "⏸️ 감시 중지"
                    )

                    if last_api_response:

                        elapsed = int(
                            time.time()
                            - last_api_response
                        )

                        response_text = (
                            f"{elapsed}초 전"
                        )

                    else:

                        response_text = (
                            "아직 없음"
                        )

                    if last_successful_scan:

                        elapsed = int(
                            time.time()
                            - last_successful_scan
                        )

                        scan_text = (
                            f"{elapsed}초 전"
                        )

                    else:

                        scan_text = "아직 없음"

                    send_telegram(
                        "📊 <b>용아맥 API 봇 상태</b>\n\n"
                        f"상태: {monitor_text}\n"
                        f"극장: {THEATER_NAME}\n"
                        "🎞️ IMAX\n"
                        f"검사 간격: {INTERVAL_SECONDS}초\n"
                        f"마지막 API 응답: {response_text}\n"
                        f"마지막 정상 검사: {scan_text}\n"
                        f"마지막 HTTP: {last_api_status}"
                    )

                # ------------------------------------------------
                # TEST
                # ------------------------------------------------

                elif text == "/test":

                    logger.info(
                        "🔎 /test"
                    )

                    send_telegram(
                        "🔎 <b>CGV API를 직접 조회하는 중...</b>\n\n"
                        "화면을 클릭하지 않고\n"
                        "CGV 시간표 API를 조회합니다."
                    )

                    try:

                        sessions = (
                            await perform_test_scan()
                        )

                        if sessions:

                            lines = [
                                "🎬 <b>오늘 용아맥 IMAX 현황</b>",
                                "",
                                f"📅 {pretty_date(make_ymd(0))}",
                                "",
                            ]

                            for item in sessions:

                                start = format_time(
                                    item.get("start")
                                )

                                end = format_time(
                                    item.get("end")
                                )

                                seats = item.get(
                                    "seats",
                                    0,
                                )

                                total = item.get(
                                    "totalSeats"
                                )

                                if total:
                                    seat_text = (
                                        f"{seats}/{total}석"
                                    )
                                else:
                                    seat_text = (
                                        f"{seats}석"
                                    )

                                icon = (
                                    "🟢"
                                    if seats > 0
                                    else "⚪"
                                )

                                lines.append(
                                    f"{icon} "
                                    f"<b>{start} ~ {end}</b> "
                                    f"💺 {seat_text}"
                                )

                            send_telegram(
                                "\n".join(lines)
                            )

                        else:

                            send_telegram(
                                "🔎 <b>오늘 용아맥 API 조회 결과</b>\n\n"
                                "현재 조건에 맞는 IMAX 회차가 없습니다.\n\n"
                                "영화 / IMAX 필터 또는 CGV API 응답을 확인하세요."
                            )

                    except Exception as exc:

                        logger.exception(
                            "/test 실패"
                        )

                        send_telegram(
                            "🔴 <b>/test 실패</b>\n\n"
                            f"{str(exc)[:800]}"
                        )

        except asyncio.CancelledError:

            raise

        except Exception as exc:

            logger.exception(
                "Telegram 루프 오류: %s",
                exc,
            )

            await asyncio.sleep(5)


# ============================================================
# CGV API 호출
# ============================================================

def request_cgv_api(
    scn_ymd,
):

    global http_session
    global last_api_response
    global last_api_status

    if http_session is None:

        http_session = create_http_session()

    params = {
        "coCd": CO_CD,
        "siteNo": SITE_NO,
        "scnYmd": scn_ymd,
        "rtctlScopCd": RTCTL_SCOP_CD,
    }

    headers = {
        "User-Agent": CHROME_USER_AGENT,
        "Accept": (
            "application/json, text/plain, */*"
        ),
        "Accept-Language": (
            "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7"
        ),
        "Referer": CGV_WEB_URL,
        "Origin": "https://cgv.co.kr",
    }

    logger.info(
        "📡 CGV API 요청"
    )

    logger.info(
        "URL: %s",
        CGV_API_URL,
    )

    logger.info(
        "PARAMS: %s",
        params,
    )

    for attempt in range(
        1,
        API_RETRIES + 1,
    ):

        try:

            response = http_session.get(
                CGV_API_URL,
                params=params,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )

            last_api_response = time.time()
            last_api_status = response.status_code

            logger.info(
                "📡 CGV API HTTP %s",
                response.status_code,
            )

            logger.info(
                "최종 URL: %s",
                response.url,
            )

            text = response.text

            # ------------------------------------------------
            # 401
            # ------------------------------------------------

            if response.status_code == 401:

                logger.error(
                    "🔴 CGV API 401 인증 거부"
                )

                logger.error(
                    "응답: %s",
                    text[:1000],
                )

                return None

            # ------------------------------------------------
            # 기타 HTTP 오류
            # ------------------------------------------------

            if response.status_code != 200:

                logger.error(
                    "🔴 CGV API HTTP 오류: %s",
                    response.status_code,
                )

                logger.error(
                    "응답: %s",
                    text[:1000],
                )

                if attempt < API_RETRIES:

                    time.sleep(1)
                    continue

                return None

            # ------------------------------------------------
            # JSON
            # ------------------------------------------------

            try:

                data = response.json()

            except Exception as exc:

                logger.error(
                    "🔴 JSON 파싱 실패: %s",
                    exc,
                )

                logger.error(
                    "응답: %s",
                    text[:1000],
                )

                return None

            if not isinstance(
                data,
                dict,
            ):

                logger.error(
                    "🔴 예상하지 못한 API 응답"
                )

                return None

            status_code = data.get(
                "statusCode"
            )

            status_message = data.get(
                "statusMessage"
            )

            logger.info(
                "CGV API statusCode=%s statusMessage=%s",
                status_code,
                status_message,
            )

            return data

        except Exception as exc:

            logger.error(
                "CGV API 요청 예외 (%d/%d): %s",
                attempt,
                API_RETRIES,
                exc,
            )

            if attempt < API_RETRIES:

                time.sleep(1)

    return None


# ============================================================
# rows 추출
# ============================================================

def extract_rows(
    payload,
):

    if not isinstance(
        payload,
        dict,
    ):

        return []

    data = payload.get(
        "data"
    )

    if isinstance(
        data,
        list,
    ):

        return data

    if isinstance(
        data,
        dict,
    ):

        for key in [
            "list",
            "rows",
            "result",
            "schedule",
            "scnList",
            "movieList",
            "resultList",
            "contents",
            "items",
        ]:

            value = data.get(
                key
            )

            if isinstance(
                value,
                list,
            ):

                return value

    return []


# ============================================================
# 영화 필터
# ============================================================

def target_movie(
    row,
):

    values = [
        row.get("movNm"),
        row.get("movEnm"),
        row.get("prodNm"),
        row.get("expoProdNm"),
        row.get("engProdNm"),
        row.get("movieNm"),
        row.get("movieName"),
        row.get("movName"),
    ]

    text = " ".join(
        str(x)
        for x in values
        if x is not None
    ).lower()

    return any(
        alias.lower() in text
        for alias in MOVIE_ALIASES
    )


# ============================================================
# IMAX 필터
# ============================================================

def target_format(
    row,
):

    values = [
        row.get("movkndDsplNm"),
        row.get("movkndDsplEnm"),
        row.get("scnsNm"),
        row.get("expoScnsNm"),
        row.get("scnsEnm"),
        row.get("screenNm"),
        row.get("screenName"),
        row.get("tcscnsGradNm"),
        row.get("screenType"),
        row.get("screenNmKor"),
        row.get("scnTypeNm"),
    ]

    text = " ".join(
        str(x)
        for x in values
        if x is not None
    ).lower()

    return any(
        keyword in text
        for keyword in FORMAT_KEYWORDS
    )


# ============================================================
# 좌석 수
# ============================================================

def seat_count(
    row,
):

    for key in [
        "frSeatCnt",
        "remainSeatCnt",
        "availableSeatCnt",
        "seatCnt",
        "rmSeatCnt",
        "remainCnt",
    ]:

        try:

            value = row.get(
                key
            )

            if value is not None:

                return int(
                    str(value).replace(
                        ",",
                        "",
                    )
                )

        except Exception:
            continue

    return 0


# ============================================================
# 전체 좌석
# ============================================================

def total_seat_count(
    row,
):

    for key in [
        "stcnt",
        "totalSeatCnt",
        "totalSeats",
        "seatTotalCnt",
        "totSeatCnt",
    ]:

        try:

            value = row.get(
                key
            )

            if value is not None:

                return int(
                    str(value).replace(
                        ",",
                        "",
                    )
                )

        except Exception:
            continue

    return None


# ============================================================
# 시간
# ============================================================

def format_time(
    value,
):

    if value is None:
        return "??:??"

    text = str(
        value
    ).strip()

    # 4자리 HHMM
    if (
        len(text) == 4
        and text.isdigit()
    ):

        return (
            text[:2]
            + ":"
            + text[2:]
        )

    # HH:MM:SS
    if len(text) >= 5:

        if (
            text[2] == ":"
        ):

            return text[:5]

    return text


# ============================================================
# 회차 파싱
# ============================================================

def parse_sessions(
    payload,
    scn_ymd,
    include_sold_out=False,
):

    rows = extract_rows(
        payload
    )

    logger.info(
        "CGV API rows=%d",
        len(rows),
    )

    result = []

    for row in rows:

        if not isinstance(
            row,
            dict,
        ):

            continue

        # ----------------------------------------------------
        # 영화
        # ----------------------------------------------------

        if not target_movie(
            row
        ):

            continue

        # ----------------------------------------------------
        # IMAX
        # ----------------------------------------------------

        if not target_format(
            row
        ):

            continue

        # ----------------------------------------------------
        # 좌석
        # ----------------------------------------------------

        seats = seat_count(
            row
        )

        if (
            not include_sold_out
            and seats <= 0
        ):

            continue

        # ----------------------------------------------------
        # 상영 통제
        # ----------------------------------------------------

        if row.get(
            "cntlYn"
        ) == "Y":

            continue

        item = {
            "date": scn_ymd,

            "movNo": (
                row.get("movNo")
                or row.get("movieNo")
            ),

            "movNm": (
                row.get("movNm")
                or row.get("prodNm")
                or row.get("movieNm")
                or "오디세이"
            ),

            "scnSseq": (
                row.get("scnSseq")
                or row.get("screenSeq")
            ),

            "scnsNo": (
                row.get("scnsNo")
                or row.get("screenNo")
            ),

            "start": (
                row.get("scnsrtTm")
                or row.get("scnStartTm")
                or row.get("startTime")
                or row.get("startTm")
            ),

            "end": (
                row.get("scnendTm")
                or row.get("scnEndTm")
                or row.get("endTime")
                or row.get("endTm")
            ),

            "screen": (
                row.get("expoScnsNm")
                or row.get("scnsNm")
                or row.get("scnsEnm")
                or row.get("screenNm")
                or "IMAX"
            ),

            "seats": seats,

            "totalSeats": total_seat_count(
                row
            ),
        }

        result.append(
            item
        )

    result.sort(
        key=lambda item: (
            str(
                item.get(
                    "start"
                )
                or ""
            ),
            str(
                item.get(
                    "scnSseq"
                )
                or ""
            ),
        )
    )

    return result


# ============================================================
# 회차 고유키
# ============================================================

def session_key(
    item,
):

    values = [
        item.get("date"),
        item.get("movNo"),
        item.get("scnSseq"),
        item.get("scnsNo"),
        item.get("start"),
    ]

    return "|".join(
        str(x)
        for x in values
        if x is not None
    )


# ============================================================
# Telegram 알림
# ============================================================

def notify_session(
    item,
):

    key = session_key(
        item
    )

    now = time.time()

    # 같은 회차는 6시간 동안 재알림 방지
    if key in seen_sessions:

        if (
            now
            - seen_sessions[key]
            < 21600
        ):

            return

    seen_sessions[key] = now

    date = pretty_date(
        item.get("date")
    )

    start = format_time(
        item.get("start")
    )

    end = format_time(
        item.get("end")
    )

    seats = item.get(
        "seats",
        0,
    )

    total = item.get(
        "totalSeats"
    )

    total_text = (
        f" / {total}석"
        if total
        else ""
    )

    message = (
        "🚨 <b>용아맥 예매 오픈 감지!</b>\n\n"
        f"🎬 <b>{item.get('movNm', '오디세이')}</b>\n"
        f"📅 {date}\n"
        f"🏢 {THEATER_NAME}\n"
        "🎞️ IMAX\n"
        f"🕐 {start} ~ {end}\n"
        f"💺 잔여 <b>{seats}석"
        f"{total_text}</b>\n\n"
        "⚡ CGV에서 지금 확인하세요!"
    )

    buttons = [
        [
            {
                "text": f"🎟️ {start} 바로 예매",
                "url": CGV_WEB_URL,
            }
        ]
    ]

    logger.info(
        "🚨 대상 회차 발견: %s %s %s석",
        date,
        start,
        seats,
    )

    send_telegram(
        message,
        buttons,
    )


# ============================================================
# 날짜 하나 검사
# ============================================================

async def check_date(
    scn_ymd,
    include_sold_out=False,
):

    logger.info(
        "=========================================="
    )

    logger.info(
        "🔎 날짜 검사: %s",
        pretty_date(scn_ymd),
    )

    payload = await asyncio.to_thread(
        request_cgv_api,
        scn_ymd,
    )

    if payload is None:

        logger.warning(
            "⚠️ %s API 응답 없음",
            scn_ymd,
        )

        return []

    status = payload.get(
        "statusCode"
    )

    if str(status) not in (
        "0",
        "None",
    ):

        logger.warning(
            "⚠️ CGV API statusCode=%s",
            status,
        )

        logger.warning(
            "statusMessage=%s",
            payload.get(
                "statusMessage"
            ),
        )

    sessions = parse_sessions(
        payload,
        scn_ymd,
        include_sold_out=include_sold_out,
    )

    logger.info(
        "🎯 %s 대상 회차=%d",
        pretty_date(scn_ymd),
        len(sessions),
    )

    if not include_sold_out:

        for item in sessions:

            notify_session(
                item
            )

    return sessions


# ============================================================
# 전체 검사
# ============================================================

async def perform_scan():

    global last_successful_scan

    async with scan_lock:

        dates = [
            make_ymd(i)
            for i in range(
                DAYS_AHEAD + 1
            )
        ]

        logger.info(
            "📅 검사 날짜: %s",
            ", ".join(
                pretty_date(x)
                for x in dates
            ),
        )

        for index, scn_ymd in enumerate(
            dates
        ):

            await check_date(
                scn_ymd
            )

            if (
                index
                < len(dates) - 1
            ):

                await asyncio.sleep(
                    0.5
                )

        last_successful_scan = (
            time.time()
        )


# ============================================================
# /test
# ============================================================

async def perform_test_scan():

    async with scan_lock:

        date = make_ymd(0)

        logger.info(
            "========== /test API 검사 =========="
        )

        return await check_date(
            date,
            include_sold_out=True,
        )


# ============================================================
# Health Server
# ============================================================

async def health_server():

    async def handler(
        reader,
        writer,
    ):

        try:

            raw = await reader.read(
                4096
            )

            request = raw.decode(
                "utf-8",
                errors="ignore",
            )

            first_line = (
                request.splitlines()[0]
                if request
                else ""
            )

            # ------------------------------------------------
            # STATUS
            # ------------------------------------------------

            if first_line.startswith(
                "GET /status"
            ):

                status = (
                    "RUNNING"
                    if monitor_enabled
                    else "STOPPED"
                )

                body = (
                    "🟢 용아맥 API 감시 프로세스 작동 중\n"
                    f"monitor={status}\n"
                    f"api={CGV_API_URL}\n"
                    f"browser=NOT_USED\n"
                    f"last_api_status={last_api_status}\n"
                    f"last_api_response={last_api_response}"
                )

            # ------------------------------------------------
            # TEST
            # ------------------------------------------------

            elif first_line.startswith(
                "GET /test"
            ):

                logger.info(
                    "========== HTTP /test =========="
                )

                try:

                    sessions = (
                        await perform_test_scan()
                    )

                    body = (
                        "🟢 CGV API 테스트 완료\n"
                        f"오늘 대상 IMAX 회차: "
                        f"{len(sessions)}개"
                    )

                except Exception as exc:

                    logger.exception(
                        "HTTP /test 실패"
                    )

                    body = (
                        "🔴 테스트 실패: "
                        + str(exc)
                    )

            else:

                body = (
                    "🟢 yongsan_imax_api_bot running"
                )

            encoded = body.encode(
                "utf-8"
            )

            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/plain; "
                "charset=utf-8\r\n"
                f"Content-Length: {len(encoded)}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode(
                "utf-8"
            ) + encoded

            writer.write(
                response
            )

            await writer.drain()

        except Exception:

            logger.exception(
                "health 오류"
            )

        finally:

            writer.close()

            try:
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(
        handler,
        "0.0.0.0",
        PORT,
    )

    logger.info(
        "🏥 Health server 시작: %d",
        PORT,
    )

    async with server:

        await server.serve_forever()


# ============================================================
# API 준비
# ============================================================

async def prepare_api():

    global http_session

    logger.info(
        "📡 CGV API 방식 준비"
    )

    http_session = create_http_session()

    # CGV 웹을 한 번 방문해서
    # Chrome 세션 / 쿠키 확보
    await asyncio.to_thread(
        warm_up_cgv
    )

    logger.info(
        "📡 API URL: %s",
        CGV_API_URL,
    )

    logger.info(
        "coCd=%s",
        CO_CD,
    )

    logger.info(
        "siteNo=%s",
        SITE_NO,
    )

    logger.info(
        "rtctlScopCd=%s",
        RTCTL_SCOP_CD,
    )

    logger.info(
        "🏢 극장: %s",
        THEATER_NAME,
    )

    logger.info(
        "🎬 영화: %s",
        ", ".join(
            MOVIE_ALIASES
        ),
    )

    logger.info(
        "🎞️ 포맷: %s",
        ", ".join(
            FORMAT_KEYWORDS
        ),
    )


# ============================================================
# Monitor
# ============================================================

async def monitor():

    global monitor_enabled

    logger.info(
        "=========================================="
    )

    logger.info(
        "🎬 용아맥 API 감시 시작"
    )

    logger.info(
        "방식: CGV API 직접 호출"
    )

    logger.info(
        "영화: %s",
        ", ".join(
            MOVIE_ALIASES
        ),
    )

    logger.info(
        "극장: %s",
        THEATER_NAME,
    )

    logger.info(
        "포맷: %s",
        ", ".join(
            FORMAT_KEYWORDS
        ),
    )

    logger.info(
        "검사 간격: %d초",
        INTERVAL_SECONDS,
    )

    logger.info(
        "검사 범위: 오늘 + %d일",
        DAYS_AHEAD,
    )

    logger.info(
        "=========================================="
    )

    await prepare_api()

    send_telegram(
        "🟢 <b>용아맥 API 감시 프로그램 시작</b>\n\n"
        f"🎬 {', '.join(MOVIE_ALIASES)}\n"
        f"🏢 {THEATER_NAME}\n"
        "🎞️ IMAX\n"
        f"📅 오늘 + {DAYS_AHEAD}일\n"
        f"⏱️ {INTERVAL_SECONDS}초 간격\n\n"
        "📡 CGV API 직접 조회 방식\n\n"
        "📱 명령어\n"
        "/start  감시 시작\n"
        "/stop   감시 중지\n"
        "/test   현재 용아맥 현황\n"
        "/status 봇 상태"
    )

    while True:

        if not monitor_enabled:

            await asyncio.sleep(
                1
            )

            continue

        try:

            await perform_scan()

        except Exception as exc:

            logger.exception(
                "🔴 감시 사이클 오류: %s",
                exc,
            )

            await asyncio.sleep(
                10
            )

        await asyncio.sleep(
            INTERVAL_SECONDS
        )


# ============================================================
# 종료
# ============================================================

async def shutdown():

    global http_session

    logger.info(
        "프로그램 종료"
    )

    try:

        if http_session:

            http_session.close()

    except Exception:
        pass


# ============================================================
# Main
# ============================================================

async def main():

    health_task = asyncio.create_task(
        health_server()
    )

    monitor_task = asyncio.create_task(
        monitor()
    )

    telegram_task = asyncio.create_task(
        telegram_command_loop()
    )

    try:

        await asyncio.gather(
            health_task,
            monitor_task,
            telegram_task,
        )

    finally:

        health_task.cancel()
        monitor_task.cancel()
        telegram_task.cancel()

        await shutdown()


# ============================================================
# 실행
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        logger.info(
            "프로그램 종료"
        )

    except Exception:

        logger.exception(
            "치명적 오류"
            )

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta

import requests

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None


# ============================================================
# 기본 설정
# ============================================================

API_URL = "https://api.cgv.co.kr/cnm/atkt/searchMovScnInfo"

THEATER_NAME = os.getenv(
    "THEATER_NAME",
    "용산아이파크몰",
).strip()

SITE_NO = os.getenv(
    "SITE_NO",
    "0013",
).strip()

CO_CD = os.getenv(
    "CO_CD",
    "A420",
).strip()

RTCTL_SCOP_CD = os.getenv(
    "RTCTL_SCOP_CD",
    "08",
).strip()

MOVIE_ALIASES = [
    x.strip()
    for x in os.getenv(
        "MOVIE_ALIASES",
        "오디세이,The Odyssey,ODYSSEY",
    ).split(",")
    if x.strip()
]

FORMAT_KEYWORDS = [
    x.strip().lower()
    for x in os.getenv(
        "FORMAT_KEYWORDS",
        "IMAX,아이맥스",
    ).split(",")
    if x.strip()
]

INTERVAL_SECONDS = max(
    20,
    int(
        os.getenv(
            "INTERVAL_SECONDS",
            "20",
        )
    ),
)

DAYS_AHEAD = max(
    0,
    int(
        os.getenv(
            "DAYS_AHEAD",
            "7",
        )
    ),
)

REQUEST_TIMEOUT = max(
    5,
    int(
        os.getenv(
            "REQUEST_TIMEOUT",
            "30",
        )
    ),
)

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    "",
).strip()

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    "",
).strip()

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
# 전역 상태
# ============================================================

monitor_enabled = True

monitor_lock = asyncio.Lock()

seen_sessions = {}

last_api_response = 0

last_api_status = None

last_api_error = None

http_session = None


# ============================================================
# 날짜
# ============================================================

def make_ymd(offset=0):
    return (
        datetime.now()
        + timedelta(days=offset)
    ).strftime("%Y%m%d")


def pretty_date(ymd):
    if not ymd or len(ymd) != 8:
        return str(ymd)

    return (
        f"{ymd[:4]}-"
        f"{ymd[4:6]}-"
        f"{ymd[6:]}"
    )


# ============================================================
# HTTP 세션 생성
# ============================================================

def create_http_session():

    global http_session

    if curl_requests is not None:

        logger.info(
            "curl_cffi 사용: impersonate=chrome"
        )

        http_session = curl_requests.Session(
            impersonate="chrome",
        )

    else:

        logger.warning(
            "curl_cffi 없음. 일반 requests 사용"
        )

        http_session = requests.Session()

    return http_session


# ============================================================
# 기본 헤더
# ============================================================

def build_headers():

    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive",
        "Referer": "https://cgv.co.kr/",
        "Origin": "https://cgv.co.kr",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/138.0.0.0 Safari/537.36"
        ),
    }


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
# Telegram API
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

    global monitor_enabled

    offset = 0

    if not TELEGRAM_BOT_TOKEN:

        logger.warning(
            "Telegram 명령어 비활성화"
        )

        return

    logger.info(
        "Telegram 명령어 감시 시작"
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
                    "offset": offset,
                    "timeout": 20,
                    "allowed_updates": [
                        "message"
                    ],
                },
            )

            if not result or not result.get("ok"):

                await asyncio.sleep(3)

                continue

            for update in result.get(
                "result",
                [],
            ):

                offset = (
                    update.get(
                        "update_id",
                        0,
                    )
                    + 1
                )

                message = update.get(
                    "message"
                )

                if not message:
                    continue

                chat_id = str(
                    message.get(
                        "chat",
                        {},
                    ).get(
                        "id",
                        "",
                    )
                )

                if (
                    TELEGRAM_CHAT_ID
                    and chat_id != TELEGRAM_CHAT_ID
                ):

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
                # /start
                # ------------------------------------------------

                if text == "/start":

                    monitor_enabled = True

                    send_telegram(
                        "🟢 <b>용아맥 API 감시를 시작했습니다.</b>\n\n"
                        f"🎬 {', '.join(MOVIE_ALIASES)}\n"
                        f"🏢 {THEATER_NAME}\n"
                        "🎞️ IMAX\n"
                        f"⏱️ {INTERVAL_SECONDS}초 간격\n\n"
                        "📡 CGV 시간표 API 직접 조회 방식"
                    )

                # ------------------------------------------------
                # /stop
                # ------------------------------------------------

                elif text == "/stop":

                    monitor_enabled = False

                    send_telegram(
                        "⏸️ <b>용아맥 API 감시를 중지했습니다.</b>\n\n"
                        "봇은 계속 실행 중입니다.\n"
                        "/start 로 다시 시작할 수 있습니다."
                    )

                # ------------------------------------------------
                # /status
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

                        response_text = "아직 없음"

                    send_telegram(
                        "📊 <b>용아맥 API 봇 상태</b>\n\n"
                        f"상태: {monitor_text}\n"
                        f"극장: {THEATER_NAME}\n"
                        f"siteNo: {SITE_NO}\n"
                        f"coCd: {CO_CD}\n"
                        f"rtctlScopCd: {RTCTL_SCOP_CD}\n"
                        f"검사 간격: {INTERVAL_SECONDS}초\n"
                        f"최근 API 응답: {response_text}\n"
                        f"최근 HTTP 상태: {last_api_status or '없음'}"
                    )

                # ------------------------------------------------
                # /test
                # ------------------------------------------------

                elif text == "/test":

                    send_telegram(
                        "🔎 <b>CGV 시간표 API 테스트 중...</b>\n\n"
                        f"📅 {pretty_date(make_ymd(0))}\n"
                        f"🏢 {THEATER_NAME}\n"
                        "🎞️ IMAX"
                    )

                    try:

                        sessions = await scan_date(
                            make_ymd(0),
                            include_sold_out=True,
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
                                "🔎 <b>오늘 용아맥 IMAX 현황</b>\n\n"
                                "조회된 오디세이 IMAX 회차가 없습니다.\n\n"
                                f"HTTP 상태: {last_api_status or '없음'}"
                            )

                    except Exception as exc:

                        logger.exception(
                            "/test 실패"
                        )

                        send_telegram(
                            "🔴 <b>/test 실패</b>\n\n"
                            f"{str(exc)[:700]}"
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

async def call_cgv_api(
    scn_ymd,
):

    global last_api_response
    global last_api_status
    global last_api_error

    if http_session is None:

        create_http_session()

    params = {
        "coCd": CO_CD,
        "siteNo": SITE_NO,
        "scnYmd": scn_ymd,
        "rtctlScopCd": RTCTL_SCOP_CD,
    }

    headers = build_headers()

    logger.info(
        "=========================================="
    )

    logger.info(
        "📡 CGV API 요청"
    )

    logger.info(
        "URL: %s",
        API_URL,
    )

    logger.info(
        "PARAMS: %s",
        params,
    )

    try:

        def request():

            return http_session.get(
                API_URL,
                params=params,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )

        response = await asyncio.to_thread(
            request
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

        # --------------------------------------------------------
        # 401
        # --------------------------------------------------------

        if response.status_code == 401:

            body = response.text[:1500]

            last_api_error = (
                f"HTTP 401: {body}"
            )

            logger.error(
                "🔴 CGV API 401 인증 거부"
            )

            logger.error(
                "응답: %s",
                body,
            )

            return None

        # --------------------------------------------------------
        # 기타 HTTP 오류
        # --------------------------------------------------------

        if response.status_code != 200:

            body = response.text[:1500]

            last_api_error = (
                f"HTTP {response.status_code}: {body}"
            )

            logger.error(
                "🔴 CGV API 오류: HTTP %s",
                response.status_code,
            )

            logger.error(
                "응답: %s",
                body,
            )

            return None

        # --------------------------------------------------------
        # JSON
        # --------------------------------------------------------

        try:

            data = response.json()

        except Exception as exc:

            last_api_error = (
                f"JSON parse error: {exc}"
            )

            logger.error(
                "🔴 JSON 파싱 실패"
            )

            logger.error(
                "응답: %s",
                response.text[:1500],
            )

            return None

        logger.info(
            "✅ CGV JSON 응답 확보"
        )

        logger.info(
            "JSON 최상위 keys: %s",
            list(data.keys())
            if isinstance(data, dict)
            else type(data).__name__,
        )

        return data

    except Exception as exc:

        last_api_error = str(exc)

        logger.exception(
            "🔴 CGV API 요청 실패"
        )

        return None


# ============================================================
# rows 추출
# ============================================================

def extract_rows(payload):

    if not isinstance(
        payload,
        dict,
    ):

        return []

    data = payload.get(
        "data",
        [],
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
            "scnInfo",
            "resultList",
        ]:

            value = data.get(
                key
            )

            if isinstance(
                value,
                list,
            ):

                return value

    # 혹시 data 아래에 또 다른 구조가 있는 경우
    if isinstance(
        data,
        dict,
    ):

        for value in data.values():

            if isinstance(
                value,
                list,
            ):

                if value and isinstance(
                    value[0],
                    dict,
                ):

                    return value

    return []


# ============================================================
# 영화 필터
# ============================================================

def target_movie(row):

    values = [
        row.get("movNm"),
        row.get("movEnm"),
        row.get("prodNm"),
        row.get("expoProdNm"),
        row.get("engProdNm"),
        row.get("movieNm"),
    ]

    text = " ".join(
        str(value)
        for value in values
        if value is not None
    ).lower()

    return any(
        alias.lower() in text
        for alias in MOVIE_ALIASES
    )


# ============================================================
# IMAX 필터
# ============================================================

def target_format(row):

    values = [
        row.get("movkndDsplNm"),
        row.get("movkndDsplEnm"),
        row.get("scnsNm"),
        row.get("expoScnsNm"),
        row.get("scnsEnm"),
        row.get("screenNm"),
        row.get("screenName"),
        row.get("tcscnsGradNm"),
    ]

    text = " ".join(
        str(value)
        for value in values
        if value is not None
    ).lower()

    return any(
        keyword in text
        for keyword in FORMAT_KEYWORDS
    )


# ============================================================
# 좌석
# ============================================================

def seat_count(row):

    for key in [
        "frSeatCnt",
        "remainSeatCnt",
        "availableSeatCnt",
        "seatCnt",
    ]:

        try:

            value = row.get(
                key
            )

            if value is not None:

                return int(value)

        except Exception:

            continue

    return 0


def total_seat_count(row):

    for key in [
        "stcnt",
        "totalSeatCnt",
        "totalSeats",
    ]:

        try:

            value = row.get(
                key
            )

            if value is not None:

                return int(value)

        except Exception:

            continue

    return None


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

        if not target_movie(row):

            continue

        if not target_format(row):

            continue

        seats = seat_count(
            row
        )

        if (
            not include_sold_out
            and seats <= 0
        ):

            continue

        if row.get(
            "cntlYn"
        ) == "Y":

            continue

        result.append(
            {
                "date": scn_ymd,

                "movNo": row.get(
                    "movNo"
                ),

                "movNm": (
                    row.get("movNm")
                    or row.get("prodNm")
                    or row.get("movieNm")
                    or "오디세이"
                ),

                "scnSseq": row.get(
                    "scnSseq"
                ),

                "scnsNo": row.get(
                    "scnsNo"
                ),

                "start": (
                    row.get("scnsrtTm")
                    or row.get("scnStartTm")
                    or row.get("startTime")
                ),

                "end": (
                    row.get("scnendTm")
                    or row.get("scnEndTm")
                    or row.get("endTime")
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
        )

    result.sort(
        key=lambda item: (
            str(
                item.get("start")
                or ""
            ),
            str(
                item.get("scnSseq")
                or ""
            ),
        )
    )

    return result


# ============================================================
# 날짜 하나 검사
# ============================================================

async def scan_date(
    scn_ymd,
    include_sold_out=False,
):

    logger.info(
        "🔎 날짜 검사: %s",
        pretty_date(scn_ymd),
    )

    payload = await call_cgv_api(
        scn_ymd
    )

    if payload is None:

        return []

    sessions = parse_sessions(
        payload,
        scn_ymd,
        include_sold_out=include_sold_out,
    )

    logger.info(
        "%s → 대상 IMAX 회차 %d개",
        pretty_date(scn_ymd),
        len(sessions),
    )

    if not include_sold_out:

        for session in sessions:

            notify_session(
                session
            )

    return sessions


# ============================================================
# 시간 표시
# ============================================================

def format_time(value):

    if value is None:

        return "??:??"

    text = str(
        value
    )

    if (
        len(text) == 4
        and text.isdigit()
    ):

        return (
            text[:2]
            + ":"
            + text[2:]
        )

    if (
        len(text) >= 12
        and text.isdigit()
    ):

        # YYYYMMDDHHMM 형태 대응
        return (
            text[-4:-2]
            + ":"
            + text[-2:]
        )

    return text


# ============================================================
# Telegram 알림
# ============================================================

def notify_session(item):

    key = "|".join(
        str(value)
        for value in [
            item.get("date"),
            item.get("movNo"),
            item.get("scnSseq"),
            item.get("scnsNo"),
            item.get("start"),
        ]
        if value is not None
    )

    now = time.time()

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
        "⚡ 지금 CGV에서 확인하세요!"
    )

    buttons = [
        [
            {
                "text": f"🎟️ {start} 바로 예매",
                "url": (
                    "https://cgv.co.kr/"
                    "cnm/movieBook/cinema"
                    "?siteNm=CGV%EC%9A%A9%EC%82%B0"
                    "&siteNo=0013"
                ),
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
# 전체 감시
# ============================================================

async def perform_scan():

    async with monitor_lock:

        dates = [
            make_ymd(i)
            for i in range(
                DAYS_AHEAD + 1
            )
        ]

        logger.info(
            "=========================================="
        )

        logger.info(
            "📡 API 감시 날짜: %s",
            ", ".join(
                dates
            ),
        )

        for index, date in enumerate(
            dates
        ):

            try:

                await scan_date(
                    date,
                    include_sold_out=False,
                )

            except Exception as exc:

                logger.exception(
                    "%s 검사 실패: %s",
                    date,
                    exc,
                )

            if (
                index
                < len(dates) - 1
            ):

                await asyncio.sleep(1)


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

            if first_line.startswith(
                "GET /status"
            ):

                status = (
                    "RUNNING"
                    if monitor_enabled
                    else "STOPPED"
                )

                body = (
                    "🟢 용아맥 API 감시 작동 중\n"
                    f"monitor={status}\n"
                    f"api_status={last_api_status}\n"
                    f"last_response={last_api_response}\n"
                )

            elif first_line.startswith(
                "GET /test"
            ):

                try:

                    sessions = await scan_date(
                        make_ymd(0),
                        include_sold_out=True,
                    )

                    body = (
                        "🟢 CGV API 테스트 완료\n"
                        f"오늘 IMAX 회차: "
                        f"{len(sessions)}개\n"
                        f"HTTP: {last_api_status}"
                    )

                except Exception as exc:

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
        "Health server 시작: %d",
        PORT,
    )

    async with server:

        await server.serve_forever()


# ============================================================
# 메인 감시
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
        "siteNo: %s",
        SITE_NO,
    )

    logger.info(
        "coCd: %s",
        CO_CD,
    )

    logger.info(
        "rtctlScopCd: %s",
        RTCTL_SCOP_CD,
    )

    logger.info(
        "포맷: %s",
        ", ".join(
            FORMAT_KEYWORDS
        ),
    )

    logger.info(
        "간격: %d초",
        INTERVAL_SECONDS,
    )

    logger.info(
        "=========================================="
    )

    create_http_session()

    send_telegram(
        "🟢 <b>용아맥 API 감시 프로그램 시작</b>\n\n"
        f"🎬 {', '.join(MOVIE_ALIASES)}\n"
        f"🏢 {THEATER_NAME}\n"
        f"📍 siteNo={SITE_NO}\n"
        "🎞️ IMAX\n"
        "📡 CGV 시간표 API 직접 조회\n\n"
        "📱 Telegram 명령어\n"
        "/start  감시 시작\n"
        "/stop   감시 중지\n"
        "/test   오늘 시간표 테스트\n"
        "/status 봇 상태"
    )

    while True:

        if not monitor_enabled:

            await asyncio.sleep(1)

            continue

        try:

            await perform_scan()

        except Exception as exc:

            logger.exception(
                "감시 사이클 오류: %s",
                exc,
            )

            await asyncio.sleep(30)

        await asyncio.sleep(
            INTERVAL_SECONDS
        )


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

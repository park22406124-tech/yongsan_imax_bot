import asyncio
import logging
import os
import time
from datetime import datetime, timedelta

import requests
from playwright.async_api import async_playwright


# ============================================================
# 설정
# ============================================================

CGV_BOOKING_URL = "https://cgv.co.kr/cnm/movieBook/cinema"

THEATER_NAME = os.getenv(
    "THEATER_NAME",
    "용산아이파크몰",
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

REQUEST_WAIT_SECONDS = max(
    5,
    int(
        os.getenv(
            "REQUEST_WAIT_SECONDS",
            "20",
        )
    ),
)

BROWSER_HEADLESS = (
    os.getenv(
        "BROWSER_HEADLESS",
        "true",
    ).lower()
    in ("1", "true", "yes", "y")
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
    "yongsan-imax-bot"
)


# ============================================================
# 전역
# ============================================================

playwright = None
browser = None
context = None
page = None

monitor_lock = asyncio.Lock()

seen_sessions = {}

last_schedule_response = 0

# Telegram으로 감시 ON/OFF
monitor_enabled = True

# Telegram getUpdates offset
telegram_update_offset = 0


# ============================================================
# 날짜
# ============================================================

def make_ymd(offset):
    return (
        datetime.now()
        + timedelta(days=offset)
    ).strftime("%Y%m%d")


def pretty_date(ymd):
    if len(ymd) != 8:
        return ymd

    return (
        f"{ymd[:4]}-"
        f"{ymd[4:6]}-"
        f"{ymd[6:]}"
    )


# ============================================================
# Telegram 메시지 전송
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
                response.text[:300],
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
                response.text[:300],
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
            "Telegram 명령어 기능 비활성화: "
            "TELEGRAM_BOT_TOKEN 없음"
        )
        return

    logger.info(
        "Telegram 명령어 감시 시작"
    )

    # --------------------------------------------------------
    # Webhook이 남아 있으면 getUpdates가 작동하지 않을 수 있으므로
    # 시작할 때 webhook 제거
    # --------------------------------------------------------

    try:
        await telegram_api(
            "deleteWebhook",
            {
                "drop_pending_updates": False,
            },
        )
    except Exception:
        pass

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

                # 지정한 채팅방만 허용
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
                # /start
                # ------------------------------------------------

                if text == "/start":

                    monitor_enabled = True

                    logger.info(
                        "▶️ Telegram /start"
                    )

                    send_telegram(
                        "🟢 <b>용아맥 감시를 시작했습니다.</b>\n\n"
                        f"🎬 {', '.join(MOVIE_ALIASES)}\n"
                        f"🏢 {THEATER_NAME}\n"
                        "🎞️ IMAX\n"
                        f"⏱️ {INTERVAL_SECONDS}초 간격"
                    )

                # ------------------------------------------------
                # /stop
                # ------------------------------------------------

                elif text == "/stop":

                    monitor_enabled = False

                    logger.info(
                        "⏸️ Telegram /stop"
                    )

                    send_telegram(
                        "⏸️ <b>용아맥 감시를 중지했습니다.</b>\n\n"
                        "브라우저와 봇은 계속 실행 중입니다.\n"
                        "다시 감시하려면 /start 를 입력하세요."
                    )

                # ------------------------------------------------
                # /status
                # ------------------------------------------------

                elif text in (
                    "/status",
                    "/상태",
                ):

                    if monitor_enabled:
                        status_text = "🟢 감시 중"
                    else:
                        status_text = "⏸️ 감시 중지"

                    browser_text = (
                        "🟢 정상"
                        if page
                        else "🔴 없음"
                    )

                    if last_schedule_response:
                        elapsed = int(
                            time.time()
                            - last_schedule_response
                        )

                        response_text = (
                            f"{elapsed}초 전"
                        )
                    else:
                        response_text = (
                            "아직 없음"
                        )

                    send_telegram(
                        "📊 <b>용아맥 봇 상태</b>\n\n"
                        f"상태: {status_text}\n"
                        f"브라우저: {browser_text}\n"
                        f"극장: {THEATER_NAME}\n"
                        "🎞️ IMAX\n"
                        f"검사 간격: {INTERVAL_SECONDS}초\n"
                        f"최근 시간표 응답: {response_text}"
                    )

                # ------------------------------------------------
                # /test
                # ------------------------------------------------

                elif text == "/test":

                    logger.info(
                        "🔎 Telegram /test 실행"
                    )

                    send_telegram(
                        "🔎 <b>현재 용아맥 현황을 확인하는 중...</b>\n\n"
                        "CGV에서 오늘 시간표를 확인합니다."
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

                                if seats > 0:
                                    seat_icon = "🟢"
                                else:
                                    seat_icon = "⚪"

                                lines.append(
                                    f"{seat_icon} "
                                    f"<b>{start} ~ {end}</b>  "
                                    f"💺 {seat_text}"
                                )

                            send_telegram(
                                "\n".join(lines)
                            )

                        else:

                            send_telegram(
                                "🔎 <b>오늘 용아맥 IMAX 현황</b>\n\n"
                                "현재 확인된 오디세이 IMAX 회차가 없습니다.\n\n"
                                "⚠️ CGV가 시간표 응답을 정상적으로 "
                                "보내지 않은 경우에도 이렇게 표시될 수 있습니다."
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
                "Telegram 명령어 루프 오류: %s",
                exc,
            )

            await asyncio.sleep(5)


# ============================================================
# 브라우저
# ============================================================

async def start_browser():
    global playwright
    global browser
    global context
    global page

    logger.info(
        "Chromium 시작"
    )

    playwright = (
        await async_playwright()
        .start()
    )

    browser = await playwright.chromium.launch(
        headless=BROWSER_HEADLESS,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
        ],
    )

    context = await browser.new_context(
        locale="ko-KR",
        timezone_id="Asia/Seoul",
        viewport={
            "width": 1365,
            "height": 900,
        },
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/138.0.0.0 Safari/537.36"
        ),
    )

    await context.add_init_script(
        """
        Object.defineProperty(
            navigator,
            'webdriver',
            {
                get: () => undefined
            }
        );
        """
    )

    page = await context.new_page()

    # --------------------------------------------------------
    # Request 감시
    # --------------------------------------------------------

    def on_request(request):

        try:

            url = request.url

            if (
                "searchMovScnInfo" in url
                or "searchMov" in url
                or "ScnInfo" in url
            ):

                logger.info(
                    "🌐 CGV 관련 REQUEST: %s %s",
                    request.method,
                    url,
                )

        except Exception:
            pass

    # --------------------------------------------------------
    # Response 감시
    # --------------------------------------------------------

    async def on_response(response):

        global last_schedule_response

        try:

            url = response.url

            if (
                "searchMovScnInfo" not in url
                and "searchMov" not in url
                and "ScnInfo" not in url
            ):
                return

            logger.info(
                "🌐 CGV 관련 RESPONSE: HTTP %s %s",
                response.status,
                url,
            )

            try:
                data = await response.json()
            except Exception:
                return

            if not isinstance(
                data,
                dict,
            ):
                return

            if (
                "data" in data
                or "statusCode" in data
                or "statusMessage" in data
            ):

                last_schedule_response = (
                    time.time()
                )

                logger.info(
                    "🎯 CGV 시간표 JSON 응답 확보"
                )

                try:

                    page._latest_cgv_schedule = {
                        "url": url,
                        "status": response.status,
                        "data": data,
                    }

                except Exception:
                    pass

        except Exception as exc:

            logger.debug(
                "response 처리 오류: %s",
                exc,
            )

    page.on(
        "request",
        on_request,
    )

    page.on(
        "response",
        on_response,
    )

    logger.info(
        "CGV 접속"
    )

    try:

        await page.goto(
            CGV_BOOKING_URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )

    except Exception as exc:

        logger.warning(
            "CGV 이동 예외: %s",
            exc,
        )

    await asyncio.sleep(4)

    logger.info(
        "현재 페이지: %s",
        page.url,
    )


# ============================================================
# 극장 선택창
# ============================================================

async def open_theater_picker():

    selectors = [
        'input[placeholder*="극장명"]',
        'input[placeholder*="극장을"]',
        'input[placeholder*="극장"]',
        'button:has-text("극장")',
        '[aria-label*="극장"]',
        '[aria-label*="극장 선택"]',
    ]

    for selector in selectors:

        try:

            locator = page.locator(
                selector
            ).first

            if await locator.is_visible(
                timeout=1200
            ):

                await locator.click(
                    timeout=3000
                )

                await asyncio.sleep(1)

                logger.info(
                    "극장 선택창 열림: %s",
                    selector,
                )

                return True

        except Exception:
            continue

    return False


# ============================================================
# 극장 선택
# ============================================================

async def select_theater():

    logger.info(
        "🏢 CGV 극장 선택 시도: %s",
        THEATER_NAME,
    )

    await open_theater_picker()

    await asyncio.sleep(1)

    search_input = None

    selectors = [
        'input[placeholder*="극장명"]',
        'input[placeholder*="극장을"]',
        'input[placeholder*="극장"]',
        'input[type="search"]',
    ]

    for selector in selectors:

        try:

            locator = page.locator(
                selector
            ).first

            if await locator.is_visible(
                timeout=1500
            ):

                search_input = locator
                break

        except Exception:
            continue

    if search_input:

        try:

            await search_input.fill(
                THEATER_NAME
            )

            await asyncio.sleep(1)

        except Exception:
            pass

    try:

        result = page.get_by_text(
            THEATER_NAME,
            exact=True,
        ).last

        if await result.is_visible(
            timeout=5000
        ):

            await result.click(
                timeout=5000
            )

            await asyncio.sleep(2)

            logger.info(
                "✅ %s 선택 완료",
                THEATER_NAME,
            )

            return True

    except Exception:
        pass

    try:

        result = page.get_by_text(
            THEATER_NAME,
            exact=False,
        ).last

        if await result.is_visible(
            timeout=5000
        ):

            await result.click(
                timeout=5000
            )

            await asyncio.sleep(2)

            logger.info(
                "✅ %s 선택 완료",
                THEATER_NAME,
            )

            return True

    except Exception:
        pass

    logger.warning(
        "⚠️ 극장 선택 실패: %s",
        THEATER_NAME,
    )

    return False


# ============================================================
# 날짜 선택
# ============================================================

async def select_date(
    target_ymd,
):

    target = datetime.strptime(
        target_ymd,
        "%Y%m%d",
    )

    day = str(
        target.day
    )

    weekday = [
        "월",
        "화",
        "수",
        "목",
        "금",
        "토",
        "일",
    ][target.weekday()]

    logger.info(
        "📅 날짜 선택 시도: %s (%s)",
        pretty_date(target_ymd),
        weekday,
    )

    patterns = [
        target.strftime("%Y-%m-%d"),
        target.strftime("%Y.%m.%d"),
        target.strftime("%m/%d"),
        target.strftime("%m.%d"),
        f"{target.month}월 {target.day}일",
        f"{target.month}월{target.day}일",
    ]

    for pattern in patterns:

        for selector in [
            f'[aria-label*="{pattern}"]',
            f'[title*="{pattern}"]',
            f'button:has-text("{pattern}")',
        ]:

            try:

                locator = page.locator(
                    selector
                ).first

                if await locator.is_visible(
                    timeout=700
                ):

                    await locator.click(
                        timeout=3000
                    )

                    await asyncio.sleep(1)

                    logger.info(
                        "✅ 날짜 클릭: %s",
                        pattern,
                    )

                    return True

            except Exception:
                continue

    # --------------------------------------------------------
    # 버튼 기반 탐색
    # --------------------------------------------------------

    try:

        buttons = await page.locator(
            "button"
        ).all()

        for button in buttons:

            try:

                if not await button.is_visible(
                    timeout=100
                ):
                    continue

                text = (
                    await button.inner_text()
                ).strip()

                normalized = (
                    " ".join(
                        text.split()
                    )
                )

                if not normalized:
                    continue

                if day not in normalized:
                    continue

                if (
                    weekday in normalized
                    or len(normalized) <= 12
                ):

                    await button.click(
                        timeout=3000
                    )

                    await asyncio.sleep(1)

                    logger.info(
                        "✅ 날짜 버튼 클릭: %s",
                        normalized,
                    )

                    return True

            except Exception:
                continue

    except Exception:
        pass

    logger.warning(
        "⚠️ 날짜 클릭 실패: %s",
        target_ymd,
    )

    return False


# ============================================================
# 최신 응답
# ============================================================

async def get_latest_schedule():

    try:

        value = getattr(
            page,
            "_latest_cgv_schedule",
            None,
        )

        if value:
            return value

    except Exception:
        pass

    return None


# ============================================================
# 날짜 검사
# ============================================================

async def check_date(
    scn_ymd,
):

    logger.info(
        "=========================================="
    )

    logger.info(
        "🔎 날짜 검사: %s",
        pretty_date(scn_ymd),
    )

    try:
        page._latest_cgv_schedule = None
    except Exception:
        pass

    clicked = await select_date(
        scn_ymd
    )

    if not clicked:
        return []

    logger.info(
        "⏳ CGV 시간표 응답 대기..."
    )

    deadline = (
        time.time()
        + REQUEST_WAIT_SECONDS
    )

    while time.time() < deadline:

        result = await get_latest_schedule()

        if result:

            logger.info(
                "🎯 시간표 응답 확보: %s",
                result["url"],
            )

            return await process_response(
                result["data"],
                scn_ymd,
            )

        await asyncio.sleep(
            0.5
        )

    logger.warning(
        "⚠️ %s: CGV 시간표 응답 없음",
        scn_ymd,
    )

    logger.warning(
        "현재 페이지: %s",
        page.url,
    )

    return []


# ============================================================
# 응답에서 rows 추출
# ============================================================

def extract_rows(payload):

    if not isinstance(
        payload,
        dict,
    ):
        return []

    status = payload.get(
        "statusCode"
    )

    if status not in (
        None,
        0,
        "0",
    ):

        logger.warning(
            "CGV statusCode=%s / %s",
            status,
            payload.get(
                "statusMessage",
                "",
            ),
        )

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
        "CGV 응답 rows=%d",
        len(rows),
    )

    result = []

    for row in rows:

        if not target_movie(row):
            continue

        if not target_format(row):
            continue

        seats = seat_count(
            row
        )

        # 일반 감시에서는 잔여석 있는 회차만
        # /test에서는 매진 회차도 표시
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

    # 시간순 정렬
    result.sort(
        key=lambda x: (
            str(x.get("start") or ""),
            str(x.get("scnSseq") or ""),
        )
    )

    return result


# ============================================================
# 응답 처리
# ============================================================

async def process_response(
    payload,
    scn_ymd,
):

    sessions = parse_sessions(
        payload,
        scn_ymd,
        include_sold_out=False,
    )

    logger.info(
        "%s: 대상 IMAX + 잔여석 %d개",
        scn_ymd,
        len(sessions),
    )

    for item in sessions:
        notify_session(
            item
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

    return text


# ============================================================
# 예매 오픈 알림
# ============================================================

def notify_session(item):

    key = "|".join(
        str(x)
        for x in [
            item.get("date"),
            item.get("movNo"),
            item.get("scnSseq"),
            item.get("scnsNo"),
            item.get("start"),
        ]
        if x is not None
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
        item["date"]
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
        f"🎬 <b>{item['movNm']}</b>\n"
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
                "url": CGV_BOOKING_URL,
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

async def perform_scan(
    test_mode=False,
):

    async with monitor_lock:

        dates = [
            make_ymd(i)
            for i in range(
                DAYS_AHEAD + 1
            )
        ]

        if test_mode:

            dates = [
                make_ymd(0)
            ]

        logger.info(
            "검사 날짜: %s",
            ", ".join(
                dates
            ),
        )

        for index, date in enumerate(
            dates
        ):

            await check_date(
                date
            )

            if (
                index
                < len(dates) - 1
            ):

                await asyncio.sleep(
                    1
                )


# ============================================================
# Telegram /test
# ============================================================

async def perform_test_scan():

    async with monitor_lock:

        date = make_ymd(0)

        logger.info(
            "========== /test 날짜 검사: %s ==========",
            pretty_date(date),
        )

        try:

            page._latest_cgv_schedule = None

        except Exception:
            pass

        clicked = await select_date(
            date
        )

        if not clicked:

            raise RuntimeError(
                f"날짜 선택 실패: {date}"
            )

        logger.info(
            "⏳ /test 시간표 응답 대기..."
        )

        deadline = (
            time.time()
            + REQUEST_WAIT_SECONDS
        )

        while time.time() < deadline:

            result = await get_latest_schedule()

            if result:

                logger.info(
                    "🎯 /test 시간표 응답 확보"
                )

                return parse_sessions(
                    result["data"],
                    date,
                    include_sold_out=True,
                )

            await asyncio.sleep(
                0.5
            )

        raise RuntimeError(
            "CGV 시간표 응답을 받지 못했습니다."
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
            # /status
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
                    "🟢 용아맥 감시 프로세스 작동 중\n"
                    f"monitor={status}\n"
                    f"browser={'OK' if page else 'NO'}\n"
                    f"last_response="
                    f"{last_schedule_response}"
                )

            # ------------------------------------------------
            # 기존 HTTP /test
            # ------------------------------------------------

            elif first_line.startswith(
                "GET /test"
            ):

                logger.info(
                    "========== HTTP /test 시작 =========="
                )

                try:

                    sessions = (
                        await perform_test_scan()
                    )

                    body = (
                        "🟢 CGV 테스트 완료\n"
                        f"오늘 IMAX 회차: "
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
                    "🟢 yongsan_imax_bot running"
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
# CGV 준비
# ============================================================

async def prepare_cgv():

    logger.info(
        "CGV 예매 페이지 준비"
    )

    logger.info(
        "페이지 제목: %s",
        await page.title(),
    )

    logger.info(
        "페이지 URL: %s",
        page.url,
    )

    selected = await select_theater()

    if not selected:

        raise RuntimeError(
            "THEATER_SELECTION_FAILED"
        )

    await asyncio.sleep(2)

    logger.info(
        "✅ CGV 예매 화면 준비 완료"
    )


# ============================================================
# 메인 감시
# ============================================================

async def monitor():

    global monitor_enabled

    logger.info(
        "=========================================="
    )

    logger.info(
        "🎬 용아맥 감시 시작"
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
        "간격: %d초",
        INTERVAL_SECONDS,
    )

    logger.info(
        "=========================================="
    )

    await start_browser()

    await prepare_cgv()

    send_telegram(
        "🟢 <b>용아맥 감시 프로그램 시작</b>\n\n"
        f"🎬 {', '.join(MOVIE_ALIASES)}\n"
        f"🏢 {THEATER_NAME}\n"
        "🎞️ IMAX\n\n"
        "📱 Telegram 명령어\n"
        "/start  감시 시작\n"
        "/stop   감시 중지\n"
        "/test   현재 용아맥 현황\n"
        "/status 봇 상태"
    )

    while True:

        # ----------------------------------------------------
        # /stop 상태
        # ----------------------------------------------------

        if not monitor_enabled:

            await asyncio.sleep(
                1
            )

            continue

        # ----------------------------------------------------
        # 감시
        # ----------------------------------------------------

        try:

            await perform_scan()

        except Exception as exc:

            logger.exception(
                "감시 사이클 오류: %s",
                exc,
            )

            await asyncio.sleep(
                30
            )

        await asyncio.sleep(
            INTERVAL_SECONDS
        )


# ============================================================
# 종료
# ============================================================

async def shutdown():

    global browser
    global playwright

    logger.info(
        "프로그램 종료 처리"
    )

    try:

        if browser:

            await browser.close()

    except Exception:
        pass

    try:

        if playwright:

            await playwright.stop()

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

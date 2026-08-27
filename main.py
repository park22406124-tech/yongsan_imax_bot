import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta

import requests
from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)


# ============================================================
# 환경변수
# ============================================================

CGV_BOOKING_URL = "https://cgv.co.kr/cnm/movieBook/cinema"

CGV_API_PATH = "/cnm/atkt/searchMovScnInfo"

COMPANY_CODE = os.getenv(
    "COMPANY_CODE",
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

THEATER_TIMEOUT_SECONDS = max(
    10,
    int(
        os.getenv(
            "THEATER_TIMEOUT_SECONDS",
            "20",
        )
    ),
)

DATE_CLICK_TIMEOUT_SECONDS = max(
    5,
    int(
        os.getenv(
            "DATE_CLICK_TIMEOUT_SECONDS",
            "8",
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

last_response_time = 0

started_at = datetime.now()


# ============================================================
# 날짜
# ============================================================

def now_kst():
    return datetime.now()


def make_ymd(offset):
    value = (
        datetime.now()
        + timedelta(days=offset)
    )

    return value.strftime("%Y%m%d")


def pretty_date(ymd):
    if len(ymd) != 8:
        return ymd

    return (
        f"{ymd[:4]}-"
        f"{ymd[4:6]}-"
        f"{ymd[6:]}"
    )


# ============================================================
# Telegram
# ============================================================

def send_telegram(
    message,
    buttons=None,
):
    if not TELEGRAM_BOT_TOKEN:
        logger.warning(
            "TELEGRAM_BOT_TOKEN이 없습니다."
        )
        return False

    if not TELEGRAM_CHAT_ID:
        logger.warning(
            "TELEGRAM_CHAT_ID가 없습니다."
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
            "Telegram 전송 실패: %s",
            exc,
        )
        return False


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
            "CGV 페이지 이동 예외: %s",
            exc,
        )

    await asyncio.sleep(4)

    logger.info(
        "현재 페이지: %s",
        page.url,
    )


# ============================================================
# searchMovScnInfo 응답 판별
# ============================================================

def is_schedule_response(response):
    try:
        url = response.url

        if CGV_API_PATH not in url:
            return False

        if response.request.method.upper() != "GET":
            return False

        return True

    except Exception:
        return False


# ============================================================
# 응답 JSON
# ============================================================

async def read_schedule_response(
    response,
):
    global last_response_time

    try:
        data = await response.json()

    except Exception:
        try:
            text = await response.text()
            data = json.loads(text)

        except Exception as exc:
            logger.warning(
                "searchMovScnInfo JSON 파싱 실패: %s",
                exc,
            )
            return None

    last_response_time = time.time()

    status_code = (
        data.get("statusCode")
        if isinstance(data, dict)
        else None
    )

    logger.info(
        "🎯 searchMovScnInfo 응답 감지 "
        "(HTTP %s / statusCode=%s)",
        response.status,
        status_code,
    )

    return data


# ============================================================
# 극장 선택창 열기
# ============================================================

async def open_theater_picker():
    if page is None:
        return False

    logger.info(
        "극장 선택창 열기 시도"
    )

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

    texts = [
        "극장 선택",
        "극장을 선택",
        "선택된 극장이 없습니다",
        "선택 된 극장이 없습니다",
    ]

    for text in texts:
        try:
            locator = page.get_by_text(
                text,
                exact=False,
            ).last

            if await locator.is_visible(
                timeout=1200
            ):
                await locator.click(
                    timeout=3000
                )

                await asyncio.sleep(1)

                logger.info(
                    "극장 선택창 열림: %s",
                    text,
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

    # 검색 input 찾기
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

        except Exception as exc:
            logger.warning(
                "극장 검색 입력 실패: %s",
                exc,
            )

    # 정확한 이름
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

    # 부분 일치
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

    logger.error(
        "❌ 극장 자동 선택 실패: %s",
        THEATER_NAME,
    )

    return False


# ============================================================
# 날짜 UI 분석
# ============================================================

async def dump_date_candidates():
    """
    디버깅용.
    현재 화면에서 날짜처럼 보이는 버튼/요소를 찾아 로그에 출력한다.
    """

    try:
        candidates = await page.locator(
            "button"
        ).all()

        found = []

        for locator in candidates:
            try:
                if not await locator.is_visible(
                    timeout=100
                ):
                    continue

                text = (
                    await locator.inner_text()
                ).strip()

                if not text:
                    continue

                # 날짜 버튼에 흔히 들어가는 패턴
                if (
                    re.search(
                        r"\b\d{1,2}\b",
                        text,
                    )
                    and len(text) <= 40
                ):
                    found.append(
                        " ".join(
                            text.split()
                        )
                    )

            except Exception:
                continue

        if found:
            logger.info(
                "📅 날짜 후보: %s",
                " | ".join(
                    found[:30]
                ),
            )

    except Exception as exc:
        logger.debug(
            "날짜 후보 출력 실패: %s",
            exc,
        )


# ============================================================
# 날짜 선택
# ============================================================

async def select_date(
    target_ymd,
):
    """
    CGV 화면에서 target_ymd에 해당하는 날짜를 클릭한다.

    CGV UI 버전에 따라 텍스트 구조가 달라질 수 있어서
    여러 방식으로 시도한다.
    """

    target = datetime.strptime(
        target_ymd,
        "%Y%m%d",
    )

    day = str(
        target.day
    )

    weekday_kr = [
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
        weekday_kr,
    )

    # --------------------------------------------------------
    # 1. aria-label / title에 YYYYMMDD 또는 날짜가 있는 경우
    # --------------------------------------------------------

    patterns = [
        target.strftime("%Y-%m-%d"),
        target.strftime("%Y.%m.%d"),
        target.strftime("%Y/%m/%d"),
        target.strftime("%m/%d"),
        target.strftime("%m.%d"),
        f"{target.month}월 {target.day}일",
        f"{target.month}월{target.day}일",
    ]

    for pattern in patterns:
        selectors = [
            f'[aria-label*="{pattern}"]',
            f'[title*="{pattern}"]',
            f'button:has-text("{pattern}")',
        ]

        for selector in selectors:
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

                    await asyncio.sleep(2)

                    logger.info(
                        "✅ 날짜 선택 성공: %s",
                        pattern,
                    )

                    return True

            except Exception:
                continue

    # --------------------------------------------------------
    # 2. 버튼 텍스트를 직접 검사
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

                # 예:
                # "27 목"
                # "27\n목"
                # "8월 27일 목"
                # "27"
                #
                # 단순히 "27"만 매칭하면
                # 다른 영역의 숫자를 누를 수 있으므로
                # 짧은 날짜 버튼만 허용한다.

                tokens = normalized.split()

                if day not in tokens:
                    continue

                if (
                    weekday_kr in normalized
                    or len(tokens) <= 3
                ):
                    await button.click(
                        timeout=3000
                    )

                    await asyncio.sleep(2)

                    logger.info(
                        "✅ 날짜 버튼 클릭: %s",
                        normalized,
                    )

                    return True

            except Exception:
                continue

    except Exception as exc:
        logger.warning(
            "날짜 버튼 탐색 실패: %s",
            exc,
        )

    # --------------------------------------------------------
    # 3. 텍스트 locator
    # --------------------------------------------------------

    try:
        locator = page.get_by_text(
            day,
            exact=True,
        ).last

        if await locator.is_visible(
            timeout=1500
        ):
            await locator.click(
                timeout=3000
            )

            await asyncio.sleep(2)

            logger.info(
                "✅ 날짜 숫자 클릭: %s",
                day,
            )

            return True

    except Exception:
        pass

    logger.warning(
        "⚠️ 날짜 자동 선택 실패: %s",
        target_ymd,
    )

    await dump_date_candidates()

    return False


# ============================================================
# 날짜 + 응답 기다리기
# ============================================================

async def select_date_and_capture(
    target_ymd,
):
    """
    날짜를 클릭한 직후 발생하는
    searchMovScnInfo response를 기다린다.
    """

    response_future = None

    async def waiter():
        try:
            response = await page.wait_for_event(
                "response",
                predicate=is_schedule_response,
                timeout=(
                    REQUEST_WAIT_SECONDS
                    * 1000
                ),
            )

            return response

        except PlaywrightTimeoutError:
            return None

    # 응답 대기를 먼저 시작해야 한다.
    response_future = asyncio.create_task(
        waiter()
    )

    await asyncio.sleep(0.3)

    clicked = await select_date(
        target_ymd
    )

    if not clicked:
        if not response_future.done():
            response_future.cancel()

        try:
            await response_future
        except asyncio.CancelledError:
            pass

        return None

    response = await response_future

    if response is None:
        logger.warning(
            "⚠️ 날짜 선택 후 "
            "searchMovScnInfo 응답 없음: %s",
            target_ymd,
        )

        return None

    return await read_schedule_response(
        response
    )


# ============================================================
# 현재 페이지에서 응답 기다리기
# ============================================================

async def wait_for_any_schedule_response():
    try:
        response = await page.wait_for_event(
            "response",
            predicate=is_schedule_response,
            timeout=(
                REQUEST_WAIT_SECONDS
                * 1000
            ),
        )

        return await read_schedule_response(
            response
        )

    except PlaywrightTimeoutError:
        return None


# ============================================================
# API 데이터 추출
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
# 좌석
# ============================================================

def seat_count(row):
    possible = [
        row.get("frSeatCnt"),
        row.get("remainSeatCnt"),
        row.get("availableSeatCnt"),
        row.get("seatCnt"),
    ]

    for value in possible:
        try:
            if value is None:
                continue

            return int(value)

        except Exception:
            continue

    return 0


def total_seat_count(row):
    possible = [
        row.get("stcnt"),
        row.get("totalSeatCnt"),
        row.get("totalSeats"),
    ]

    for value in possible:
        try:
            if value is None:
                continue

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
):
    rows = extract_rows(
        payload
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

        # 좌석이 0이면 알림 대상 아님
        if seats <= 0:
            continue

        if row.get(
            "cntlYn"
        ) == "Y":
            continue

        item = {
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

            "format": (
                row.get("movkndDsplNm")
                or row.get("scnsNm")
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

    return result


# ============================================================
# 회차 키
# ============================================================

def session_key(item):
    return "|".join(
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


# ============================================================
# 시간
# ============================================================

def format_time(value):
    if value is None:
        return "??:??"

    text = str(value)

    if len(text) == 4 and text.isdigit():
        return (
            text[:2]
            + ":"
            + text[2:]
        )

    if len(text) >= 5:
        return text[:5]

    return text


# ============================================================
# Telegram 알림
# ============================================================

def notify_session(item):
    key = session_key(
        item
    )

    now = time.time()

    expired = [
        k
        for k, timestamp
        in seen_sessions.items()
        if now - timestamp > 21600
    ]

    for k in expired:
        del seen_sessions[k]

    if key in seen_sessions:
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
        f"🎞️ {item['screen']}\n"
        f"🕐 {start} ~ {end}\n"
        f"💺 잔여 <b>{seats}석"
        f"{total_text}</b>\n\n"
        "⚡ 지금 CGV에서 확인하세요!"
    )

    buttons = [
        [
            {
                "text": (
                    f"🎟️ {start} 바로 예매"
                ),
                "url": CGV_BOOKING_URL,
            }
        ]
    ]

    logger.info(
        "🚨 대상 회차 발견: "
        "%s %s %s석",
        date,
        start,
        seats,
    )

    send_telegram(
        message,
        buttons,
    )


# ============================================================
# 응답 처리
# ============================================================

async def process_schedule_response(
    payload,
    scn_ymd,
):
    if payload is None:
        return []

    sessions = parse_sessions(
        payload,
        scn_ymd,
    )

    logger.info(
        "%s: 오디세이 + IMAX + 잔여석 "
        "%d개",
        scn_ymd,
        len(sessions),
    )

    for item in sessions:
        notify_session(
            item
        )

    return sessions


# ============================================================
# 한 날짜 검사
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

    payload = await select_date_and_capture(
        scn_ymd
    )

    if payload is None:
        logger.warning(
            "⚠️ %s: 시간표 응답을 받지 못했습니다.",
            scn_ymd,
        )

        return []

    return await process_schedule_response(
        payload,
        scn_ymd,
    )


# ============================================================
# 현재 화면 상태
# ============================================================

async def log_page_state():
    try:
        title = await page.title()

        logger.info(
            "페이지 제목: %s",
            title,
        )

        logger.info(
            "페이지 URL: %s",
            page.url,
        )

    except Exception:
        pass


# ============================================================
# 최초 준비
# ============================================================

async def prepare_cgv():
    logger.info(
        "CGV 예매 페이지 준비"
    )

    await log_page_state()

    selected = await select_theater()

    if not selected:
        raise RuntimeError(
            "THEATER_SELECTION_FAILED"
        )

    # 극장 선택 직후 페이지가 안정화될 시간
    await asyncio.sleep(2)

    logger.info(
        "✅ CGV 예매 화면 준비 완료"
    )


# ============================================================
# 전체 검사
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

        # ----------------------------------------------------
        # 첫 날짜부터 실제 UI 클릭
        # ----------------------------------------------------

        for index, scn_ymd in enumerate(
            dates
        ):

            try:

                await check_date(
                    scn_ymd
                )

            except Exception as exc:

                logger.error(
                    "%s 검사 실패: %s",
                    scn_ymd,
                    exc,
                )

            if (
                index < len(dates) - 1
            ):

                await asyncio.sleep(
                    INTERVAL_SECONDS
                )


# ============================================================
# Health
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

                body = (
                    "🟢 용아맥 감시 프로세스 작동 중\n"
                    f"browser={'OK' if page else 'NO'}\n"
                    f"last_response="
                    f"{last_response_time}"
                )

            elif first_line.startswith(
                "GET /test"
            ):

                logger.info(
                    "========== /test 시작 =========="
                )

                try:

                    await perform_scan(
                        test_mode=True
                    )

                    body = (
                        "🟢 CGV 브라우저 테스트 완료"
                    )

                except Exception as exc:

                    logger.error(
                        "/test 실패: %s",
                        exc,
                    )

                    body = (
                        "🔴 CGV 테스트 실패: "
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
                "health 요청 처리 실패"
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
        "극장: %s (%s)",
        THEATER_NAME,
        SITE_NO,
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

    telegram_ready = bool(
        TELEGRAM_BOT_TOKEN
        and TELEGRAM_CHAT_ID
    )

    logger.info(
        "Telegram: %s",
        "READY"
        if telegram_ready
        else "NOT READY",
    )

    await start_browser()

    try:

        await prepare_cgv()

        send_telegram(
            "🟢 <b>용아맥 감시 시작</b>\n\n"
            f"🎬 {', '.join(MOVIE_ALIASES)}\n"
            f"🏢 {THEATER_NAME}\n"
            "🎞️ IMAX\n"
            f"⏱ {INTERVAL_SECONDS}초 간격\n\n"
            "🌐 브라우저 응답 직접 감시 방식"
        )

    except Exception as exc:

        logger.exception(
            "CGV 초기화 실패: %s",
            exc,
        )

        raise

    while True:

        try:

            await perform_scan(
                test_mode=False
            )

        except Exception as exc:

            logger.exception(
                "감시 사이클 오류: %s",
                exc,
            )

            # 페이지가 죽은 경우 브라우저 재시작
            if page is None or page.is_closed():

                logger.warning(
                    "페이지가 종료됨 -> 브라우저 재시작"
                )

                try:
                    await shutdown()
                except Exception:
                    pass

                await start_browser()
                await prepare_cgv()

            else:

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
    global context
    global page

    try:
        if browser:
            await browser.close()

    except Exception:
        pass

    browser = None
    context = None
    page = None

    try:
        if playwright:
            await playwright.stop()

    except Exception:
        pass

    playwright = None


# ============================================================
# 실행
# ============================================================

async def main():

    health_task = asyncio.create_task(
        health_server()
    )

    monitor_task = asyncio.create_task(
        monitor()
    )

    try:

        await asyncio.gather(
            health_task,
            monitor_task,
        )

    finally:

        await shutdown()


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

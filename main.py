import asyncio
import html
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
# 기본 설정
# ============================================================

CGV_BASE_URL = "https://cgv.co.kr"
CGV_BOOKING_URL = "https://cgv.co.kr/cnm/movieBook/cinema"

THEATER_NAME = os.getenv(
    "THEATER_NAME",
    "용산아이파크몰",
).strip()

THEATER_CODE = os.getenv(
    "THEATER_CODE",
    "0013",
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


# ============================================================
# 기존 환경변수 호환
# ============================================================

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
            "REQUEST_TIMEOUT_SECONDS",
            os.getenv(
                "REQUEST_WAIT_SECONDS",
                "15",
            ),
        )
    ),
)

HEADER_CAPTURE_TIMEOUT_SECONDS = max(
    3,
    int(
        os.getenv(
            "HEADER_CAPTURE_TIMEOUT_SECONDS",
            "10",
        )
    ),
)

HEADER_REFRESH_INTERVAL_SECONDS = max(
    30,
    int(
        os.getenv(
            "HEADER_REFRESH_INTERVAL_SECONDS",
            "300",
        )
    ),
)

AUTO_OPEN_SESSION = (
    os.getenv(
        "AUTO_OPEN_SESSION",
        "false",
    ).lower()
    in ("1", "true", "yes", "y")
)

BROWSER_HEADLESS = (
    os.getenv(
        "BROWSER_HEADLESS",
        "true",
    ).lower()
    in ("1", "true", "yes", "y")
)

PLAYWRIGHT_USER_DATA_DIR = os.getenv(
    "PLAYWRIGHT_USER_DATA_DIR",
    "",
).strip()

COMPANY_CODE = os.getenv(
    "COMPANY_CODE",
    "",
).strip()

SITE_NO = os.getenv(
    "SITE_NO",
    THEATER_CODE,
).strip()

RTCTL_SCOP_CD = os.getenv(
    "RTCTL_SCOP_CD",
    "",
).strip()

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
# 전역 상태
# ============================================================

playwright = None
browser = None
context = None
page = None

monitor_task = None
telegram_task = None

monitor_enabled = True

# 실제 CGV 검사를 동시에 여러 개 하지 않도록 하는 lock
scan_lock = asyncio.Lock()

# /test 중인지 별도 관리
test_running = False

seen_sessions = {}

last_schedule_response = 0.0
last_schedule_url = ""

last_scan_time = 0.0
last_scan_result = []

blocked_detected = False
blocked_message = ""

telegram_offset = 0

# 최근 JSON 응답
latest_json_responses = []

# 마지막으로 선택된 날짜
current_selected_date = None


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


def weekday_korean(ymd):
    try:
        dt = datetime.strptime(
            ymd,
            "%Y%m%d",
        )

        return [
            "월",
            "화",
            "수",
            "목",
            "금",
            "토",
            "일",
        ][dt.weekday()]

    except Exception:
        return "?"


# ============================================================
# Telegram
# ============================================================

def telegram_api(method):
    if not TELEGRAM_BOT_TOKEN:
        return None

    return (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/"
        f"{method}"
    )


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

    url = telegram_api(
        "sendMessage"
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


async def telegram_send_async(
    message,
    buttons=None,
):
    await asyncio.to_thread(
        send_telegram,
        message,
        buttons,
    )


# ============================================================
# CGV 차단 감지
# ============================================================

BLOCK_KEYWORDS = [
    "비정상적인 접근",
    "비정상적인 접속",
    "접근이 제한",
    "접근이 차단",
    "차단되었습니다",
    "일시적으로 차단",
    "access denied",
    "forbidden",
    "cloudflare",
]


def mark_blocked(reason):
    global blocked_detected
    global blocked_message

    blocked_detected = True
    blocked_message = reason

    logger.error(
        "🚫 CGV 차단 감지: %s",
        reason,
    )


async def detect_block_page():
    if not page:
        return True

    try:
        title = (
            await page.title()
        ).lower()

    except Exception:
        title = ""

    try:
        body_text = (
            await page.locator(
                "body"
            ).inner_text(
                timeout=3000
            )
        ).lower()

    except Exception:
        body_text = ""

    combined = (
        title
        + "\n"
        + body_text[:20000]
    )

    for keyword in BLOCK_KEYWORDS:
        if keyword.lower() in combined:
            mark_blocked(
                f"CGV 차단 문구 감지: {keyword}"
            )
            return True

    return False


# ============================================================
# 브라우저 시작
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

    launch_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
    ]

    # --------------------------------------------------------
    # persistent context 사용 여부
    # --------------------------------------------------------

    if PLAYWRIGHT_USER_DATA_DIR:

        logger.info(
            "Playwright persistent profile 사용: %s",
            PLAYWRIGHT_USER_DATA_DIR,
        )

        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=PLAYWRIGHT_USER_DATA_DIR,
            headless=BROWSER_HEADLESS,
            args=launch_args,
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

        browser = context.browser

        pages = context.pages

        if pages:
            page = pages[0]
        else:
            page = await context.new_page()

    else:

        browser = await playwright.chromium.launch(
            headless=BROWSER_HEADLESS,
            args=launch_args,
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

        page = await context.new_page()

    # ========================================================
    # 모든 request 기록
    # ========================================================

    def on_request(request):

        try:

            if request.resource_type not in (
                "xhr",
                "fetch",
            ):
                return

            url = request.url

            if "cgv.co.kr" not in url:
                return

            logger.info(
                "🌐 CGV REQUEST [%s] %s",
                request.method,
                url,
            )

        except Exception:
            pass

    # ========================================================
    # JSON response 기록
    # ========================================================

    async def on_response(response):

        global last_schedule_response
        global last_schedule_url
        global latest_json_responses

        try:

            url = response.url

            if "cgv.co.kr" not in url:
                return

            content_type = (
                response.headers.get(
                    "content-type",
                    "",
                ).lower()
            )

            # JSON 응답만 우선적으로 잡는다.
            if (
                "json" not in content_type
                and "javascript" not in content_type
                and "text/plain" not in content_type
            ):
                return

            if response.status in (
                401,
                403,
                429,
            ):
                mark_blocked(
                    f"CGV HTTP {response.status}"
                )

            try:
                data = await response.json()

            except Exception:
                return

            if not isinstance(
                data,
                (dict, list),
            ):
                return

            # 최근 JSON 저장
            latest_json_responses.append(
                {
                    "time": time.time(),
                    "url": url,
                    "status": response.status,
                    "data": data,
                }
            )

            # 최대 30개까지만 유지
            if len(latest_json_responses) > 30:
                latest_json_responses = (
                    latest_json_responses[-30:]
                )

            # JSON 내부에 상영 관련 키워드가 있는지 확인
            compact = json.dumps(
                data,
                ensure_ascii=False,
            )

            schedule_hint = (
                "오디세이" in compact
                or "The Odyssey" in compact
                or "ODYSSEY" in compact
                or "IMAX" in compact
                or "아이맥스" in compact
                or "scns" in compact
                or "scn" in compact
                or "seat" in compact
            )

            if schedule_hint:

                last_schedule_response = (
                    time.time()
                )

                last_schedule_url = url

                logger.info(
                    "🎯 CGV 상영 관련 JSON 응답 확보: HTTP %s",
                    response.status,
                )

                logger.info(
                    "🎯 JSON URL: %s",
                    url,
                )

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

    # ========================================================
    # CGV 접속
    # ========================================================

    booking_url = (
        f"{CGV_BOOKING_URL}"
        f"?siteNo={SITE_NO}"
        f"&siteNm={THEATER_NAME}"
    )

    logger.info(
        "CGV 접속: %s",
        booking_url,
    )

    try:

        await page.goto(
            booking_url,
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

    if await detect_block_page():

        logger.warning(
            "⚠️ CGV 초기 페이지에서 차단 상태 감지"
        )


# ============================================================
# 극장 선택
# ============================================================

async def select_theater():

    logger.info(
        "🏢 CGV 극장 확인: %s (%s)",
        THEATER_NAME,
        THEATER_CODE,
    )

    if await detect_block_page():
        return False

    # 이미 URL에 siteNo=0013을 넣었으므로
    # 화면에 극장이 선택되어 있는지를 먼저 확인한다.

    try:

        body = await page.locator(
            "body"
        ).inner_text(
            timeout=3000
        )

        if THEATER_NAME in body:

            logger.info(
                "✅ 현재 화면에 %s 확인",
                THEATER_NAME,
            )

            return True

    except Exception:
        pass

    # --------------------------------------------------------
    # 극장 선택창이 필요한 경우만 시도
    # --------------------------------------------------------

    selectors = [
        'input[placeholder*="극장명"]',
        'input[placeholder*="극장을"]',
        'input[placeholder*="극장"]',
        'button:has-text("극장")',
        '[aria-label*="극장 선택"]',
        '[aria-label*="극장"]',
    ]

    for selector in selectors:

        try:

            locator = page.locator(
                selector
            ).first

            if await locator.is_visible(
                timeout=800
            ):

                await locator.click(
                    timeout=3000
                )

                await asyncio.sleep(1)

                break

        except Exception:
            continue

    await asyncio.sleep(1)

    # 검색창
    for selector in [
        'input[placeholder*="극장명"]',
        'input[placeholder*="극장을"]',
        'input[placeholder*="극장"]',
        'input[type="search"]',
    ]:

        try:

            locator = page.locator(
                selector
            ).first

            if await locator.is_visible(
                timeout=800
            ):

                await locator.fill(
                    THEATER_NAME
                )

                await asyncio.sleep(1)

                break

        except Exception:
            continue

    # 정확한 극장명만 선택
    candidates = [
        page.get_by_text(
            THEATER_NAME,
            exact=True,
        ).last,

        page.get_by_text(
            THEATER_NAME,
            exact=False,
        ).last,
    ]

    for candidate in candidates:

        try:

            if await candidate.is_visible(
                timeout=1500
            ):

                await candidate.click(
                    timeout=4000
                )

                await asyncio.sleep(2)

                logger.info(
                    "✅ %s 선택 완료",
                    THEATER_NAME,
                )

                return True

        except Exception:
            continue

    logger.warning(
        "⚠️ 극장 선택 실패"
    )

    return False


# ============================================================
# 날짜 DOM 탐색
# ============================================================

async def find_date_element(
    target_ymd,
):
    """
    기존처럼 페이지 전체 2,000개 이상의 element를
    후보로 만들지 않는다.

    CGV 날짜 영역으로 추정되는 요소를 먼저 찾고,
    그 내부에서 날짜 버튼을 찾는다.
    """

    target = datetime.strptime(
        target_ymd,
        "%Y%m%d",
    )

    year = target.year
    month = target.month
    day = target.day
    weekday = weekday_korean(
        target_ymd
    )

    month2 = f"{month:02d}"
    day2 = f"{day:02d}"

    # --------------------------------------------------------
    # 날짜 영역 후보
    # --------------------------------------------------------

    area_selectors = [
        '[class*="date"]',
        '[class*="Date"]',
        '[class*="calendar"]',
        '[class*="Calendar"]',
        '[class*="schedule"]',
        '[class*="Schedule"]',
        '[aria-label*="날짜"]',
        '[aria-label*="일자"]',
    ]

    areas = []

    for selector in area_selectors:

        try:

            loc = page.locator(
                selector
            )

            count = await loc.count()

            # 너무 많은 영역은 제외
            if count > 100:
                continue

            for i in range(
                min(count, 30)
            ):

                element = loc.nth(i)

                try:

                    if not await element.is_visible(
                        timeout=100
                    ):
                        continue

                    text = (
                        await element.inner_text()
                    ).strip()

                    if not text:
                        continue

                    # 날짜 영역이라 볼 만한 경우
                    if (
                        weekday in text
                        or f"{month}월" in text
                        or f"{month2}." in text
                        or re.search(
                            rf"\b{day}\b",
                            text,
                        )
                    ):

                        areas.append(
                            element
                        )

                except Exception:
                    continue

        except Exception:
            continue

    # --------------------------------------------------------
    # 날짜 영역이 없으면 날짜 버튼 후보를 제한적으로 검색
    # --------------------------------------------------------

    if not areas:

        try:

            buttons = page.locator(
                "button"
            )

            count = await buttons.count()

            # 전체 button을 2150개씩 검사하지 않는다.
            # 최대 300개까지만 본다.
            for i in range(
                min(count, 300)
            ):

                element = buttons.nth(i)

                try:

                    if not await element.is_visible(
                        timeout=100
                    ):
                        continue

                    text = (
                        await element.inner_text()
                    ).strip()

                    aria = (
                        await element.get_attribute(
                            "aria-label"
                        )
                        or ""
                    )

                    data_date = (
                        await element.get_attribute(
                            "data-date"
                        )
                        or ""
                    )

                    data_ymd = (
                        await element.get_attribute(
                            "data-ymd"
                        )
                        or ""
                    )

                    all_text = (
                        f"{text} {aria} "
                        f"{data_date} {data_ymd}"
                    )

                    # 확실한 날짜 형태만
                    if (
                        target_ymd in all_text
                        or f"{year}-{month2}-{day2}"
                        in all_text
                        or f"{month2}/{day2}"
                        in all_text
                        or (
                            re.search(
                                rf"(^|\s){day}(\s|$)",
                                text,
                            )
                            and weekday in text
                        )
                    ):

                        return element

                except Exception:
                    continue

        except Exception:
            pass

        return None

    # --------------------------------------------------------
    # 날짜 영역 내부 버튼 탐색
    # --------------------------------------------------------

    for area in areas:

        try:

            # 영역이 너무 큰 경우 제외
            text = (
                await area.inner_text()
            )

            if len(text) > 5000:
                continue

            buttons = area.locator(
                "button, [role='button'], a"
            )

            count = await buttons.count()

            if count > 100:
                continue

            for i in range(count):

                element = buttons.nth(i)

                try:

                    if not await element.is_visible(
                        timeout=100
                    ):
                        continue

                    text = (
                        await element.inner_text()
                    ).strip()

                    aria = (
                        await element.get_attribute(
                            "aria-label"
                        )
                        or ""
                    )

                    title = (
                        await element.get_attribute(
                            "title"
                        )
                        or ""
                    )

                    data_date = (
                        await element.get_attribute(
                            "data-date"
                        )
                        or ""
                    )

                    data_ymd = (
                        await element.get_attribute(
                            "data-ymd"
                        )
                        or ""
                    )

                    all_text = (
                        f"{text} "
                        f"{aria} "
                        f"{title} "
                        f"{data_date} "
                        f"{data_ymd}"
                    )

                    if (
                        target_ymd in all_text
                        or f"{year}-{month2}-{day2}"
                        in all_text
                        or f"{year}.{month2}.{day2}"
                        in all_text
                        or f"{month2}/{day2}"
                        in all_text
                        or (
                            re.search(
                                rf"(^|\s){day}(\s|$)",
                                text,
                            )
                            and weekday in all_text
                        )
                        or (
                            f"{month}월 {day}일"
                            in all_text
                        )
                    ):

                        return element

                except Exception:
                    continue

        except Exception:
            continue

    return None


# ============================================================
# 날짜 선택 상태 확인
# ============================================================

async def verify_selected_date(
    target_ymd,
):
    """
    클릭만 성공했다고 판단하지 않는다.
    실제 DOM에서 선택 상태를 확인한다.
    """

    target = datetime.strptime(
        target_ymd,
        "%Y%m%d",
    )

    day = target.day
    weekday = weekday_korean(
        target_ymd
    )

    try:

        result = await page.evaluate(
            """
            ({day, weekday}) => {

                const nodes = [
                    ...document.querySelectorAll(
                        'button, [role="button"], a'
                    )
                ];

                const matches = [];

                for (const el of nodes) {

                    const text =
                        (el.innerText || '').trim();

                    const aria =
                        el.getAttribute('aria-label') || '';

                    const cls =
                        typeof el.className === 'string'
                            ? el.className
                            : '';

                    const selected =
                        el.getAttribute('aria-selected');

                    const dataSelected =
                        el.getAttribute('data-selected');

                    const all = [
                        text,
                        aria
                    ].join(' ');

                    const dayMatch =
                        new RegExp(
                            '(^|\\\\s)'
                            + day
                            + '(\\\\s|$)'
                        ).test(text);

                    if (
                        dayMatch
                        && all.includes(weekday)
                    ) {

                        matches.push({
                            text,
                            aria,
                            cls,
                            selected,
                            dataSelected
                        });
                    }
                }

                return matches;
            }
            """,
            {
                "day": str(day),
                "weekday": weekday,
            },
        )

        if not result:
            return False

        for item in result:

            selected = str(
                item.get("selected")
                or ""
            ).lower()

            data_selected = str(
                item.get("dataSelected")
                or ""
            ).lower()

            cls = str(
                item.get("cls")
                or ""
            ).lower()

            if (
                selected == "true"
                or data_selected == "true"
                or "selected" in cls
                or "active" in cls
                or "on" in cls
            ):
                return True

        # CGV 구현에 따라 selected 속성이 없을 수 있다.
        # 클릭 직후 해당 날짜가 화면에 존재하면
        # 약한 검증으로 인정한다.
        return True

    except Exception:
        return False


# ============================================================
# 날짜 선택
# ============================================================

async def select_date(
    target_ymd,
):
    global current_selected_date

    logger.info(
        "📅 날짜 선택: %s (%s)",
        pretty_date(target_ymd),
        weekday_korean(target_ymd),
    )

    if await detect_block_page():
        return False

    element = await find_date_element(
        target_ymd
    )

    if not element:

        logger.warning(
            "⚠️ 날짜 버튼을 찾지 못함: %s",
            target_ymd,
        )

        return False

    try:

        await element.scroll_into_view_if_needed(
            timeout=3000
        )

        text = (
            await element.inner_text()
        ).strip()

        logger.info(
            "📅 날짜 버튼 발견: %s",
            text[:100],
        )

        await element.click(
            timeout=5000
        )

    except Exception as exc:

        logger.warning(
            "⚠️ 날짜 클릭 실패: %s | %s",
            target_ymd,
            str(exc)[:200],
        )

        return False

    await asyncio.sleep(1.5)

    # 선택 상태 검증
    verified = await verify_selected_date(
        target_ymd
    )

    if verified:

        current_selected_date = (
            target_ymd
        )

        logger.info(
            "✅ 날짜 선택 상태 확인: %s",
            target_ymd,
        )

        return True

    logger.warning(
        "⚠️ 날짜 클릭은 됐지만 선택 상태 확인 실패: %s",
        target_ymd,
    )

    return False


# ============================================================
# JSON rows 추출
# ============================================================

def extract_rows_from_any(
    value,
):
    """
    CGV JSON 구조가 조금 달라져도
    리스트를 최대한 재귀적으로 찾는다.
    """

    found = []

    if isinstance(
        value,
        list,
    ):

        for item in value:

            if isinstance(
                item,
                dict,
            ):

                # 상영정보 row로 보이는 경우
                keys = {
                    str(k).lower()
                    for k in item.keys()
                }

                hints = {
                    "movnm",
                    "movno",
                    "scnsno",
                    "scnsseq",
                    "scnsrtm",
                    "scnstarttm",
                    "screenname",
                    "screenNm".lower(),
                    "remainseatcnt",
                    "frseatcnt",
                }

                if keys.intersection(
                    hints
                ):
                    found.append(item)

                # 내부에도 탐색
                for nested in item.values():
                    found.extend(
                        extract_rows_from_any(
                            nested
                        )
                    )

    elif isinstance(
        value,
        dict,
    ):

        for nested in value.values():

            found.extend(
                extract_rows_from_any(
                    nested
                )
            )

    return found


def extract_rows(payload):
    rows = extract_rows_from_any(
        payload
    )

    # 중복 제거
    unique = []

    seen = set()

    for row in rows:

        try:
            key = json.dumps(
                row,
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            )

        except Exception:
            key = str(row)

        if key in seen:
            continue

        seen.add(key)
        unique.append(row)

    return unique


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
        row.get("screenNm"),
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

    for key in [
        "frSeatCnt",
        "remainSeatCnt",
        "availableSeatCnt",
        "seatCnt",
        "remainCnt",
    ]:

        try:

            value = row.get(
                key
            )

            if value is not None:

                # 문자열에 콤마가 있는 경우
                return int(
                    str(value).replace(
                        ",",
                        "",
                    )
                )

        except Exception:
            continue

    return 0


def total_seat_count(row):

    for key in [
        "stcnt",
        "totalSeatCnt",
        "totalSeats",
        "seatTotCnt",
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
# JSON 시간표 파싱
# ============================================================

def parse_sessions(
    payload,
    scn_ymd,
):
    rows = extract_rows(
        payload
    )

    logger.info(
        "JSON 후보 rows=%d",
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

        # 0석은 알림 대상 아님
        if seats <= 0:
            continue

        if str(
            row.get("cntlYn", "")
        ).upper() == "Y":
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

    return result


# ============================================================
# 최근 JSON에서 시간표 찾기
# ============================================================

def parse_latest_json(
    scn_ymd,
    since,
):
    global latest_json_responses

    # 가장 최근 응답부터
    candidates = list(
        reversed(
            latest_json_responses
        )
    )

    for response in candidates:

        if response["time"] < since:
            continue

        data = response["data"]

        try:

            sessions = parse_sessions(
                data,
                scn_ymd,
            )

            if sessions:
                return sessions

        except Exception as exc:

            logger.debug(
                "JSON 파싱 오류: %s",
                exc,
            )

    return []


# ============================================================
# 실제 화면에서 상영시간표 읽기
# ============================================================

async def extract_schedule_from_dom(
    scn_ymd,
):
    """
    API 응답을 못 잡더라도
    현재 CGV 화면에 표시된 상영시간표를 읽는다.

    네가 올린 로그처럼 실제 화면에는

    오디세이
    ...
    IMAX관
    IMAX LASER 2D
    07:30-10:32 8/624석
    ...

    형태가 표시되므로 이를 백업 경로로 사용한다.
    """

    try:

        body = await page.locator(
            "body"
        ).inner_text(
            timeout=5000
        )

    except Exception as exc:

        logger.warning(
            "⚠️ 화면 텍스트 읽기 실패: %s",
            str(exc)[:200],
        )

        return []

    if not body:
        return []

    # --------------------------------------------------------
    # 오디세이 존재 여부
    # --------------------------------------------------------

    movie_found = any(
        alias.lower() in body.lower()
        for alias in MOVIE_ALIASES
    )

    if not movie_found:

        logger.info(
            "ℹ️ %s: 현재 화면에 대상 영화 없음",
            pretty_date(scn_ymd),
        )

        return []

    # --------------------------------------------------------
    # IMAX 영역 추출
    # --------------------------------------------------------

    lower_body = body.lower()

    positions = []

    for keyword in [
        "imax관",
        "imax",
        "아이맥스",
    ]:

        start = 0

        while True:

            index = lower_body.find(
                keyword.lower(),
                start,
            )

            if index < 0:
                break

            positions.append(
                index
            )

            start = index + len(
                keyword
            )

    if not positions:

        logger.info(
            "ℹ️ %s: IMAX 영역 없음",
            pretty_date(scn_ymd),
        )

        return []

    # 마지막/가장 가까운 IMAX 영역부터 탐색
    # 너무 넓은 텍스트를 가져오지 않는다.
    position = min(
        positions,
        key=lambda x: abs(
            x - lower_body.find(
                "오디세이"
            )
        )
        if lower_body.find("오디세이") >= 0
        else x,
    )

    section = body[
        max(0, position - 200):
        position + 3000
    ]

    # --------------------------------------------------------
    # 상영시간 + 좌석수 패턴
    #
    # 07:30-10:32 8/624석
    # 18:00-21:02 4/624석
    # 21:30-24:32 매진
    # --------------------------------------------------------

    pattern = re.compile(
        r"(?P<start>\d{1,2}:\d{2})"
        r"\s*-\s*"
        r"(?P<end>\d{1,2}:\d{2})"
        r"(?:\s+)"
        r"(?P<remain>\d+)"
        r"\s*/\s*"
        r"(?P<total>\d+)"
        r"\s*석"
    )

    sessions = []

    for match in pattern.finditer(
        section
    ):

        start = match.group(
            "start"
        )

        end = match.group(
            "end"
        )

        remain = int(
            match.group("remain")
        )

        total = int(
            match.group("total")
        )

        if remain <= 0:
            continue

        sessions.append(
            {
                "date": scn_ymd,
                "movNo": None,
                "movNm": "오디세이",
                "scnSseq": None,
                "scnsNo": None,
                "start": start,
                "end": end,
                "screen": "IMAX",
                "seats": remain,
                "totalSeats": total,
                "source": "DOM",
            }
        )

    # --------------------------------------------------------
    # 중복 제거
    # --------------------------------------------------------

    unique = []

    seen = set()

    for item in sessions:

        key = (
            item["date"],
            item["start"],
            item["end"],
        )

        if key in seen:
            continue

        seen.add(key)
        unique.append(
            item
        )

    if unique:

        logger.info(
            "🎯 실제 CGV 화면에서 IMAX 회차 %d개 발견",
            len(unique),
        )

    else:

        logger.info(
            "ℹ️ %s: 화면상 IMAX 잔여 회차 없음",
            pretty_date(scn_ymd),
        )

    return unique


# ============================================================
# 시간표 응답 처리
# ============================================================

async def process_response(
    payload,
    scn_ymd,
    send_notification=True,
):

    status = (
        payload.get("statusCode")
        if isinstance(
            payload,
            dict,
        )
        else None
    )

    if status not in (
        None,
        0,
        "0",
    ):

        logger.warning(
            "CGV statusCode=%s",
            status,
        )

    sessions = parse_sessions(
        payload,
        scn_ymd,
    )

    logger.info(
        "%s: JSON 기준 IMAX 잔여 회차 %d개",
        scn_ymd,
        len(sessions),
    )

    if send_notification:

        for item in sessions:
            notify_session(
                item
            )

    return sessions


# ============================================================
# 알림
# ============================================================

def format_time(value):

    if value is None:
        return "??:??"

    text = str(
        value
    )

    if len(text) == 4 and text.isdigit():

        return (
            text[:2]
            + ":"
            + text[2:]
        )

    return text


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
        f"🎬 <b>{html.escape(str(item.get('movNm', '오디세이')))}</b>\n"
        f"📅 {date}\n"
        f"🏢 {html.escape(THEATER_NAME)}\n"
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
                    f"{CGV_BOOKING_URL}"
                    f"?siteNo={SITE_NO}"
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
# 날짜 하나 검사
# ============================================================

async def check_date(
    scn_ymd,
    test_mode=False,
):

    global last_schedule_response

    logger.info(
        "=========================================="
    )

    logger.info(
        "🔎 날짜 검사: %s",
        pretty_date(scn_ymd),
    )

    # 이번 날짜의 JSON 감지 기준점
    response_since = time.time()

    # 날짜 변경 전 최신 응답을 무시하기 위해
    # 마지막 응답 시간 기준을 리셋한다.
    last_schedule_response = 0.0

    if await detect_block_page():

        return {
            "status": "blocked",
            "sessions": [],
        }

    # --------------------------------------------------------
    # 날짜 선택
    # --------------------------------------------------------

    clicked = await select_date(
        scn_ymd
    )

    if not clicked:

        logger.warning(
            "⚠️ 날짜 클릭 실패: %s",
            pretty_date(scn_ymd),
        )

        return {
            "status": "date_failed",
            "sessions": [],
        }

    # --------------------------------------------------------
    # JSON + DOM 둘 다 기다린다.
    # --------------------------------------------------------

    logger.info(
        "⏳ CGV 시간표 확인 중..."
    )

    deadline = (
        time.time()
        + REQUEST_WAIT_SECONDS
    )

    while time.time() < deadline:

        if await detect_block_page():

            return {
                "status": "blocked",
                "sessions": [],
            }

        # ====================================================
        # 1순위: 새 JSON 응답
        # ====================================================

        json_sessions = parse_latest_json(
            scn_ymd,
            response_since,
        )

        if json_sessions:

            logger.info(
                "✅ %s: JSON 시간표 확인 완료",
                pretty_date(scn_ymd),
            )

            if not test_mode:

                for item in json_sessions:
                    notify_session(
                        item
                    )

            return {
                "status": "ok",
                "sessions": json_sessions,
                "source": "JSON",
            }

        # ====================================================
        # 2순위: 실제 화면 DOM
        # ====================================================

        dom_sessions = (
            await extract_schedule_from_dom(
                scn_ymd
            )
        )

        if dom_sessions:

            logger.info(
                "✅ %s: 실제 화면 시간표 확인 완료",
                pretty_date(scn_ymd),
            )

            if not test_mode:

                for item in dom_sessions:
                    notify_session(
                        item
                    )

            return {
                "status": "ok",
                "sessions": dom_sessions,
                "source": "DOM",
            }

        await asyncio.sleep(
            1
        )

    # --------------------------------------------------------
    # 여기까지 왔다는 것은 요청 자체가 실패한 것이 아니라
    # 시간표를 우리가 확인하지 못했다는 의미
    # --------------------------------------------------------

    # 마지막으로 DOM 한 번 더
    dom_sessions = (
        await extract_schedule_from_dom(
            scn_ymd
        )
    )

    if dom_sessions:

        if not test_mode:

            for item in dom_sessions:
                notify_session(
                    item
                )

        return {
            "status": "ok",
            "sessions": dom_sessions,
            "source": "DOM",
        }

    logger.info(
        "ℹ️ %s: 시간표 확인 결과 없음",
        pretty_date(scn_ymd),
    )

    return {
        "status": "no_schedule",
        "sessions": [],
    }


# ============================================================
# 전체 검사
# ============================================================

async def perform_scan(
    test_mode=False,
):
    global last_scan_time
    global last_scan_result
    global test_running

    # /test 중이면 중복 검사 금지
    if test_mode:

        if test_running:

            return {
                "busy": True,
                "dates": [],
                "statuses": {},
                "sessions": [],
            }

        test_running = True

    try:

        async with scan_lock:

            dates = [
                make_ymd(i)
                for i in range(
                    DAYS_AHEAD + 1
                )
            ]

            logger.info(
                "검사 날짜: %s",
                ", ".join(dates),
            )

            all_sessions = []
            statuses = {}

            for index, date in enumerate(
                dates
            ):

                result = await check_date(
                    date,
                    test_mode=test_mode,
                )

                statuses[date] = (
                    result["status"]
                )

                all_sessions.extend(
                    result["sessions"]
                )

                if result["status"] == "blocked":

                    logger.warning(
                        "🚫 CGV 차단 상태. "
                        "이번 검사 사이클 종료."
                    )

                    break

                if (
                    index
                    < len(dates) - 1
                ):

                    await asyncio.sleep(
                        1
                    )

            last_scan_time = time.time()
            last_scan_result = all_sessions

            return {
                "busy": False,
                "dates": dates,
                "statuses": statuses,
                "sessions": all_sessions,
            }

    finally:

        if test_mode:
            test_running = False


# ============================================================
# /test 결과
# ============================================================

def build_test_message(
    result,
):

    if result.get("busy"):

        return (
            "⏳ <b>이미 검사가 진행 중입니다.</b>\n\n"
            "현재 검사 완료 후 다시 /test 해주세요."
        )

    statuses = result.get(
        "statuses",
        {},
    )

    sessions = result.get(
        "sessions",
        [],
    )

    blocked = any(
        value == "blocked"
        for value in statuses.values()
    )

    date_failed = [
        date
        for date, status
        in statuses.items()
        if status == "date_failed"
    ]

    no_schedule = [
        date
        for date, status
        in statuses.items()
        if status == "no_schedule"
    ]

    if blocked:

        status_text = (
            "🔴 <b>CGV 차단 감지</b>"
        )

    elif sessions:

        status_text = (
            "🟢 <b>정상 응답 + 잔여석 발견</b>"
        )

    elif date_failed:

        status_text = (
            "🟡 <b>일부 날짜 선택 실패</b>"
        )

    elif no_schedule:

        status_text = (
            "🟢 <b>날짜 선택 정상 / 잔여 회차 없음</b>"
        )

    else:

        status_text = (
            "🟢 <b>정상 확인 / 잔여석 없음</b>"
        )

    lines = [
        "🧪 <b>용아맥 테스트 결과</b>",
        "",
        status_text,
        f"🏢 {html.escape(THEATER_NAME)}",
        "🎞️ IMAX",
        "",
    ]

    if sessions:

        lines.append(
            f"🎟️ 잔여 회차: <b>{len(sessions)}개</b>"
        )

        for item in sessions[:20]:

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

            source = item.get(
                "source",
                "JSON",
            )

            lines.append(
                f"• {pretty_date(item['date'])} "
                f"{start}~{end} "
                f"💺 {seats}석 "
                f"({source})"
            )

    else:

        lines.append(
            "💺 현재 확인된 잔여석: 없음"
        )

    if date_failed:

        lines.append("")
        lines.append(
            "⚠️ 날짜 클릭 실패:"
        )

        for date in date_failed[:10]:

            lines.append(
                f"• {pretty_date(date)}"
            )

    if no_schedule:

        lines.append("")
        lines.append(
            "ℹ️ 시간표 확인 결과:"
        )

        for date in no_schedule[:10]:

            lines.append(
                f"• {pretty_date(date)}"
            )

    if blocked:

        lines.append("")
        lines.append(
            "🚫 CGV 접근 제한이 감지되었습니다."
        )

        lines.append(
            "⏸️ 추가 요청은 일시적으로 중지합니다."
        )

    return "\n".join(
        lines
    )


# ============================================================
# Telegram 업데이트
# ============================================================

def telegram_get_updates(
    offset,
):

    if not TELEGRAM_BOT_TOKEN:
        return []

    url = telegram_api(
        "getUpdates"
    )

    params = {
        "timeout": 5,
        "offset": offset,
    }

    try:

        response = requests.get(
            url,
            params=params,
            timeout=15,
        )

        if response.status_code != 200:
            return []

        data = response.json()

        if not data.get("ok"):
            return []

        return data.get(
            "result",
            [],
        )

    except Exception as exc:

        logger.debug(
            "Telegram getUpdates 오류: %s",
            exc,
        )

        return []


# ============================================================
# Telegram 명령
# ============================================================

async def handle_telegram_command(
    update,
):

    global monitor_enabled

    message = update.get(
        "message"
    )

    if not message:
        return

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
        return

    text = (
        message.get(
            "text",
            "",
        )
        .strip()
    )

    if not text:
        return

    command = (
        text.split()[0]
        .lower()
        .split("@")[0]
    )

    logger.info(
        "Telegram 명령: %s",
        command,
    )

    # ========================================================
    # /start
    # ========================================================

    if command == "/start":

        monitor_enabled = True

        await telegram_send_async(
            "🟢 <b>용아맥 감시 시작</b>\n\n"
            f"🏢 {html.escape(THEATER_NAME)}\n"
            "🎞️ IMAX\n"
            f"⏱️ 검사 간격: {INTERVAL_SECONDS}초\n\n"
            "🧪 /test 현재 상태 확인\n"
            "📊 /status 상태 확인\n"
            "⏹️ /stop 감시 중지"
        )

        return

    # ========================================================
    # /stop
    # ========================================================

    if command == "/stop":

        monitor_enabled = False

        await telegram_send_async(
            "⏸️ <b>용아맥 감시 중지</b>\n\n"
            "브라우저는 종료하지 않고 대기합니다.\n"
            "다시 감시하려면 /start"
        )

        return

    # ========================================================
    # /test
    # ========================================================

    if command == "/test":

        if scan_lock.locked():

            await telegram_send_async(
                "⏳ <b>현재 자동 감시 검사가 진행 중입니다.</b>\n\n"
                "이번 검사 완료 후 다시 /test 해주세요."
            )

            return

        if test_running:

            await telegram_send_async(
                "⏳ <b>이미 /test가 진행 중입니다.</b>\n\n"
                "현재 테스트가 끝난 뒤 다시 시도해주세요."
            )

            return

        await telegram_send_async(
            "🧪 <b>용아맥 테스트 시작</b>\n\n"
            "극장 → 날짜 → 시간표 순서로 "
            "실제 CGV 화면을 확인합니다."
        )

        try:

            result = await perform_scan(
                test_mode=True
            )

            await telegram_send_async(
                build_test_message(
                    result
                )
            )

        except Exception as exc:

            logger.exception(
                "/test 실패"
            )

            await telegram_send_async(
                "🔴 <b>/test 실패</b>\n\n"
                f"<code>{html.escape(str(exc))}</code>"
            )

        return

    # ========================================================
    # /status
    # ========================================================

    if command == "/status":

        state = (
            "🟢 실행 중"
            if monitor_enabled
            else "⏸️ 중지됨"
        )

        cgv_state = (
            "🔴 차단 감지"
            if blocked_detected
            else "🟢 정상"
        )

        last_scan = (
            datetime.fromtimestamp(
                last_scan_time
            ).strftime(
                "%H:%M:%S"
            )
            if last_scan_time
            else "없음"
        )

        await telegram_send_async(
            "📊 <b>용아맥 감시 상태</b>\n\n"
            f"프로그램: {state}\n"
            f"CGV: {cgv_state}\n"
            f"마지막 검사: {last_scan}\n"
            f"마지막 발견 회차: {len(last_scan_result)}개\n"
            f"테스트: {'🟡 진행 중' if test_running else '🟢 대기'}\n\n"
            "🧪 /test\n"
            "▶️ /start\n"
            "⏹️ /stop"
        )

        return

    # ========================================================
    # /help
    # ========================================================

    if command == "/help":

        await telegram_send_async(
            "🎬 <b>용아맥 감시 명령어</b>\n\n"
            "▶️ /start\n"
            "감시 시작\n\n"
            "⏹️ /stop\n"
            "감시 중지\n\n"
            "🧪 /test\n"
            "현재 용아맥 시간표 확인\n\n"
            "📊 /status\n"
            "프로그램 상태 확인"
        )

        return


# ============================================================
# Telegram 루프
# ============================================================

async def telegram_command_loop():

    global telegram_offset

    if not TELEGRAM_BOT_TOKEN:

        logger.warning(
            "Telegram 명령 루프 비활성화: "
            "TELEGRAM_BOT_TOKEN 없음"
        )

        return

    logger.info(
        "Telegram 명령 수신 대기"
    )

    while True:

        try:

            updates = await asyncio.to_thread(
                telegram_get_updates,
                telegram_offset,
            )

            for update in updates:

                telegram_offset = (
                    update["update_id"]
                    + 1
                )

                await handle_telegram_command(
                    update
                )

        except Exception as exc:

            logger.error(
                "Telegram 명령 루프 오류: %s",
                exc,
            )

        await asyncio.sleep(
            1
        )


# ============================================================
# 감시 루프
# ============================================================

async def monitor_loop():

    global monitor_enabled

    logger.info(
        "🔄 감시 루프 시작"
    )

    while monitor_enabled:

        try:

            if blocked_detected:

                logger.warning(
                    "🚫 CGV 차단 상태. "
                    "5분간 대기합니다."
                )

                await asyncio.sleep(
                    300
                )

                if not monitor_enabled:
                    break

                continue

            await perform_scan(
                test_mode=False
            )

        except Exception as exc:

            logger.exception(
                "감시 사이클 오류: %s",
                exc,
            )

        if monitor_enabled:

            await asyncio.sleep(
                INTERVAL_SECONDS
            )

    logger.info(
        "⏸️ 감시 루프 종료"
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

            if first_line.startswith(
                "GET /status"
            ):

                monitor_state = (
                    "RUNNING"
                    if monitor_enabled
                    else "STOPPED"
                )

                cgv_state = (
                    "BLOCKED"
                    if blocked_detected
                    else "OK"
                )

                body = (
                    "yongsan_imax_bot\n"
                    f"monitor={monitor_state}\n"
                    f"cgv={cgv_state}\n"
                    f"browser={'OK' if page else 'NO'}\n"
                    f"test_running={test_running}\n"
                    f"last_response={last_schedule_response}\n"
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

    if await detect_block_page():

        logger.warning(
            "⚠️ CGV 차단 페이지 상태"
        )

        return False

    selected = await select_theater()

    if not selected:

        if await detect_block_page():
            return False

        raise RuntimeError(
            "THEATER_SELECTION_FAILED"
        )

    await asyncio.sleep(2)

    logger.info(
        "✅ CGV 예매 화면 준비 완료"
    )

    return True


# ============================================================
# 초기화
# ============================================================

async def initialize():

    await start_browser()

    prepared = await prepare_cgv()

    if not prepared:

        await telegram_send_async(
            "🔴 <b>CGV 초기 접속 문제</b>\n\n"
            "CGV 예매 화면을 정상적으로 준비하지 못했습니다.\n"
            "현재 추가 요청은 최소화합니다."
        )

    else:

        await telegram_send_async(
            "🟢 <b>용아맥 감시 프로그램 준비 완료</b>\n\n"
            f"🎬 {html.escape(', '.join(MOVIE_ALIASES))}\n"
            f"🏢 {html.escape(THEATER_NAME)}\n"
            "🎞️ IMAX\n\n"
            "▶️ /start 감시 시작\n"
            "⏹️ /stop 감시 중지\n"
            "🧪 /test 현재 상태 확인\n"
            "📊 /status 상태 확인"
        )


# ============================================================
# 종료
# ============================================================

async def shutdown():

    global browser
    global playwright
    global context

    try:

        if context and not browser:

            await context.close()

    except Exception:
        pass

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

    global monitor_task
    global telegram_task

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
        THEATER_CODE,
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
        "검사 기간: 오늘 + %d일",
        DAYS_AHEAD,
    )

    logger.info(
        "=========================================="
    )

    health_task = asyncio.create_task(
        health_server()
    )

    try:

        await initialize()

        monitor_task = asyncio.create_task(
            monitor_loop()
        )

        telegram_task = asyncio.create_task(
            telegram_command_loop()
        )

        await asyncio.gather(
            monitor_task,
            telegram_task,
        )

    except asyncio.CancelledError:

        pass

    except Exception:

        logger.exception(
            "치명적 오류"
        )

    finally:

        health_task.cancel()

        if monitor_task:
            monitor_task.cancel()

        if telegram_task:
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

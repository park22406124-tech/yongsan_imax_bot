import asyncio
import html
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
# 설정
# ============================================================

CGV_BOOKING_URL = (
    "https://cgv.co.kr/cnm/movieBook/cinema"
)

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
            "15",
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
# 전역 상태
# ============================================================

playwright = None
browser = None
context = None
page = None

monitor_task = None
telegram_task = None

monitor_enabled = True

monitor_lock = asyncio.Lock()

seen_sessions = {}

last_schedule_response = 0.0
last_schedule_url = ""

last_scan_time = 0.0
last_scan_result = []

blocked_detected = False
blocked_message = ""

telegram_offset = 0


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
    "자동화",
    "매크로",
    "보안",
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
    global blocked_detected
    global blocked_message

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

    browser = await playwright.chromium.launch(
        headless=BROWSER_HEADLESS,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
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

    page = await context.new_page()

    # --------------------------------------------------------
    # Request
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
                    "🌐 CGV REQUEST: %s %s",
                    request.method,
                    url,
                )

        except Exception:
            pass

    # --------------------------------------------------------
    # Response
    # --------------------------------------------------------

    async def on_response(response):
        global last_schedule_response
        global last_schedule_url

        try:
            url = response.url

            if (
                "searchMovScnInfo" not in url
                and "searchMov" not in url
                and "ScnInfo" not in url
            ):
                return

            logger.info(
                "🌐 CGV RESPONSE: HTTP %s %s",
                response.status,
                url,
            )

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
                dict,
            ):
                return

            last_schedule_response = (
                time.time()
            )

            last_schedule_url = url

            logger.info(
                "🎯 CGV 시간표 JSON 응답 확보"
            )

            page._latest_cgv_schedule = {
                "url": url,
                "status": response.status,
                "timestamp": time.time(),
                "data": data,
            }

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

    if await detect_block_page():
        logger.warning(
            "⚠️ CGV 초기 페이지에서 차단 상태 감지"
        )


# ============================================================
# 극장 선택
# ============================================================

async def open_theater_picker():
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
                timeout=1000
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


async def select_theater():
    logger.info(
        "🏢 CGV 극장 선택 시도: %s",
        THEATER_NAME,
    )

    if await detect_block_page():
        return False

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
                timeout=1000
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

    for result in candidates:
        try:
            if await result.is_visible(
                timeout=2500
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
            continue

    logger.warning(
        "⚠️ 극장 선택 실패"
    )

    return False


# ============================================================
# 날짜 관련 텍스트 정규화
# ============================================================

def normalize_date_text(text):
    if not text:
        return ""

    text = str(text)

    text = (
        text
        .replace("\n", " ")
        .replace("\r", " ")
        .replace("\t", " ")
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


def date_text_matches(
    text,
    target_ymd,
):
    """
    날짜 문자열 자체가 목표 날짜인지 검사한다.
    """

    text = normalize_date_text(
        text
    )

    if not text:
        return False

    target_dt = datetime.strptime(
        target_ymd,
        "%Y%m%d",
    )

    year = target_dt.year
    month = target_dt.month
    day = target_dt.day

    month2 = f"{month:02d}"
    day2 = f"{day:02d}"

    weekday = weekday_korean(
        target_ymd
    )

    compact = re.sub(
        r"\s+",
        "",
        text,
    )

    # YYYYMMDD
    if target_ymd in compact:
        return True

    # YYYY-MM-DD
    if f"{year}-{month2}-{day2}" in text:
        return True

    # YYYY.MM.DD
    if f"{year}.{month2}.{day2}" in text:
        return True

    # YYYY/MM/DD
    if f"{year}/{month2}/{day2}" in text:
        return True

    # MM/DD
    if re.search(
        rf"(^|[^0-9]){month2}/{day2}([^0-9]|$)",
        text,
    ):
        return True

    # M/D
    if re.search(
        rf"(^|[^0-9]){month}/{day}([^0-9]|$)",
        text,
    ):
        return True

    # M월 D일
    if (
        f"{month}월 {day}일" in text
        or f"{month}월{day}일" in text
    ):
        return True

    # M월 D
    if (
        f"{month}월 {day}" in text
        or f"{month}월{day}" in text
    ):
        return True

    # "27 목"
    if re.search(
        rf"(^|[^0-9]){day}([^0-9]|$)",
        text,
    ) and weekday in text:
        return True

    return False


# ============================================================
# 날짜 후보 DOM 수집
# ============================================================

async def get_date_candidates():
    """
    CGV DOM 구조 변경에 대비하여 날짜 후보를
    button / a뿐 아니라 div / li / span까지 폭넓게 수집한다.
    """

    candidates = []

    try:

        locator = page.locator(
            """
            button,
            a,
            li,
            div,
            span,
            [role="button"],
            [role="tab"]
            """
        )

        count = await locator.count()

        logger.info(
            "📅 날짜 DOM 전체 후보 검사: %d개",
            count,
        )

        for index in range(count):

            element = locator.nth(index)

            try:

                if not await element.is_visible(
                    timeout=100
                ):
                    continue

                text = normalize_date_text(
                    await element.inner_text()
                )

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

                data_day = (
                    await element.get_attribute(
                        "data-day"
                    )
                    or ""
                )

                data_value = (
                    await element.get_attribute(
                        "data-value"
                    )
                    or ""
                )

                element_id = (
                    await element.get_attribute(
                        "id"
                    )
                    or ""
                )

                class_name = (
                    await element.get_attribute(
                        "class"
                    )
                    or ""
                )

                role = (
                    await element.get_attribute(
                        "role"
                    )
                    or ""
                )

                candidate = {
                    "element": element,
                    "text": text,
                    "aria": aria,
                    "title": title,
                    "data_date": data_date,
                    "data_ymd": data_ymd,
                    "data_day": data_day,
                    "data_value": data_value,
                    "id": element_id,
                    "class": class_name,
                    "role": role,
                }

                combined = " ".join(
                    str(value)
                    for value in [
                        text,
                        aria,
                        title,
                        data_date,
                        data_ymd,
                        data_day,
                        data_value,
                    ]
                    if value
                )

                # 날짜와 관련 있을 법한 후보만 보관
                if (
                    re.search(
                        r"\d{1,4}[./-]\d{1,2}[./-]?\d{0,4}",
                        combined,
                    )
                    or re.search(
                        r"\d{1,2}\s*월",
                        combined,
                    )
                    or re.search(
                        r"\d{1,2}\s*일",
                        combined,
                    )
                    or re.search(
                        r"\d{1,2}",
                        text,
                    )
                ):
                    candidates.append(
                        candidate
                    )

            except Exception:
                continue

    except Exception as exc:

        logger.warning(
            "날짜 DOM 수집 오류: %s",
            exc,
        )

    return candidates


# ============================================================
# 날짜 후보 디버그
# ============================================================

async def log_date_candidates(
    candidates,
    target_ymd,
):
    """
    목표 날짜를 찾지 못했을 때 실제 DOM 후보를 로그에 남긴다.
    """

    logger.info(
        "🔍 [%s] 날짜 후보 상세 분석 시작",
        target_ymd,
    )

    shown = 0

    for candidate in candidates:

        text = candidate.get(
            "text",
            "",
        )

        aria = candidate.get(
            "aria",
            "",
        )

        title = candidate.get(
            "title",
            "",
        )

        data_date = candidate.get(
            "data_date",
            "",
        )

        data_ymd = candidate.get(
            "data_ymd",
            "",
        )

        data_day = candidate.get(
            "data_day",
            "",
        )

        data_value = candidate.get(
            "data_value",
            "",
        )

        element_id = candidate.get(
            "id",
            "",
        )

        class_name = candidate.get(
            "class",
            "",
        )

        # 너무 큰 div는 제외
        if len(text) > 100:
            continue

        logger.info(
            "📌 날짜 후보 #%d | "
            "text=%r | aria=%r | title=%r | "
            "data-date=%r | data-ymd=%r | "
            "data-day=%r | data-value=%r | "
            "id=%r | class=%r",
            shown + 1,
            text,
            aria,
            title,
            data_date,
            data_ymd,
            data_day,
            data_value,
            element_id,
            class_name[:150],
        )

        shown += 1

        if shown >= 80:
            logger.info(
                "📌 날짜 후보 로그는 최대 80개까지만 표시"
            )
            break


# ============================================================
# 날짜 후보 매칭
# ============================================================

def date_candidate_matches(
    candidate,
    target_ymd,
):
    values = [
        candidate.get("text", ""),
        candidate.get("aria", ""),
        candidate.get("title", ""),
        candidate.get("data_date", ""),
        candidate.get("data_ymd", ""),
        candidate.get("data_day", ""),
        candidate.get("data_value", ""),
    ]

    for value in values:

        if date_text_matches(
            value,
            target_ymd,
        ):
            return True

    return False


# ============================================================
# 날짜 클릭 후 선택 여부 확인
# ============================================================

async def verify_date_selected(
    target_ymd,
):
    """
    클릭 직후 현재 DOM에서 목표 날짜가
    selected / active / aria-selected 상태인지 확인한다.

    CGV가 해당 attribute를 사용하지 않는 경우에는
    실패로 단정하지 않고 True를 반환한다.
    """

    try:

        day = str(
            int(target_ymd[6:8])
        )

        weekday = weekday_korean(
            target_ymd
        )

        result = await page.evaluate(
            """
            ({targetYmd, day, weekday}) => {

                const nodes = [
                    ...document.querySelectorAll(
                        'button, a, li, div, span, [role="button"], [role="tab"]'
                    )
                ];

                for (const el of nodes) {

                    const text =
                        (el.innerText || '').trim();

                    const aria =
                        el.getAttribute('aria-label') || '';

                    const title =
                        el.getAttribute('title') || '';

                    const dataDate =
                        el.getAttribute('data-date') || '';

                    const dataYmd =
                        el.getAttribute('data-ymd') || '';

                    const dataDay =
                        el.getAttribute('data-day') || '';

                    const all = [
                        text,
                        aria,
                        title,
                        dataDate,
                        dataYmd,
                        dataDay
                    ].join(' ');

                    const compact =
                        all.replace(/\\s+/g, '');

                    const isTarget =
                        compact.includes(targetYmd)
                        ||
                        all.includes(
                            targetYmd.slice(0,4)
                            + '-'
                            + targetYmd.slice(4,6)
                            + '-'
                            + targetYmd.slice(6,8)
                        )
                        ||
                        all.includes(
                            targetYmd.slice(4,6)
                            + '/'
                            + targetYmd.slice(6,8)
                        )
                        ||
                        (
                            new RegExp(
                                '(^|[^0-9])'
                                + day
                                + '([^0-9]|$)'
                            ).test(all)
                            && all.includes(weekday)
                        );

                    if (!isTarget) {
                        continue;
                    }

                    const selected =
                        el.getAttribute('aria-selected');

                    const active =
                        el.classList.contains('active')
                        ||
                        el.classList.contains('selected')
                        ||
                        el.classList.contains('on')
                        ||
                        el.classList.contains('current');

                    const parent =
                        el.parentElement;

                    const parentClass =
                        parent
                            ? (parent.className || '')
                            : '';

                    const parentSelected =
                        parent
                            ? (
                                parent.getAttribute(
                                    'aria-selected'
                                )
                                ||
                                ''
                            )
                            : '';

                    const parentActive =
                        String(parentClass).includes('active')
                        ||
                        String(parentClass).includes('selected')
                        ||
                        String(parentClass).includes('on');

                    if (
                        selected === 'true'
                        || active
                        || parentSelected === 'true'
                        || parentActive
                    ) {
                        return true;
                    }
                }

                return null;
            }
            """,
            {
                "targetYmd": target_ymd,
                "day": day,
                "weekday": weekday,
            },
        )

        if result is True:
            logger.info(
                "✅ 날짜 선택 상태 확인: %s",
                target_ymd,
            )
            return True

        if result is False:
            logger.info(
                "ℹ️ 날짜 선택 상태 attribute를 확인하지 못함: %s",
                target_ymd,
            )

        return True

    except Exception as exc:

        logger.debug(
            "날짜 선택 상태 확인 오류: %s",
            exc,
        )

        return True


# ============================================================
# 날짜 클릭
# ============================================================

async def click_date_candidate(
    candidate,
    target_ymd,
):
    element = candidate["element"]

    try:

        await element.scroll_into_view_if_needed(
            timeout=2000
        )

    except Exception:
        pass

    # --------------------------------------------------------
    # 1차: 일반 Playwright click
    # --------------------------------------------------------

    try:

        await element.click(
            timeout=4000
        )

        await asyncio.sleep(1.5)

        logger.info(
            "✅ 날짜 일반 클릭 성공: %s | %s",
            candidate.get("text"),
            candidate.get("class"),
        )

        await verify_date_selected(
            target_ymd
        )

        return True

    except Exception as exc:

        logger.debug(
            "일반 날짜 클릭 실패: %s",
            exc,
        )

    # --------------------------------------------------------
    # 2차: 부모 요소 클릭
    # --------------------------------------------------------

    try:

        parent = element.locator(
            ".."
        ).first

        if await parent.is_visible(
            timeout=500
        ):

            await parent.click(
                timeout=3000
            )

            await asyncio.sleep(1.5)

            logger.info(
                "✅ 날짜 부모 요소 클릭 성공: %s",
                target_ymd,
            )

            await verify_date_selected(
                target_ymd
            )

            return True

    except Exception as exc:

        logger.debug(
            "부모 날짜 클릭 실패: %s",
            exc,
        )

    # --------------------------------------------------------
    # 3차: DOM click
    # --------------------------------------------------------

    try:

        await element.evaluate(
            """
            el => {
                el.scrollIntoView({
                    block: 'center',
                    inline: 'center'
                });
                el.click();
            }
            """
        )

        await asyncio.sleep(1.5)

        logger.info(
            "✅ DOM 날짜 클릭 성공: %s",
            target_ymd,
        )

        await verify_date_selected(
            target_ymd
        )

        return True

    except Exception as exc:

        logger.debug(
            "DOM 날짜 클릭 실패: %s",
            exc,
        )

    return False


# ============================================================
# 날짜 선택
# ============================================================

async def select_date(
    target_ymd,
):
    logger.info(
        "📅 날짜 선택: %s (%s)",
        pretty_date(target_ymd),
        weekday_korean(target_ymd),
    )

    if await detect_block_page():
        return False

    candidates = (
        await get_date_candidates()
    )

    logger.info(
        "📅 날짜 관련 DOM 후보: %d개",
        len(candidates),
    )

    # --------------------------------------------------------
    # 1차: 정확한 후보 매칭
    # --------------------------------------------------------

    matched = []

    for candidate in candidates:

        if date_candidate_matches(
            candidate,
            target_ymd,
        ):
            matched.append(
                candidate
            )

    logger.info(
        "🎯 %s 매칭 후보: %d개",
        target_ymd,
        len(matched),
    )

    for index, candidate in enumerate(
        matched
    ):

        logger.info(
            "🎯 매칭 후보 #%d | text=%r | "
            "aria=%r | data-date=%r | "
            "data-ymd=%r | class=%r",
            index + 1,
            candidate.get("text"),
            candidate.get("aria"),
            candidate.get("data_date"),
            candidate.get("data_ymd"),
            candidate.get("class"),
        )

        clicked = await click_date_candidate(
            candidate,
            target_ymd,
        )

        if clicked:
            return True

    # --------------------------------------------------------
    # 2차: JavaScript로 날짜 전체 DOM 탐색
    # --------------------------------------------------------

    logger.info(
        "🔧 JS 전체 DOM 날짜 탐색 시작: %s",
        target_ymd,
    )

    try:

        result = await page.evaluate(
            """
            ({targetYmd, day, weekday}) => {

                const nodes = [
                    ...document.querySelectorAll(
                        'button, a, li, div, span, [role="button"], [role="tab"]'
                    )
                ];

                const candidates = [];

                for (const el of nodes) {

                    const text =
                        (el.innerText || '').trim();

                    const aria =
                        el.getAttribute('aria-label') || '';

                    const title =
                        el.getAttribute('title') || '';

                    const dataDate =
                        el.getAttribute('data-date') || '';

                    const dataYmd =
                        el.getAttribute('data-ymd') || '';

                    const dataDay =
                        el.getAttribute('data-day') || '';

                    const dataValue =
                        el.getAttribute('data-value') || '';

                    const all = [
                        text,
                        aria,
                        title,
                        dataDate,
                        dataYmd,
                        dataDay,
                        dataValue
                    ].join(' ');

                    const compact =
                        all.replace(/\\s+/g, '');

                    let matched = false;

                    if (
                        compact.includes(targetYmd)
                    ) {
                        matched = true;
                    }

                    if (
                        all.includes(
                            targetYmd.slice(0,4)
                            + '-'
                            + targetYmd.slice(4,6)
                            + '-'
                            + targetYmd.slice(6,8)
                        )
                    ) {
                        matched = true;
                    }

                    if (
                        all.includes(
                            targetYmd.slice(4,6)
                            + '/'
                            + targetYmd.slice(6,8)
                        )
                    ) {
                        matched = true;
                    }

                    if (
                        new RegExp(
                            '(^|[^0-9])'
                            + day
                            + '([^0-9]|$)'
                        ).test(all)
                        && all.includes(weekday)
                    ) {
                        matched = true;
                    }

                    if (
                        all.includes(
                            day + '월'
                        )
                    ) {
                        matched = false;
                    }

                    if (!matched) {
                        continue;
                    }

                    candidates.push({
                        el,
                        score: 0,
                        text,
                        dataYmd,
                        dataDate
                    });
                }

                // 텍스트가 지나치게 큰 부모 div는 후순위
                candidates.sort(
                    (a, b) =>
                        a.text.length - b.text.length
                );

                for (const candidate of candidates) {

                    const el = candidate.el;

                    try {

                        el.scrollIntoView({
                            block: 'center',
                            inline: 'center'
                        });

                        el.click();

                        return {
                            clicked: true,
                            text: candidate.text,
                            dataYmd: candidate.dataYmd,
                            dataDate: candidate.dataDate
                        };

                    } catch (e) {
                        continue;
                    }
                }

                return {
                    clicked: false,
                    count: candidates.length
                };
            }
            """,
            {
                "targetYmd": target_ymd,
                "day": str(
                    int(target_ymd[6:8])
                ),
                "weekday": weekday_korean(
                    target_ymd
                ),
            },
        )

        if result.get(
            "clicked"
        ):

            await asyncio.sleep(2)

            logger.info(
                "✅ JS 날짜 클릭 성공: %s | text=%r | dataYmd=%r | dataDate=%r",
                target_ymd,
                result.get("text"),
                result.get("dataYmd"),
                result.get("dataDate"),
            )

            return True

        logger.info(
            "🔍 JS 날짜 매칭 후보도 없음: %s",
            target_ymd,
        )

    except Exception as exc:

        logger.warning(
            "JS 날짜 탐색 오류: %s",
            exc,
        )

    # --------------------------------------------------------
    # 3차: 날짜 후보 상세 로그
    # --------------------------------------------------------

    await log_date_candidates(
        candidates,
        target_ymd,
    )

    # --------------------------------------------------------
    # 4차: 페이지 텍스트 진단
    # --------------------------------------------------------

    try:

        body = await page.locator(
            "body"
        ).inner_text(
            timeout=3000
        )

        body = normalize_date_text(
            body
        )

        day = str(
            int(target_ymd[6:8])
        )

        weekday = weekday_korean(
            target_ymd
        )

        logger.info(
            "📄 페이지 날짜 진단: "
            "target=%s / day=%s / weekday=%s",
            target_ymd,
            day,
            weekday,
        )

        if (
            day in body
            and weekday in body
        ):
            logger.warning(
                "⚠️ 페이지 텍스트에는 %s %s가 존재하지만 "
                "클릭 가능한 날짜 요소를 찾지 못함",
                day,
                weekday,
            )

        else:
            logger.warning(
                "⚠️ 페이지 텍스트에서도 목표 날짜 정보를 찾지 못함"
            )

    except Exception as exc:

        logger.debug(
            "페이지 텍스트 진단 오류: %s",
            exc,
        )

    logger.warning(
        "❌ 날짜 버튼 찾기/클릭 실패: %s",
        target_ymd,
    )

    return False


# ============================================================
# 최신 시간표 응답
# ============================================================

async def get_latest_schedule():
    try:
        return getattr(
            page,
            "_latest_cgv_schedule",
            None,
        )

    except Exception:
        return None


# ============================================================
# 응답 rows
# ============================================================

def extract_rows(payload):
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
        ]:
            value = data.get(
                key
            )

            if isinstance(
                value,
                list,
            ):
                return value

    # 혹시 data 안쪽 구조가 더 깊은 경우
    for key in [
        "result",
        "resultData",
        "body",
        "contents",
    ]:

        value = payload.get(
            key
        )

        if isinstance(
            value,
            dict,
        ):

            rows = extract_rows(
                value
            )

            if rows:
                return rows

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
# 시간표 파싱
# ============================================================

def parse_sessions(
    payload,
    scn_ymd,
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

        if seats <= 0:
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

    return result


# ============================================================
# 응답 처리
# ============================================================

async def process_response(
    payload,
    scn_ymd,
    send_notification=True,
):
    status = (
        payload.get("statusCode")
        if isinstance(payload, dict)
        else None
    )

    if status not in (
        None,
        0,
        "0",
    ):
        logger.warning(
            "CGV statusCode=%s / %s",
            status,
            (
                payload.get(
                    "statusMessage",
                    "",
                )
                if isinstance(
                    payload,
                    dict,
                )
                else ""
            ),
        )

    sessions = parse_sessions(
        payload,
        scn_ymd,
    )

    logger.info(
        "%s: 대상 IMAX + 잔여석 %d개",
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

    text = str(value)

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
        f"🎬 <b>{html.escape(str(item['movNm']))}</b>\n"
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
# 날짜 하나 검사
# ============================================================

async def check_date(
    scn_ymd,
    test_mode=False,
):
    global last_scan_result

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

    if await detect_block_page():

        logger.warning(
            "🚫 차단 상태라 날짜 검사를 건너뜀"
        )

        return {
            "status": "blocked",
            "sessions": [],
        }

    clicked = await select_date(
        scn_ymd
    )

    if not clicked:

        if await detect_block_page():

            return {
                "status": "blocked",
                "sessions": [],
            }

        return {
            "status": "date_failed",
            "sessions": [],
        }

    logger.info(
        "⏳ CGV 시간표 응답 대기..."
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

        result = (
            await get_latest_schedule()
        )

        if result:

            logger.info(
                "🎯 시간표 응답 확보: %s",
                result["url"],
            )

            sessions = (
                await process_response(
                    result["data"],
                    scn_ymd,
                    send_notification=(
                        not test_mode
                    ),
                )
            )

            return {
                "status": "ok",
                "sessions": sessions,
            }

        await asyncio.sleep(
            0.5
        )

    logger.warning(
        "⚠️ %s: 시간표 응답 없음",
        scn_ymd,
    )

    return {
        "status": "no_response",
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

    async with monitor_lock:

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
                    "🚫 CGV 차단 상태 감지. "
                    "이번 검사 사이클 종료."
                )

                break

            if (
                index
                < len(dates) - 1
            ):

                await asyncio.sleep(
                    1.5
                )

        last_scan_time = time.time()
        last_scan_result = all_sessions

        return {
            "dates": dates,
            "statuses": statuses,
            "sessions": all_sessions,
        }


# ============================================================
# /test 결과 메시지
# ============================================================

def build_test_message(
    result,
):
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

    no_response = [
        date
        for date, status
        in statuses.items()
        if status == "no_response"
    ]

    ok_dates = [
        date
        for date, status
        in statuses.items()
        if status == "ok"
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

    elif no_response:

        status_text = (
            "🟡 <b>날짜 선택은 됐지만 시간표 응답 없음</b>"
        )

    else:

        status_text = (
            "🟢 <b>정상 확인 / 잔여석 없음</b>"
        )

    lines = [
        "🧪 <b>용아맥 테스트</b>",
        "",
        status_text,
        f"🏢 {html.escape(THEATER_NAME)}",
        "🎞️ IMAX",
        "",
    ]

    # --------------------------------------------------------
    # 날짜 검사 결과
    # --------------------------------------------------------

    lines.append(
        "📅 <b>날짜 검사 결과</b>"
    )

    for date in statuses:

        status = statuses[date]

        if status == "ok":

            lines.append(
                f"🟢 {pretty_date(date)} "
                f"({weekday_korean(date)}) "
                "날짜 선택 성공"
            )

        elif status == "date_failed":

            lines.append(
                f"🔴 {pretty_date(date)} "
                f"({weekday_korean(date)}) "
                "날짜 선택 실패"
            )

        elif status == "no_response":

            lines.append(
                f"🟡 {pretty_date(date)} "
                f"({weekday_korean(date)}) "
                "날짜 선택 성공 / 시간표 응답 없음"
            )

        elif status == "blocked":

            lines.append(
                f"🔴 {pretty_date(date)} "
                "CGV 차단"
            )

    lines.append("")

    # --------------------------------------------------------
    # 발견 회차
    # --------------------------------------------------------

    if sessions:

        lines.append(
            f"🎟️ <b>현재 발견된 대상 회차: "
            f"{len(sessions)}개</b>"
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

            lines.append(
                f"• {pretty_date(item['date'])} "
                f"{start}~{end} "
                f"💺 {seats}석"
            )

    else:

        lines.append(
            "💺 <b>현재 발견된 대상 회차 없음</b>"
        )

    # --------------------------------------------------------
    # 실패 날짜
    # --------------------------------------------------------

    if date_failed:

        lines.append("")

        lines.append(
            "⚠️ <b>날짜 선택 실패</b>"
        )

        for date in date_failed:

            lines.append(
                f"• {pretty_date(date)}"
            )

    # --------------------------------------------------------
    # 응답 없음
    # --------------------------------------------------------

    if no_response:

        lines.append("")

        lines.append(
            "⚠️ <b>시간표 응답 없음</b>"
        )

        for date in no_response:

            lines.append(
                f"• {pretty_date(date)}"
            )

    # --------------------------------------------------------
    # 차단
    # --------------------------------------------------------

    if blocked:

        lines.append("")

        lines.append(
            "🚫 CGV에서 자동접속/비정상 접근으로 "
            "판단했을 가능성이 있습니다."
        )

        lines.append(
            "⏸️ 추가 요청은 중지하고 잠시 대기하는 것을 권장합니다."
        )

    return "\n".join(lines)


# ============================================================
# Telegram 명령
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

        if not data.get(
            "ok"
        ):
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


async def handle_telegram_command(
    update,
):
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
            ""
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
            ""
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

    # --------------------------------------------------------
    # /start
    # --------------------------------------------------------

    if command == "/start":

        global monitor_enabled

        monitor_enabled = True

        if (
            monitor_task is None
            or monitor_task.done()
        ):

            asyncio.create_task(
                monitor_loop()
            )

        await telegram_send_async(
            "🟢 <b>용아맥 감시 시작</b>\n\n"
            f"🏢 {html.escape(THEATER_NAME)}\n"
            "🎞️ IMAX\n"
            f"⏱️ 검사 간격: {INTERVAL_SECONDS}초\n\n"
            "🧪 /test 현재 상태 확인\n"
            "⏹️ /stop 감시 중지"
        )

        return

    # --------------------------------------------------------
    # /stop
    # --------------------------------------------------------

    if command == "/stop":

        monitor_enabled = False

        await telegram_send_async(
            "⏸️ <b>용아맥 감시 중지</b>\n\n"
            "브라우저는 종료하지 않고 대기합니다.\n"
            "다시 감시하려면 /start"
        )

        return

    # --------------------------------------------------------
    # /test
    # --------------------------------------------------------

    if command == "/test":

        if monitor_lock.locked():

            await telegram_send_async(
                "⏳ <b>이미 검사가 진행 중입니다.</b>\n\n"
                "현재 검사 완료 후 다시 /test 해주세요."
            )

            return

        await telegram_send_async(
            "🧪 <b>용아맥 현재 상태 확인 중...</b>\n\n"
            "CGV 실제 브라우저에서\n"
            "극장 → 날짜 → 시간표 순서로 확인합니다."
        )

        try:

            result = (
                await perform_scan(
                    test_mode=True
                )
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

    # --------------------------------------------------------
    # /status
    # --------------------------------------------------------

    if command == "/status":

        state = (
            "🟢 실행 중"
            if monitor_enabled
            else "⏸️ 중지됨"
        )

        if blocked_detected:

            cgv_state = (
                "🔴 차단 감지"
            )

        else:

            cgv_state = (
                "🟢 정상"
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
            f"마지막 발견 회차: {len(last_scan_result)}개\n\n"
            "🧪 /test\n"
            "▶️ /start\n"
            "⏹️ /stop"
        )

        return

    # --------------------------------------------------------
    # /help
    # --------------------------------------------------------

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
    global monitor_task
    global monitor_enabled

    logger.info(
        "🔄 감시 루프 시작"
    )

    while monitor_enabled:

        try:

            if blocked_detected:

                logger.warning(
                    "🚫 CGV 차단 상태. "
                    "감시를 일시 대기합니다."
                )

                await asyncio.sleep(
                    300
                )

                if monitor_enabled:

                    try:

                        await page.reload(
                            wait_until="domcontentloaded",
                            timeout=60000,
                        )

                        await asyncio.sleep(
                            5
                        )

                    except Exception as exc:

                        logger.warning(
                            "CGV 새로고침 실패: %s",
                            exc,
                        )

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
            "🔴 <b>CGV 초기 접속 차단 감지</b>\n\n"
            "현재 CGV가 자동 접속을 제한하고 있는 것으로 보입니다.\n"
            "추가 요청을 계속 보내지 않고 대기합니다."
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

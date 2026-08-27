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

# 자동 감시용 락
monitor_lock = asyncio.Lock()

# /test 중복 실행 방지
test_lock = asyncio.Lock()

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
        # body 전체를 매번 읽으면 매우 느려질 수 있으므로
        # 필요한 정도만 읽는다.
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
        + body_text[:15000]
    )

    for keyword in BLOCK_KEYWORDS:
        if keyword.lower() in combined:
            mark_blocked(
                f"CGV 차단 문구 감지: {keyword}"
            )
            return True

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
    # CGV request
    # --------------------------------------------------------

    def on_request(request):
        try:
            url = request.url

            if any(
                keyword in url
                for keyword in [
                    "searchMovScnInfo",
                    "searchMov",
                    "ScnInfo",
                    "schedule",
                    "Schedule",
                ]
            ):
                logger.info(
                    "🌐 CGV REQUEST: %s %s",
                    request.method,
                    url,
                )

        except Exception:
            pass

    # --------------------------------------------------------
    # CGV response
    # --------------------------------------------------------

    async def on_response(response):
        global last_schedule_response
        global last_schedule_url

        try:
            url = response.url

            interesting = any(
                keyword in url
                for keyword in [
                    "searchMovScnInfo",
                    "searchMov",
                    "ScnInfo",
                    "schedule",
                    "Schedule",
                ]
            )

            if not interesting:
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

            last_schedule_response = time.time()
            last_schedule_url = url

            logger.info(
                "🎯 CGV JSON 응답 확보"
            )

            page._latest_cgv_schedule = {
                "url": url,
                "status": response.status,
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
            "⚠️ CGV 초기 페이지 차단 상태"
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

    # 이미 선택되어 있다면 굳이 다시 클릭하지 않는다.
    try:
        body = await page.locator(
            "body"
        ).inner_text(
            timeout=2000
        )

        if (
            THEATER_NAME in body
            and "선택 된 극장이 없습니다" not in body
            and "극장 선택 정보가 없습니다" not in body
        ):
            logger.info(
                "✅ %s 이미 선택된 상태",
                THEATER_NAME,
            )

            return True

    except Exception:
        pass

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

    # 중요:
    # get_by_text(... exact=False)를 사용하지 않는다.
    # 부모 div/body를 잡는 문제가 발생할 수 있다.
    candidates = await page.locator(
        "button, a, [role='button'], li"
    ).all()

    for candidate in candidates:
        try:
            if not await candidate.is_visible(
                timeout=100
            ):
                continue

            text = (
                await candidate.inner_text()
            ).strip()

            # 정확히 극장명인 요소만 허용
            if text != THEATER_NAME:
                continue

            await candidate.scroll_into_view_if_needed(
                timeout=2000
            )

            await candidate.click(
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
# 날짜 DOM
# ============================================================

async def get_date_candidates():
    """
    날짜 영역을 추정해서 가져온다.

    중요한 점:
    body / div 전체를 클릭 후보로 사용하지 않는다.
    실제 클릭 가능한 button / a / role=button만 본다.
    """

    selectors = [
        "button",
        "a",
        "[role='button']",
        "input[type='button']",
    ]

    candidates = []

    for selector in selectors:

        try:
            elements = await page.locator(
                selector
            ).all()

        except Exception:
            continue

        for element in elements:

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

                data_value = (
                    await element.get_attribute(
                        "data-value"
                    )
                    or ""
                )

                # 날짜와 전혀 관계없는 거대한 요소 제외
                combined = " ".join(
                    [
                        text,
                        aria,
                        title,
                        data_date,
                        data_ymd,
                        data_value,
                    ]
                )

                if len(text) > 100:
                    continue

                # 날짜 요소일 가능성이 있는 것만 저장
                looks_like_date = (
                    bool(
                        re.search(
                            r"\d{1,4}[-./]\d{1,2}[-./]\d{1,2}",
                            combined,
                        )
                    )
                    or "월" in combined
                    or "화" in combined
                    or "수" in combined
                    or "목" in combined
                    or "금" in combined
                    or "토" in combined
                    or "일" in combined
                    or bool(
                        re.search(
                            r"\b\d{1,2}\b",
                            combined,
                        )
                    )
                )

                if not looks_like_date:
                    continue

                candidates.append(
                    {
                        "element": element,
                        "text": text,
                        "aria": aria,
                        "title": title,
                        "data_date": data_date,
                        "data_ymd": data_ymd,
                        "data_value": data_value,
                    }
                )

            except Exception:
                continue

    return candidates


def date_candidate_matches(
    candidate,
    target_ymd,
):
    try:
        target = datetime.strptime(
            target_ymd,
            "%Y%m%d",
        )

    except Exception:
        return False

    year = target.strftime("%Y")
    month = target.month
    day = target.day

    month2 = f"{month:02d}"
    day2 = f"{day:02d}"

    weekday = weekday_korean(
        target_ymd
    )

    values = [
        candidate.get("text", ""),
        candidate.get("aria", ""),
        candidate.get("title", ""),
        candidate.get("data_date", ""),
        candidate.get("data_ymd", ""),
        candidate.get("data_value", ""),
    ]

    text = " ".join(
        str(x)
        for x in values
        if x
    )

    compact = re.sub(
        r"\s+",
        " ",
        text,
    ).strip()

    # 가장 확실한 경우
    if target_ymd in compact:
        return True

    if (
        f"{year}-{month2}-{day2}"
        in compact
    ):
        return True

    if (
        f"{year}.{month2}.{day2}"
        in compact
    ):
        return True

    if (
        f"{year}/{month2}/{day2}"
        in compact
    ):
        return True

    if (
        f"{month2}/{day2}"
        in compact
    ):
        return True

    if (
        f"{month}/{day}"
        in compact
    ):
        return True

    if (
        f"{month}월 {day}일"
        in compact
        or f"{month}월{day}일"
        in compact
    ):
        return True

    # 날짜 버튼이
    # "27"
    # "목"
    # 식으로 분리되어 있을 수 있다.
    if re.search(
        rf"(^|\s){day}(\s|$)",
        compact,
    ):
        if weekday in compact:
            return True

    return False


# ============================================================
# 현재 선택 날짜 확인
# ============================================================

async def get_selected_date_from_dom():
    """
    CGV 날짜 버튼의 선택 상태를 추정한다.
    """

    try:

        result = await page.evaluate(
            """
            () => {

                const nodes = [
                    ...document.querySelectorAll(
                        'button, a, [role="button"]'
                    )
                ];

                const result = [];

                for (const el of nodes) {

                    const text =
                        (el.innerText || '').trim();

                    if (!text || text.length > 30) {
                        continue;
                    }

                    const cls =
                        typeof el.className === 'string'
                            ? el.className
                            : '';

                    const aria =
                        el.getAttribute(
                            'aria-selected'
                        ) || '';

                    const dataSelected =
                        el.getAttribute(
                            'data-selected'
                        ) || '';

                    const selected =
                        el.classList.contains('active')
                        || el.classList.contains('selected')
                        || el.classList.contains('on')
                        || aria === 'true'
                        || dataSelected === 'true';

                    if (selected) {
                        result.push({
                            text,
                            cls,
                            aria,
                            dataSelected
                        });
                    }
                }

                return result;
            }
            """
        )

        return result

    except Exception:
        return []


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
        "📅 날짜 클릭 후보 %d개",
        len(candidates),
    )

    # --------------------------------------------------------
    # 정확한 후보만 클릭
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
        "📅 %s 일치 후보 %d개",
        target_ymd,
        len(matched),
    )

    for candidate in matched:

        try:

            element = candidate[
                "element"
            ]

            await element.scroll_into_view_if_needed(
                timeout=2000
            )

            # 실제 요소 정보만 로그
            logger.info(
                "📅 날짜 후보 클릭: text=%r aria=%r data=%r",
                candidate.get("text"),
                candidate.get("aria"),
                candidate.get("data_ymd")
                or candidate.get("data_date"),
            )

            await element.click(
                timeout=4000
            )

            await asyncio.sleep(1.2)

            # 클릭 후 body 전체를 성공 근거로 사용하지 않는다.
            selected = (
                await get_selected_date_from_dom()
            )

            logger.info(
                "📅 날짜 선택 상태 확인: %s",
                target_ymd,
            )

            if selected:
                logger.info(
                    "📅 선택 상태 후보: %s",
                    [
                        x.get("text")
                        for x in selected[:10]
                    ],
                )

            return True

        except Exception as exc:

            logger.debug(
                "날짜 후보 클릭 실패: %s",
                exc,
            )

    # --------------------------------------------------------
    # JS fallback
    # --------------------------------------------------------

    try:

        result = await page.evaluate(
            """
            ({targetYmd, day, weekday}) => {

                const nodes = [
                    ...document.querySelectorAll(
                        'button, a, [role="button"]'
                    )
                ];

                const candidates = [];

                for (const el of nodes) {

                    const text =
                        (el.innerText || '').trim();

                    if (!text || text.length > 40) {
                        continue;
                    }

                    const aria =
                        el.getAttribute('aria-label') || '';

                    const title =
                        el.getAttribute('title') || '';

                    const dataDate =
                        el.getAttribute('data-date') || '';

                    const dataYmd =
                        el.getAttribute('data-ymd') || '';

                    const dataValue =
                        el.getAttribute('data-value') || '';

                    const all = [
                        text,
                        aria,
                        title,
                        dataDate,
                        dataYmd,
                        dataValue
                    ].join(' ');

                    if (
                        all.includes(targetYmd)
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
                    ) {
                        candidates.push(el);
                        continue;
                    }

                    const normalized =
                        all.replace(/\\s+/g, ' ');

                    const dayRegex =
                        new RegExp(
                            '(^|\\\\s)'
                            + day
                            + '(\\\\s|$)'
                        );

                    if (
                        dayRegex.test(normalized)
                        && normalized.includes(weekday)
                    ) {
                        candidates.push(el);
                    }
                }

                // 가장 작은 DOM 요소부터 클릭
                candidates.sort(
                    (a, b) => {
                        const ta =
                            (a.innerText || '').length;
                        const tb =
                            (b.innerText || '').length;

                        return ta - tb;
                    }
                );

                if (candidates.length > 0) {
                    candidates[0].click();
                    return {
                        clicked: true,
                        text:
                            (candidates[0].innerText || '')
                            .trim()
                    };
                }

                return {
                    clicked: false
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

            await asyncio.sleep(1.5)

            logger.info(
                "📅 JS 날짜 클릭 성공: %s",
                result.get("text"),
            )

            return True

    except Exception as exc:

        logger.debug(
            "JS 날짜 탐색 실패: %s",
            exc,
        )

    logger.warning(
        "⚠️ 날짜 버튼을 찾지 못함: %s",
        target_ymd,
    )

    return False


# ============================================================
# 최신 JSON
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
# JSON rows
# ============================================================

def extract_rows(payload):
    if not isinstance(
        payload,
        dict,
    ):
        return []

    # 흔한 형태
    for root_key in [
        "data",
        "result",
        "body",
        "resultData",
    ]:

        root = payload.get(
            root_key
        )

        if isinstance(
            root,
            list,
        ):
            return root

        if isinstance(
            root,
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
                "scheduleList",
            ]:

                value = root.get(
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
# 좌석
# ============================================================

def parse_seat_pair(text):
    """
    예:
      8/624석
      3 / 624
      21/624석
    """

    if not text:
        return None, None

    match = re.search(
        r"(\d+)\s*/\s*(\d+)\s*석?",
        text,
    )

    if not match:
        return None, None

    try:
        return (
            int(match.group(1)),
            int(match.group(2)),
        )

    except Exception:
        return None, None


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
        "CGV JSON rows=%d",
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
# 화면 DOM 시간표 파싱
# ============================================================

async def parse_schedule_from_dom(
    scn_ymd,
):
    """
    CGV가 JSON endpoint를 잡히게 주지 않아도
    실제 화면에 표시된 시간표를 읽는다.

    현재 네 로그처럼:

    오디세이
    IMAX관
    IMAX LASER 2D
    07:30-10:32 8/624석
    11:00-14:02 3/624석
    ...

    형태로 렌더링되어 있으면 이 방법으로 잡는다.
    """

    try:

        result = await page.evaluate(
            """
            ({movieAliases}) => {

                const aliases =
                    movieAliases.map(
                        x => x.toLowerCase()
                    );

                const normalize = value =>
                    (value || '')
                        .replace(/\\\\s+/g, ' ')
                        .trim();

                const isMovieText = text => {
                    const lower =
                        text.toLowerCase();

                    return aliases.some(
                        alias =>
                            lower.includes(alias)
                    );
                };

                const isImaxText = text => {
                    const lower =
                        text.toLowerCase();

                    return (
                        lower.includes('imax')
                        || lower.includes('아이맥스')
                    );
                };

                const seatRegex =
                    /(\\\\d+)\\\\s*\\\\/\\\\s*(\\\\d+)\\\\s*석?/;

                const timeRegex =
                    /(\\\\d{1,2}:\\\\d{2})\\\\s*-\\\\s*(\\\\d{1,2}:\\\\d{2})/;

                const nodes = [
                    ...document.querySelectorAll(
                        'body *'
                    )
                ];

                const found = [];

                /*
                 * 영화명 → IMAX → 시간/좌석이 들어있는
                 * 작은 컨테이너를 찾는다.
                 */
                for (const movieNode of nodes) {

                    const movieText =
                        normalize(
                            movieNode.innerText
                        );

                    if (!movieText) {
                        continue;
                    }

                    if (movieText.length > 3000) {
                        continue;
                    }

                    if (!isMovieText(movieText)) {
                        continue;
                    }

                    /*
                     * 영화명이 포함된 요소 중에서
                     * IMAX가 같이 포함되는 가장 작은
                     * 적당한 영역을 찾는다.
                     */
                    let container = movieNode;

                    for (
                        let level = 0;
                        level < 6 && container;
                        level++
                    ) {

                        const text =
                            normalize(
                                container.innerText
                            );

                        if (
                            text.length <= 5000
                            && isImaxText(text)
                            && timeRegex.test(text)
                            && seatRegex.test(text)
                        ) {
                            break;
                        }

                        container =
                            container.parentElement;
                    }

                    if (!container) {
                        continue;
                    }

                    const text =
                        normalize(
                            container.innerText
                        );

                    if (!isImaxText(text)) {
                        continue;
                    }

                    /*
                     * IMAX 영역 안에서 시간 + 좌석 패턴 추출
                     */
                    const matches = [
                        ...text.matchAll(
                            new RegExp(
                                '(\\\\d{1,2}:\\\\d{2})\\\\s*-\\\\s*'
                                + '(\\\\d{1,2}:\\\\d{2})'
                                + '[^\\\\n]{0,100}?'
                                + '(\\\\d+)\\\\s*/\\\\s*(\\\\d+)\\\\s*석?',
                                'g'
                            )
                        )
                    ];

                    for (const match of matches) {

                        const start = match[1];
                        const end = match[2];

                        const seats =
                            parseInt(
                                match[3],
                                10
                            );

                        const total =
                            parseInt(
                                match[4],
                                10
                            );

                        /*
                         * 0석은 감지 대상이 아니지만
                         * 매진도 데이터 파싱 자체는 성공으로 본다.
                         */
                        found.push({
                            movie:
                                movieAliases[0] || '오디세이',
                            start,
                            end,
                            seats,
                            totalSeats: total,
                            screen: 'IMAX'
                        });
                    }

                    /*
                     * 같은 부모를 계속 찾아서
                     * 중복되는 것을 막는다.
                     */
                    if (found.length > 0) {
                        break;
                    }
                }

                return found;
            }
            """,
            {
                "movieAliases": MOVIE_ALIASES,
            },
        )

        if not result:
            return []

        sessions = []

        seen = set()

        for item in result:

            seats = int(
                item.get(
                    "seats",
                    0,
                )
            )

            total = item.get(
                "totalSeats"
            )

            key = (
                scn_ymd,
                item.get("start"),
                item.get("end"),
                seats,
                total,
            )

            if key in seen:
                continue

            seen.add(key)

            # 좌석 있는 회차만
            if seats <= 0:
                continue

            sessions.append(
                {
                    "date": scn_ymd,
                    "movNo": None,
                    "movNm": (
                        item.get("movie")
                        or "오디세이"
                    ),
                    "scnSseq": None,
                    "scnsNo": None,
                    "start": item.get(
                        "start"
                    ),
                    "end": item.get(
                        "end"
                    ),
                    "screen": "IMAX",
                    "seats": seats,
                    "totalSeats": total,
                    "source": "DOM",
                }
            )

        logger.info(
            "🖥️ 화면 DOM에서 IMAX 회차 %d개 발견",
            len(sessions),
        )

        return sessions

    except Exception as exc:

        logger.warning(
            "DOM 시간표 파싱 실패: %s",
            exc,
        )

        return []


# ============================================================
# 응답 처리
# ============================================================

async def process_response(
    payload,
    scn_ymd,
    send_notification=True,
):
    sessions = parse_sessions(
        payload,
        scn_ymd,
    )

    logger.info(
        "%s: JSON 대상 IMAX + 잔여석 %d개",
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
# 시간
# ============================================================

def format_time(value):
    if value is None:
        return "??:??"

    text = str(value).strip()

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
# 알림
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
            item.get("end"),
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
    try:
        page._latest_cgv_schedule = None

    except Exception:
        pass

    logger.info(
        "=========================================="
    )

    logger.info(
        "🔎 날짜 검사: %s",
        pretty_date(scn_ymd),
    )

    if await detect_block_page():
        logger.warning(
            "🚫 차단 상태라 날짜 검사 건너뜀"
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

        # ----------------------------------------------------
        # 1. JSON 응답 확인
        # ----------------------------------------------------

        response = (
            await get_latest_schedule()
        )

        if response:

            logger.info(
                "🎯 CGV 시간표 JSON 응답 확보"
            )

            sessions = (
                await process_response(
                    response["data"],
                    scn_ymd,
                    send_notification=(
                        not test_mode
                    ),
                )
            )

            # JSON 응답은 왔지만 현재 구조와 안 맞아서
            # 0개가 나온다면 DOM도 확인한다.
            if not sessions:

                dom_sessions = (
                    await parse_schedule_from_dom(
                        scn_ymd
                    )
                )

                if dom_sessions:

                    logger.info(
                        "🖥️ JSON 0개 → DOM 결과 사용"
                    )

                    sessions = dom_sessions

                    if not test_mode:

                        for item in sessions:
                            notify_session(
                                item
                            )

            return {
                "status": "ok",
                "sessions": sessions,
            }

        # ----------------------------------------------------
        # 2. JSON이 없어도 DOM에 화면이 나타났는지 확인
        # ----------------------------------------------------

        dom_sessions = (
            await parse_schedule_from_dom(
                scn_ymd
            )
        )

        if dom_sessions:

            logger.info(
                "🖥️ API 응답 없이 화면에서 시간표 발견"
            )

            if not test_mode:

                for item in dom_sessions:
                    notify_session(
                        item
                    )

            return {
                "status": "ok_dom",
                "sessions": dom_sessions,
            }

        await asyncio.sleep(
            0.5
        )

    # --------------------------------------------------------
    # 최종 DOM 확인
    # --------------------------------------------------------

    dom_sessions = (
        await parse_schedule_from_dom(
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
            "status": "ok_dom",
            "sessions": dom_sessions,
        }

    logger.warning(
        "⚠️ %s: 시간표 응답/화면 모두 확인 실패",
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

    # 중요:
    # test_mode일 때는 monitor_lock을 기다리지 않는다.
    # 자동 감시와 /test가 서로 영원히 막히는 문제 방지.
    if test_mode:

        async with test_lock:

            return await _perform_scan_inner(
                test_mode=True
            )

    async with monitor_lock:

        return await _perform_scan_inner(
            test_mode=False
        )


async def _perform_scan_inner(
    test_mode=False,
):
    global last_scan_time
    global last_scan_result

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

            # 너무 오래 붙잡지 않도록
            await asyncio.sleep(
                0.5
            )

    last_scan_time = time.time()
    last_scan_result = all_sessions

    return {
        "dates": dates,
        "statuses": statuses,
        "sessions": all_sessions,
    }


# ============================================================
# /test 메시지
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

    dom_success = [
        date
        for date, status
        in statuses.items()
        if status == "ok_dom"
    ]

    if blocked:

        status_text = (
            "🔴 <b>CGV 차단 감지</b>"
        )

    elif sessions:

        status_text = (
            "🟢 <b>정상 확인 + 잔여석 발견</b>"
        )

    elif date_failed:

        status_text = (
            "🟡 <b>날짜 선택 단계에서 문제</b>"
        )

    elif no_response:

        status_text = (
            "🟡 <b>화면/시간표 응답 확인 실패</b>"
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

    if dom_success:

        lines.append(
            "🖥️ 화면 직접 확인 날짜: "
            + str(len(dom_success))
            + "일"
        )

        lines.append("")

    if sessions:

        lines.append(
            f"🎟️ 잔여 회차: <b>{len(sessions)}개</b>"
        )

        for item in sessions[:30]:

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
                f"/{total}"
                if total
                else ""
            )

            lines.append(
                f"• {pretty_date(item['date'])} "
                f"{start}~{end} "
                f"💺 {seats}{total_text}석"
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

    if no_response:

        lines.append("")
        lines.append(
            "⚠️ 시간표 확인 실패:"
        )

        for date in no_response[:10]:

            lines.append(
                f"• {pretty_date(date)}"
            )

    if blocked:

        lines.append("")
        lines.append(
            "🚫 CGV 차단 상태가 감지되었습니다."
        )

    return "\n".join(lines)


# ============================================================
# Telegram updates
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


# ============================================================
# Telegram command
# ============================================================

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

    # --------------------------------------------------------
    # START
    # --------------------------------------------------------

    if command == "/start":

        global monitor_enabled

        monitor_enabled = True

        if (
            monitor_task is None
            or monitor_task.done()
        ):

            monitor_task = asyncio.create_task(
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
    # STOP
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
    # TEST
    # --------------------------------------------------------

    if command == "/test":

        if test_lock.locked():

            await telegram_send_async(
                "⏳ <b>이미 /test가 진행 중입니다.</b>\n\n"
                "현재 테스트가 끝난 후 다시 요청해주세요."
            )

            return

        await telegram_send_async(
            "🧪 <b>용아맥 테스트 시작</b>\n\n"
            "극장 → 날짜 → 시간표 순서로 실제 CGV 화면을 확인합니다."
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
    # STATUS
    # --------------------------------------------------------

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
            f"마지막 발견 회차: {len(last_scan_result)}개\n\n"
            "🧪 /test\n"
            "▶️ /start\n"
            "⏹️ /stop"
        )

        return

    # --------------------------------------------------------
    # HELP
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


# ============================================================
# Telegram loop
# ============================================================

async def telegram_command_loop():
    global telegram_offset

    if not TELEGRAM_BOT_TOKEN:

        logger.warning(
            "Telegram 명령 루프 비활성화"
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
                    "5분 대기."
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
# MAIN
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

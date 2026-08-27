import asyncio
import html
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

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
    30,
    int(
        os.getenv(
            "INTERVAL_SECONDS",
            "30",
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

BROWSER_HEADLESS = (
    os.getenv(
        "BROWSER_HEADLESS",
        "true",
    ).lower()
    in ("1", "true", "yes", "y")
)

# ------------------------------------------------------------
# 로그인 세션 보존
#
# Railway에서 Persistent Volume을 연결했다면
# 이 디렉터리를 계속 유지할 수 있다.
# ------------------------------------------------------------

PLAYWRIGHT_USER_DATA_DIR = os.getenv(
    "PLAYWRIGHT_USER_DATA_DIR",
    "/tmp/cgv-browser",
).strip()

# ------------------------------------------------------------
# 저장된 storage_state 사용 가능
# ------------------------------------------------------------

STORAGE_STATE_PATH = os.getenv(
    "STORAGE_STATE_PATH",
    "",
).strip()

# ------------------------------------------------------------
# 발견 즉시 회차를 자동 클릭할지
# ------------------------------------------------------------

AUTO_OPEN_SESSION = (
    os.getenv(
        "AUTO_OPEN_SESSION",
        "true",
    ).lower()
    in ("1", "true", "yes", "y")
)

# ------------------------------------------------------------
# 회차 클릭 후 몇 초 동안 상태 확인
# ------------------------------------------------------------

SESSION_OPEN_WAIT = max(
    2,
    int(
        os.getenv(
            "SESSION_OPEN_WAIT",
            "5",
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

monitor_task = None
telegram_task = None

monitor_enabled = True

monitor_lock = asyncio.Lock()

seen_sessions = {}

last_scan_time = 0.0
last_scan_result = []

blocked_detected = False
blocked_message = ""

telegram_offset = 0

opened_session_key = None


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
        return False

    if not TELEGRAM_CHAT_ID:
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

        return response.status_code == 200

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
# 차단 감지
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
    "captcha",
    "robot",
    "자동화된 접근",
]


async def detect_block_page():

    global blocked_detected
    global blocked_message

    if not page:
        return False

    try:
        title = (
            await page.title()
        ).lower()
    except Exception:
        title = ""

    try:
        body = (
            await page.locator(
                "body"
            ).inner_text(
                timeout=3000
            )
        ).lower()
    except Exception:
        body = ""

    combined = (
        title
        + "\n"
        + body[:20000]
    )

    for keyword in BLOCK_KEYWORDS:

        if keyword.lower() in combined:

            blocked_detected = True
            blocked_message = keyword

            logger.error(
                "🚫 CGV 보안/차단 상태 감지: %s",
                keyword,
            )

            return True

    return False


# ============================================================
# CGV URL
# ============================================================

def build_cgv_url():

    return (
        "https://cgv.co.kr/cnm/movieBook/cinema"
        f"?siteNm={THEATER_NAME}"
        f"&siteNo={THEATER_CODE}"
    )


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

    Path(
        PLAYWRIGHT_USER_DATA_DIR
    ).mkdir(
        parents=True,
        exist_ok=True,
    )

    playwright = (
        await async_playwright()
        .start()
    )

    browser_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
    ]

    # --------------------------------------------------------
    # Persistent context
    # 로그인 쿠키/세션을 브라우저 프로필에 저장
    # --------------------------------------------------------

    if STORAGE_STATE_PATH:

        storage_file = Path(
            STORAGE_STATE_PATH
        )

        if storage_file.exists():

            logger.info(
                "저장된 CGV 세션 사용: %s",
                STORAGE_STATE_PATH,
            )

            browser = await playwright.chromium.launch(
                headless=BROWSER_HEADLESS,
                args=browser_args,
            )

            context = (
                await browser.new_context(
                    locale="ko-KR",
                    timezone_id="Asia/Seoul",
                    viewport={
                        "width": 1365,
                        "height": 900,
                    },
                    storage_state=str(
                        storage_file
                    ),
                )
            )

            page = await context.new_page()

        else:

            logger.warning(
                "STORAGE_STATE_PATH 파일 없음: %s",
                STORAGE_STATE_PATH,
            )

            context = (
                await playwright.chromium.launch_persistent_context(
                    user_data_dir=PLAYWRIGHT_USER_DATA_DIR,
                    headless=BROWSER_HEADLESS,
                    locale="ko-KR",
                    timezone_id="Asia/Seoul",
                    viewport={
                        "width": 1365,
                        "height": 900,
                    },
                    args=browser_args,
                )
            )

            browser = context.browser
            page = context.pages[0] if context.pages else await context.new_page()

    else:

        context = (
            await playwright.chromium.launch_persistent_context(
                user_data_dir=PLAYWRIGHT_USER_DATA_DIR,
                headless=BROWSER_HEADLESS,
                locale="ko-KR",
                timezone_id="Asia/Seoul",
                viewport={
                    "width": 1365,
                    "height": 900,
                },
                args=browser_args,
            )
        )

        browser = context.browser

        if context.pages:
            page = context.pages[0]
        else:
            page = await context.new_page()

    logger.info(
        "CGV 예매 페이지 접속"
    )

    try:

        await page.goto(
            build_cgv_url(),
            wait_until="domcontentloaded",
            timeout=60000,
        )

    except Exception as exc:

        logger.warning(
            "CGV 이동 예외: %s",
            exc,
        )

    await asyncio.sleep(5)

    logger.info(
        "현재 URL: %s",
        page.url,
    )

    logger.info(
        "페이지 제목: %s",
        await page.title(),
    )

    if await detect_block_page():

        logger.warning(
            "⚠️ CGV 보안 페이지 감지"
        )


# ============================================================
# 로그인 상태 확인
# ============================================================

async def detect_login_state():

    if not page:
        return False

    try:

        text = await page.locator(
            "body"
        ).inner_text(
            timeout=3000
        )

        login_words = [
            "로그인",
            "로그인해주세요",
        ]

        logout_words = [
            "로그아웃",
            "마이페이지",
        ]

        if any(
            word in text
            for word in logout_words
        ):
            return True

        if any(
            word in text
            for word in login_words
        ):
            return False

    except Exception:
        pass

    return False


# ============================================================
# 영화 선택
# ============================================================

async def select_movie():

    logger.info(
        "🎬 영화 선택 탐색"
    )

    for alias in MOVIE_ALIASES:

        selectors = [
            page.get_by_text(
                alias,
                exact=True,
            ),
            page.get_by_text(
                alias,
                exact=False,
            ),
        ]

        for locator in selectors:

            try:

                count = await locator.count()

                for i in range(
                    min(count, 10)
                ):

                    item = locator.nth(i)

                    if not await item.is_visible(
                        timeout=500
                    ):
                        continue

                    text = (
                        await item.inner_text()
                    ).strip()

                    if not text:
                        continue

                    logger.info(
                        "🎬 영화 후보 발견: %s",
                        text,
                    )

                    try:

                        await item.scroll_into_view_if_needed(
                            timeout=2000
                        )

                        await item.click(
                            timeout=4000
                        )

                        await asyncio.sleep(
                            2
                        )

                        logger.info(
                            "✅ 영화 선택: %s",
                            text,
                        )

                        return True

                    except Exception:
                        continue

            except Exception:
                continue

    return False


# ============================================================
# 날짜 버튼 찾기
# ============================================================

async def find_date_button(
    target_ymd,
):

    target_dt = datetime.strptime(
        target_ymd,
        "%Y%m%d",
    )

    day = str(
        target_dt.day
    )

    month = str(
        target_dt.month
    )

    weekday = weekday_korean(
        target_ymd
    )

    candidates = []

    try:

        elements = await page.locator(
            "button, [role='button'], a"
        ).all()

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

                combined = " ".join(
                    [
                        text,
                        aria,
                        title,
                        data_date,
                        data_ymd,
                    ]
                )

                # 정확한 날짜
                if target_ymd in combined:
                    return element

                if (
                    f"{target_ymd[:4]}-"
                    f"{target_ymd[4:6]}-"
                    f"{target_ymd[6:]}"
                    in combined
                ):
                    return element

                if (
                    f"{month}월 {day}일"
                    in combined
                ):
                    return element

                # 날짜 버튼이 "27 목" 같은 형태
                normalized = " ".join(
                    combined.split()
                )

                if (
                    re.search(
                        rf"(^|\s){day}(\s|$)",
                        normalized,
                    )
                    and weekday in normalized
                ):
                    candidates.append(
                        element
                    )

            except Exception:
                continue

    except Exception:
        pass

    if candidates:
        return candidates[0]

    return None


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

    button = await find_date_button(
        target_ymd
    )

    if not button:

        logger.warning(
            "⚠️ 날짜 버튼을 찾지 못함: %s",
            target_ymd,
        )

        return False

    try:

        await button.scroll_into_view_if_needed(
            timeout=3000
        )

        await button.click(
            timeout=5000
        )

        await asyncio.sleep(2)

        logger.info(
            "✅ 날짜 선택 성공: %s",
            target_ymd,
        )

        return True

    except Exception as exc:

        logger.warning(
            "⚠️ 날짜 클릭 실패: %s / %s",
            target_ymd,
            exc,
        )

        return False


# ============================================================
# 텍스트 정규화
# ============================================================

def normalize_text(value):

    if value is None:
        return ""

    return re.sub(
        r"\s+",
        " ",
        str(value),
    ).strip()


# ============================================================
# 영화 + IMAX 회차 DOM 탐색
# ============================================================

async def find_session_candidates():

    candidates = []

    try:

        # CGV의 시간표는 DOM 구조가 변할 수 있기 때문에
        # 특정 클래스 하나에 의존하지 않는다.
        elements = await page.locator(
            "button, [role='button'], a"
        ).all()

    except Exception:
        return candidates

    for element in elements:

        try:

            if not await element.is_visible(
                timeout=100
            ):
                continue

            text = normalize_text(
                await element.inner_text()
            )

            if not text:
                continue

            # 시간 형태
            has_time = bool(
                re.search(
                    r"\b([01]?\d|2[0-3]):[0-5]\d\b",
                    text,
                )
                or re.search(
                    r"\b([01]?\d|2[0-3])[0-5]\d\b",
                    text,
                )
            )

            if not has_time:
                continue

            # IMAX가 포함되면 가장 확실
            parent_text = ""

            try:

                parent = element.locator(
                    ".."
                )

                parent_text = normalize_text(
                    await parent.inner_text(
                        timeout=500
                    )
                )

            except Exception:
                pass

            combined = (
                text
                + " "
                + parent_text
            ).lower()

            # IMAX가 명시된 회차 우선
            is_imax = any(
                keyword in combined
                for keyword in FORMAT_KEYWORDS
            )

            # 영화명도 확인
            movie_match = any(
                alias.lower()
                in combined
                for alias in MOVIE_ALIASES
            )

            if not is_imax and not movie_match:
                continue

            candidates.append(
                {
                    "element": element,
                    "text": text,
                    "context": parent_text,
                    "is_imax": is_imax,
                }
            )

        except Exception:
            continue

    # IMAX 명시된 것부터
    candidates.sort(
        key=lambda x: (
            not x["is_imax"],
            len(x["text"]),
        )
    )

    return candidates


# ============================================================
# 좌석 여부
# ============================================================

def extract_seat_number(text):

    patterns = [
        r"잔여\s*[:：]?\s*(\d+)",
        r"(\d+)\s*석",
        r"(\d+)\s*좌석",
        r"(\d+)\s*석\s*남",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.I,
        )

        if match:
            try:
                return int(
                    match.group(1)
                )
            except Exception:
                pass

    return None


# ============================================================
# 회차 클릭
# ============================================================

async def open_session(
    candidate,
    scn_ymd,
):

    global opened_session_key

    element = candidate["element"]
    text = candidate["text"]
    context_text = candidate["context"]

    combined = (
        text
        + " "
        + context_text
    )

    seat_count = (
        extract_seat_number(
            combined
        )
    )

    # 좌석이 0으로 표시되면 제외
    if seat_count == 0:

        logger.info(
            "매진 회차 제외: %s",
            text,
        )

        return None

    # 회차 키
    key = "|".join(
        [
            scn_ymd,
            text,
            context_text[:100],
        ]
    )

    logger.info(
        "🎟️ 회차 발견: %s",
        combined[:300],
    )

    if key in seen_sessions:

        if (
            time.time()
            - seen_sessions[key]
            < 21600
        ):

            logger.info(
                "이미 처리한 회차: %s",
                key,
            )

            return None

    seen_sessions[key] = time.time()

    if not AUTO_OPEN_SESSION:

        return {
            "date": scn_ymd,
            "text": text,
            "context": context_text,
            "seats": seat_count,
        }

    try:

        await element.scroll_into_view_if_needed(
            timeout=3000
        )

        await element.click(
            timeout=5000
        )

        await asyncio.sleep(
            SESSION_OPEN_WAIT
        )

        opened_session_key = key

        logger.info(
            "🚀 회차 자동 선택 완료: %s",
            text,
        )

        logger.info(
            "현재 URL: %s",
            page.url,
        )

        return {
            "date": scn_ymd,
            "text": text,
            "context": context_text,
            "seats": seat_count,
            "url": page.url,
            "opened": True,
        }

    except Exception as exc:

        logger.warning(
            "회차 클릭 실패: %s",
            exc,
        )

        return None


# ============================================================
# 회차 탐색
# ============================================================

async def scan_current_date(
    scn_ymd,
):

    candidates = (
        await find_session_candidates()
    )

    logger.info(
        "🎟️ %s 회차 후보 %d개",
        pretty_date(scn_ymd),
        len(candidates),
    )

    if not candidates:
        return []

    results = []

    for candidate in candidates:

        result = await open_session(
            candidate,
            scn_ymd,
        )

        if result:
            results.append(
                result
            )

            # 첫 발견 회차를 바로 연다.
            if AUTO_OPEN_SESSION:
                break

    return results


# ============================================================
# 날짜 하나 검사
# ============================================================

async def check_date(
    scn_ymd,
    test_mode=False,
):

    logger.info(
        "=========================================="
    )

    logger.info(
        "🔎 날짜 검사: %s",
        pretty_date(scn_ymd),
    )

    if await detect_block_page():

        return {
            "status": "blocked",
            "sessions": [],
        }

    selected = await select_date(
        scn_ymd
    )

    if not selected:

        return {
            "status": "date_failed",
            "sessions": [],
        }

    await asyncio.sleep(2)

    sessions = await scan_current_date(
        scn_ymd
    )

    # 테스트 모드에서도 발견 회차는 알려준다.
    if sessions:

        for item in sessions:

            await notify_session(
                item,
                test_mode=test_mode,
            )

        return {
            "status": "found",
            "sessions": sessions,
        }

    return {
        "status": "ok",
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
                    "🚫 차단 상태. 검사 중단"
                )

                break

            # 회차를 열었다면
            # 현재 브라우저는 해당 예매 흐름에 둔다.
            if result["sessions"] and AUTO_OPEN_SESSION:

                logger.info(
                    "🎯 회차가 열렸으므로 이번 검사 종료"
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
            "dates": dates,
            "statuses": statuses,
            "sessions": all_sessions,
        }


# ============================================================
# Telegram 알림
# ============================================================

async def notify_session(
    item,
    test_mode=False,
):

    date = pretty_date(
        item.get("date")
    )

    text = item.get(
        "text",
        "",
    )

    context = item.get(
        "context",
        "",
    )

    seats = item.get(
        "seats"
    )

    opened = item.get(
        "opened",
        False,
    )

    if opened:

        title = (
            "🚨 <b>용아맥 회차 자동 선택 완료!</b>"
        )

        description = (
            "브라우저에서 해당 회차까지 자동으로 이동했습니다."
        )

    else:

        title = (
            "🎟️ <b>용아맥 회차 발견!</b>"
        )

        description = (
            "회차를 발견했습니다."
        )

    seat_text = (
        f"{seats}석"
        if seats is not None
        else "좌석 정보 확인 필요"
    )

    message = (
        f"{title}\n\n"
        f"🎬 {html.escape(', '.join(MOVIE_ALIASES))}\n"
        f"🏢 {html.escape(THEATER_NAME)}\n"
        f"📅 {date} ({weekday_korean(item.get('date'))})\n"
        f"🎞️ {html.escape(text[:250])}\n"
        f"💺 {seat_text}\n\n"
        f"{description}\n\n"
        "⚠️ 최종 좌석 선택 및 결제는 직접 확인하세요."
    )

    buttons = [
        [
            {
                "text": "🎬 CGV 예매 페이지 열기",
                "url": build_cgv_url(),
            }
        ]
    ]

    await telegram_send_async(
        message,
        buttons,
    )


# ============================================================
# 테스트 메시지
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

    if any(
        x == "blocked"
        for x in statuses.values()
    ):

        state = (
            "🔴 <b>CGV 보안/차단 상태</b>"
        )

    elif sessions:

        state = (
            "🟢 <b>회차 발견 + 자동 선택 완료</b>"
        )

    elif any(
        x == "date_failed"
        for x in statuses.values()
    ):

        state = (
            "🟡 <b>일부 날짜 선택 실패</b>"
        )

    else:

        state = (
            "🟢 <b>정상 검사 / 대상 회차 없음</b>"
        )

    lines = [
        "🧪 <b>용아맥 테스트</b>",
        "",
        state,
        f"🏢 {html.escape(THEATER_NAME)}",
        "🎞️ IMAX",
        "",
    ]

    if sessions:

        for item in sessions:

            lines.append(
                "🎟️ "
                + html.escape(
                    item.get(
                        "text",
                        "",
                    )[:200]
                )
            )

            lines.append(
                f"📅 {pretty_date(item.get('date'))}"
            )

            if item.get("opened"):
                lines.append(
                    "🚀 브라우저에서 회차 자동 선택 완료"
                )

    else:

        lines.append(
            "현재 발견된 대상 회차 없음"
        )

    failed = [
        date
        for date, status
        in statuses.items()
        if status == "date_failed"
    ]

    if failed:

        lines.append("")
        lines.append(
            "⚠️ 날짜 선택 실패:"
        )

        for date in failed[:10]:

            lines.append(
                f"• {pretty_date(date)}"
            )

    return "\n".join(lines)


# ============================================================
# Telegram 업데이트
# ============================================================

def telegram_get_updates(
    offset,
):

    if not TELEGRAM_BOT_TOKEN:
        return []

    try:

        response = requests.get(
            telegram_api(
                "getUpdates"
            ),
            params={
                "timeout": 5,
                "offset": offset,
            },
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

    except Exception:
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

    # --------------------------------------------------------
    # /start
    # --------------------------------------------------------

    if command == "/start":

        monitor_enabled = True

        await telegram_send_async(
            "🟢 <b>용아맥 감시 시작</b>\n\n"
            f"🏢 {html.escape(THEATER_NAME)}\n"
            "🎞️ IMAX\n"
            f"⏱️ {INTERVAL_SECONDS}초 간격\n\n"
            "회차 발견 시 브라우저에서 자동으로 회차를 선택합니다."
        )

        return

    # --------------------------------------------------------
    # /stop
    # --------------------------------------------------------

    if command == "/stop":

        monitor_enabled = False

        await telegram_send_async(
            "⏸️ <b>감시 중지</b>"
        )

        return

    # --------------------------------------------------------
    # /test
    # --------------------------------------------------------

    if command == "/test":

        if monitor_lock.locked():

            await telegram_send_async(
                "⏳ 현재 검사가 진행 중입니다."
            )

            return

        await telegram_send_async(
            "🧪 <b>CGV 실제 화면으로 테스트 중...</b>\n\n"
            "극장 → 날짜 → 회차 순서로 확인합니다."
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

    # --------------------------------------------------------
    # /status
    # --------------------------------------------------------

    if command == "/status":

        monitor = (
            "🟢 실행 중"
            if monitor_enabled
            else "⏸️ 중지"
        )

        cgv = (
            "🔴 보안/차단 감지"
            if blocked_detected
            else "🟢 정상"
        )

        login = await detect_login_state()

        login_text = (
            "🟢 로그인 상태"
            if login
            else "🟡 로그인 상태 확인 필요"
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
            "📊 <b>용아맥 상태</b>\n\n"
            f"감시: {monitor}\n"
            f"CGV: {cgv}\n"
            f"로그인: {login_text}\n"
            f"마지막 검사: {last_scan}\n"
            f"발견 회차: {len(last_scan_result)}개"
        )

        return

    # --------------------------------------------------------
    # /login
    # --------------------------------------------------------

    if command == "/login":

        await telegram_send_async(
            "🔐 <b>로그인 세션 안내</b>\n\n"
            "현재 브라우저가 로그인되어 있다면 "
            "해당 세션을 계속 사용합니다.\n\n"
            "처음 로그인하는 경우에는 "
            "BROWSER_HEADLESS=false 환경에서 한 번 로그인한 뒤 "
            "Persistent Volume에 브라우저 프로필을 보존하는 방식을 권장합니다."
        )

        return

    # --------------------------------------------------------
    # /help
    # --------------------------------------------------------

    if command == "/help":

        await telegram_send_async(
            "🎬 <b>용아맥 알리미</b>\n\n"
            "/start 감시 시작\n"
            "/stop 감시 중지\n"
            "/test 즉시 검사\n"
            "/status 상태\n"
            "/login 로그인 세션 안내"
        )


# ============================================================
# Telegram 루프
# ============================================================

async def telegram_command_loop():

    global telegram_offset

    if not TELEGRAM_BOT_TOKEN:

        logger.warning(
            "Telegram 비활성화"
        )

        return

    logger.info(
        "Telegram 명령 대기"
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
                "Telegram 오류: %s",
                exc,
            )

        await asyncio.sleep(1)


# ============================================================
# 감시 루프
# ============================================================

async def monitor_loop():

    global monitor_task

    logger.info(
        "🔄 감시 루프 시작"
    )

    while monitor_enabled:

        try:

            if blocked_detected:

                logger.warning(
                    "🚫 CGV 보안 상태. "
                    "요청을 계속 보내지 않습니다."
                )

                await asyncio.sleep(
                    300
                )

                continue

            await perform_scan(
                test_mode=False
            )

        except Exception as exc:

            logger.exception(
                "감시 오류: %s",
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

            first = (
                request.splitlines()[0]
                if request
                else ""
            )

            if first.startswith(
                "GET /status"
            ):

                body = (
                    "yongsan_imax_bot\n"
                    f"monitor={monitor_enabled}\n"
                    f"blocked={blocked_detected}\n"
                    f"browser={'OK' if page else 'NO'}\n"
                    f"url={page.url if page else ''}\n"
                )

            else:

                body = (
                    "yongsan_imax_bot running"
                )

            data = body.encode(
                "utf-8"
            )

            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/plain; "
                "charset=utf-8\r\n"
                f"Content-Length: {len(data)}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode(
                "utf-8"
            ) + data

            writer.write(
                response
            )

            await writer.drain()

        except Exception:
            pass

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
        "Health server: %d",
        PORT,
    )

    async with server:

        await server.serve_forever()


# ============================================================
# CGV 준비
# ============================================================

async def prepare_cgv():

    logger.info(
        "=========================================="
    )

    logger.info(
        "🎬 CGV 브라우저 준비"
    )

    logger.info(
        "극장: %s (%s)",
        THEATER_NAME,
        THEATER_CODE,
    )

    logger.info(
        "영화: %s",
        ", ".join(
            MOVIE_ALIASES
        ),
    )

    logger.info(
        "포맷: %s",
        ", ".join(
            FORMAT_KEYWORDS
        ),
    )

    logger.info(
        "=========================================="
    )

    if await detect_block_page():

        return False

    logged_in = await detect_login_state()

    if logged_in:

        logger.info(
            "🟢 CGV 로그인 세션 확인"
        )

    else:

        logger.warning(
            "🟡 CGV 로그인 상태가 확인되지 않았습니다."
        )

    return True


# ============================================================
# 종료
# ============================================================

async def shutdown():

    global browser
    global playwright

    try:

        if context:

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
        "🎬 용아맥 예매 보조 봇 시작"
    )

    logger.info(
        "=========================================="
    )

    health_task = asyncio.create_task(
        health_server()
    )

    try:

        await start_browser()

        prepared = await prepare_cgv()

        if not prepared:

            await telegram_send_async(
                "🔴 <b>CGV 보안/차단 상태</b>\n\n"
                "추가 자동 요청을 중단합니다."
            )

        else:

            await telegram_send_async(
                "🟢 <b>용아맥 봇 준비 완료</b>\n\n"
                f"🎬 {html.escape(', '.join(MOVIE_ALIASES))}\n"
                f"🏢 {html.escape(THEATER_NAME)}\n"
                "🎞️ IMAX\n\n"
                "🎟️ 회차 발견 시 브라우저에서 "
                "해당 회차를 자동 선택합니다.\n\n"
                "/test 즉시 테스트\n"
                "/start 감시 시작\n"
                "/stop 감시 중지\n"
                "/status 상태"
            )

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
            "종료"
        )

    except Exception:

        logger.exception(
            "치명적 오류"
                    )

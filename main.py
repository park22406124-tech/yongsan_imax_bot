import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

import requests
from playwright.async_api import async_playwright


# ============================================================
# 기본 설정
# ============================================================

CGV_BOOKING_URL = "https://cgv.co.kr/cnm/movieBook/cinema"

CGV_API_BASE = (
    "https://api.cgv.co.kr/cnm/atkt/searchMovScnInfo"
)

COMPANY_CODE = os.getenv(
    "COMPANY_CODE",
    "A420",
)

SITE_NO = os.getenv(
    "SITE_NO",
    "0013",
)

RTCTL_SCOP_CD = os.getenv(
    "RTCTL_SCOP_CD",
    "08",
)

THEATER_NAME = os.getenv(
    "THEATER_NAME",
    "용산아이파크몰",
)

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

HEADER_REFRESH_SECONDS = max(
    300,
    int(
        os.getenv(
            "HEADER_REFRESH_INTERVAL_SECONDS",
            "600",
        )
    ),
)

HEADER_CAPTURE_TIMEOUT = max(
    30,
    int(
        os.getenv(
            "HEADER_CAPTURE_TIMEOUT_SECONDS",
            "90",
        )
    ),
)

REQUEST_TIMEOUT = max(
    10,
    int(
        os.getenv(
            "REQUEST_TIMEOUT_SECONDS",
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

auth_headers = {}

last_auth_refresh = 0

monitor_lock = asyncio.Lock()

seen_sessions = {}

started_at = datetime.now()


# ============================================================
# 날짜
# ============================================================

def today_ymd():
    return datetime.now().strftime(
        "%Y%m%d"
    )


def make_ymd(offset):
    date = (
        datetime.now()
        + timedelta(days=offset)
    )

    return date.strftime(
        "%Y%m%d"
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
                response.text[:300],
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
# CGV 요청 캡처
# ============================================================

async def capture_cgv_request(
    timeout_seconds=None,
):
    if timeout_seconds is None:
        timeout_seconds = HEADER_CAPTURE_TIMEOUT

    if context is None:
        raise RuntimeError(
            "브라우저 context가 없습니다."
        )

    loop = asyncio.get_running_loop()

    future = loop.create_future()

    def on_request(request):
        try:
            url = request.url

            if not url.startswith(
                CGV_API_BASE
            ):
                return

            logger.info(
                "🎯 searchMovScnInfo 요청 감지"
            )

            if not future.done():
                future.set_result(
                    request
                )

        except Exception as exc:
            logger.warning(
                "요청 감시 오류: %s",
                exc,
            )

    context.on(
        "request",
        on_request,
    )

    try:
        return await asyncio.wait_for(
            future,
            timeout=timeout_seconds,
        )

    except asyncio.TimeoutError:
        return None

    finally:
        try:
            context.remove_listener(
                "request",
                on_request,
            )
        except Exception:
            pass


# ============================================================
# 극장 선택 UI
# ============================================================

async def open_theater_picker():
    if page is None:
        return False

    selectors = [
        'input[placeholder*="극장명"]',
        'input[placeholder*="극장을"]',
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
                return True

        except Exception:
            pass

    texts = [
        "극장을 선택",
        "극장 선택",
        "선택 된 극장이 없습니다",
        "선택된 극장이 없습니다",
    ]

    for text in texts:
        try:
            locator = page.get_by_text(
                text,
                exact=False,
            ).last

            if await locator.is_visible(
                timeout=1500
            ):
                await locator.click(
                    timeout=3000
                )

                await asyncio.sleep(
                    1
                )

                return True

        except Exception:
            pass

    buttons = [
        'button[aria-label*="극장"]',
        '[aria-label*="극장 선택"]',
        'button:has-text("극장")',
    ]

    for selector in buttons:
        try:
            locator = page.locator(
                selector
            ).first

            if await locator.is_visible(
                timeout=1500
            ):
                await locator.click(
                    timeout=3000
                )

                await asyncio.sleep(
                    1
                )

                return True

        except Exception:
            pass

    return False


async def select_theater():
    logger.info(
        "🏢 CGV 극장 선택 시도: %s",
        THEATER_NAME,
    )

    opened = (
        await open_theater_picker()
    )

    if not opened:
        logger.warning(
            "극장 선택창을 자동으로 열지 못했습니다."
        )

    await asyncio.sleep(1)

    search_selectors = [
        'input[placeholder*="극장명"]',
        'input[placeholder*="극장을"]',
        'input[type="search"]',
        'input',
    ]

    search_input = None

    for selector in search_selectors:
        try:
            locator = page.locator(
                selector
            ).first

            if await locator.is_visible(
                timeout=2000
            ):
                search_input = locator
                break

        except Exception:
            pass

    if search_input is not None:
        try:
            await search_input.fill(
                THEATER_NAME
            )

            await asyncio.sleep(
                0.5
            )

            await page.keyboard.press(
                "Enter"
            )

            await asyncio.sleep(
                1
            )

        except Exception as exc:
            logger.warning(
                "극장 검색 입력 실패: %s",
                exc,
            )

    # 정확한 이름 우선
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

            await asyncio.sleep(
                2
            )

            logger.info(
                "✅ 용산아이파크몰 선택 완료"
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

            await asyncio.sleep(
                2
            )

            logger.info(
                "✅ 용산아이파크몰 선택 완료"
            )

            return True

    except Exception:
        pass

    logger.warning(
        "⚠️ 용산아이파크몰 자동 선택 실패"
    )

    return False


# ============================================================
# 인증 헤더 추출
# ============================================================

async def extract_auth_from_request(
    request,
):
    global auth_headers
    global last_auth_refresh

    headers = await request.all_headers()

    cookie = headers.get(
        "cookie",
        "",
    )

    signature = headers.get(
        "x-signature",
        "",
    )

    timestamp = headers.get(
        "x-timestamp",
        "",
    )

    authorization = headers.get(
        "authorization",
        "",
    )

    if not signature:
        logger.error(
            "AUTH_CAPTURE_FAILED: X-SIGNATURE 없음"
        )

        return False

    if not timestamp:
        logger.error(
            "AUTH_CAPTURE_FAILED: X-TIMESTAMP 없음"
        )

        return False

    auth_headers = {
        "cookie": cookie,
        "authorization": authorization,
        "accept": headers.get(
            "accept",
            "application/json",
        ),
        "accept-language": headers.get(
            "accept-language",
            "ko-KR,ko;q=0.9",
        ),
        "origin": headers.get(
            "origin",
            "https://cgv.co.kr",
        ),
        "referer": headers.get(
            "referer",
            CGV_BOOKING_URL,
        ),
        "user-agent": headers.get(
            "user-agent",
            "",
        ),
        "x-signature": signature,
        "x-timestamp": timestamp,
    }

    last_auth_refresh = time.time()

    logger.info(
        "================================================"
    )

    logger.info(
        "✅ CGV 인증 헤더 캡처 성공"
    )

    logger.info(
        "   Cookie       : %s",
        "YES" if cookie else "NO",
    )

    logger.info(
        "   Authorization: %s",
        "YES" if authorization else "NO",
    )

    logger.info(
        "   X-SIGNATURE  : YES"
    )

    logger.info(
        "   X-TIMESTAMP  : %s",
        timestamp,
    )

    logger.info(
        "================================================"
    )

    return True


# ============================================================
# 인증 갱신
# ============================================================

async def refresh_auth(
    reason="scheduled",
):
    global page

    logger.info(
        "🔐 CGV 인증 갱신 시작: %s",
        reason,
    )

    if page is None:
        await start_browser()

    # 요청 감시를 먼저 걸어놓는다.
    capture_task = asyncio.create_task(
        capture_cgv_request()
    )

    try:
        # 페이지 재진입
        try:
            await page.goto(
                CGV_BOOKING_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )
        except Exception as exc:
            logger.warning(
                "CGV 재접속 중 예외: %s",
                exc,
            )

        await asyncio.sleep(3)

        # 극장 선택
        await select_theater()

        # 여기서 CGV가 searchMovScnInfo를
        # 발생시키기를 기다린다.
        request = await capture_task

        if request is not None:
            return await extract_auth_from_request(
                request
            )

        logger.warning(
            "⚠️ searchMovScnInfo 자동 캡처 실패"
        )

        return False

    except Exception as exc:
        logger.exception(
            "인증 갱신 실패: %s",
            exc,
        )

        if not capture_task.done():
            capture_task.cancel()

        return False


# ============================================================
# CGV API 조회
# ============================================================

async def query_schedule(
    scn_ymd,
):
    if not auth_headers:
        raise RuntimeError(
            "CGV 인증 헤더가 없습니다."
        )

    params = {
        "coCd": COMPANY_CODE,
        "siteNo": SITE_NO,
        "scnYmd": scn_ymd,
        "rtctlScopCd": RTCTL_SCOP_CD,
    }

    url = (
        f"{CGV_API_BASE}?"
        f"{urlencode(params)}"
    )

    headers = {
        "Accept": auth_headers.get(
            "accept",
            "application/json",
        ),
        "Accept-Language": auth_headers.get(
            "accept-language",
            "ko-KR,ko;q=0.9",
        ),
        "Origin": auth_headers.get(
            "origin",
            "https://cgv.co.kr",
        ),
        "Referer": auth_headers.get(
            "referer",
            CGV_BOOKING_URL,
        ),
        "User-Agent": auth_headers.get(
            "user-agent",
            "",
        ),
        "X-SIGNATURE": auth_headers[
            "x-signature"
        ],
        "X-TIMESTAMP": auth_headers[
            "x-timestamp"
        ],
    }

    if auth_headers.get(
        "cookie"
    ):
        headers["Cookie"] = auth_headers[
            "cookie"
        ]

    if auth_headers.get(
        "authorization"
    ):
        headers["Authorization"] = (
            auth_headers[
                "authorization"
            ]
        )

    logger.info(
        "CGV 조회: %s",
        scn_ymd,
    )

    # 브라우저 컨텍스트와 같은 세션을
    # 유지하기 위해 Playwright request 사용.
    api_context = await context.request.new_context(
        extra_http_headers=headers
    )

    try:
        response = await api_context.get(
            url,
            timeout=REQUEST_TIMEOUT * 1000,
        )

        status = response.status

        text = await response.text()

        if status == 401:
            raise RuntimeError(
                "CGV API HTTP 401"
            )

        if status == 403:
            raise RuntimeError(
                "CGV API HTTP 403"
            )

        if status == 429:
            raise RuntimeError(
                "CGV API HTTP 429"
            )

        if status >= 400:
            raise RuntimeError(
                f"CGV API HTTP {status}"
            )

        try:
            return json.loads(text)

        except json.JSONDecodeError:
            raise RuntimeError(
                "CGV API JSON 파싱 실패: "
                + text[:300]
            )

    finally:
        await api_context.dispose()


# ============================================================
# 데이터 추출
# ============================================================

def extract_rows(payload):
    if not isinstance(
        payload,
        dict,
    ):
        return []

    if payload.get(
        "statusCode"
    ) not in (None, 0, "0"):
        raise RuntimeError(
            "CGV statusCode="
            + str(
                payload.get(
                    "statusCode"
                )
            )
            + " "
            + str(
                payload.get(
                    "statusMessage",
                    "",
                )
            )
        )

    rows = payload.get(
        "data",
        [],
    )

    if isinstance(
        rows,
        list,
    ):
        return rows

    return []


# ============================================================
# 필터
# ============================================================

def target_movie(row):
    values = [
        row.get("movNm"),
        row.get("movEnm"),
        row.get("prodNm"),
        row.get("expoProdNm"),
        row.get("engProdNm"),
    ]

    text = " ".join(
        str(x)
        for x in values
        if x
    ).lower()

    return any(
        alias.lower() in text
        for alias in MOVIE_ALIASES
    )


def target_format(row):
    values = [
        row.get("movkndDsplNm"),
        row.get("movkndDsplEnm"),
        row.get("tcscnsGradNm"),
        row.get("scnsNm"),
        row.get("expoScnsNm"),
        row.get("scnsEnm"),
    ]

    text = " ".join(
        str(x)
        for x in values
        if x
    ).lower()

    return any(
        keyword in text
        for keyword in FORMAT_KEYWORDS
    )


def bookable(row):
    try:
        seats = int(
            row.get(
                "frSeatCnt",
                0,
            )
            or 0
        )
    except Exception:
        seats = 0

    if row.get(
        "cntlYn"
    ) == "Y":
        return False

    return seats > 0


# ============================================================
# 시간
# ============================================================

def format_time(value):
    value = str(
        value or ""
    )

    if len(value) != 4:
        return value or "??:??"

    return (
        value[:2]
        + ":"
        + value[2:]
    )


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

        if not bookable(row):
            continue

        seats = int(
            row.get(
                "frSeatCnt",
                0,
            )
            or 0
        )

        item = {
            "date": scn_ymd,
            "movNo": row.get(
                "movNo"
            ),
            "movNm": (
                row.get("movNm")
                or row.get("prodNm")
                or ""
            ),
            "scnSseq": row.get(
                "scnSseq"
            ),
            "scnsNo": row.get(
                "scnsNo"
            ),
            "start": row.get(
                "scnsrtTm"
            ),
            "end": row.get(
                "scnendTm"
            ),
            "screen": (
                row.get("expoScnsNm")
                or row.get("scnsNm")
                or row.get("scnsEnm")
                or "IMAX"
            ),
            "format": (
                row.get(
                    "movkndDsplNm"
                )
                or row.get(
                    "scnsNm"
                )
                or "IMAX"
            ),
            "seats": seats,
            "totalSeats": row.get(
                "stcnt"
            ),
        }

        result.append(
            item
        )

    return result


# ============================================================
# 예매 URL
# ============================================================

def booking_url():
    return CGV_BOOKING_URL


# ============================================================
# Telegram 알림
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


def notify_session(
    item,
):
    key = session_key(
        item
    )

    # 중복 알림 방지
    now = time.time()

    # 6시간 지난 기록 삭제
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

    date = item["date"]

    if len(date) == 8:
        date = (
            f"{date[:4]}-"
            f"{date[4:6]}-"
            f"{date[6:]}"
        )

    start = format_time(
        item["start"]
    )

    end = format_time(
        item["end"]
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
        f"💺 잔여 <b>{item['seats']}석"
        f"{total_text}</b>\n\n"
        "⚡ 지금 CGV에서 확인하세요!"
    )

    buttons = [
        [
            {
                "text": (
                    f"🎟️ {start} 바로 예매"
                ),
                "url": booking_url(),
            }
        ]
    ]

    logger.info(
        "🚨 대상 회차 발견: %s %s %s석",
        date,
        start,
        item["seats"],
    )

    send_telegram(
        message,
        buttons,
    )


# ============================================================
# 날짜 하나 조회
# ============================================================

async def check_date(
    scn_ymd,
):
    try:
        payload = await query_schedule(
            scn_ymd
        )

        sessions = parse_sessions(
            payload,
            scn_ymd,
        )

        logger.info(
            "%s: 대상 IMAX 회차 %d개",
            scn_ymd,
            len(sessions),
        )

        for item in sessions:
            notify_session(
                item
            )

        return sessions

    except Exception:
        raise


# ============================================================
# 인증 필요 여부
# ============================================================

def auth_expired():
    if not auth_headers:
        return True

    if not auth_headers.get(
        "x-signature"
    ):
        return True

    if not auth_headers.get(
        "x-timestamp"
    ):
        return True

    if (
        time.time()
        - last_auth_refresh
        > HEADER_REFRESH_SECONDS
    ):
        return True

    return False


# ============================================================
# 전체 검사
# ============================================================

async def perform_scan(
    test_mode=False,
):
    async with monitor_lock:

        if auth_expired():

            success = await refresh_auth(
                "initial"
                if not auth_headers
                else "expired"
            )

            if not success:
                raise RuntimeError(
                    "AUTH_CAPTURE_FAILED"
                )

        dates = [
            make_ymd(i)
            for i in range(
                DAYS_AHEAD + 1
            )
        ]

        if test_mode:
            dates = [
                today_ymd()
            ]

        for index, scn_ymd in enumerate(
            dates
        ):

            try:

                await check_date(
                    scn_ymd
                )

            except RuntimeError as exc:

                text = str(exc)

                logger.error(
                    "%s 조회 실패: %s",
                    scn_ymd,
                    text,
                )

                if (
                    "401" in text
                    or "AUTH" in text
                ):

                    logger.warning(
                        "🔐 인증 문제 감지 -> "
                        "브라우저에서 인증 헤더 재캡처"
                    )

                    success = (
                        await refresh_auth(
                            "401"
                        )
                    )

                    if not success:
                        raise

                    # 새 인증으로 즉시 재시도
                    await check_date(
                        scn_ymd
                    )

                else:
                    raise

            if (
                index < len(dates) - 1
                and not test_mode
            ):
                await asyncio.sleep(
                    INTERVAL_SECONDS
                )


# ============================================================
# Health 서버
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
                    f"auth={'OK' if auth_headers else 'NO'}"
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
                        "🟢 CGV 테스트 성공"
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

    telegram_ready = (
        bool(
            TELEGRAM_BOT_TOKEN
            and TELEGRAM_CHAT_ID
        )
    )

    logger.info(
        "Telegram: %s",
        "READY"
        if telegram_ready
        else "NOT READY",
    )

    await start_browser()

    # 최초 인증 캡처
    try:

        success = await refresh_auth(
            "startup"
        )

        if not success:
            logger.error(
                "=========================================="
            )

            logger.error(
                "AUTH_CAPTURE_FAILED"
            )

            logger.error(
                "CGV의 searchMovScnInfo 요청을 "
                "자동으로 캡처하지 못했습니다."
            )

            logger.error(
                "현재 감시를 시작하지 않습니다."
            )

            logger.error(
                "=========================================="
            )

            # 60초 후 다시 시도
            while True:

                await asyncio.sleep(
                    60
                )

                success = (
                    await refresh_auth(
                        "retry"
                    )
                )

                if success:
                    break

        send_telegram(
            "🟢 <b>용아맥 감시 시작</b>\n\n"
            f"🎬 {', '.join(MOVIE_ALIASES)}\n"
            f"🏢 {THEATER_NAME}\n"
            "🎞️ IMAX\n"
            f"⏱ {INTERVAL_SECONDS}초 간격\n\n"
            "🔐 CGV 인증 세션 정상 확보"
        )

    except Exception as exc:

        logger.exception(
            "최초 인증 실패: %s",
            exc,
        )

        raise

    while True:

        try:

            await perform_scan(
                test_mode=False
            )

        except Exception as exc:

            logger.error(
                "감시 사이클 오류: %s",
                exc,
            )

            # 인증 오류면 바로 재갱신
            if (
                "401" in str(exc)
                or "AUTH" in str(exc)
            ):

                try:
                    await refresh_auth(
                        "monitor-error"
                    )
                except Exception:
                    logger.exception(
                        "인증 재갱신 실패"
                    )

            else:

                # 일반 오류는 조금 쉬었다가
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

import asyncio
import html
import logging
import os
import re
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
    int(os.getenv("INTERVAL_SECONDS", "40")),
)

DAYS_AHEAD = max(
    0,
    int(os.getenv("DAYS_AHEAD", "7")),
)

REQUEST_WAIT_SECONDS = max(
    5,
    int(os.getenv("REQUEST_WAIT_SECONDS", "12")),
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
    format="%(asctime)s | %(levelname)s | %(message)s",
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

monitor_enabled = True
monitor_task = None
telegram_task = None

monitor_lock = asyncio.Lock()

telegram_offset = 0

seen_sessions = {}

last_scan_time = 0
last_scan_result = []

blocked_detected = False
blocked_message = ""

latest_schedule = None


# ============================================================
# 날짜
# ============================================================

def make_ymd(offset=0):
    return (
        datetime.now()
        + timedelta(days=offset)
    ).strftime("%Y%m%d")


def pretty_date(ymd):
    return (
        f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
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
            telegram_api("sendMessage"),
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
    "too many requests",
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
        return False

    try:
        title = (
            await page.title()
        ).lower()
    except Exception:
        title = ""

    try:
        body = (
            await page.locator("body")
            .inner_text(timeout=3000)
        ).lower()
    except Exception:
        body = ""

    text = (
        title
        + "\n"
        + body[:30000]
    )

    for keyword in BLOCK_KEYWORDS:
        if keyword.lower() in text:
            mark_blocked(keyword)
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

    logger.info("Chromium 시작")

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
            "width": 1440,
            "height": 1000,
        },
    )

    page = await context.new_page()

    # --------------------------------------------------------
    # 모든 응답 감시
    # --------------------------------------------------------

    async def on_response(response):

        global latest_schedule

        try:

            url = response.url
            status = response.status

            if status in (
                401,
                403,
                429,
            ):
                logger.warning(
                    "CGV HTTP %s: %s",
                    status,
                    url,
                )

            lower = url.lower()

            interesting = any(
                key in lower
                for key in [
                    "scn",
                    "schedule",
                    "movie",
                    "screen",
                    "cinema",
                ]
            )

            if not interesting:
                return

            if status != 200:
                return

            content_type = (
                response.headers.get(
                    "content-type",
                    "",
                )
                .lower()
            )

            if (
                "json" not in content_type
                and "javascript" not in content_type
            ):
                return

            try:
                data = await response.json()
            except Exception:
                return

            if not isinstance(data, dict):
                return

            # 일정 관련 응답으로 보이는 JSON만 보관
            serialized = str(data).lower()

            if any(
                key in serialized
                for key in [
                    "movno",
                    "scnsno",
                    "scnsrt",
                    "scnseq",
                    "screen",
                    "movie",
                ]
            ):
                latest_schedule = {
                    "url": url,
                    "status": status,
                    "data": data,
                    "time": time.time(),
                }

                logger.info(
                    "🎯 CGV 일정 JSON 확보: %s",
                    url,
                )

        except Exception:
            pass

    page.on(
        "response",
        on_response,
    )

    # --------------------------------------------------------
    # CGV 용산 직접 진입
    # --------------------------------------------------------

    url = (
        f"{CGV_BOOKING_URL}"
        f"?siteNm={THEATER_NAME}"
        f"&siteNo={THEATER_CODE}"
    )

    logger.info(
        "CGV 용산 예매 페이지 접속"
    )

    try:

        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60000,
        )

    except Exception as exc:

        logger.warning(
            "CGV 접속 예외: %s",
            exc,
        )

    await asyncio.sleep(5)

    logger.info(
        "현재 URL: %s",
        page.url,
    )

    logger.info(
        "현재 제목: %s",
        await page.title(),
    )

    await detect_block_page()


# ============================================================
# 페이지 안정화
# ============================================================

async def wait_for_cgv_ready():

    logger.info(
        "⏳ CGV 화면 로딩 대기"
    )

    for _ in range(20):

        if await detect_block_page():
            return False

        try:

            text = await page.locator(
                "body"
            ).inner_text(
                timeout=2000
            )

            if (
                "용산" in text
                or "영화순" in text
                or "시간순" in text
                or "전체" in text
            ):
                logger.info(
                    "✅ CGV 화면 로딩 확인"
                )
                return True

        except Exception:
            pass

        await asyncio.sleep(1)

    return True


# ============================================================
# 날짜 DOM 진단
# ============================================================

async def dump_date_dom():

    try:

        result = await page.evaluate(
            """
            () => {

                const result = [];

                const nodes = [
                    ...document.querySelectorAll('*')
                ];

                for (const el of nodes) {

                    const text =
                        (el.innerText || '')
                        .trim()
                        .replace(/\\\\s+/g, ' ');

                    if (!text) continue;

                    if (
                        text.length <= 40
                        &&
                        (
                            /\\\\d{1,2}/.test(text)
                            ||
                            /월|화|수|목|금|토|일/.test(text)
                        )
                    ) {

                        const rect =
                            el.getBoundingClientRect();

                        if (
                            rect.width > 0
                            &&
                            rect.height > 0
                        ) {

                            result.push({
                                tag: el.tagName,
                                cls: String(
                                    el.className || ''
                                ).slice(0, 150),
                                text: text.slice(0, 100),
                                aria:
                                    el.getAttribute(
                                        'aria-label'
                                    ) || '',
                                role:
                                    el.getAttribute(
                                        'role'
                                    ) || '',
                                date:
                                    el.getAttribute(
                                        'data-date'
                                    ) || '',
                                y: Math.round(
                                    rect.y
                                )
                            });
                        }
                    }

                    if (result.length >= 200) {
                        break;
                    }
                }

                return result;
            }
            """
        )

        logger.info(
            "📋 날짜 DOM 후보 %d개",
            len(result),
        )

        for item in result[:80]:

            logger.info(
                "DATE DOM | %s | %s | %s | %s",
                item.get("tag"),
                item.get("text"),
                item.get("aria"),
                item.get("date"),
            )

    except Exception as exc:

        logger.debug(
            "날짜 DOM 진단 실패: %s",
            exc,
        )


# ============================================================
# 날짜 클릭
# ============================================================

async def click_date(target_ymd):

    target_dt = datetime.strptime(
        target_ymd,
        "%Y%m%d",
    )

    day = target_dt.day
    month = target_dt.month
    year = target_dt.year

    weekday = weekday_korean(
        target_ymd
    )

    logger.info(
        "📅 날짜 선택: %s (%s)",
        pretty_date(target_ymd),
        weekday,
    )

    # --------------------------------------------------------
    # JS 기반 실제 화면 탐색
    # 태그 종류를 전혀 믿지 않는다.
    # --------------------------------------------------------

    try:

        result = await page.evaluate(
            """
            ({year, month, day, weekday}) => {

                function visible(el) {

                    const r =
                        el.getBoundingClientRect();

                    const style =
                        window.getComputedStyle(el);

                    return (
                        r.width > 0 &&
                        r.height > 0 &&
                        style.display !== 'none' &&
                        style.visibility !== 'hidden'
                    );
                }

                function clickElement(el) {

                    el.scrollIntoView({
                        block: 'center',
                        inline: 'center'
                    });

                    const events = [
                        'pointerdown',
                        'mousedown',
                        'pointerup',
                        'mouseup',
                        'click'
                    ];

                    for (const name of events) {

                        el.dispatchEvent(
                            new MouseEvent(
                                name,
                                {
                                    bubbles: true,
                                    cancelable: true,
                                    view: window
                                }
                            )
                        );
                    }

                    return true;
                }

                const all = [
                    ...document.querySelectorAll('*')
                ];

                // --------------------------------------------
                // 1. 날짜 속성이 있는 요소
                // --------------------------------------------

                for (const el of all) {

                    if (!visible(el)) continue;

                    const attrs = [
                        el.getAttribute('data-date'),
                        el.getAttribute('data-ymd'),
                        el.getAttribute('data-day'),
                        el.getAttribute('date'),
                        el.getAttribute('value'),
                        el.getAttribute('aria-label'),
                        el.getAttribute('title')
                    ].filter(Boolean);

                    const joined =
                        attrs.join(' ');

                    const yyyy =
                        String(year);

                    const mm =
                        String(month).padStart(2, '0');

                    const dd =
                        String(day).padStart(2, '0');

                    if (
                        joined.includes(
                            yyyy + mm + dd
                        )
                        ||
                        joined.includes(
                            yyyy + '-' + mm + '-' + dd
                        )
                        ||
                        joined.includes(
                            yyyy + '.' + mm + '.' + dd
                        )
                        ||
                        joined.includes(
                            mm + '/' + dd
                        )
                    ) {

                        clickElement(el);

                        return {
                            ok: true,
                            method: 'attribute',
                            text:
                                (el.innerText || '')
                                .trim()
                        };
                    }
                }

                // --------------------------------------------
                // 2. 날짜 숫자 + 요일
                // --------------------------------------------

                const candidates = [];

                for (const el of all) {

                    if (!visible(el)) continue;

                    const text =
                        (el.innerText || '')
                        .trim()
                        .replace(/\\\\s+/g, ' ');

                    if (!text) continue;

                    if (text.length > 80) continue;

                    const hasDay =
                        new RegExp(
                            '(^|\\\\s|\\\\n)'
                            + day
                            + '(?=\\\\s|$|\\\\n)'
                        ).test(text)
                        ||
                        text === String(day);

                    const hasWeekday =
                        text.includes(weekday);

                    if (
                        hasDay
                        && hasWeekday
                    ) {

                        candidates.push(el);
                    }
                }

                // 가장 작은 요소부터
                candidates.sort(
                    (a, b) => {

                        const ar =
                            a.getBoundingClientRect();

                        const br =
                            b.getBoundingClientRect();

                        return (
                            ar.width * ar.height
                        )
                        -
                        (
                            br.width * br.height
                        );
                    }
                );

                for (
                    const el
                    of candidates
                ) {

                    clickElement(el);

                    return {
                        ok: true,
                        method: 'day-weekday',
                        text:
                            (el.innerText || '')
                            .trim()
                    };
                }

                // --------------------------------------------
                // 3. 숫자만 있는 날짜 요소
                //    주변 부모에서 요일 확인
                // --------------------------------------------

                for (const el of all) {

                    if (!visible(el)) continue;

                    const text =
                        (el.innerText || '')
                        .trim();

                    if (
                        text !== String(day)
                    ) {
                        continue;
                    }

                    let parent = el;

                    for (let i = 0; i < 5; i++) {

                        if (!parent) break;

                        const parentText =
                            (
                                parent.innerText
                                || ''
                            )
                            .trim()
                            .replace(
                                /\\\\s+/g,
                                ' '
                            );

                        if (
                            parentText.includes(
                                weekday
                            )
                        ) {

                            clickElement(el);

                            return {
                                ok: true,
                                method: 'day-parent-weekday',
                                text: text
                            };
                        }

                        parent =
                            parent.parentElement;
                    }
                }

                return {
                    ok: false
                };
            }
            """,
            {
                "year": year,
                "month": month,
                "day": day,
                "weekday": weekday,
            },
        )

        if result.get("ok"):

            logger.info(
                "✅ 날짜 클릭 성공: %s | %s | %s",
                target_ymd,
                result.get("method"),
                result.get("text"),
            )

            await asyncio.sleep(2)

            return True

    except Exception as exc:

        logger.warning(
            "날짜 JS 클릭 오류: %s",
            exc,
        )

    # --------------------------------------------------------
    # 2차 Playwright 텍스트 탐색
    # --------------------------------------------------------

    try:

        day_text = str(day)

        locators = [
            page.get_by_text(
                day_text,
                exact=True,
            ),
            page.locator(
                f"text={day_text}"
            ),
        ]

        for locator in locators:

            count = await locator.count()

            logger.info(
                "날짜 숫자 '%s' 후보: %d개",
                day_text,
                count,
            )

            for i in range(
                min(count, 20)
            ):

                item = locator.nth(i)

                try:

                    if not await item.is_visible(
                        timeout=500
                    ):
                        continue

                    await item.scroll_into_view_if_needed(
                        timeout=2000
                    )

                    await item.click(
                        timeout=3000,
                        force=True,
                    )

                    await asyncio.sleep(2)

                    logger.info(
                        "✅ 강제 날짜 클릭 성공: %s",
                        target_ymd,
                    )

                    return True

                except Exception:
                    continue

    except Exception as exc:

        logger.debug(
            "Playwright 날짜 탐색 실패: %s",
            exc,
        )

    logger.warning(
        "⚠️ 날짜 버튼을 찾지 못함: %s",
        target_ymd,
    )

    await dump_date_dom()

    return False


# ============================================================
# 극장 확인
# ============================================================

async def ensure_theater():

    # 직접 siteNo URL로 들어왔기 때문에
    # 극장이 이미 선택되어 있는지 먼저 확인한다.

    try:

        body = await page.locator(
            "body"
        ).inner_text(
            timeout=3000
        )

        if THEATER_NAME in body:
            logger.info(
                "✅ 용산아이파크몰 화면 확인"
            )
            return True

    except Exception:
        pass

    # 혹시 선택이 안 된 경우 검색
    selectors = [
        'input[placeholder*="극장"]',
        'input[placeholder*="검색"]',
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

                await locator.fill(
                    THEATER_NAME
                )

                await asyncio.sleep(1)

                result = page.get_by_text(
                    THEATER_NAME,
                    exact=True,
                ).last

                if await result.is_visible(
                    timeout=2000
                ):

                    await result.click(
                        timeout=3000
                    )

                    await asyncio.sleep(2)

                    return True

        except Exception:
            continue

    return False


# ============================================================
# JSON 탐색
# ============================================================

def recursive_lists(obj):

    found = []

    if isinstance(obj, dict):

        for value in obj.values():

            if isinstance(value, list):
                found.append(value)

            found.extend(
                recursive_lists(value)
            )

    elif isinstance(obj, list):

        for value in obj:
            found.extend(
                recursive_lists(value)
            )

    return found


def extract_rows(payload):

    if not isinstance(
        payload,
        dict,
    ):
        return []

    preferred = [
        "list",
        "rows",
        "result",
        "schedule",
        "scnList",
        "movieList",
        "scnInfoList",
    ]

    data = payload.get("data")

    containers = []

    if isinstance(data, dict):
        containers.append(data)

    containers.append(payload)

    for container in containers:

        for key in preferred:

            value = container.get(key)

            if (
                isinstance(value, list)
                and value
                and all(
                    isinstance(x, dict)
                    for x in value
                )
            ):
                return value

    # 최후의 수단
    lists = recursive_lists(
        payload
    )

    best = []

    for rows in lists:

        if not rows:
            continue

        score = 0

        for row in rows[:10]:

            if not isinstance(
                row,
                dict,
            ):
                continue

            keys = set(
                row.keys()
            )

            score += len(
                keys.intersection(
                    {
                        "movNo",
                        "movNm",
                        "scnsNo",
                        "scnSseq",
                        "scnsrtTm",
                        "scnStartTm",
                    }
                )
            )

        if score > len(best):
            best = rows

    return best


# ============================================================
# 영화 / 포맷
# ============================================================

def row_text(row):

    return " ".join(
        str(row.get(key, ""))
        for key in [
            "movNm",
            "movEnm",
            "prodNm",
            "expoProdNm",
            "engProdNm",
            "movieNm",
            "movkndDsplNm",
            "movkndDsplEnm",
            "scnsNm",
            "expoScnsNm",
            "scnsEnm",
            "screenNm",
            "screenName",
            "tcscnsGradNm",
        ]
    ).lower()


def target_movie(row):

    text = row_text(row)

    return any(
        alias.lower()
        in text
        for alias in MOVIE_ALIASES
    )


def target_format(row):

    text = row_text(row)

    return any(
        keyword
        in text
        for keyword in FORMAT_KEYWORDS
    )


def seat_count(row):

    for key in [
        "frSeatCnt",
        "remainSeatCnt",
        "availableSeatCnt",
        "seatCnt",
        "remainCnt",
    ]:

        try:

            value = row.get(key)

            if value is not None:
                return int(value)

        except Exception:
            pass

    return 0


def total_seat_count(row):

    for key in [
        "stcnt",
        "totalSeatCnt",
        "totalSeats",
    ]:

        try:

            value = row.get(key)

            if value is not None:
                return int(value)

        except Exception:
            pass

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

    logger.info(
        "CGV JSON rows=%d",
        len(rows),
    )

    sessions = []

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

        seats = seat_count(row)

        if seats <= 0:
            continue

        start = (
            row.get("scnsrtTm")
            or row.get("scnStartTm")
            or row.get("startTime")
            or row.get("startTm")
        )

        end = (
            row.get("scnendTm")
            or row.get("scnEndTm")
            or row.get("endTime")
            or row.get("endTm")
        )

        sessions.append(
            {
                "date": scn_ymd,
                "movNo": row.get("movNo"),
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
                "start": start,
                "end": end,
                "screen": (
                    row.get("expoScnsNm")
                    or row.get("scnsNm")
                    or row.get("screenNm")
                    or "IMAX"
                ),
                "seats": seats,
                "totalSeats": total_seat_count(
                    row
                ),
            }
        )

    return sessions


# ============================================================
# 알림
# ============================================================

def format_time(value):

    if value is None:
        return "??:??"

    text = str(value)

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
    )

    now = time.time()

    if (
        key in seen_sessions
        and now - seen_sessions[key] < 21600
    ):
        return

    seen_sessions[key] = now

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
        "🚨 <b>용아맥 예매 오픈!</b>\n\n"
        f"🎬 <b>{html.escape(str(item['movNm']))}</b>\n"
        f"📅 {pretty_date(item['date'])}\n"
        f"🏢 {html.escape(THEATER_NAME)}\n"
        "🎞️ IMAX\n"
        f"🕐 {start} ~ {end}\n"
        f"💺 잔여 <b>{seats}석"
        f"{total_text}</b>\n\n"
        "⚡ CGV 예매 화면으로 이동하세요."
    )

    buttons = [
        [
            {
                "text": "🎟️ CGV 예매 화면 열기",
                "url": (
                    f"{CGV_BOOKING_URL}"
                    f"?siteNm={THEATER_NAME}"
                    f"&siteNo={THEATER_CODE}"
                ),
            }
        ]
    ]

    send_telegram(
        message,
        buttons,
    )

    logger.info(
        "🚨 회차 발견: %s %s / %s석",
        pretty_date(item["date"]),
        start,
        seats,
    )


# ============================================================
# 날짜 검사
# ============================================================

async def check_date(
    scn_ymd,
    test_mode=False,
):

    global latest_schedule

    logger.info(
        "=========================================="
    )

    logger.info(
        "🔎 날짜 검사: %s",
        pretty_date(scn_ymd),
    )

    latest_schedule = None

    if await detect_block_page():
        return {
            "status": "blocked",
            "sessions": [],
        }

    clicked = await click_date(
        scn_ymd
    )

    if not clicked:

        return {
            "status": "date_failed",
            "sessions": [],
        }

    logger.info(
        "⏳ 날짜 클릭 후 CGV 회차 로딩 대기"
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

        if latest_schedule:

            age = (
                time.time()
                - latest_schedule["time"]
            )

            if age <= 10:

                sessions = parse_sessions(
                    latest_schedule["data"],
                    scn_ymd,
                )

                if not test_mode:

                    for item in sessions:
                        notify_session(item)

                return {
                    "status": "ok",
                    "sessions": sessions,
                }

        await asyncio.sleep(
            0.5
        )

    # --------------------------------------------------------
    # JSON 응답이 안 잡혔더라도
    # 화면 자체에서 오디세이/IMAX가 보이는지 확인
    # --------------------------------------------------------

    try:

        body = await page.locator(
            "body"
        ).inner_text(
            timeout=3000
        )

        lower = body.lower()

        if (
            any(
                alias.lower()
                in lower
                for alias in MOVIE_ALIASES
            )
            and
            any(
                keyword
                in lower
                for keyword in FORMAT_KEYWORDS
            )
        ):

            logger.info(
                "📺 화면에서 오디세이/IMAX 텍스트 확인"
            )

            return {
                "status": "screen_loaded",
                "sessions": [],
            }

    except Exception:
        pass

    logger.warning(
        "⚠️ %s 시간표 응답 없음",
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

        statuses = {}
        sessions = []

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

            sessions.extend(
                result["sessions"]
            )

            if result["status"] == "blocked":
                logger.warning(
                    "🚫 차단 감지. 검사 중지"
                )
                break

            if index < len(dates) - 1:

                await asyncio.sleep(
                    1.0
                )

        last_scan_time = time.time()
        last_scan_result = sessions

        return {
            "dates": dates,
            "statuses": statuses,
            "sessions": sessions,
        }


# ============================================================
# /test 메시지
# ============================================================

def build_test_message(result):

    statuses = result[
        "statuses"
    ]

    sessions = result[
        "sessions"
    ]

    failed = [
        d
        for d, s in statuses.items()
        if s == "date_failed"
    ]

    blocked = any(
        s == "blocked"
        for s in statuses.values()
    )

    if blocked:

        headline = (
            "🔴 <b>CGV 차단 감지</b>"
        )

    elif sessions:

        headline = (
            "🟢 <b>IMAX 잔여 회차 발견</b>"
        )

    elif failed:

        headline = (
            "🟡 <b>날짜 선택 문제</b>"
        )

    else:

        headline = (
            "🟢 <b>날짜 선택 정상</b>"
        )

    lines = [
        "🧪 <b>용아맥 테스트</b>",
        "",
        headline,
        f"🏢 {html.escape(THEATER_NAME)}",
        "🎞️ IMAX",
        "",
    ]

    if sessions:

        lines.append(
            f"🎟️ 발견 회차: <b>{len(sessions)}개</b>"
        )

        for item in sessions[:20]:

            lines.append(
                "• "
                f"{pretty_date(item['date'])} "
                f"{format_time(item.get('start'))}"
                "~"
                f"{format_time(item.get('end'))} "
                f"💺 {item.get('seats', 0)}석"
            )

    else:

        lines.append(
            "현재 발견된 대상 회차 없음"
        )

    if failed:

        lines.append("")
        lines.append(
            "⚠️ 날짜 선택 실패:"
        )

        for date in failed:

            lines.append(
                f"• {pretty_date(date)}"
            )

    return "\n".join(lines)


# ============================================================
# Telegram 업데이트
# ============================================================

def telegram_get_updates(offset):

    if not TELEGRAM_BOT_TOKEN:
        return []

    try:

        response = requests.get(
            telegram_api("getUpdates"),
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


async def handle_telegram_command(
    update
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
            {}
        ).get(
            "id",
            ""
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

    # --------------------------------------------------------
    # start
    # --------------------------------------------------------

    if command == "/start":

        monitor_enabled = True

        await telegram_send_async(
            "🟢 <b>용아맥 감시 시작</b>\n\n"
            f"🏢 {html.escape(THEATER_NAME)}\n"
            "🎞️ IMAX\n"
            f"⏱️ {INTERVAL_SECONDS}초 간격\n\n"
            "🧪 /test\n"
            "📊 /status\n"
            "⏹️ /stop"
        )

        return

    # --------------------------------------------------------
    # stop
    # --------------------------------------------------------

    if command == "/stop":

        monitor_enabled = False

        await telegram_send_async(
            "⏸️ <b>감시 중지</b>"
        )

        return

    # --------------------------------------------------------
    # test
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
    # status
    # --------------------------------------------------------

    if command == "/status":

        monitor = (
            "🟢 실행 중"
            if monitor_enabled
            else "⏸️ 중지"
        )

        cgv = (
            "🔴 차단"
            if blocked_detected
            else "🟢 정상"
        )

        await telegram_send_async(
            "📊 <b>상태</b>\n\n"
            f"프로그램: {monitor}\n"
            f"CGV: {cgv}\n"
            f"발견 회차: {len(last_scan_result)}개"
        )

        return


async def telegram_command_loop():

    global telegram_offset

    if not TELEGRAM_BOT_TOKEN:
        return

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
# 감시
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
                    "🚫 차단 상태. 5분 대기"
                )

                await asyncio.sleep(
                    300
                )

                continue

            await perform_scan(
                test_mode=False
            )

        except Exception:

            logger.exception(
                "감시 오류"
            )

        if monitor_enabled:

            await asyncio.sleep(
                INTERVAL_SECONDS
            )

    logger.info(
        "⏸️ 감시 루프 종료"
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

            if request.startswith(
                "GET /status"
            ):

                body = (
                    "yongsan_imax_bot\n"
                    f"monitor={monitor_enabled}\n"
                    f"blocked={blocked_detected}\n"
                    f"browser={page is not None}\n"
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
# 초기화
# ============================================================

async def initialize():

    await start_browser()

    await wait_for_cgv_ready()

    if await detect_block_page():

        await telegram_send_async(
            "🔴 <b>CGV 차단 감지</b>\n\n"
            "추가 요청을 보내지 않고 대기합니다."
        )

        return

    theater_ok = await ensure_theater()

    if theater_ok:

        logger.info(
            "✅ 용산아이파크몰 준비 완료"
        )

        await telegram_send_async(
            "🟢 <b>용아맥 알리미 준비 완료</b>\n\n"
            f"🎬 {html.escape(', '.join(MOVIE_ALIASES))}\n"
            f"🏢 {html.escape(THEATER_NAME)}\n"
            "🎞️ IMAX\n\n"
            "🧪 /test 테스트\n"
            "▶️ /start 시작\n"
            "⏹️ /stop 중지\n"
            "📊 /status 상태"
        )

    else:

        logger.warning(
            "⚠️ 극장 확인 실패"
        )

        await telegram_send_async(
            "🟡 <b>CGV 접속은 됐지만 "
            "용산 극장 확인이 필요합니다.</b>\n\n"
            "로그의 DATE DOM 정보를 확인합니다."
        )


# ============================================================
# 종료
# ============================================================

async def shutdown():

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
        "🎬 용아맥 오디세이 알리미 시작"
    )

    logger.info(
        "영화: %s",
        ", ".join(MOVIE_ALIASES),
    )

    logger.info(
        "극장: %s (%s)",
        THEATER_NAME,
        THEATER_CODE,
    )

    logger.info(
        "포맷: %s",
        ", ".join(FORMAT_KEYWORDS),
    )

    logger.info(
        "간격: %s초",
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

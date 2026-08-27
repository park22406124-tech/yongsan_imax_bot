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
# 환경변수
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

CGV_URL = "https://cgv.co.kr/cnm/movieBook/cinema"
CGV_API_HOST = "https://api.cgv.co.kr"

THEATER_NAME = os.getenv(
    "THEATER_NAME",
    "용산아이파크몰"
)

THEATER_CODE = os.getenv(
    "THEATER_CODE",
    "0013"
)

MOVIE_ALIASES = [
    x.strip()
    for x in os.getenv(
        "MOVIE_ALIASES",
        "오디세이,The Odyssey,ODYSSEY"
    ).split(",")
    if x.strip()
]

FORMAT_KEYWORDS = [
    x.strip().lower()
    for x in os.getenv(
        "FORMAT_KEYWORDS",
        "IMAX,아이맥스"
    ).split(",")
    if x.strip()
]

INTERVAL_SECONDS = int(
    os.getenv("INTERVAL_SECONDS", "20")
)

HEADLESS = os.getenv(
    "BROWSER_HEADLESS",
    "true"
).lower() == "true"

LOOK_AHEAD_DAYS = int(
    os.getenv("LOOK_AHEAD_DAYS", "7")
)

# 중복 알림 방지
SEEN_TTL_SECONDS = int(
    os.getenv("SEEN_TTL_SECONDS", "3600")
)


# ============================================================
# 로깅
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("yongsan-imax-bot")


# ============================================================
# 전역 상태
# ============================================================

browser = None
context = None
page = None

last_auth_time = 0

seen_notifications = {}


# ============================================================
# Telegram
# ============================================================

def telegram_send(message: str, buttons=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram 환경변수가 없습니다.")
        return

    url = (
        f"https://api.telegram.org/"
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
            timeout=15
        )

        if response.status_code != 200:
            logger.error(
                "Telegram 오류 %s: %s",
                response.status_code,
                response.text[:500]
            )

    except Exception:
        logger.exception("Telegram 전송 실패")


# ============================================================
# 날짜
# ============================================================

def date_string(days_from_today=0):
    target = datetime.now() + timedelta(
        days=days_from_today
    )

    return target.strftime("%Y%m%d")


# ============================================================
# 영화명 검사
# ============================================================

def movie_matches(movie_name: str) -> bool:
    if not movie_name:
        return False

    lower = movie_name.lower()

    return any(
        alias.lower() in lower
        for alias in MOVIE_ALIASES
    )


# ============================================================
# IMAX 검사
# ============================================================

def format_matches(item: dict) -> bool:
    values = [
        str(item.get("scnsNm", "")),
        str(item.get("scnsNm", "")),
        str(item.get("scnNm", "")),
        str(item.get("scnRoomNm", "")),
        str(item.get("formatNm", "")),
        str(item.get("screenNm", "")),
        str(item.get("screenName", "")),
    ]

    combined = " ".join(values).lower()

    return any(
        keyword in combined
        for keyword in FORMAT_KEYWORDS
    )


# ============================================================
# CGV 브라우저 시작
# ============================================================

async def start_browser():
    global browser
    global context
    global page

    logger.info("Chromium 시작")

    playwright = await async_playwright().start()

    browser = await playwright.chromium.launch(
        headless=HEADLESS,
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

    page = await context.new_page()

    await page.add_init_script(
        """
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined
        });
        """
    )

    logger.info("CGV 접속")

    try:
        await page.goto(
            CGV_URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )
    except Exception as e:
        logger.warning(
            "CGV 페이지 접속 중 예외: %s",
            e
        )

    await asyncio.sleep(5)

    logger.info(
        "현재 페이지: %s",
        page.url
    )

    return playwright


# ============================================================
# 브라우저 세션 확인
# ============================================================

async def refresh_browser_session():
    global page
    global last_auth_time

    if page is None:
        return

    logger.info("CGV 브라우저 세션 새로고침")

    try:
        await page.goto(
            CGV_URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        await asyncio.sleep(4)

        last_auth_time = time.time()

    except Exception:
        logger.exception(
            "브라우저 세션 새로고침 실패"
        )


# ============================================================
# CGV API 호출
#
# 핵심:
# 브라우저 페이지의 evaluate() 안에서 fetch를 실행한다.
# 따라서 일반 requests가 아니라 CGV 페이지의 브라우저
# 세션/쿠키 컨텍스트를 그대로 이용한다.
# ============================================================

async def cgv_api_request(
    scn_ymd: str
):
    global page

    if page is None:
        raise RuntimeError(
            "브라우저가 아직 시작되지 않았습니다."
        )

    params = {
        "coCd": "A420",
        "siteNo": THEATER_CODE,
        "scnYmd": scn_ymd,
        "rtctlScopCd": "08",
    }

    query = urlencode(params)

    url = (
        f"{CGV_API_HOST}"
        f"/cnm/atkt/searchMovScnInfo"
        f"?{query}"
    )

    logger.info(
        "CGV 조회: %s",
        scn_ymd
    )

    result = await page.evaluate(
        """
        async (url) => {
            const response = await fetch(url, {
                method: "GET",
                credentials: "include",
                headers: {
                    "Accept": "application/json",
                    "Accept-Language": "ko-KR,ko;q=0.9"
                }
            });

            const text = await response.text();

            return {
                status: response.status,
                text: text
            };
        }
        """,
        url
    )

    status = result.get("status", 0)
    text = result.get("text", "")

    if status == 401:
        raise RuntimeError(
            "CGV API HTTP 401"
        )

    if status >= 400:
        raise RuntimeError(
            f"CGV API HTTP {status}"
        )

    try:
        return json.loads(text)
    except Exception:
        raise RuntimeError(
            f"CGV API JSON 파싱 실패: {text[:500]}"
        )


# ============================================================
# 응답 데이터 추출
# ============================================================

def extract_data(response):
    if not isinstance(response, dict):
        return []

    data = response.get("data")

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in (
            "list",
            "result",
            "items",
            "data",
        ):
            value = data.get(key)

            if isinstance(value, list):
                return value

    for key in (
        "result",
        "items",
        "list",
    ):
        value = response.get(key)

        if isinstance(value, list):
            return value

    return []


# ============================================================
# IMAX 회차 필터
# ============================================================

def find_imax_sessions(response, scn_ymd):
    data = extract_data(response)

    results = []

    for item in data:

        if not isinstance(item, dict):
            continue

        movie_name = str(
            item.get("movNm", "")
        )

        if not movie_matches(movie_name):
            continue

        if not format_matches(item):
            continue

        start = str(
            item.get("scnsrtTm", "")
        )

        end = str(
            item.get("scnendTm", "")
        )

        remaining = item.get(
            "frSeatCnt",
            0
        )

        try:
            remaining = int(remaining or 0)
        except Exception:
            remaining = 0

        result = {
            "movNo": item.get("movNo"),
            "movNm": movie_name,
            "scnYmd": scn_ymd,
            "scnSseq": item.get("scnSseq"),
            "scnsNo": item.get("scnsNo"),
            "scnsNm": (
                item.get("scnsNm")
                or item.get("scnNm")
                or item.get("scnRoomNm")
                or ""
            ),
            "scnsrtTm": start,
            "scnendTm": end,
            "frSeatCnt": remaining,
            "stcnt": item.get("stcnt"),
        }

        results.append(result)

    return results


# ============================================================
# 시간 포맷
# ============================================================

def format_time(value):
    value = str(value or "")

    if len(value) >= 4:
        return (
            f"{value[:2]}:{value[2:4]}"
        )

    return value


# ============================================================
# 예매 버튼
# ============================================================

def reservation_url(item):
    params = {
        "siteNo": THEATER_CODE,
        "scnYmd": item.get("scnYmd", ""),
        "movNo": item.get("movNo", ""),
    }

    return (
        "https://cgv.co.kr/cnm/movieBook/cinema?"
        + urlencode(params)
    )


# ============================================================
# 알림
# ============================================================

def notify_session(item):
    movie = item["movNm"]

    date = item["scnYmd"]

    if len(date) == 8:
        date = (
            f"{date[:4]}-"
            f"{date[4:6]}-"
            f"{date[6:]}"
        )

    start = format_time(
        item["scnsrtTm"]
    )

    end = format_time(
        item["scnendTm"]
    )

    seats = item["frSeatCnt"]

    screen = (
        item.get("scnsNm")
        or "IMAX"
    )

    message = (
        "🚨 <b>용아맥 예매 오픈 감지!</b>\n\n"
        f"🎬 <b>{movie}</b>\n"
        f"📅 {date}\n"
        f"🏢 {THEATER_NAME}\n"
        f"🎞️ {screen}\n"
        f"🕐 {start} ~ {end}\n"
        f"💺 잔여좌석: <b>{seats}</b>\n"
    )

    url = reservation_url(item)

    buttons = [
        [
            {
                "text": f"🎟️ {start} 바로 예매",
                "url": url,
            }
        ]
    ]

    key = (
        f"{item.get('scnYmd')}:"
        f"{item.get('movNo')}:"
        f"{item.get('scnSseq')}:"
        f"{item.get('scnsNo')}"
    )

    now = time.time()

    # 오래된 기록 제거
    expired = [
        k
        for k, timestamp in seen_notifications.items()
        if now - timestamp > SEEN_TTL_SECONDS
    ]

    for k in expired:
        del seen_notifications[k]

    if key in seen_notifications:
        logger.info(
            "중복 알림 생략: %s",
            key
        )
        return

    seen_notifications[key] = now

    telegram_send(
        message,
        buttons
    )

    logger.info(
        "Telegram 알림 전송: %s",
        key
    )


# ============================================================
# 하루 조회
# ============================================================

async def check_date(scn_ymd):
    try:
        response = await cgv_api_request(
            scn_ymd
        )

        sessions = find_imax_sessions(
            response,
            scn_ymd
        )

        logger.info(
            "%s: IMAX 대상 회차 %d개",
            scn_ymd,
            len(sessions)
        )

        for item in sessions:
            notify_session(item)

        return sessions

    except Exception as e:

        logger.error(
            "%s 조회 실패: %s",
            scn_ymd,
            e
        )

        raise


# ============================================================
# 테스트
# ============================================================

async def test_once():
    logger.info(
        "테스트 조회 시작"
    )

    today = date_string(0)

    try:
        sessions = await check_date(
            today
        )

        logger.info(
            "테스트 완료: %d개",
            len(sessions)
        )

        return sessions

    except Exception as e:

        logger.error(
            "테스트 실패: %s",
            e
        )

        return []


# ============================================================
# 메인 감시
# ============================================================

async def monitor():
    global last_auth_time

    playwright = await start_browser()

    try:

        telegram_send(
            "🟢 <b>용아맥 감시 시작</b>\n\n"
            f"극장: {THEATER_NAME}\n"
            f"영화: {', '.join(MOVIE_ALIASES)}\n"
            f"포맷: IMAX\n"
            f"간격: {INTERVAL_SECONDS}초"
        )

        date_index = 0

        while True:

            # 일정 시간마다 브라우저 세션 갱신
            if (
                time.time() - last_auth_time
                > 600
            ):
                await refresh_browser_session()

            scn_ymd = date_string(
                date_index
            )

            try:
                await check_date(
                    scn_ymd
                )

            except Exception as e:

                logger.warning(
                    "조회 오류 발생: %s",
                    e
                )

                # 인증 문제 가능성이 있으므로
                # 브라우저 세션 재생성
                if "401" in str(e):

                    logger.warning(
                        "401 감지 -> 브라우저 세션 갱신"
                    )

                    await refresh_browser_session()

            date_index += 1

            if date_index >= LOOK_AHEAD_DAYS:
                date_index = 0

            await asyncio.sleep(
                max(15, INTERVAL_SECONDS)
            )

    finally:

        try:
            await browser.close()
        except Exception:
            pass

        try:
            await playwright.stop()
        except Exception:
            pass


# ============================================================
# HTTP 서버
#
# Railway health check용.
# FastAPI/Flask를 별도로 설치하지 않고
# asyncio 서버로 간단하게 구현.
# ============================================================

async def health_server():
    async def handle(
        reader,
        writer
    ):
        try:
            request = await reader.read(
                4096
            )

            request_text = request.decode(
                "utf-8",
                errors="ignore"
            )

            first_line = (
                request_text
                .splitlines()[0]
                if request_text
                else ""
            )

            if first_line.startswith(
                "GET /test"
            ):

                await test_once()

                body = (
                    "CGV test completed"
                )

            elif first_line.startswith(
                "GET /status"
            ):

                body = (
                    "🟢 용아맥 감시 프로세스 작동 중"
                )

            else:

                body = (
                    "🟢 yongsan-imax-bot running"
                )

            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/plain; "
                "charset=utf-8\r\n"
                f"Content-Length: "
                f"{len(body.encode('utf-8'))}\r\n"
                "Connection: close\r\n"
                "\r\n"
                f"{body}"
            )

            writer.write(
                response.encode("utf-8")
            )

            await writer.drain()

        except Exception:
            logger.exception(
                "health request 처리 실패"
            )

        finally:
            writer.close()

    port = int(
        os.getenv("PORT", "8080")
    )

    server = await asyncio.start_server(
        handle,
        "0.0.0.0",
        port
    )

    logger.info(
        "Health server 시작: %d",
        port
    )

    async with server:
        await server.serve_forever()


# ============================================================
# ENTRY POINT
# ============================================================

async def main():

    if not TELEGRAM_BOT_TOKEN:
        logger.warning(
            "TELEGRAM_BOT_TOKEN이 없습니다."
        )

    if not TELEGRAM_CHAT_ID:
        logger.warning(
            "TELEGRAM_CHAT_ID가 없습니다."
        )

    await asyncio.gather(
        health_server(),
        monitor(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        logger.info(
            "프로그램 종료"
        )

    except Exception:
        logger.exception(
            "치명적 오류"
    )

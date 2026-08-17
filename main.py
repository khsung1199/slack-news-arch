#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
뉴스 -> Slack 자동 게시 봇
- RSS(feedparser) 우선, HTML(BeautifulSoup CSS 셀렉터) 폴백
- URL 해시 기반 중복 제거 (state/seen.json)
- Slack Block Kit: 제목=하이퍼링크, 아래 요약 + 시간

실행:
    $env:SLACK_BOT_TOKEN = Read-Host "토큰"
    python main.py --dry-run      # 게시 없이 콘솔 확인
    python main.py                # 실제 게시
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

# --------------------------- 설정 ---------------------------
KST = timezone(timedelta(hours=9))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")
STATE_PATH = os.path.join(BASE_DIR, "state", "seen.json")
TOKEN_PATH = os.path.join(BASE_DIR, "token.txt")
SLACK_API = "https://slack.com/api/chat.postMessage"
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, text/html;q=0.9",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}
FETCH_RETRY = 3  # 429 / 5xx 재시도 횟수
FETCH_BACKOFF = 4.0  # 재시도 대기 기준(초)
SEEN_LIMIT = 3000  # 캐시 무한 증가 방지
REQ_TIMEOUT = 15

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("newsbot")


# --------------------------- 유틸 ---------------------------
def load_config() -> dict:
    """config.yaml 로드. 파일 없으면 즉시 종료."""
    if not os.path.exists(CONFIG_PATH):
        log.error("config.yaml 이 없습니다: %s", CONFIG_PATH)
        sys.exit(1)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except UnicodeDecodeError:
        log.error("config.yaml 인코딩 오류 - 메모장에서 UTF-8로 다시 저장하세요.")
        sys.exit(1)
    except yaml.YAMLError as e:
        log.error("config.yaml 문법 오류: %s", e)
        sys.exit(1)


def load_token() -> str:
    """토큰 조회 순서: 환경변수 -> token.txt (스케줄러 실행 시 env 가 없으므로)."""
    tok = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if tok:
        return tok
    try:
        with open(TOKEN_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except FileNotFoundError:
        pass
    return ""


def load_state() -> list:
    """이미 게시한 기사 키 목록."""
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f).get("seen", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_state(seen: list) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {"seen": seen[-SEEN_LIMIT:], "updated": datetime.now(KST).isoformat()},
            f,
            ensure_ascii=False,
            indent=1,
        )


def url_key(url: str) -> str:
    """추적 파라미터를 제거한 URL 해시 = 중복 판정 키."""
    clean = re.sub(r"[?&](utm_[^&]+|fbclid|gclid)=[^&]*", "", url)
    return hashlib.sha1(clean.encode("utf-8")).hexdigest()[:16]


def clean_text(raw: str, limit: int) -> str:
    """HTML 태그/공백 제거 후 limit 길이로 자름."""
    if not raw:
        return ""
    text = BeautifulSoup(raw, "html.parser").get_text(" ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + "\u2026" if len(text) > limit else text


def dedupe_summary(title: str, summary: str) -> str:
    """요약이 제목의 반복이면 버림. (구글뉴스 RSS 등은 description이 제목 링크뿐)"""
    if not summary:
        return ""
    norm = lambda t: re.sub(r"[^0-9a-z가-힣]", "", t.lower())
    nt, ns = norm(title), norm(summary)
    if not ns:
        return ""
    # 요약이 제목에 포함되거나, 제목이 요약의 대부분을 차지하면 무의미
    if ns in nt or nt in ns:
        return ""
    if len(nt) and len(ns) and (len(nt) / len(ns)) > 0.8 and ns.startswith(nt[:20]):
        return ""
    return summary


# 한국어 문장 경계: '다.' / '요.' 뒤에 공백이 없어도 분리 (RSS 본문에 흔함)
_SENT_SPLIT = re.compile(
    r"(?<=다\.)(?=[^\s\d])"  # ...나눴다.이번  ->  분리
    r"|(?<=요\.)(?=[^\s\d])"
    r"|(?<=[.!?])\s+"  # 일반 문장 끝 + 공백
)

# 문장이 제대로 끝났는지 (마침표/물음표/느낌표로 종료)
_COMPLETE = re.compile(r"[.!?]\s*$")


def _tidy(text: str) -> str:
    """요약용 잡음 제거: 출처 인용표기, Q/A 라벨, 잘림 흔적."""
    t = text.replace("\u2026", " ").replace("...", " ")
    t = re.sub(r"\[[\d\s.,\-]+\]", " ", t)  # [12-0127, 2012.4.3.]
    t = re.sub(r"^\s*[QA]\s*[.:]?\s*\(?(질문|답변)\)?\s*", "", t)
    t = re.sub(r"\b[A-Z]\s*\.\s*$", "", t)  # 끝에 남은 A.
    t = re.sub(r"(법제처|출처)\s*$", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def to_lines(summary: str, max_lines: int, max_len: int) -> str:
    """완결된 문장만 골라 max_lines 줄까지. 절대 …로 자르지 않음."""
    if not summary:
        return ""

    text = _tidy(summary)
    if not text:
        return ""

    parts = [_tidy(p) for p in _SENT_SPLIT.split(text)]
    parts = [p for p in parts if p]

    lines, total = [], 0
    for p in parts:
        if len(lines) >= max_lines:
            break
        if not _COMPLETE.search(p):  # 마침표 없음 = 잘린 조각 -> 버림
            continue
        if lines and total + len(p) > max_len:
            break
        lines.append(p)
        total += len(p)

    if lines:
        return "\n".join(lines)

    # 완결 문장이 없으면(RSS가 본문을 잘라 보낸 경우):
    # 마지막 어절을 버려 어중간한 글자로 끝나지 않게 정리
    cut = text[:max_len]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,·-") + "."


def parse_published(entry) -> datetime:
    """RSS 발행시각. 타임존이 없는 피드는 feedparser 가 UTC 로 해석함.
    파싱 불가면 None."""
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    if not st:
        return None
    try:
        return datetime(*st[:6], tzinfo=timezone.utc)
    except ValueError:
        return None


def match_keywords(text: str, keywords: list) -> bool:
    """keywords 가 비어 있으면 전체 통과."""
    if not keywords:
        return True
    low = text.lower()
    return any(str(k).lower() in low for k in keywords)


# -------------------------- 수집기 --------------------------
def fetch_url(url: str, label: str = ""):
    """429 / 5xx 는 백오프 후 재시도. (response, 오류메시지) 반환."""
    wait = FETCH_BACKOFF
    last = ""
    for attempt in range(1, FETCH_RETRY + 1):
        try:
            r = requests.get(url, headers=UA, timeout=REQ_TIMEOUT, allow_redirects=True)
        except Exception as e:
            last = "요청실패 {}".format(type(e).__name__)
        else:
            if r.status_code == 200:
                return r, ""
            last = "HTTP {}".format(r.status_code)
            if r.status_code not in (429, 500, 502, 503, 504):
                return None, last  # 404 등은 재시도 무의미
            wait = min(float(r.headers.get("Retry-After", wait)), 30.0)

        if attempt < FETCH_RETRY:
            log.warning(
                "[%s] %s - %.0f초 후 재시도 (%d/%d)",
                label or url,
                last,
                wait,
                attempt,
                FETCH_RETRY,
            )
            time.sleep(wait)
            wait *= 2
    return None, last


def fetch_rss(src: dict, summary_len: int) -> list:
    """RSS/Atom 피드 파싱."""
    items = []
    name = src.get("name", "?")
    resp, err = fetch_url(src["url"], name)
    if resp is None:
        log.error("[%s] RSS 실패: %s", name, err)
        return items
    try:
        feed = feedparser.parse(resp.content)
    except Exception as e:
        log.error("[%s] RSS 파싱 실패: %s", name, e)
        return items

    for entry in feed.entries[:30]:
        title = clean_text(entry.get("title", ""), 300)
        link = entry.get("link", "")
        if not title or not link:
            continue
        raw_summary = (
            entry.get("summary")
            or entry.get("description")
            or (
                entry.get("content", [{}])[0].get("value")
                if entry.get("content")
                else ""
            )
            or ""
        )
        summary = dedupe_summary(title, clean_text(raw_summary, summary_len))
        items.append(
            {
                "title": title,
                "link": link,
                "summary": summary,
                "source": src.get("name", "RSS"),
                "published": parse_published(entry),
            }
        )
    return items


def fetch_html(src: dict, summary_len: int) -> list:
    """CSS 셀렉터 기반 HTML 스크래핑."""
    items = []
    sel = src.get("selectors", {}) or {}
    if not sel.get("item") or not sel.get("title"):
        log.warning("[%s] selectors.item / title 누락", src.get("name", "?"))
        return items

    resp, err = fetch_url(src["url"], src.get("name", "?"))
    if resp is None:
        log.error("[%s] HTML 실패: %s", src.get("name", "?"), err)
        return items
    try:
        resp.encoding = resp.apparent_encoding or "utf-8"
        soup = BeautifulSoup(resp.text, "lxml")
    except Exception as e:
        log.error("[%s] HTML 파싱 실패: %s", src.get("name", "?"), e)
        return items

    for node in soup.select(sel["item"])[:30]:
        t_el = node.select_one(sel["title"])
        if not t_el:
            continue
        title = clean_text(t_el.get_text(), 300)

        l_el = node.select_one(sel.get("link") or sel["title"])
        href = (l_el.get("href") if l_el else None) or ""
        if not title or not href:
            continue
        link = urljoin(src["url"], href)  # 상대경로 -> 절대경로

        summary = ""
        if sel.get("summary"):
            s_el = node.select_one(sel["summary"])
            if s_el:
                summary = dedupe_summary(
                    title, clean_text(s_el.get_text(), summary_len)
                )

        items.append(
            {
                "title": title,
                "link": link,
                "summary": summary,
                "source": src.get("name", "HTML"),
                "published": None,  # HTML 목록에는 발행시각이 없음
            }
        )

    if not items:
        log.warning("[%s] 0건 - 셀렉터가 변경됐을 가능성", src.get("name", "?"))
    return items


# -------------------------- Slack --------------------------
def esc(text: str) -> str:
    """Slack mrkdwn 이스케이프: & < > 만 처리."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_blocks(item: dict, opts: dict) -> list:
    """제목 = *<url|[출처] 원문제목>*, 아래 요약을 문장별 줄바꿈으로."""
    label = "[{}] {}".format(item["source"], item["title"])
    body = "*<{}|{}>*".format(item["link"], esc(label))

    summary = to_lines(
        item["summary"],
        opts.get("summary_lines", 2),
        opts.get("summary_len", 220),
    )
    if summary:
        body += "\n" + esc(summary)

    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": body}}]

    if opts.get("show_footer", True):
        stamp = item.get("published") or datetime.now(KST)
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": stamp.astimezone(KST).strftime("%Y-%m-%d %H:%M"),
                    }
                ],
            }
        )

    blocks.append({"type": "divider"})
    return blocks


def post_slack(token: str, channel: str, item: dict, opts: dict) -> bool:
    """단일 기사 게시. 429 는 1회 재시도."""
    payload = {
        "channel": channel,
        "text": "[{}] {}".format(item["source"], item["title"]),  # 알림/폴백용
        "blocks": build_blocks(item, opts),
        "unfurl_links": False,  # 링크 미리보기 중복 방지
        "unfurl_media": False,
    }
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json; charset=utf-8",
    }

    for attempt in (1, 2):
        try:
            r = requests.post(
                SLACK_API, headers=headers, json=payload, timeout=REQ_TIMEOUT
            )
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 3))
                log.warning("rate limit - %ds 대기", wait)
                time.sleep(wait)
                continue
            data = r.json()
            if data.get("ok"):
                return True
            log.error("Slack 오류: %s", data.get("error"))
            return False
        except Exception as e:
            log.error("Slack 전송 예외(%d회): %s", attempt, e)
            time.sleep(2)
    return False


# --------------------------- 메인 ---------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--dry-run", action="store_true", help="Slack 전송 없이 결과만 출력"
    )
    args = ap.parse_args()

    cfg = load_config()
    token = load_token()
    if not token and not args.dry_run:
        log.error(
            "토큰을 찾을 수 없습니다. 환경변수 SLACK_BOT_TOKEN 또는 "
            "%s 파일을 준비하세요.",
            TOKEN_PATH,
        )
        sys.exit(1)

    slack_cfg = cfg.get("slack", {}) or {}
    channel = slack_cfg.get("channel", "")
    if not channel and not args.dry_run:
        log.error("config.yaml 의 slack.channel 이 비어 있습니다.")
        sys.exit(1)

    max_post = int(slack_cfg.get("max_per_run", 5))
    summary_len = int(slack_cfg.get("summary_len", 220))
    opts = {
        "summary_lines": int(slack_cfg.get("summary_lines", 2)),
        "summary_len": summary_len,
        "show_footer": bool(slack_cfg.get("show_footer", True)),
    }
    # 소스별 상한: 0 이면 제한 없음. 한 소스가 전체를 독점하는 것을 방지
    per_source_max = int(slack_cfg.get("per_source_max", 0))
    # 이 시각보다 오래된 기사는 게시하지 않음
    cutoff = datetime.now(timezone.utc) - timedelta(
        hours=float(cfg.get("max_age_hours", 24))
    )

    seen = load_state()
    seen_set = set(seen)
    posted = 0

    sources = cfg.get("sources", []) or []
    dead = []

    for src in sources:
        kind = src.get("type", "rss")
        items = (
            fetch_rss(src, summary_len * 3)
            if kind == "rss"
            else fetch_html(src, summary_len * 3)
        )
        log.info("[%s] %d건 수집", src.get("name", "?"), len(items))
        if not items:
            dead.append(src.get("name", "?"))

        # 발행시각 기준으로 최신 기사만 남기고 최신순 정렬.
        # 발행시각을 모르는 기사는 오래된 것인지 판단할 수 없어 게시하지 않는다.
        fresh, stale, undated = [], 0, 0
        for item in items:
            if item["published"] is None:
                undated += 1
            elif item["published"] < cutoff:
                stale += 1
            else:
                fresh.append(item)
        fresh.sort(key=lambda it: it["published"], reverse=True)
        if stale or undated:
            log.info(
                "[%s] 제외 - 오래된 기사 %d건, 발행시각 없음 %d건",
                src.get("name", "?"),
                stale,
                undated,
            )
        items = fresh

        # 이 소스 전용 상한 (소스별 max 로 개별 지정 가능)
        src_cap = int(src.get("max", per_source_max)) or 10**6
        src_posted = 0

        for item in items:
            if posted >= max_post:
                log.info("max_per_run(%d) 도달 - 나머지는 다음 실행", max_post)
                break
            if src_posted >= src_cap:
                log.info(
                    "[%s] 소스 상한(%d) 도달 - 다음 소스로",
                    src.get("name", "?"),
                    src_cap,
                )
                break

            key = url_key(item["link"])
            if key in seen_set:
                continue
            if not match_keywords(
                item["title"] + " " + item["summary"], src.get("keywords", []) or []
            ):
                continue

            if args.dry_run:
                preview = to_lines(
                    item["summary"], opts["summary_lines"], opts["summary_len"]
                )
                print(
                    "\n[{}] {}\n{}\n{}".format(
                        item["source"], item["title"], item["link"], preview
                    )
                )
                seen_set.add(key)  # 같은 실행 안에서 중복 출력 방지용 (저장 안 함)
                posted += 1
                src_posted += 1
                continue

            if post_slack(token, channel, item, opts):
                seen_set.add(key)
                seen.append(key)
                posted += 1
                src_posted += 1
                time.sleep(1.2)  # Slack tier 권장 간격

        if posted >= max_post:
            break

    if not args.dry_run:  # dry-run 이 seen 을 오염시키면 실제 게시가 건너뛰어진다
        save_state(seen)
    log.info("완료 - %d건 게시", posted)

    # 클라우드에서 조용히 실패하는 것을 막기 위한 상태 보고
    if dead:
        log.warning("수집 0건 소스: %s", ", ".join(dead))
    if sources and len(dead) == len(sources):
        log.error("모든 소스 수집 실패 - IP 차단 또는 피드 URL 변경 의심")
        sys.exit(1)  # Actions 를 빨간불로 만들어 알림


if __name__ == "__main__":
    main()

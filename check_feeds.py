#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSS 후보 URL 자동 탐지기
- 사이트별로 흔한 RSS 경로를 순서대로 두드려 보고, 살아있는 피드를 보고합니다.
- <description> 에 실제 문장이 있는지도 판정 → 요약 사용 가능 여부 확인

실행:
    python check_feeds.py
    python check_feeds.py https://example.com    # 임의 사이트 추가 검사
"""

import re
import sys
import time
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
}
TIMEOUT = 12
DELAY = 2.0  # 요청 사이 대기(초) - 429 방지
RETRY_429 = 3  # 429 응답 시 재시도 횟수

# 사이트별 후보 경로 (일반 CMS 관례 기준)
CANDIDATES = {
    # 확인 완료 - 재검증용
    "https://c3korea.com/": ["/feed/"],
    "https://masilground.com/": ["/rss"],
    "https://www.ancnews.kr/": ["/rss/allArticle.xml"],
    # 429 로 미확인 - 이번엔 지연/재시도 적용
    "https://masilwide.com/interview/": [
        "/interview/feed/",
        "/feed/",
        "/category/interview/feed/",
        "/?feed=rss2",
        "/comments/feed/",
    ],
}


def polite_get(url: str):
    """429(요청 과다) 응답 시 대기 후 재시도하는 GET."""
    wait = DELAY
    for attempt in range(RETRY_429 + 1):
        time.sleep(wait if attempt else DELAY)
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT, allow_redirects=True)
        except Exception as e:
            return None, "요청실패: {}".format(type(e).__name__)
        if r.status_code != 429:
            return r, ""
        # Retry-After 헤더 우선, 없으면 지수 백오프
        wait = float(r.headers.get("Retry-After", wait * 3))
        wait = min(wait, 30.0)
        if attempt < RETRY_429:
            print(
                "      429 - {:.0f}초 대기 후 재시도 ({}/{})".format(
                    wait, attempt + 1, RETRY_429
                )
            )
    return r, "HTTP 429 (재시도 소진)"


def probe_feed(url: str):
    """URL이 유효한 피드인지 검사. (ok, 항목수, 요약있음, 메모)"""
    r, err = polite_get(url)
    if r is None:
        return False, 0, False, err
    if err:
        return False, 0, False, err

    if r.status_code != 200:
        return False, 0, False, "HTTP {}".format(r.status_code)

    ctype = r.headers.get("Content-Type", "")
    feed = feedparser.parse(r.content)
    if not feed.entries:
        return False, 0, False, "피드 아님 ({})".format(ctype.split(";")[0] or "?")

    # 요약 필드에 실제 문장이 있는지 (제목 반복이면 무의미)
    has_summary = False
    for e in feed.entries[:5]:
        title = re.sub(r"\W", "", (e.get("title") or "")).lower()
        raw = e.get("summary") or e.get("description") or ""
        body = re.sub(r"\W", "", BeautifulSoup(raw, "html.parser").get_text()).lower()
        if body and title not in body and len(body) > len(title) * 1.4:
            has_summary = True
            break

    return True, len(feed.entries), has_summary, feed.feed.get("title", "")[:40]


def discover_from_html(page_url: str):
    """페이지 <head> 의 rel=alternate 링크에서 피드 자동 발견."""
    found = []
    try:
        r, _ = polite_get(page_url)
        if r is None or r.status_code != 200:
            return found
        soup = BeautifulSoup(r.text, "html.parser")
        for link in soup.find_all("link", rel=lambda v: v and "alternate" in v):
            t = (link.get("type") or "").lower()
            if "rss" in t or "atom" in t or "xml" in t:
                href = link.get("href")
                if href:
                    found.append(urljoin(page_url, href))
    except Exception:
        pass
    return found


def check_site(page_url: str, paths: list):
    print("\n" + "=" * 66)
    print("SITE  {}".format(page_url))
    print("=" * 66)

    root = "{0.scheme}://{0.netloc}".format(urlparse(page_url))
    tried = []

    # 1) HTML <head> 자동 발견 (가장 정확)
    auto = discover_from_html(page_url)
    if auto:
        print("  [head 링크에서 발견]")
        for u in auto:
            tried.append(u)
    else:
        print("  [head 링크 없음 - 후보 경로로 시도]")

    # 2) 관례적 후보 경로
    for p in paths:
        tried.append(urljoin(root, p))

    seen = set()
    hits = []
    for u in tried:
        if u in seen:
            continue
        seen.add(u)
        ok, n, has_sum, note = probe_feed(u)
        mark = "OK " if ok else "-- "
        detail = "{}건, 요약{}".format(n, "있음" if has_sum else "없음") if ok else note
        print("  {}{}\n      {}".format(mark, u, detail))
        if ok:
            hits.append((u, n, has_sum))

    if hits:
        best = max(hits, key=lambda x: (x[2], x[1]))  # 요약 있는 것 우선
        print("\n  >>> 추천: {}".format(best[0]))
        print("      config.yaml 에 type: rss 로 등록하세요.")
    else:
        print("\n  >>> RSS 없음. type: html + 셀렉터 방식으로 가야 합니다.")


def main():
    targets = dict(CANDIDATES)
    for extra in sys.argv[1:]:
        targets[extra] = ["/feed", "/rss", "/rss.xml", "/atom.xml", "/feed/"]

    for page_url, paths in targets.items():
        check_site(page_url, paths)

    print("\n" + "=" * 66)
    print("완료. 'OK' 로 나온 URL 을 config.yaml 의 url 값에 넣으세요.")
    print("=" * 66)


if __name__ == "__main__":
    main()

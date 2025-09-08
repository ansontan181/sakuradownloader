# -*- coding: utf-8 -*-
"""
抓取网页中的 m3u8 链接并输出到 CSV。
在 Render/Docker 中运行，已加：
- Chromium 启动参数：--no-sandbox, --disable-dev-shm-usage
- 更稳的异常处理与日志输出
- 轻度滚动与点击播放，触发网络请求
"""

import os
import re
import csv
import json
from pathlib import Path
from typing import Set, List, Dict, Optional

from playwright.sync_api import sync_playwright, Page, Frame

# 读取/写入文件名
INPUT_FILE = "urls.txt"
OUTPUT_CSV = "m3u8_results.csv"

# 点击播放常见选择器
PLAY_CLICK_SELECTORS = [
    ".vjs-big-play-button",
    ".plyr__control--overlaid",
    "button[title*=播放]",
    "button[aria-label*=播放]",
    ".btn-play,.start,.play",
    "div[class*=play]",
]

# 统一 UA（与你的后端保持一致）
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def log(msg: str):
    print(msg, flush=True)


def load_urls(path: str) -> List[str]:
    file = Path(path)
    if not file.exists():
        raise FileNotFoundError(f"找不到 {path}")
    urls: List[str] = []
    for line in file.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            urls.append(s)
    return urls


def looks_like_m3u8(url: str, ct: Optional[str]) -> bool:
    u = (url or "").lower()
    if ".m3u8" in u:
        return True
    if ct:
        ct = ct.lower()
        if "application/vnd.apple.mpegurl" in ct or "application/x-mpegurl" in ct:
            return True
    return False


def infer_m3u8_from_ts(ts_url: str) -> List[str]:
    """
    看到 .ts 请求时，猜测同目录下常见的 m3u8 文件名。
    """
    base = ts_url.rsplit("/", 1)[0]
    return [
        f"{base}/index.m3u8",
        f"{base}/playlist.m3u8",
        f"{base}/master.m3u8",
        f"{base}/video.m3u8",
    ]


def try_click_play(frame_like) -> bool:
    """
    在主页面或子 frame 上尝试点击播放按钮。
    """
    for sel in PLAY_CLICK_SELECTORS:
        try:
            el = frame_like.locator(sel).first
            if el.count() > 0:
                el.click(timeout=1500)
                return True
        except Exception:
            continue
    return False


def human_title(page: Page) -> str:
    try:
        t = (page.title() or "").strip()
        return t if t else "unknown"
    except Exception:
        return "unknown"


def crawl_one(page: Page, page_url: str) -> List[Dict[str, str]]:
    """
    打开单个页面，监听所有响应，提取 m3u8。
    返回该页面的结果字典列表。
    """
    found_m3u8: Set[str] = set()
    hinted_m3u8: Set[str] = set()

    def on_response(resp):
        try:
            url = resp.url or ""
            headers = resp.headers or {}
            ct = headers.get("content-type", "")
            # 1) 直接命中 m3u8
            if looks_like_m3u8(url, ct):
                found_m3u8.add(url)
                return

            # 2) JSON 里嵌有 m3u8
            if "application/json" in ct.lower() or url.lower().endswith(".json"):
                try:
                    # 读取文本（大文件/流式也基本可行）
                    txt = resp.text()
                    blob = txt
                    # 尝试美化 JSON 再正则，容错
                    s = txt.strip()
                    if s.startswith("{") or s.startswith("["):
                        try:
                            d = json.loads(txt)
                            blob = json.dumps(d, ensure_ascii=False)
                        except Exception:
                            pass
                    for m in re.findall(r"https?://[^\"'\\s]+?\.m3u8[^\"'\\s]*", blob):
                        found_m3u8.add(m)
                except Exception:
                    pass

            # 3) 看到 ts 则猜测目录下的 m3u8 常名
            if ".ts" in url.lower():
                for cand in infer_m3u8_from_ts(url):
                    hinted_m3u8.add(cand)
        except Exception:
            # 不让监听崩
            pass

    page.on("response", on_response)

    log(f"[OPEN] {page_url}")
    try:
        page.goto(page_url, wait_until="domcontentloaded", timeout=45_000)
    except Exception:
        # 有些站点会劫持/跳转，尽量不因 goto 失败直接返回
        pass

    # 轻微滚动/等待加载，触发更多请求
    try:
        page.wait_for_timeout(1500)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight/2);")
        page.wait_for_timeout(1200)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
        page.wait_for_timeout(1200)
    except Exception:
        pass

    # 尝试点击播放
    try_click_play(page)
    try:
        for fr in page.frames:
            try_click_play(fr)
    except Exception:
        pass

    # 再等网络请求
    page.wait_for_timeout(5_000)
    page.wait_for_timeout(3_000)

    # 页面标题（用于标注）
    title = human_title(page)

    results: List[Dict[str, str]] = []
    if not found_m3u8 and hinted_m3u8:
        # 仅推断，需二次验证
        for u in sorted(hinted_m3u8):
            results.append(
                {
                    "title": title,
                    "page_url": page_url,
                    "m3u8_url": u,
                    "referer": page_url,
                    "user_agent": UA,
                    "note": "推断自TS（需验证）",
                }
            )
        log(f"[HINT] 仅推断到候选 m3u8：{len(hinted_m3u8)} | 标题: {title}")
    elif found_m3u8:
        for u in sorted(found_m3u8):
            results.append(
                {
                    "title": title,
                    "page_url": page_url,
                    "m3u8_url": u,
                    "referer": page_url,
                    "user_agent": UA,
                    "note": "捕获",
                }
            )
        log(f"[OK] 捕获 m3u8：{len(found_m3u8)} | 标题: {title}")
    else:
        log(f"[WARN] 未抓到 m3u8：{page_url}")

    return results


def main():
    urls = load_urls(INPUT_FILE)
    all_rows: List[Dict[str, str]] = []

    with sync_playwright() as p:
        # 关键：容器内必须关闭沙箱并使用 shm 降低内存占用
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent=UA,
            locale="zh-CN",
            ignore_https_errors=True,  # 某些站点证书不规范
        )

        try:
            for page_url in urls:
                page = context.new_page()
                rows = crawl_one(page, page_url)
                all_rows.extend(rows)
                try:
                    page.close()
                except Exception:
                    pass
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass

    # 去重：按 (m3u8_url, title)
    uniq: Dict[tuple, Dict[str, str]] = {}
    for r in all_rows:
        uniq[(r["m3u8_url"], r["title"])] = r
    all_rows = list(uniq.values())

    # 写 CSV
    header = ["title", "page_url", "m3u8_url", "referer", "user_agent", "note"]
    out_file = Path(OUTPUT_CSV)
    with out_file.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        writer.writerows(all_rows)

    log(f"\n[DONE] 共写入 {len(all_rows)} 条到 {OUTPUT_CSV}")


if __name__ == "__main__":
    main()

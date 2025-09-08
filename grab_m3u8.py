import csv, re, json, os, urllib.parse
from playwright.sync_api import sync_playwright

INPUT_FILE = "urls.txt"
OUTPUT_CSV = "m3u8_results.csv"

PLAY_CLICK_SELECTORS = [
    ".vjs-big-play-button",
    ".plyr__control--overlaid",
    "button[title*=播放]",
    "button[aria-label*=播放]",
    ".btn-play,.start,.play"
]

def load_urls(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"找不到 {path}")
    with open(path, "r", encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]

def looks_like_m3u8(url:str, ct:str|None):
    u = url.lower()
    if ".m3u8" in u: return True
    if ct:
        ct = ct.lower()
        if "application/vnd.apple.mpegurl" in ct or "application/x-mpegurl" in ct:
            return True
    return False

def infer_m3u8_from_ts(url:str):
    base = url.rsplit("/", 1)[0]
    cands = [f"{base}/index.m3u8", f"{base}/playlist.m3u8", f"{base}/master.m3u8", f"{base}/video.m3u8"]
    return cands

def try_click_play(frame_like):
    for sel in PLAY_CLICK_SELECTORS:
        try:
            frame_like.locator(sel).first.click(timeout=1500)
            return True
        except Exception:
            continue
    return False

# ============ 规范化与强去重 ============

def normalize_m3u8(url: str) -> str:
    """将 m3u8 地址规范化：仅保留到第一个 .m3u8 为止，去掉 query/fragment，host 小写。"""
    if not url:
        return url
    m = re.search(r'https?://[^"\']+?\.m3u8', url, re.I)
    core = m.group(0) if m else url
    parts = urllib.parse.urlsplit(core)
    core2 = urllib.parse.urlunsplit((
        parts.scheme.lower(),
        parts.netloc.lower(),
        parts.path,
        "", ""   # 去掉 query / fragment
    ))
    return core2

def prefer_master_then_unique(urls: list[str]) -> list[str]:
    """如同目录下既有 master.m3u8 又有 index/playlist 等，优先仅保留 master。"""
    seen, ordered = set(), []
    for u in urls:
        k = normalize_m3u8(u)
        if k not in seen:
            seen.add(k)
            ordered.append(k)

    masters = [u for u in ordered if u.endswith("master.m3u8")]
    if not masters:
        return ordered

    keep_dirs = {u.rsplit("/", 1)[0] for u in masters}
    result, kept_dir = [], set()
    for u in ordered:
        d = u.rsplit("/", 1)[0]
        if d in keep_dirs and u.endswith("master.m3u8"):
            if d not in kept_dir:
                kept_dir.add(d)
                result.append(u)
    return result or ordered

# =======================================

def main():
    urls = load_urls(INPUT_FILE)
    results = []

    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA, locale="zh-CN")

        for page_url in urls:
            page = context.new_page()
            found_m3u8 = set()
            hinted_m3u8 = set()

            def on_response(resp):
                url = resp.url
                ct = resp.headers.get("content-type","")
                if looks_like_m3u8(url, ct):
                    found_m3u8.add(url)
                    return
                if "application/json" in ct.lower() or url.lower().endswith(".json"):
                    try:
                        txt = resp.text()
                        blob = txt
                        if txt.strip().startswith("{") or txt.strip().startswith("["):
                            try:
                                data = json.loads(txt)
                                blob = json.dumps(data, ensure_ascii=False)
                            except:
                                pass
                        for m in re.findall(r"https?://[^\"'\\s]+?\.m3u8[^\"'\\s]*", blob):
                            found_m3u8.add(m)
                    except Exception:
                        pass
                if ".ts" in url.lower():
                    for cand in infer_m3u8_from_ts(url):
                        hinted_m3u8.add(cand)

            page.on("response", on_response)

            print(f"[OPEN] {page_url}")
            try:
                page.goto(page_url, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass

            try_click_play(page)
            try:
                for fr in page.frames:
                    try_click_play(fr)
            except Exception:
                pass

            page.wait_for_timeout(7000)
            page.wait_for_timeout(5000)

            try:
                title = page.title().strip()
            except Exception:
                title = "unknown"

            if not found_m3u8 and hinted_m3u8:
                final = prefer_master_then_unique(sorted(hinted_m3u8))
                for u in final:
                    results.append({
                        "title": title,
                        "page_url": page_url,
                        "m3u8_url": u,
                        "referer": page_url,
                        "user_agent": UA,
                        "note": "推断自TS（需验证）"
                    })
                print(f"[HINT] 仅推断到候选 m3u8：{len(final)} | 标题: {title}")
            elif found_m3u8:
                final = prefer_master_then_unique(sorted(found_m3u8))
                for u in final:
                    results.append({
                        "title": title,
                        "page_url": page_url,
                        "m3u8_url": u,
                        "referer": page_url,
                        "user_agent": UA,
                        "note": "捕获"
                    })
                print(f"[OK] 捕获 m3u8：{len(final)} | 标题: {title}")
            else:
                print(f"[WARN] 未抓到 m3u8：{page_url}")

            page.close()

        context.close()
        browser.close()

    # ===== 最终过滤 & 去重 =====
    # 只保留 .m3u8 结尾的 URL
    results = [r for r in results if r["m3u8_url"].lower().endswith(".m3u8")]

    # 按规范化 URL 聚合（保留第一条记录）
    bucket = {}
    for r in results:
        norm = normalize_m3u8(r["m3u8_url"])
        if norm not in bucket:
            r["m3u8_url"] = norm
            bucket[norm] = r

    # 若同目录存在 master 与其它，优先只保留 master
    final_urls = prefer_master_then_unique(list(bucket.keys()))
    results = [bucket[u] for u in final_urls]

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["title","page_url","m3u8_url","referer","user_agent","note"])
        writer.writeheader()
        writer.writerows(results)

    print(f"\n[DONE] 共写入 {len(results)} 条到 {OUTPUT_CSV}")

if __name__ == "__main__":
    main()

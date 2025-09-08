# app.py
import os
import re
import csv
import sys
import json
import time
import uuid
import queue
import shutil
import zipfile
import threading
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional, Iterable

from flask import Flask, request, jsonify, Response, send_file
from flask_cors import CORS

# -------------------------
# 基础配置
# -------------------------
BASE_DIR = Path(__file__).resolve().parent
JOBS_DIR = BASE_DIR / "jobs"
JOBS_DIR.mkdir(exist_ok=True)

app = Flask(__name__, static_folder=str(BASE_DIR), static_url_path="")
# 允许所有来源跨域（也可以替换为你的 GitHub Pages 源域名）
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=False)


@app.after_request
def add_cors_headers(resp):
    """统一给响应补充 CORS 头以及允许预检。"""
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


# -------------------------
# 简单的任务管理（内存 + 文件夹）
# -------------------------
class JobState:
    def __init__(self, job_id: str, workdir: Path):
        self.job_id = job_id
        self.workdir = workdir
        self.log_q: "queue.Queue[str]" = queue.Queue()
        self.done = False
        self.artifact: Optional[Path] = None  # mp4 或 zip 文件路径

    def log(self, msg: str):
        # 同时打印到容器日志 & 放到 SSE
        line = msg.rstrip("\n")
        print(line, flush=True)
        self.log_q.put(line)


JOBS: Dict[str, JobState] = {}


def sanitize_filename(name: str) -> str:
    safe = re.sub(r'[\\/:*?"<>|]+', "_", name or "").strip().strip(".")
    return safe or "video"


def run_and_stream(cmd: list, job: JobState, cwd: Path = None):
    """运行子进程并把输出实时写到日志队列。"""
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        job.log(line.rstrip("\n"))
    returncode = proc.wait()
    if returncode != 0:
        job.log(f"[ERROR] command failed ({returncode}): {' '.join(cmd)}")
    return returncode


def read_m3u8_csv(csv_path: Path) -> list[dict]:
    rows = []
    if not csv_path.exists():
        return rows
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows


def download_with_ffmpeg(row: dict, out_dir: Path, job: JobState) -> Optional[Path]:
    """按你的 PowerShell 脚本逻辑下载一个 m3u8 为 mp4。"""
    title = row.get("title") or "video"
    m3u8 = (row.get("m3u8_url") or "").strip()
    page_url = (row.get("page_url") or "").strip()
    ref = (row.get("referer") or "").strip() or page_url
    ua = (row.get("user_agent") or "").strip() or "Mozilla/5.0"

    if not m3u8:
        job.log("[WARN] skip: empty m3u8_url")
        return None

    # 展开 '?vid=真实m3u8'
    m = re.search(r"\?vid=(https?[^&\s]+)", m3u8)
    if m:
        m3u8 = m.group(1)

    # URL 解码
    try:
        from urllib.parse import unquote
        m3u8 = unquote(m3u8)
    except Exception:
        pass

    # 只取第一个 .m3u8 真实地址（去掉多余尾巴）
    m = re.search(r"https?://[^\"'\s]+?\.m3u8", m3u8)
    if m:
        m3u8 = m.group(0)

    out_base = sanitize_filename(title)
    out = out_dir / f"{out_base}.mp4"
    n = 1
    while out.exists():
        out = out_dir / f"{out_base}_{n}.mp4"
        n += 1

    job.log(f"[downloading] {title}")
    job.log(f"      m3u8 url : {m3u8}")
    job.log(f"      output   : {out.name}")

    args = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-headers", f"User-Agent: {ua}",
        "-headers", f"Referer: {ref}",
        "-i", m3u8,
        "-c", "copy",
        str(out),
    ]
    rc = run_and_stream(args, job, cwd=out_dir)
    if rc != 0:
        job.log(f"[WARN] failed : {title}")
        return None

    try:
        size = out.stat().st_size
        if size < 5_000_000:
            job.log(f"[WARN] small : {out.name} ({size} Bytes)")
        else:
            job.log(f"[OK] done   : {out.name} ({size} Bytes)")
    except FileNotFoundError:
        job.log(f"[WARN] not found after ffmpeg : {out.name}")
        return None
    return out


def pack_zip(files: list[Path], dest_zip: Path):
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(p, arcname=p.name)


def worker(job: JobState, urls_text: str):
    """后台线程：写入 urls.txt → 执行 grab_m3u8.py → 按 CSV 下载 MP4 → 产出单文件或 ZIP。"""
    try:
        workdir = job.workdir
        workdir.mkdir(parents=True, exist_ok=True)
        urls_txt = workdir / "urls.txt"
        urls_txt.write_text(urls_text.strip() + "\n", encoding="utf-8")

        job.log("任务已创建，开始抓取 m3u8 ...")

        # 运行你的抓取脚本（在工作目录中）
        # 该脚本会输出 m3u8_results.csv
        cmd = [sys.executable, str(BASE_DIR / "grab_m3u8.py")]
        rc = run_and_stream(cmd, job, cwd=workdir)
        if rc != 0:
            job.log("[ERROR] 抓取 m3u8 脚本执行失败，请检查日志。")
            job.done = True
            return

        csv_path = workdir / "m3u8_results.csv"
        rows = read_m3u8_csv(csv_path)
        job.log(f"[DONE] 共写入 {len(rows)} 条到 m3u8_results.csv")
        if not rows:
            job.log("[ERROR] 未发现可下载的 m3u8 结果。")
            job.done = True
            return

        job.log("开始下载 MP4（ffmpeg）...")
        out_dir = workdir / "outputs"
        out_dir.mkdir(exist_ok=True)

        produced: list[Path] = []
        for i, row in enumerate(rows, 1):
            job.log(f"[{i}/{len(rows)}] processing ...")
            p = download_with_ffmpeg(row, out_dir, job)
            if p:
                produced.append(p)

        if not produced:
            job.log("[ERROR] 全部下载失败。")
            job.done = True
            return

        if len(produced) == 1:
            job.artifact = produced[0]
            job.log(f"[FINAL] 产物：{job.artifact.name}")
        else:
            dest_zip = workdir / "videos.zip"
            pack_zip(produced, dest_zip)
            job.artifact = dest_zip
            job.log(f"[FINAL] 产物：{job.artifact.name}")

    except Exception as e:
        job.log(f"[FATAL] {type(e).__name__}: {e}")
    finally:
        job.done = True


def create_job(urls_text: str) -> JobState:
    job_id = uuid.uuid4().hex[:12]
    workdir = JOBS_DIR / job_id
    state = JobState(job_id, workdir)
    JOBS[job_id] = state
    threading.Thread(target=worker, args=(state, urls_text), daemon=True).start()
    return state


def stream_logs(job: JobState) -> Iterable[str]:
    """给 SSE 使用：不断从队列拿日志，直到任务 done 且队列清空。"""
    # 心跳间隔，避免中间网络设备断流
    HEARTBEAT_SEC = 12
    last_beat = time.time()

    while True:
        try:
            line = job.log_q.get(timeout=0.5)
            yield line
        except queue.Empty:
            pass

        # 发送心跳
        now = time.time()
        if now - last_beat >= HEARTBEAT_SEC:
            yield "[heartbeat]"
            last_beat = now

        if job.done and job.log_q.empty():
            break


# -------------------------
# 路由
# -------------------------
@app.route("/")
def home():
    return "Backend is running!"

@app.route("/healthz")
def healthz():
    return "ok"


@app.route("/oneclick_start", methods=["POST", "OPTIONS"])
def oneclick_start():
    if request.method == "OPTIONS":
        return ("", 204)
    data = request.get_json(silent=True) or {}
    urls = (data.get("urls") or "").strip()
    if not urls:
        return jsonify({"error": "empty urls"}), 400

    job = create_job(urls)
    return jsonify({"job_id": job.job_id})


@app.route("/oneclick_stream/<job_id>")
def oneclick_stream(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return Response("not found", status=404)

    def gen():
        # 实时日志
        for line in stream_logs(job):
            yield f"data: {line}\n\n"
        # 完成事件（把下载地址告诉前端）
        artifact_path = f"/artifact/{job.job_id}"
        yield f"event: done\ndata: {artifact_path}\n\n"

    headers = {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Access-Control-Allow-Origin": "*",
    }
    return Response(gen(), headers=headers)


@app.route("/artifact/<job_id>")
def artifact(job_id: str):
    job = JOBS.get(job_id)
    if not job or not job.artifact or not job.artifact.exists():
        return Response("not found", status=404)
    # 触发浏览器下载
    return send_file(
        str(job.artifact),
        as_attachment=True,
        download_name=job.artifact.name
    )


# -------------------------
# 本地调试入口（Render 用 gunicorn 启动）
# -------------------------
if __name__ == "__main__":
    # 本地调试：python app.py
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=True)

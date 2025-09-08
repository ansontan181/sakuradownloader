# app.py
import os
import sys
import shutil
import subprocess
import uuid
from pathlib import Path
from datetime import datetime
from flask import (
    Flask, request, send_file, send_from_directory,
    jsonify, Response, url_for
)

# ========= 基本路径 =========
BASE_DIR    = Path(__file__).resolve().parent
URLS_TXT    = BASE_DIR / "urls.txt"
RESULT_CSV  = BASE_DIR / "m3u8_results.csv"
SCRIPT_PY   = BASE_DIR / "grab_m3u8.py"          # 你的 m3u8 抓取脚本
PS1_PATH    = BASE_DIR / "download_from_csv.ps1" # 你的 PowerShell 下载器
DOWNLOADS   = BASE_DIR / "downloads"             # 统一下载根目录
DOWNLOADS.mkdir(exist_ok=True)

# 任务表（非常简单的内存实现）
JOBS = {}  # job_id -> {"urls": str, "artifact": Path|None}

app = Flask(__name__, static_folder=str(BASE_DIR), static_url_path="")


# ========= 工具函数 =========
def _run_grabber():
    """运行 grab_m3u8.py 生成 m3u8_results.csv，返回 (ok, log_text)。"""
    if not SCRIPT_PY.exists():
        return False, f"找不到脚本：{SCRIPT_PY}"
    # 清理旧结果
    if RESULT_CSV.exists():
        try:
            RESULT_CSV.unlink()
        except Exception:
            pass

    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PY)],
        cwd=str(BASE_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=60 * 30
    )
    if proc.returncode != 0:
        return False, proc.stdout
    if not RESULT_CSV.exists():
        return False, "脚本运行后未发现 m3u8_results.csv"
    return True, proc.stdout


def _run_ps1(csv_path: Path, out_dir: Path):
    """
    运行 PowerShell 下载器。
    优先尝试：-CsvPath -OutDir；失败则退化为仅 -CsvPath 并把新 MP4 挪到 out_dir。
    返回 (ok, log_text, produced_files[List[Path]])
    """
    if not PS1_PATH.exists():
        return False, f"找不到 PowerShell 脚本：{PS1_PATH}", []

    before = set(p.resolve() for p in BASE_DIR.glob("*.mp4"))

    # 方案 A：带 -OutDir（推荐）
    cmd_a = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(PS1_PATH),
        "-CsvPath", str(csv_path),
        "-OutDir", str(out_dir)
    ]
    proc_a = subprocess.run(
        cmd_a, cwd=str(BASE_DIR),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        timeout=60 * 60
    )
    if proc_a.returncode == 0:
        files = list(out_dir.glob("*.mp4"))
        if files:
            return True, proc_a.stdout, files
        # 兜底：如果仍落在当前目录
        more = []
        for f in BASE_DIR.glob("*.mp4"):
            if f.resolve() not in before:
                dst = out_dir / f.name
                try:
                    shutil.move(str(f), dst)
                    more.append(dst)
                except Exception:
                    pass
        if more:
            return True, proc_a.stdout, more
        log_acc = "[Info] -OutDir 模式未发现文件，尝试仅 -CsvPath。\n" + proc_a.stdout
    else:
        log_acc = "[Info] -OutDir 模式执行失败，尝试仅 -CsvPath。\n" + proc_a.stdout

    # 方案 B：仅 -CsvPath
    cmd_b = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(PS1_PATH),
        "-CsvPath", str(csv_path)
    ]
    proc_b = subprocess.run(
        cmd_b, cwd=str(BASE_DIR),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        timeout=60 * 60
    )
    if proc_b.returncode != 0:
        return False, (log_acc + "\n" + proc_b.stdout), []

    produced = []
    for f in BASE_DIR.glob("*.mp4"):
        if f.resolve() not in before:
            dst = out_dir / f.name
            try:
                shutil.move(str(f), dst)
                produced.append(dst)
            except Exception:
                pass
    return (True, log_acc + "\n" + proc_b.stdout, produced)


def _zip_dir(dir_path: Path):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_base = BASE_DIR / f"downloads_{stamp}"
    zip_path = shutil.make_archive(str(zip_base), "zip", root_dir=str(dir_path))
    return Path(zip_path)


# ========= 页面与原有三个接口 =========
@app.route("/", methods=["GET"])
def index():
    return send_from_directory(str(BASE_DIR), "index.html")


@app.route("/run", methods=["POST"])
def run_only():
    """只抓 m3u8：可接收 urls（覆盖 urls.txt），运行抓取脚本并返回 CSV。"""
    try:
        data = request.get_json(force=True, silent=True) or {}
        raw_urls = (data.get("urls") or "").strip()
        if raw_urls:
            URLS_TXT.write_text(raw_urls + "\n", encoding="utf-8")
        elif not URLS_TXT.exists():
            return ("缺少 urls.txt 或请求体未提供 urls。", 400)

        ok, log = _run_grabber()
        if not ok:
            return (f"抓取失败：\n{log}", 500)

        return send_file(
            RESULT_CSV,
            mimetype="text/csv; charset=utf-8",
            as_attachment=True,
            download_name="m3u8_results.csv"
        )
    except subprocess.TimeoutExpired:
        return ("执行超时，请减少链接数量或放宽超时。", 500)
    except Exception as e:
        return (f"服务器异常：{e}", 500)


@app.route("/download", methods=["POST"])
def download_from_csv():
    """根据现有 CSV 下载 MP4（调用 PS1），完成后打包 ZIP 或返回单 MP4。"""
    try:
        data = request.get_json(force=True, silent=True) or {}
        csv_path = Path(data.get("csv_path") or RESULT_CSV)
        if not csv_path.exists():
            return (f"找不到 CSV：{csv_path}，请先抓取。", 400)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = DOWNLOADS / f"sess_{stamp}"
        out_dir.mkdir(parents=True, exist_ok=True)

        ok, log, files = _run_ps1(csv_path, out_dir)
        if not ok:
            return (f"下载器执行失败：\n{log}", 500)
        if not files:
            return (f"下载完成，但未找到 MP4 文件。\n日志：\n{log}", 200)

        if len(files) == 1:
            return send_file(files[0], as_attachment=True, download_name=files[0].name, mimetype="video/mp4")
        zip_path = _zip_dir(out_dir)
        return send_file(zip_path, as_attachment=True, download_name=zip_path.name, mimetype="application/zip")

    except subprocess.TimeoutExpired:
        return ("下载过程超时，请减少并发或分批下载。", 500)
    except Exception as e:
        return (f"服务器异常：{e}", 500)


@app.route("/oneclick", methods=["POST"])
def oneclick():
    """同步版：一键抓 m3u8 + 下载，直接返回文件。"""
    try:
        data = request.get_json(force=True, silent=True) or {}
        raw_urls = (data.get("urls") or "").strip()
        if not raw_urls:
            return ("请粘贴至少一个链接（每行一个）", 400)
        URLS_TXT.write_text(raw_urls + "\n", encoding="utf-8")

        ok, log = _run_grabber()
        if not ok:
            return (f"抓取失败：\n{log}", 500)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = DOWNLOADS / f"sess_{stamp}"
        out_dir.mkdir(parents=True, exist_ok=True)

        ok2, log2, files = _run_ps1(RESULT_CSV, out_dir)
        if not ok2:
            return (f"下载失败：\n{log2}", 500)
        if not files:
            return (f"已完成，但未发现 MP4 文件。\n日志：\n{log2}", 200)

        if len(files) == 1:
            return send_file(files[0], as_attachment=True, download_name=files[0].name, mimetype="video/mp4")
        zip_path = _zip_dir(out_dir)
        return send_file(zip_path, as_attachment=True, download_name=zip_path.name, mimetype="application/zip")

    except subprocess.TimeoutExpired:
        return ("执行超时，请减少链接数量或分批处理。", 500)
    except Exception as e:
        return (f"服务器异常：{e}", 500)


# ========= 新增：SSE 实时日志的一键流程 =========
@app.route("/oneclick_start", methods=["POST"])
def oneclick_start():
    data = request.get_json(force=True, silent=True) or {}
    raw_urls = (data.get("urls") or "").strip()
    if not raw_urls:
        return ("请粘贴至少一个链接（每行一个）", 400)
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"urls": raw_urls, "artifact": None}
    return jsonify({"job_id": job_id})


def _sse_line(text: str):
    return f"data: {text}\n\n"


def _sse_event(event: str, data: str):
    return f"event: {event}\n" + _sse_line(data)


@app.route("/oneclick_stream/<job_id>", methods=["GET"])
def oneclick_stream(job_id):
    if job_id not in JOBS:
        return ("未知 job_id", 404)
    raw_urls = JOBS[job_id]["urls"]

    def generate():
        try:
            URLS_TXT.write_text(raw_urls + "\n", encoding="utf-8")
            yield _sse_line("已接收链接，开始抓取 m3u8…")

            ok, log = _run_grabber()
            yield _sse_line("抓取脚本输出：")
            for line in (log or "").splitlines():
                yield _sse_line("  " + line)
            if not ok:
                yield _sse_event("error", "抓取失败，已结束。")
                return

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_dir = DOWNLOADS / f"sess_{stamp}_{job_id}"
            out_dir.mkdir(parents=True, exist_ok=True)
            yield _sse_line("开始下载 MP4（ffmpeg）…")

            ok2, log2, files = _run_ps1(RESULT_CSV, out_dir)
            yield _sse_line("下载器输出：")
            for line in (log2 or "").splitlines():
                yield _sse_line("  " + line)

            if not ok2:
                yield _sse_event("error", "下载失败，已结束。")
                return
            if not files:
                yield _sse_event("error", "未发现 MP4 文件，已结束。")
                return

            if len(files) == 1:
                artifact = files[0]
            else:
                artifact = _zip_dir(out_dir)

            JOBS[job_id]["artifact"] = artifact
            dl_url = f"/artifact/{job_id}"
            yield _sse_line("全部完成 ✅")
            yield _sse_event("done", dl_url)
        except Exception as e:
            yield _sse_event("error", f"服务器异常：{e}")

    return Response(generate(), mimetype="text/event-stream")


@app.route("/artifact/<job_id>", methods=["GET"])
def artifact_download(job_id):
    info = JOBS.get(job_id)
    if not info or not info.get("artifact"):
        return ("产物未就绪或 job_id 无效", 404)
    f = info["artifact"]
    mime = "application/zip" if f.suffix.lower() == ".zip" else "video/mp4"
    return send_file(f, as_attachment=True, download_name=f.name, mimetype=mime)


@app.route("/last-log", methods=["GET"])
def last_log():
    return jsonify({"message": "未启用持久日志。如需日志，请在 /run 或 /download 中将日志写入文件保存。"})


if __name__ == "__main__":
    # 本地启动
    app.run(host="0.0.0.0", port=5000, debug=True)

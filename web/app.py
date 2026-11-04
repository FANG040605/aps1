"""本地服务：只用标准库 http.server，不引 Flask。

客户要求"办公室电脑上点一下能出来、不装东西"，所以：
- 只用 http.server + email（解析上传）+ json
- 上传的文件存到 out/web_jobs/<job_id>/，产物也写在那里
- 报错一律回中文 detail，前端直接显示，不白屏、不吐堆栈
"""

import argparse
import json
import os
import shutil
import threading
import time
import uuid
import webbrowser
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlparse

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from preflight import ensure_deps  # noqa: E402

ensure_deps()  # 缺 openpyxl 时给中文提示，不甩堆栈

from core import pipeline  # noqa: E402
from core.reader import WorkbookError  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
JOB_ROOT = os.path.join(os.path.dirname(ROOT), "out", "web_jobs")
MAX_BYTES = 12 * 1024 * 1024
KEEP_JOBS = 5
JOBS, LOCK = {}, threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "HongdaAPS/1.0"

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send_file(os.path.join(ROOT, "index.html"), "text/html; charset=utf-8")
        if path.startswith("/api/result/"):
            return self._result(path.rsplit("/", 1)[-1])
        if path.startswith("/api/download/"):
            return self._download(path)
        self._json({"detail": "没有这个地址：" + path}, 404)

    def do_POST(self):
        if urlparse(self.path).path != "/api/plan":
            return self._json({"detail": "没有这个接口"}, 404)
        try:
            body = self._read_body()
            fields, filename, data = _parse_upload(body, self.headers.get("Content-Type", ""))
            job_id = self._plan(fields, filename, data)
            self._json(JOBS[job_id]["payload"], 200)
        except (_Bad, WorkbookError) as e:
            self._json({"detail": str(e)}, 400)
        except Exception as e:                      # 兜底：绝不把堆栈丢给前端
            self.log_error("排产失败 %r", e)
            self._json({"detail": f"排产时出错：{type(e).__name__}。请把台账发给我们核对。"}, 500)

    # ---------- 业务 ----------

    def _plan(self, fields, filename, data):
        _check_file(filename, data)
        job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        work = os.path.join(JOB_ROOT, job_id)
        os.makedirs(work, exist_ok=True)
        src = os.path.join(work, "台账.xlsx")
        with open(src, "wb") as f:
            f.write(data)
        start = _parse_day(fields.get("start_date"))
        allow = fields.get("allow_switch", "true") != "false"
        with LOCK:                                   # 排程是同步的，串行更稳，单次 0.1s
            run = pipeline.run_pipeline(src, work, start, allow)
        payload = pipeline.payload(run, job_id)
        payload["summary"] = _summary(run)
        JOBS[job_id] = {"payload": payload, "dir": work, "at": time.time()}
        _cleanup()
        return job_id

    def _result(self, job_id):
        job = JOBS.get(job_id)
        if not job:
            return self._json({"detail": "这次结果已经过期，请重新上传台账生成"}, 404)
        self._json(job["payload"], 200)

    def _download(self, path):
        parts = path.split("/")
        job_id, kind = parts[3], parts[4] if len(parts) > 4 else ""
        job = JOBS.get(job_id)
        if not job or kind not in pipeline.KINDS:
            return self._json({"detail": "文件不存在或已过期，请重新生成"}, 404)
        name = pipeline.KINDS[kind][0]
        self._send_file(os.path.join(job["dir"], name), "application/octet-stream",
                        quoted_name=name)

    # ---------- 传输 ----------

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise _Bad("没有收到文件，请选择台账后再点「开始排产」")
        if length > MAX_BYTES:
            raise _Bad("文件超过 12 MB，请确认是不是选错了文件")
        return self.rfile.read(length)

    def _json(self, obj, code):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_file(self, path, ctype, quoted_name=None):
        if not os.path.exists(path):
            return self._json({"detail": "文件不存在：" + os.path.basename(path)}, 404)
        with open(path, "rb") as f:
            raw = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        if quoted_name:
            self.send_header("Content-Disposition",
                             f"attachment; filename=file{os.path.splitext(quoted_name)[1]}; "
                             f"filename*=UTF-8''{quote(quoted_name)}")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        print(f"[{time.strftime('%H:%M:%S')}] " + fmt % args)


class _Bad(Exception):
    """已知的、可以直说给用户听的错误。"""


def _parse_upload(body, ctype):
    """解析 multipart；也兼容直接 POST 二进制（curl 自测用）。"""
    if "multipart/form-data" not in ctype:
        return {}, "台账.xlsx", body
    msg = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + ctype.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
    fields, filename, data = {}, "", b""
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        fname = part.get_filename()
        if fname:
            filename, data = fname, part.get_payload(decode=True)
        else:
            fields[name] = (part.get_payload(decode=True) or b"").decode("utf-8", "ignore")
    return fields, filename, data


def _check_file(filename, data):
    """三道闸：扩展名、是不是 zip、有没有内容。"""
    if not filename:
        raise _Bad("没有收到文件名，请重新选择台账")
    if not filename.lower().endswith((".xlsx", ".xlsm")):
        raise _Bad(f"这个文件打不开，不是 Excel 台账。当前文件：{filename}。"
                   "请上传 .xlsx 文件，且要包含「订单」「设备」「工艺路线」三个工作表。")
    if data[:2] != b"PK":
        raise _Bad("文件内容不像 Excel（可能已损坏或只是改了扩展名），请用 Excel 重新另存为 .xlsx")
    if len(data) < 1024:
        raise _Bad("文件太小，可能是空表，请检查台账内容")


def _parse_day(text):
    if not text:
        return None
    from datetime import datetime
    try:
        return datetime.strptime(text, "%Y-%m-%d").replace(hour=8)
    except ValueError:
        raise _Bad("起始日格式不对，请用 2026-11-09 这样的格式")


def _summary(run):
    return {"orders": len(run["orders"]), "planned": len({r["order"] for r in run["rows"]}),
            "tasks": len(run["rows"]), "violations": len(run["violations"]),
            "issues": len(run["issues"])}


def _cleanup():
    """只留最近 KEEP_JOBS 次，别把客户数据越攒越多。"""
    if len(JOBS) <= KEEP_JOBS:
        return
    for job_id, _ in sorted(JOBS.items(), key=lambda kv: kv[1]["at"])[:len(JOBS) - KEEP_JOBS]:
        shutil.rmtree(JOBS[job_id]["dir"], ignore_errors=True)
        JOBS.pop(job_id, None)


def _open_browser(url):
    """等一下再开浏览器：服务已经 listen 了才去访问，避免打开是空白。"""
    try:
        webbrowser.open(url)
    except Exception:
        print(f"没能自动打开浏览器，请手动访问：{url}")


def main():
    ap = argparse.ArgumentParser(description="宏达精密 · 本地排产助手（网页版）")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()
    os.makedirs(JOB_ROOT, exist_ok=True)
    ThreadingHTTPServer.allow_reuse_address = False      # 端口被占用时直接报错，不静默起两个
    try:
        srv = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError:
        print(f"端口 {args.port} 已被占用。换一个端口再试：python web/app.py --port 8766")
        return
    url = f"http://{args.host}:{args.port}"
    print(f"排产助手已启动：{url}    （Ctrl+C 停止）")
    if not args.no_open:
        threading.Timer(1.2, _open_browser, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()

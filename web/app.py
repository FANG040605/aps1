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
import traceback
import urllib.request
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
from core import cleaner, events, profile  # noqa: E402
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
        if path == "/api/ping":
            return self._json({"ok": True, "app": "aps-scheduler"}, 200)
        if path == "/api/profile":
            return self._json(_profile_payload(), 200)
        if path.startswith("/api/result/"):
            return self._result(path.rsplit("/", 1)[-1])
        if path.startswith("/api/download/"):
            return self._download(path)
        self._json({"detail": "没有这个地址：" + path}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/plan":
                return self._plan()
            if path == "/api/profile":
                return self._save_profile()
            if path == "/api/machine":
                return self._machine_event()
            if path == "/api/machine/recover":
                return self._machine_recover()
            if path == "/api/machine/undo":
                return self._machine_undo()
            self._json({"detail": "没有这个接口"}, 404)
        except (_Bad, WorkbookError) as e:
            self._json({"detail": str(e)}, 400)
        except ValueError as e:                     # 事件参数不对，话说清楚就行
            self._json({"detail": str(e)}, 400)
        except Exception as e:                      # 兜底：绝不把堆栈丢给前端
            traceback.print_exc()                   # 但黑窗口里要留全，方便顾问定位
            self.log_error("请求失败 %r", e)
            self._json({"detail": f"处理时出错：{type(e).__name__}。请把这批文件发给我们核对。"}, 500)

    # ---------- 业务 ----------

    def _plan(self):
        """排产。文件里只有订单表也行——设备与工艺路线从产线基线取。"""
        body = self._read_body()
        fields, filename, data = _parse_upload(body, self.headers.get("Content-Type", ""))
        _check_file(filename, data)
        job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        work = os.path.join(JOB_ROOT, job_id)
        os.makedirs(work, exist_ok=True)
        src = os.path.join(work, "台账.xlsx")
        with open(src, "wb") as f:
            f.write(data)
        start = _parse_day(fields.get("start_date"))
        allow = fields.get("allow_switch", "true") != "false"
        try:
            with LOCK:                               # 排程是同步的，串行更稳，单次约 2~3s（含交期局部搜索）
                run = pipeline.run_pipeline(src, work, start, allow)
        except Exception:
            # 失败的请求不留目录：这些目录不进 JOBS，_cleanup 永远清不到，
            # 攒下来就是一堆只有客户台账、没有结果的空壳。
            shutil.rmtree(work, ignore_errors=True)
            raise
        payload = pipeline.payload(run, job_id)
        payload["summary"] = _summary(run)
        JOBS[job_id] = {"payload": payload, "dir": work, "at": time.time()}
        _cleanup()
        self._json(payload, 200)
        return job_id

    def _save_profile(self):
        """上传完整台账 → 存成产线基线（设备 + 工艺路线），之后只传订单就行。"""
        body = self._read_body()
        _, filename, data = _parse_upload(body, self.headers.get("Content-Type", ""))
        _check_file(filename, data)
        tmp = os.path.join(JOB_ROOT, "_profile_upload.xlsx")
        os.makedirs(JOB_ROOT, exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(data)
        try:
            from core import reader
            tables = reader.read_workbook(tmp)       # 要求三张表齐全
            profile.save(tables)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        self._json(_profile_payload(), 200)

    def _machine_event(self):
        """登记设备故障 / 保养：{machine, kind, until, note}。"""
        req = self._read_json()
        machine = (req.get("machine") or "").strip()
        kind = (req.get("kind") or "故障").strip()
        if not machine:
            raise _Bad("请先选一台机台")
        until = events.parse_time(req.get("until"))
        ev = events.add(machine, kind, until=until, note=req.get("note", ""))
        self._json({"ok": True, "event": ev, "profile": _profile_payload()}, 200)

    def _machine_recover(self):
        """机台修好了：截断它所有未结束的停机。"""
        req = self._read_json()
        machine = (req.get("machine") or "").strip()
        if not machine:
            raise _Bad("请先选一台机台")
        n = events.recover(machine)
        self._json({"ok": True, "closed": n, "profile": _profile_payload()}, 200)

    def _machine_undo(self):
        """撤销一条登记错的事件。"""
        req = self._read_json()
        if not events.remove((req.get("id") or "").strip()):
            raise _Bad("这条记录已经不存在了")
        self._json({"ok": True, "profile": _profile_payload()}, 200)

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

    def _read_json(self):
        """读 JSON 请求体（设备故障登记这类小表单用）。"""
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise _Bad("请求格式不对，刷新页面后再试一次")
        return data if isinstance(data, dict) else {}

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
                   "请上传 .xlsx 文件。日常排产只传订单表就行"
                   "（设备与工艺路线用已保存的产线基线）；第一次使用要先传完整台账"
                   "（订单 / 设备 / 工艺路线三张表，可分三个工作表，也可一个工作表上下排三段）。")
    if data[:2] != b"PK":
        raise _Bad("文件内容不像 Excel（可能已损坏或只是改了扩展名），请用 Excel 重新另存为 .xlsx")
    if len(data) < 1024:
        raise _Bad("文件太小，可能是空表，请检查台账内容")


def _profile_payload():
    """产线基线概况：机台、工艺、事件流水，给页面上的「产线」页签用。"""
    base = profile.load()
    if base is None:
        return {"exists": False, "updated": "", "fingerprint": "", "summary": "",
                "machines": [], "products": [], "events": []}
    machines, _ = cleaner.clean_machines(base["设备"])
    routes, _ = cleaner.clean_routes(base["工艺路线"])
    clean = {"machines": machines, "routes": routes}
    items = events.load()
    live = events.ongoing(items)
    updated = profile.mtime()
    return {
        "exists": True,
        "updated": updated.strftime("%Y-%m-%d %H:%M") if updated else "",
        "fingerprint": profile.fingerprint(clean),
        "summary": profile.describe(clean),
        "machines": [dict(m, down=m["code"] in live,
                          down_until=(live[m["code"]].strftime("%m-%d %H:%M")
                                      if live.get(m["code"]) else None))
                     for m in profile.machines_of(clean)],
        "products": profile.products_of(clean),
        "events": [dict(e, active=e.get("machine") in live) for e in reversed(items[-30:])],
    }


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


def _already_running(url):
    """端口被占时先问一句：占它的是不是我们自己？

    是的话说明客户重复双击了启动器，直接开浏览器就行，
    没必要让客户看"端口被占用"这种他看不懂的话。
    """
    try:
        with urllib.request.urlopen(url + "/api/ping", timeout=2) as r:
            if r.status == 200:
                print(f"服务本来就已经在运行了：{url}")
                print("已经为你打开浏览器，不用重复启动。")
                _open_browser(url)
                return True
    except Exception:
        pass
    return False


def main():
    ap = argparse.ArgumentParser(description="宏达精密 · 本地排产助手（网页版）")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()
    os.makedirs(JOB_ROOT, exist_ok=True)
    ThreadingHTTPServer.allow_reuse_address = False      # 端口被占用时直接报错，不静默起两个
    url = f"http://{args.host}:{args.port}"
    try:
        srv = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError:
        if not _already_running(url):
            print(f"端口 {args.port} 被别的程序占用了。换一个端口再试：python web/app.py --port 8766")
        return
    print(f"排产助手已启动：{url}    （Ctrl+C 停止）")
    if not args.no_open:
        threading.Timer(1.2, _open_browser, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()

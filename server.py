#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""截图笔记后端（Python 3.12 标准库，零第三方依赖）。

接口契约见 API.md：笔记 CRUD + 上传截图经 Windows 内置 OCR 生成笔记。
数据保存在 data/notes.json 与 data/images/，前端静态文件从 web/ 目录读取。
"""

from __future__ import annotations

import argparse, base64, binascii, json, os, re, subprocess, sys, tempfile
import threading, time, uuid, webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
DATA_DIR = ROOT / "data"
IMAGES_DIR = DATA_DIR / "images"
NOTES_FILE = DATA_DIR / "notes.json"
OCR_SCRIPT = ROOT / "ocr" / "win_ocr.ps1"

OCR_LANG = "zh-Hans-CN"
OCR_TIMEOUT = 60
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_BODY_BYTES = 32 * 1024 * 1024
MAX_TITLE_LEN = 120
MAX_CONTENT_LEN = 100000

IMAGE_EXT_MIME = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp",
                  "gif": "image/gif", "bmp": "image/bmp"}
DATA_URL_MIME_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
                     "image/webp": "webp", "image/gif": "gif", "image/bmp": "bmp",
                     "image/x-ms-bmp": "bmp"}
STATIC_ROUTES = {"/": ("index.html", "text/html; charset=utf-8"),
                 "/index.html": ("index.html", "text/html; charset=utf-8"),
                 "/style.css": ("style.css", "text/css; charset=utf-8"),
                 "/app.js": ("app.js", "text/javascript; charset=utf-8")}

# 数据存储：内存 dict（按 id 索引）+ data/notes.json 原子写入，线程锁保护。
_notes: dict[str, dict] = {}
_notes_lock = threading.Lock()

def _load_notes() -> dict[str, dict]:
    """启动时读取 notes.json；文件损坏时备份为 notes.corrupt-<时间戳>.json。"""
    if not NOTES_FILE.exists():
        return {}
    try:
        data = json.loads(NOTES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        backup = NOTES_FILE.with_name(f"notes.corrupt-{int(time.time())}.json")
        try:
            NOTES_FILE.replace(backup)
            print(f"[警告] notes.json 读取失败（{exc}），已备份为 {backup.name}", file=sys.stderr)
        except OSError:
            print(f"[警告] notes.json 读取失败：{exc}", file=sys.stderr)
        return {}
    return {item["id"]: item for item in (data if isinstance(data, list) else [])
            if isinstance(item, dict) and item.get("id")}

def _save_notes_locked() -> None:
    """原子写入 notes.json（临时文件 + os.replace）；调用方须持有锁。"""
    fd, tmp_name = tempfile.mkstemp(dir=DATA_DIR, prefix="notes-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(list(_notes.values()), fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, NOTES_FILE)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

def notes_snapshot() -> list[dict]:
    with _notes_lock:
        items = [dict(note) for note in _notes.values()]
    items.sort(key=lambda note: note.get("created_at", ""), reverse=True)
    return items

def note_get(note_id: str) -> dict | None:
    with _notes_lock:
        note = _notes.get(note_id)
        return dict(note) if note else None

def note_add(note: dict) -> dict:
    with _notes_lock:
        _notes[note["id"]] = note
        _save_notes_locked()
    return dict(note)

def note_update(note_id: str, updates: dict) -> dict | None:
    with _notes_lock:
        note = _notes.get(note_id)
        if note is None:
            return None
        note.update(updates)
        note["updated_at"] = now_iso()
        _save_notes_locked()
        return dict(note)

def note_delete(note_id: str) -> dict | None:
    with _notes_lock:
        note = _notes.pop(note_id, None)
        if note is None:
            return None
        _save_notes_locked()
        return dict(note)

def note_count() -> int:
    with _notes_lock:
        return len(_notes)

# ---------------------------------------------------------------------------
# 文本 / 图片工具
# ---------------------------------------------------------------------------

def now_iso() -> str:
    """本地时区 ISO8601，精确到秒。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")

def _valid_id(note_id: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", note_id))

_CJK = ("\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
        "\uff01-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff65")
_CJK_GAP_RE = re.compile(f"(?<=[{_CJK}])[ \t\u00a0\u3000]+(?=[{_CJK}])")

def clean_ocr_text(raw: str) -> str:
    """按行清洗 OCR 文本：去首尾空白、去 CJK 字符间空格、丢空行。"""
    lines = []
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = _CJK_GAP_RE.sub("", line.strip())
        if line:
            lines.append(line)
    return "\n".join(lines)

def sniff_image_ext(raw: bytes) -> str | None:
    """按魔数识别图片格式，仅接受契约支持的格式。"""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"): return "png"
    if raw.startswith(b"\xff\xd8\xff"): return "jpg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP": return "webp"
    if raw[:6] in (b"GIF87a", b"GIF89a"): return "gif"
    if raw[:2] == b"BM": return "bmp"
    return None

def parse_data_url(value: str) -> tuple[str, bytes]:
    """解析 data URL，按魔数校验格式并检查解码后大小（≤20MB）。"""
    match = re.match(r"^data:(?P<mime>[\w.+-]+/[\w.+-]+);base64,(?P<data>.*)$", value.strip(), re.S)
    if match is None:
        raise HttpError(400, "图片格式无效：需要 data:<mime>;base64,<数据> 形式的 data URL")
    if match.group("mime").lower() not in DATA_URL_MIME_EXT:
        raise HttpError(400, "不支持的图片类型，仅支持 png/jpeg/webp/gif/bmp")
    payload = "".join(match.group("data").split())  # 容忍换行等空白
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise HttpError(400, "图片 base64 解码失败") from None
    if not raw:
        raise HttpError(400, "图片内容为空")
    if len(raw) > MAX_IMAGE_BYTES:
        raise HttpError(413, "图片过大（解码后最大 20MB）")
    ext = sniff_image_ext(raw)
    if ext is None:
        raise HttpError(400, "不支持的图片格式，仅支持 png/jpeg/webp/gif/bmp")
    return ext, raw

def pick_title(raw_title: object, content: str) -> str:
    """优先用请求里的 title；否则取识别文本首行前 40 字；再否则用时间兜底。"""
    if isinstance(raw_title, str) and raw_title.strip():
        return raw_title.strip()[:MAX_TITLE_LEN]
    first_line = content.split("\n", 1)[0].strip() if content else ""
    if first_line:
        return first_line[:40]
    return f"截图笔记 {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}"

# ---------------------------------------------------------------------------
# OCR 调用（Windows 内置 Windows.Media.Ocr，经 ocr/win_ocr.ps1）
# ---------------------------------------------------------------------------

class OcrError(Exception):
    """OCR 调用失败。"""

def _powershell(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    """调用 powershell.exe；Windows 下不弹出控制台窗口。"""
    kwargs: dict = {"capture_output": True, "timeout": timeout}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(["powershell.exe", "-NoProfile"] + args, **kwargs)

def _decode(data: bytes | None) -> str:
    return (data or b"").decode("utf-8", "replace").strip()

def run_ocr(image_path: Path) -> str:
    """调用 OCR 脚本识别图片，返回原始文本；失败抛 OcrError。"""
    if not OCR_SCRIPT.is_file():
        raise OcrError("未找到 OCR 脚本 ocr/win_ocr.ps1")
    args = ["-ExecutionPolicy", "Bypass", "-File", str(OCR_SCRIPT),
            "-Path", str(image_path), "-LangTag", OCR_LANG]
    try:
        proc = _powershell(args, OCR_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise OcrError(f"识别超时（超过 {OCR_TIMEOUT} 秒）") from None
    except OSError as exc:
        raise OcrError(f"无法启动 PowerShell：{exc}") from exc
    if proc.returncode != 0:
        detail = (_decode(proc.stderr) or _decode(proc.stdout) or "未知错误").splitlines()[-1]
        raise OcrError(detail[:300])
    return _decode(proc.stdout)

_OCR_STATE = {"ready": os.name == "nt"}  # 乐观默认，后台探测后修正

def probe_ocr_ready() -> None:
    """后台轻量探测 OCR 引擎语言包；探测失败时保留乐观默认值，不阻塞启动。"""
    if os.name != "nt":
        _OCR_STATE["ready"] = False
        return
    script = ("[Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType = WindowsRuntime] | Out-Null; "
              "[Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | "
              "ForEach-Object { $_.LanguageTag }")
    try:
        proc = _powershell(["-Command", script], 20)
    except (subprocess.TimeoutExpired, OSError):
        return
    if proc.returncode == 0:
        tags = [line for line in _decode(proc.stdout).splitlines() if line.strip()]
        _OCR_STATE["ready"] = bool(tags)

# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------

class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message

def _require_note(note_id: str) -> dict:
    note = note_get(note_id) if _valid_id(note_id) else None
    if note is None:
        raise HttpError(404, "笔记不存在")
    return note

class NoteRequestHandler(BaseHTTPRequestHandler):
    server_version = "SuperNote/1.0"
    protocol_version = "HTTP/1.1"

    def _send_bytes(self, status, body=b"", content_type="application/json; charset=utf-8",
                    extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, extra_headers={"Cache-Control": "no-store"})

    def _send_empty(self, status):
        self.send_response(status)
        self.end_headers()

    def _safe_fail(self, status, message):
        try:
            self._send_json(status, {"error": message})
        except OSError:
            self.close_connection = True

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _read_json(self):
        """读取并解析 JSON 请求体，返回 Python 对象。"""
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self.close_connection = True
            raise HttpError(411, "缺少 Content-Length")
        try:
            length = int(raw_length)
        except ValueError:
            raise HttpError(400, "Content-Length 无效") from None
        if length <= 0:
            raise HttpError(400, "请求体为空")
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            raise HttpError(413, "请求体过大")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise HttpError(400, "请求体不完整")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HttpError(400, "请求体不是合法的 UTF-8 JSON") from None

    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_PUT(self): self._handle("PUT")
    def do_DELETE(self): self._handle("DELETE")

    def _handle(self, method):
        try:
            getattr(self, f"_do_{method.lower()}")(unquote(urlparse(self.path).path))
        except HttpError as exc:
            self._safe_fail(exc.status, exc.message)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # 兜底，避免异常导致连接悬挂
            self._safe_fail(500, f"服务器内部错误：{exc}")

    # GET：health / 列表 / 单条 / 图片 / 静态文件
    def _do_get(self, path):
        if path == "/api/health":
            self._send_json(200, {"ok": True, "ocr": OCR_LANG,
                                  "ocr_ready": bool(_OCR_STATE["ready"]),
                                  "note_count": note_count()})
            return
        if path == "/api/notes":
            self._send_json(200, {"notes": notes_snapshot()})
            return
        if path.startswith("/api/notes/"):
            self._send_json(200, {"note": _require_note(path[len("/api/notes/"):])})
            return
        if path.startswith("/api"):
            raise HttpError(404, "未知的 API 路径")
        if path.startswith("/images/"):
            self._send_image(path[len("/images/"):])
            return
        self._send_static(path)

    def _send_image(self, name):
        # 只接受纯文件名，防目录穿越
        if not name or name != os.path.basename(name) or "/" in name or "\\" in name:
            raise HttpError(404, "图片不存在")
        image_path = IMAGES_DIR / name
        try:
            data = image_path.read_bytes()
        except OSError:
            raise HttpError(404, "图片不存在") from None
        mime = IMAGE_EXT_MIME.get(image_path.suffix.lower().lstrip("."), "application/octet-stream")
        self._send_bytes(200, data, mime, {"Cache-Control": "public, max-age=31536000, immutable",
                                           "X-Content-Type-Options": "nosniff"})

    def _send_static(self, path):
        route = STATIC_ROUTES.get(path)
        if route is None:
            raise HttpError(404, "未找到该资源")
        filename, mime = route
        try:
            data = (WEB_DIR / filename).read_bytes()
        except OSError:
            raise HttpError(404, "前端文件缺失，请确认 web/ 目录存在")
        self._send_bytes(200, data, mime, {"Cache-Control": "no-cache"})

    # POST /api/notes：保存图片 -> OCR -> 生成笔记
    def _do_post(self, path):
        if path != "/api/notes":
            raise HttpError(404, "未知的 API 路径")
        payload = self._read_json()
        if not isinstance(payload, dict):
            raise HttpError(400, "请求体必须是 JSON 对象")
        image = payload.get("image")
        if not isinstance(image, str) or not image.strip():
            raise HttpError(400, "缺少 image 字段（需为 data URL）")
        ext, raw = parse_data_url(image)
        note_id = uuid.uuid4().hex[:12]
        image_path = IMAGES_DIR / f"{note_id}.{ext}"
        fd, tmp_name = tempfile.mkstemp(dir=IMAGES_DIR, prefix="upload-", suffix=f".{ext}")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            try:
                text = run_ocr(tmp_path)
            except OcrError as exc:
                raise HttpError(500, f"OCR 失败：{exc}") from exc
            os.replace(tmp_path, image_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        content = clean_ocr_text(text)
        now = now_iso()
        note = {"id": note_id, "title": pick_title(payload.get("title"), content),
                "content": content, "image_url": f"/images/{note_id}.{ext}",
                "created_at": now, "updated_at": now}
        note_add(note)
        self._send_json(201, {"note": note})

    # PUT /api/notes/{id}：只更新 title/content 与 updated_at
    def _do_put(self, path):
        if not path.startswith("/api/notes/"):
            raise HttpError(404, "未知的 API 路径")
        note_id = path[len("/api/notes/"):]
        payload = self._read_json()
        if not isinstance(payload, dict):
            raise HttpError(400, "请求体必须是 JSON 对象")
        updates: dict = {}
        if "title" in payload:
            if not isinstance(payload["title"], str):
                raise HttpError(400, "title 必须是字符串")
            updates["title"] = payload["title"].strip()[:MAX_TITLE_LEN]
        if "content" in payload:
            if not isinstance(payload["content"], str):
                raise HttpError(400, "content 必须是字符串")
            updates["content"] = payload["content"][:MAX_CONTENT_LEN]
        if not updates:
            raise HttpError(400, "请提供 title 或 content 字段")
        note = note_update(note_id, updates)
        if note is None:
            raise HttpError(404, "笔记不存在")
        self._send_json(200, {"note": note})

    # DELETE /api/notes/{id}：删除笔记与图片
    def _do_delete(self, path):
        if not path.startswith("/api/notes/"):
            raise HttpError(404, "未知的 API 路径")
        note = note_delete(path[len("/api/notes/"):])
        if note is None:
            raise HttpError(404, "笔记不存在")
        name = os.path.basename(note.get("image_url") or "")
        if name:
            try:
                (IMAGES_DIR / name).unlink()
            except OSError:
                pass
        self._send_empty(204)

class NoteHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

def _open_browser(url: str) -> None:
    time.sleep(0.8)  # 等服务进入监听状态
    try:
        webbrowser.open(url)
    except Exception:
        pass

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="截图笔记后端服务（零依赖，使用 Windows 内置 OCR）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = parser.parse_args(argv)

    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    _notes.update(_load_notes())
    threading.Thread(target=probe_ocr_ready, name="ocr-probe", daemon=True).start()
    try:
        server = NoteHttpServer((args.host, args.port), NoteRequestHandler)
    except OSError as exc:
        print(f"[错误] 无法监听 {args.host}:{args.port}：{exc}", file=sys.stderr)
        return 1

    host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{host}:{args.port}/"
    print(f"截图笔记服务已启动：{url}", flush=True)
    print(f"数据目录：{DATA_DIR}", flush=True)
    if not args.no_browser:
        threading.Thread(target=_open_browser, args=(url,), name="browser", daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止服务。", flush=True)
    finally:
        server.server_close()
    return 0

if __name__ == "__main__":
    sys.exit(main())

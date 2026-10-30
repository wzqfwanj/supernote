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

_SPACE_RUN_RE = re.compile(r" {2,}")
_CJK_EDGE_RE = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")
_SENTENCE_END_RE = re.compile(r"[。！？!?；;…]$")
# 纯 URL / 域名行即使以 . 结尾也视为「未结束」，以便继续合并下一行
_URL_OR_DOMAIN_RE = re.compile(
    r"(?:https?://\S+|www\.\S+|[\w-]+(?:\.[\w-]+)*\."
    r"(?:com|org|net|cn|io|dev|js|css|py|html|json|md)\b)", re.I)
# 列表项行首：符号列表 / 1. 1、1) / （1） / 一、 / a)
_LIST_ITEM_RE = re.compile(
    r"^(?:"
    r"[•·▪◦‣∙●○–—*+\-]\s+"
    r"|\d{1,3}(?:[.．)](?!\d)|[、)])\s*"
    r"|[（(][一二三四五六七八九十百\d]+[）)]\s*"
    r"|[一二三四五六七八九十百]+[、.．]\s*"
    r"|[A-Za-z][)]\s+"
    r")")
# 代码行：行首常见关键字 / HTML 标签
_CODE_START_RE = re.compile(
    r"^(?:#include\b|</?[A-Za-z][\w:-]*(?:[ >]|$)|"
    r"(?:def|class|import|from|function|const|let|var|return|if|for|while|print|echo|"
    r"package|public|private|protected|static|async|await|try|except|elif|else|switch|"
    r"case|break|continue|new|this|self)\b)")
_CODE_SYMBOLS = "{};=<>()[]"


def _clean_line(line: str) -> str:
    """单行清洗：tab/全角空格转空格、去首尾空白、去 CJK 字间空格、压缩空格。"""
    line = line.replace("\t", " ").replace("\u3000", " ").replace("\u00a0", " ").strip()
    line = _CJK_GAP_RE.sub("", line)
    return _SPACE_RUN_RE.sub(" ", line).strip()


def _preprocess_lines(raw: str) -> list[str]:
    """统一换行并逐行清洗，返回非空行列表（纯文本回退用）。"""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    return [cleaned for cleaned in (_clean_line(line) for line in text.split("\n")) if cleaned]


def _ends_sentence(line: str) -> bool:
    """该行是否以句末标点结束（决定硬换行处是否分段）。"""
    if _SENTENCE_END_RE.search(line):
        return True
    if line.endswith("."):
        return _URL_OR_DOMAIN_RE.search(line) is None
    return False


def _is_heading(line: str, next_line: str | None) -> bool:
    """短行且下一行明显更长（≥本行长度+6）时视为标题。"""
    if len(line) > 24 or next_line is None or _ends_sentence(line):
        return False
    return len(next_line) >= len(line) + 6


def _is_code_line(line: str) -> bool:
    """代码行：命中行首关键字，或含 ASCII 且代码符号 ≥2 个。"""
    if _CODE_START_RE.match(line):
        return True
    if re.search(r"[A-Za-z0-9]", line):
        return sum(line.count(symbol) for symbol in _CODE_SYMBOLS) >= 2
    return False


def _classify_lines(lines: list[str]) -> list[str]:
    """逐行分类：list（列表）/ code（代码）/ heading（标题）/ text（正文）。"""
    kinds: list[str] = []
    for index, line in enumerate(lines):
        next_line = lines[index + 1] if index + 1 < len(lines) else None
        if _LIST_ITEM_RE.match(line):
            kinds.append("list")
        elif _is_code_line(line):
            kinds.append("code")
        elif _is_heading(line, next_line):
            kinds.append("heading")
        else:
            kinds.append("text")
    return kinds


def _median(values: list[float]) -> float | None:
    """中位数；空列表返回 None。"""
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _line_gap(prev: dict, cur: dict) -> float | None:
    """上一行底部到下一行顶部的垂直间距；任一行缺几何信息时返回 None。"""
    if prev.get("y") is None or prev.get("h") is None or cur.get("y") is None:
        return None
    return cur["y"] - (prev["y"] + prev["h"])


def _paragraph_threshold(records: list[dict]) -> float | None:
    """段落阈值 T = max(G + 0.8*H, 1.4*H)：H 行高中位数，G 相邻行间距中位数。"""
    heights = [record["h"] for record in records if record.get("h")]
    height = _median(heights)
    if height is None:
        return None
    gaps = []
    for prev, cur in zip(records, records[1:]):
        gap = _line_gap(prev, cur)
        if gap is not None:
            gaps.append(gap)
    median_gap = _median(gaps)
    if median_gap is None:
        return 1.4 * height
    return max(median_gap + 0.8 * height, 1.4 * height)


def _normalize_lines(raw) -> list[dict]:
    """把 OCR 结果规范为行记录列表（text 必填，x/y/w/h 可缺省）。

    raw 支持 run_ocr 返回的 list[dict]（含几何）、list[str] 或纯文本 str。
    """
    if isinstance(raw, str):
        source: list[dict] = [{"text": line} for line in _preprocess_lines(raw)]
    elif isinstance(raw, (list, tuple)):
        source = []
        for item in raw:
            if isinstance(item, dict):
                record = {"text": item.get("text", "")}
                for key in ("x", "y", "w", "h"):
                    value = item.get(key)
                    valid = isinstance(value, (int, float)) and not isinstance(value, bool)
                    record[key] = float(value) if valid else None
                source.append(record)
            elif isinstance(item, str):
                source.append({"text": item})
    else:
        return []
    records = []
    for record in source:
        text = _clean_line(str(record.get("text") or ""))
        if text:
            cleaned = dict(record)
            cleaned["text"] = text
            records.append(cleaned)
    return records


def join_wrapped(prev: str, nxt: str) -> str:
    """拼接硬换行两行：CJK 直接接、英文/数字间一个空格、连字符断词还原。"""
    if prev.endswith("-") and nxt[:1].isascii() and nxt[:1].islower():
        return prev[:-1] + nxt
    if _CJK_EDGE_RE.match(prev[-1]) or _CJK_EDGE_RE.match(nxt[0]):
        return prev + nxt
    return prev + " " + nxt


def _build_blocks(records: list[dict], kinds: list[str],
                  threshold: float | None) -> list[tuple[str, list[str]]]:
    """组装块：列表/代码/标题各成块，正文按句末标点与几何段间距合并/分段。"""
    blocks: list[tuple[str, list[str]]] = []
    current: tuple[str, list[str]] | None = None
    for index, (record, kind) in enumerate(zip(records, kinds)):
        text = record["text"]
        gap = _line_gap(records[index - 1], record) if index else None
        if kind in ("list", "code"):
            if current is not None and current[0] == kind:
                current[1].append(text)
            else:
                if current is not None:
                    blocks.append(current)
                current = (kind, [text])
        elif kind == "heading":
            if current is not None:
                blocks.append(current)
                current = None
            blocks.append(("heading", [text]))
        elif current is not None and current[0] == "list":
            # 列表项被硬换行拆开：间距小且上一项未以句末标点结尾时并回该列表项
            if (threshold is not None and gap is not None and gap <= threshold
                    and not _ends_sentence(current[1][-1])):
                current[1][-1] = join_wrapped(current[1][-1], text)
            else:
                blocks.append(current)
                current = ("text", [text])
        elif current is not None and current[0] == "text":
            force_break = threshold is not None and gap is not None and gap > threshold
            if force_break or _ends_sentence(current[1][-1]):
                blocks.append(current)
                current = ("text", [text])
            else:
                current[1][-1] = join_wrapped(current[1][-1], text)
        else:
            if current is not None:
                blocks.append(current)
            current = ("text", [text])
    if current is not None:
        blocks.append(current)
    return blocks


def format_ocr_text(raw) -> str:
    """把 OCR 结果排版为纯文本笔记正文。

    raw 可为带行几何的 list[dict]（新协议）或纯文本 str（回退）。
    - 合并硬换行；句末标点或几何段间距 gap > T 处分段；
    - 列表项、连续代码行、标题行各自成块；块之间空一行。
    """
    records = _normalize_lines(raw)
    if not records:
        return ""
    kinds = _classify_lines([record["text"] for record in records])
    blocks = _build_blocks(records, kinds, _paragraph_threshold(records))
    return "\n\n".join("\n".join(block_lines) for _, block_lines in blocks)

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

def _parse_ocr_output(stdout: str) -> list[dict]:
    """解析 OCR 脚本输出：优先 JSON 行几何；非法/旧格式回退为纯文本行。"""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("lines"), list):
        lines = []
        for item in data["lines"]:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            record = {"text": text}
            for key in ("x", "y", "w", "h"):
                value = item.get(key)
                valid = isinstance(value, (int, float)) and not isinstance(value, bool)
                record[key] = float(value) if valid else None
            lines.append(record)
        return lines
    return [{"text": line} for line in stdout.splitlines() if line.strip()]


def run_ocr(image_path: Path) -> list[dict]:
    """调用 OCR 脚本识别图片，返回行列表（含 text/x/y/w/h，几何可能缺省）；失败抛 OcrError。"""
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
    return _parse_ocr_output(_decode(proc.stdout))

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
        content = format_ocr_text(text)
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

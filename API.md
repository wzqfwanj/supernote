# 接口契约（截图笔记系统）

前端 `web/` 与后端 `server.py` 共同遵守本文件。所有响应均为 UTF-8 JSON（图片路由除外）。

## 数据模型 Note

```json
{
  "id": "a1b2c3d4e5f6",
  "title": "第一行识别文本…",
  "content": "OCR 识别全文（可编辑）",
  "image_url": "/images/a1b2c3d4e5f6.png",
  "created_at": "2026-10-29T13:20:00+08:00",
  "updated_at": "2026-10-29T13:20:00+08:00"
}
```

## 错误格式

非 2xx 响应体统一为：

```json
{ "error": "人类可读的错误信息" }
```

## 接口

| 方法 | 路径 | 说明 | 成功响应 |
| --- | --- | --- | --- |
| GET | `/api/health` | 健康检查 | 200 `{"ok":true,"ocr":"zh-Hans-CN","ocr_ready":true,"note_count":3}` |
| GET | `/api/notes` | 笔记列表（created_at 倒序） | 200 `{"notes":[Note,...]}` |
| GET | `/api/notes/{id}` | 单条笔记 | 200 `{"note":Note}`；404 |
| POST | `/api/notes` | 上传截图并 OCR 生成笔记 | 201 `{"note":Note}` |
| PUT | `/api/notes/{id}` | 修改标题/内容 | 200 `{"note":Note}`；404 |
| DELETE | `/api/notes/{id}` | 删除笔记（含图片） | 204；404 |
| GET | `/images/{id}.{ext}` | 笔记原图 | 图片二进制 |
| GET | `/`、`/style.css`、`/app.js` | 前端静态文件 | 文件内容 |

### POST /api/notes

请求体为 JSON：

```json
{ "image": "data:image/png;base64,iVBORw0KGgo...", "title": "可选标题" }
```

- `image` 必填，支持 png / jpeg / webp / gif / bmp，解码后单张 ≤ 20MB。
- 后端保存图片、调用 OCR、生成笔记。不传 `title` 时取识别文本首个非空行（最多 40 字），识别为空则用 `截图笔记 YYYY-MM-DD HH:mm`。
- 400：图片缺失/格式不支持/图片损坏；413：图片过大；500：OCR 失败（`error` 内含原因）。

### PUT /api/notes/{id}

```json
{ "title": "新标题", "content": "新内容" }
```

两个字段均可选；`title` 超过 120 字截断，`content` 不超过 100000 字。只更新 `updated_at`。

## 约定

- 后端零第三方依赖（Python 标准库），OCR 使用 Windows 内置 `Windows.Media.Ocr`（经 `ocr/win_ocr.ps1` 调用）。
- 数据保存在 `data/notes.json` 与 `data/images/`，已被 `.gitignore` 排除。
- 默认监听 `http://127.0.0.1:8765`，可通过 `python server.py --port 9000` 修改。
- 前端原生 HTML/CSS/JS，无构建步骤，极简黑白风格。

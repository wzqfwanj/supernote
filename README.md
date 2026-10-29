# 截图笔记 Supernote

给一张截图，自动提取其中的文字，保存为可编辑的笔记。单机运行，数据只存在本地。

![技术栈](https://img.shields.io/badge/stack-HTML%20%7C%20CSS%20%7C%20JS%20%2B%20Python-000000)

## 功能（最小实现）

- **三种方式输入截图**：页面内 `Ctrl+V` 粘贴、拖拽图片到页面、点击选择文件（支持多选）。
- **自动 OCR**：调用 Windows 内置 OCR（`Windows.Media.Ocr`，简体中文优先），提取中文、英文、数字与代码。
- **笔记管理**：卡片列表、详情查看、编辑标题与正文、删除笔记，数据保存在本地 `data/`。
- **明暗双主题**：默认跟随系统深浅色，可一键切换并记住选择（`localStorage`），无刷新闪烁。
- **零第三方依赖**：前端原生 HTML/CSS/JS，后端仅用 Python 标准库，无需 `pip install`。

## 环境要求

- Windows 10 / 11（OCR 使用系统自带的 `Windows.Media.Ocr`，无网络也能识别）。
- Python 3.10+（开发环境为 3.12），无需安装任何第三方包。
- 系统中安装了**简体中文 OCR 语言包**（大多数中文 Windows 已内置）。

检查 OCR 语言包：

```powershell
powershell -NoProfile -Command "[Windows.Media.Ocr.OcrEngine,Windows.Media.Ocr,ContentType=WindowsRuntime] | Out-Null; [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | Select-Object LanguageTag"
```

若输出中没有 `zh-Hans-CN`，用管理员 PowerShell 安装后重启：

```powershell
Add-WindowsCapability -Online -Name "Language.OCR~~~zh-CN~0.0.1.0"
```

## 快速开始

1. 克隆仓库：

   ```powershell
   git clone https://github.com/wzqfwanj/supernote.git
   cd supernote
   ```

2. 启动（任选其一）：

   ```powershell
   python server.py        # 或双击 run.bat
   ```

   默认自动打开浏览器并访问 <http://127.0.0.1:8765>。常用参数：

   ```powershell
   python server.py --port 9000      # 换端口
   python server.py --no-browser     # 不自动打开浏览器
   python server.py --host 0.0.0.0   # 允许局域网访问（注意本地文件安全）
   ```

3. 在页面中粘贴 / 拖拽 / 选择一张截图，等待「识别中…」结束，即可看到识别出的笔记；点击卡片可修改标题和正文。

## 数据存储

- 笔记元数据与文本：`data/notes.json`
- 截图原图：`data/images/<id>.<图片格式>`
- `data/` 已在 `.gitignore` 中排除。备份或迁移整个 `data/` 目录即可。

## 项目结构

```
supernote/
├── server.py          # Python 标准库后端：静态文件 + OCR + 笔记 CRUD API
├── ocr/
│   └── win_ocr.ps1    # 调用 Windows.Media.Ocr 的 PowerShell 桥接脚本
├── web/
│   ├── index.html     # 单页界面
│   ├── style.css      # 极简黑白样式
│   └── app.js         # 原生 JS：上传 / 列表 / 编辑 / 删除
├── data/              # 本地数据（运行时生成，不提交）
├── API.md             # 前后端接口契约
├── run.bat            # Windows 一键启动
└── README.md
```

## 接口

详见 [API.md](API.md)，摘要：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查与 OCR 可用性 |
| GET | `/api/notes` | 笔记列表（倒序） |
| POST | `/api/notes` | 上传截图（data URL）并 OCR 生成笔记 |
| GET | `/api/notes/{id}` | 单条笔记 |
| PUT | `/api/notes/{id}` | 修改标题/正文 |
| DELETE | `/api/notes/{id}` | 删除笔记 |
| GET | `/images/{id}.{ext}` | 原图 |

## 开发规则

按 `开发规则/` 执行：前端仅 HTML/CSS/JS、后端 Python、只实现最小功能；Git 版本管理，代码托管于 GitHub。

## 已知限制

- 仅支持 Windows（依赖系统 OCR），没有跨平台 OCR 引擎。
- 单张截图解码后不超过 20MB；识别质量取决于截图清晰度与系统 OCR 语言包。
- 系统 OCR 对个别字符存在混淆（如 `l/I`、`0/O`、短横线），识别结果可直接在详情弹层中编辑修正。
- 无账号、无云同步、无搜索/标签等扩展功能（遵循「最小功能」原则）。

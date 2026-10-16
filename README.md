# Pet Portrait Studio

一个基于 FastAPI 和 OpenAI Images API 的宠物肖像生成应用。用户上传宠物照片后，服务调用图片编辑模型生成风格化肖像，并记录使用模型、耗时、用量估算和提示词版本。

## 项目亮点

- **完整的产品闭环**：图片上传、模型选择、提示词编辑、生成、预览下载和历史回看都在一个网页中完成。
- **可配置的模型路由**：模型白名单由环境变量控制；主模型失败时可选地切换一次兜底模型，并记录请求模型和实际模型。
- **可追溯的生成记录**：SQLite 保存调用元数据与提示词快照，图片按随机 ID 保存到本地文件，历史接口不暴露文件系统路径。
- **明确的输入边界**：服务端限制上传大小、识别支持的图片签名、验证模型白名单并限制提示词长度。
- **离线可启动**：API 密钥只在调用生成时检查；没有密钥时仍可启动服务、浏览界面和查看本地历史。
- **成本可解释**：按可配置价格表估算费用。估算与模型服务商最终账单可能不同，未知价格会保留为空。

## 架构

```text
浏览器 static/index.html
       │ 同源 HTTP / multipart
       ▼
FastAPI app.py ─── OpenAI-compatible Images API
       │
       ├── SQLite history.db：提示词版本、生成元数据
       └── history_images/<随机 ID>.png：生成图片
```

当前项目面向单机演示和作品集讲解：SQLite 与本地图片目录便于快速运行和检查；服务暂未提供用户认证、配额控制或多实例共享存储，因此不应直接作为公网多用户服务部署。

## 快速开始

需要 Python 3.11 或更新版本。

```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# macOS / Linux: source .venv/bin/activate
pip install -r requirements.txt
Copy-Item .env.example .env
```

在 `.env` 中设置 `MODEL_OPENAI_API_KEY`，然后启动：

```bash
python app.py
```

打开 <http://localhost:8000>。健康检查位于 `/health`，接口文档位于 `/docs`。没有 API 密钥时页面和历史仍可访问，但生成接口会返回 `503` 并提示配置密钥。

## 配置

| 变量 | 用途 | 默认值 |
| --- | --- | --- |
| `MODEL_OPENAI_API_KEY` | 图片服务凭据 | 无 |
| `MODEL_OPENAI_BASE_URL` | OpenAI 兼容服务地址 | SDK 默认地址 |
| `MODEL_IMAGE_NAME` | 默认生成模型 | `gpt-image-1` |
| `MODEL_IMAGE_OPTIONS` | 允许网页选择的模型，逗号分隔 | 默认模型 |
| `MODEL_IMAGE_FALLBACK` | 主模型失败后尝试一次的兜底模型 | 关闭 |
| `MODEL_PRICE_TABLE_JSON` | 覆盖内置估算价格的 JSON 对象 | 内置示例价格 |

不要把真实密钥提交到 Git。`.env`、虚拟环境、生成图片与日志已加入 `.gitignore`。

## API 概览

- `GET /api/config`：网页可用的模型和默认提示词。
- `GET /api/prompts`、`POST /api/prompts`：查询或保存提示词版本。
- `POST /api/generate`：以 multipart 上传 `file`，并提交 `model`、`prompt`。
- `GET /api/history?limit=12`：读取最近生成记录和当前结果集的费用汇总。
- `GET /api/history/image/{id}`：读取历史图片。

上传最大 10 MB，支持 PNG、JPEG、WebP。模型必须出现在服务端配置的 `MODEL_IMAGE_OPTIONS` 中。

## 测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

图像模型调用在测试中替换为确定性本地桩，不需要真实密钥或外部 API。测试验证 HTTP 行为、输入校验、提示词关联、图片历史读取和模型兜底顺序。

## 面试介绍建议

可以按“业务闭环 → 服务端边界 → 失败恢复与可观测性 → 当前取舍”介绍：网页负责体验与预览，FastAPI 管理校验和模型调用，SQLite 保存可追溯记录；生成链路记录实际模型、耗时、用量和提示词版本，主模型失败后按配置进行一次兜底。当前是单用户本地作品集项目，下一阶段可围绕身份隔离、任务队列、对象存储和端到端部署验证继续演进。

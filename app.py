"""
宠物 Pixar 3D 卡通化生成器 - 网页服务
运行: python app.py
默认监听 0.0.0.0:8000
需要在同目录 .env 文件中配置 MODEL_OPENAI_API_KEY
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path  # 仍用于读取 static/index.html

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from openai import OpenAI

# ---------------------------------------------------------------------------
# 环境变量 / 客户端初始化
# ---------------------------------------------------------------------------
load_dotenv()  # 默认读取当前工作目录下的 .env

API_KEY = os.getenv("MODEL_OPENAI_API_KEY")
BASE_URL = os.getenv("MODEL_OPENAI_BASE_URL") or None  # 如走中转/代理可在 .env 中配置

# 默认模型（前端下拉框预选项）
DEFAULT_MODEL = os.getenv("MODEL_IMAGE_NAME", "gpt-image-1")

# 兜底模型：主模型调用失败时自动重试的模型。留空表示不启用兜底。
FALLBACK_MODEL = os.getenv("MODEL_IMAGE_FALLBACK", "").strip()

# 前端下拉框可选模型列表，逗号分隔；未配置则只显示默认模型（+兜底模型，如果有）
_raw_options = os.getenv("MODEL_IMAGE_OPTIONS", "")
if _raw_options.strip():
    MODEL_OPTIONS = [m.strip() for m in _raw_options.split(",") if m.strip()]
else:
    MODEL_OPTIONS = [DEFAULT_MODEL]

if DEFAULT_MODEL not in MODEL_OPTIONS:
    MODEL_OPTIONS.insert(0, DEFAULT_MODEL)
if FALLBACK_MODEL and FALLBACK_MODEL not in MODEL_OPTIONS:
    MODEL_OPTIONS.append(FALLBACK_MODEL)

if not API_KEY:
    raise RuntimeError(
        "未找到 MODEL_OPENAI_API_KEY，请在项目根目录创建 .env 文件并配置，"
        "参考 .env.example"
    )

_client_kwargs = {"api_key": API_KEY}
if BASE_URL:
    _client_kwargs["base_url"] = BASE_URL

client = OpenAI(**_client_kwargs)

app = FastAPI(title="宠物 Pixar 化生成器")

# 允许跨源访问：
# - 有些人会直接双击打开 static/index.html（file:// 源）而不是通过 http://host:8000 访问，
#   或者从局域网内另一台设备访问，这些情况下浏览器会因为"跨源"拦截 fetch 请求，
#   报错通常表现为一个模糊的 "Failed to fetch"。加上 CORS 中间件可以避免这个问题。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """
    兜底异常处理：防止任何未预料到的异常（比如磁盘写历史记录失败）导致连接被直接断开，
    那种情况下前端会看到无法定位原因的 "Failed to fetch"，而不是一条可读的错误信息。
    """
    return JSONResponse(
        {"ok": False, "error": f"服务器内部错误：{exc}"},
        status_code=500,
    )

# ---------------------------------------------------------------------------
# 价格表（仅供估算参考，不代表实际账单！不同渠道/中转商计费可能不同）
# gpt-image 系列的 usage 会区分"文本 token"和"图片 token"（input_tokens_details 里的
# text_tokens / image_tokens），输出 token 基本都是图片 token，所以分别配置单价：
#   input_per_1k        文本输入 token 单价
#   output_per_1k       文本输出 token 单价（图片生成场景一般用不到，多为 0）
#   image_input_per_1k  图片输入 token 单价
#   image_output_per_1k 图片输出 token 单价
#   flat_per_image      按张计费的模型（如 dall-e 系列）直接用这个，忽略上面几个字段
# 可通过 .env 的 MODEL_PRICE_TABLE_JSON 覆盖，例如：
# MODEL_PRICE_TABLE_JSON={"dall-e-3":{"flat_per_image":0.06}}
# ---------------------------------------------------------------------------
DEFAULT_PRICE_TABLE = {
    "gpt-image-1": {
        "input_per_1k": 0.0025,
        "output_per_1k": 0,
        "image_input_per_1k": 0.01,
        "image_output_per_1k": 0.04,
    },
    "gpt-image-1.5": {
        "input_per_1k": 0.0025,
        "output_per_1k": 0.005,
        "image_input_per_1k": 0.008,
        "image_output_per_1k": 0.032,
    },
    "gpt-image-1-mini": {
        "input_per_1k": 0.001,
        "output_per_1k": 0,
    },
    "dall-e-3": {"flat_per_image": 0.04},
    "dall-e-2": {"flat_per_image": 0.02},
}
_price_override_raw = os.getenv("MODEL_PRICE_TABLE_JSON", "").strip()
PRICE_TABLE = dict(DEFAULT_PRICE_TABLE)
if _price_override_raw:
    try:
        PRICE_TABLE.update(json.loads(_price_override_raw))
    except json.JSONDecodeError:
        print("警告：MODEL_PRICE_TABLE_JSON 不是合法 JSON，已忽略，使用默认价格表")

# ---------------------------------------------------------------------------
# 历史记录存储（SQLite + 本地图片文件，不区分用户，所有记录全局可见）
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
HISTORY_DIR = BASE_DIR / "history_images"
HISTORY_DIR.mkdir(exist_ok=True)
DB_PATH = BASE_DIR / "history.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS history (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            requested_model TEXT NOT NULL,
            used_model TEXT NOT NULL,
            is_fallback INTEGER NOT NULL,
            elapsed_seconds REAL NOT NULL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            total_tokens INTEGER,
            estimated_cost_usd REAL,
            background_transparent INTEGER NOT NULL,
            image_filename TEXT NOT NULL,
            prompt_version_id TEXT,
            prompt_version_number INTEGER,
            prompt_text TEXT
        )
        """
    )
    # 兼容旧数据库：如果是从旧版本升级过来，history 表可能没有下面这几列，补上。
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(history)").fetchall()}
    for col_def in [
        ("prompt_version_id", "TEXT"),
        ("prompt_version_number", "INTEGER"),
        ("prompt_text", "TEXT"),
    ]:
        col_name, col_type = col_def
        if col_name not in existing_cols:
            conn.execute(f"ALTER TABLE history ADD COLUMN {col_name} {col_type}")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prompt_versions (
            id TEXT PRIMARY KEY,
            version_number INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()

    # 首次启动：把内置默认提示词写成第 1 个版本，后续用户编辑保存的版本会往后追加。
    row = conn.execute("SELECT COUNT(*) FROM prompt_versions").fetchone()
    if row[0] == 0:
        conn.execute(
            "INSERT INTO prompt_versions (id, version_number, content, created_at) VALUES (?, ?, ?, ?)",
            (uuid.uuid4().hex, 1, PROMPT_TEMPLATE, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

    conn.close()


def save_history_record(
    used_model: str,
    requested_model: str,
    is_fallback: bool,
    elapsed_seconds: float,
    usage: dict | None,
    background_transparent: bool,
    image_bytes: bytes,
    prompt_version: dict,
) -> dict:
    record_id = uuid.uuid4().hex
    image_filename = f"{record_id}.png"
    (HISTORY_DIR / image_filename).write_bytes(image_bytes)

    input_tokens = usage.get("input_tokens") if usage else None
    output_tokens = usage.get("output_tokens") if usage else None
    total_tokens = usage.get("total_tokens") if usage else None
    cost = estimate_cost_usd(used_model, usage)
    created_at = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT INTO history (
            id, created_at, requested_model, used_model, is_fallback,
            elapsed_seconds, input_tokens, output_tokens, total_tokens,
            estimated_cost_usd, background_transparent, image_filename,
            prompt_version_id, prompt_version_number, prompt_text
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record_id, created_at, requested_model, used_model, int(is_fallback),
            elapsed_seconds, input_tokens, output_tokens, total_tokens,
            cost, int(background_transparent), image_filename,
            prompt_version["id"], prompt_version["version_number"], prompt_version["content"],
        ),
    )
    conn.commit()
    conn.close()

    return {
        "id": record_id,
        "created_at": created_at,
        "requested_model": requested_model,
        "used_model": used_model,
        "is_fallback": is_fallback,
        "elapsed_seconds": elapsed_seconds,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "estimated_cost_usd": cost,
        "background_transparent": background_transparent,
        "image_filename": image_filename,
        "prompt_version_id": prompt_version["id"],
        "prompt_version_number": prompt_version["version_number"],
        "prompt_text": prompt_version["content"],
    }


def estimate_cost_usd(model_name: str, usage: dict | None):
    """
    按价格表估算本次调用花费（美元）。查不到价格 / 缺少必要用量信息时返回 None（前端显示"未知"）。

    gpt-image 系列的 usage 会在 input_tokens_details 里细分 text_tokens（文本输入）和
    image_tokens（图片输入），输出 token（output_tokens）基本都是生成的图片 token。
    价格表里如果配置了 image_input_per_1k / image_output_per_1k 就优先按图片单价算，
    没配置就退回用文本单价（input_per_1k / output_per_1k）粗略估算。
    """
    pricing = PRICE_TABLE.get(model_name)
    if not pricing:
        return None

    if "flat_per_image" in pricing:
        return round(pricing["flat_per_image"], 6)

    if not usage:
        return None

    input_tokens = usage.get("input_tokens") or 0
    output_tokens = usage.get("output_tokens") or 0
    details = usage.get("input_tokens_details") or {}
    text_tokens = details.get("text_tokens")
    image_input_tokens = details.get("image_tokens")

    cost = 0.0

    if text_tokens is not None or image_input_tokens is not None:
        # usage 有文本/图片细分，分别按各自单价算；没配图片单价就退回文本单价
        cost += (text_tokens or 0) / 1000 * pricing.get("input_per_1k", 0)
        cost += (image_input_tokens or 0) / 1000 * pricing.get(
            "image_input_per_1k", pricing.get("input_per_1k", 0)
        )
    else:
        # 没有细分数据（比如某些中转商返回的 usage 只有总数），全部按文本输入单价粗略估算
        cost += input_tokens / 1000 * pricing.get("input_per_1k", 0)

    # 输出 token：图片生成场景基本都是图片 token，优先用 image_output_per_1k
    output_price = pricing.get("image_output_per_1k", pricing.get("output_per_1k", 0))
    cost += output_tokens / 1000 * output_price

    return round(cost, 6)


# ---------------------------------------------------------------------------
# Prompt：约束风格 = Pixar 3D 卡通、正方形、透明背景，尽量还原宠物特征
# ---------------------------------------------------------------------------
PROMPT_TEMPLATE = (
    "Turn the pet in the uploaded photo into a cute Pixar/Disney-style 3D animated "
    "character portrait. Preserve the pet's real breed, fur color and pattern, "
    "eye color, and distinguishing facial features so it is clearly recognizable "
    "as the same animal. Square 1:1 composition, head-and-shoulders framing, "
    "centered subject, soft appealing studio lighting, smooth stylized 3D render "
    "look (like a Pixar movie character), high detail on fur and eyes. "
    "The background MUST be fully transparent — no scene, no floor, no shadow "
    "cast on any backdrop — isolated subject only with clean alpha edges suitable "
    "for cutout use."
)

init_db()  # 建表要用到上面的默认提示词写入初始版本，所以放在 PROMPT_TEMPLATE 定义之后


def get_or_create_prompt_version(content: str) -> dict:
    """
    按内容去重：如果这段提示词之前保存过（内容完全一致），直接复用那个版本；
    否则追加一个新版本号。返回 {id, version_number, content, created_at}
    """
    content = (content or "").strip()
    if not content:
        content = PROMPT_TEMPLATE

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    existing = conn.execute(
        "SELECT * FROM prompt_versions WHERE content = ? ORDER BY created_at ASC LIMIT 1",
        (content,),
    ).fetchone()
    if existing:
        conn.close()
        return dict(existing)

    max_row = conn.execute("SELECT MAX(version_number) AS m FROM prompt_versions").fetchone()
    next_version = (max_row["m"] or 0) + 1
    new_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO prompt_versions (id, version_number, content, created_at) VALUES (?, ?, ?, ?)",
        (new_id, next_version, content, created_at),
    )
    conn.commit()
    conn.close()

    return {
        "id": new_id,
        "version_number": next_version,
        "content": content,
        "created_at": created_at,
    }


def list_prompt_versions() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM prompt_versions ORDER BY version_number DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _supports_transparent_background(model_name: str) -> bool:
    """只有 gpt-image 系列明确支持 background=transparent 参数。"""
    return "gpt-image" in model_name.lower()


def _extract_usage(result) -> dict | None:
    """从 OpenAI 返回结果里提取 token 用量（仅 gpt-image 系列会有 usage 字段）。"""
    usage = getattr(result, "usage", None)
    if usage is None:
        return None
    if hasattr(usage, "model_dump"):
        return usage.model_dump()
    try:
        return dict(usage)
    except (TypeError, ValueError):
        return None


def _call_image_model(model_name: str, image_bytes: bytes, filename: str, prompt_text: str):
    """
    调用单个模型做图片编辑。每次都用原始 bytes 重新构造 BytesIO，
    避免上一次失败请求把流位置消耗掉导致重试时读到空内容。
    返回 (b64_json, background_transparent_requested, usage_dict)
    """
    buf = BytesIO(image_bytes)
    buf.name = filename or "upload.png"

    kwargs = dict(
        model=model_name,
        image=buf,
        prompt=prompt_text,
        size="1024x1024",
    )

    transparent_requested = _supports_transparent_background(model_name)
    if transparent_requested:
        kwargs["background"] = "transparent"

    result = client.images.edit(**kwargs)
    usage = _extract_usage(result)
    return result.data[0].b64_json, transparent_requested, usage


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/api/config")
def get_config():
    """前端加载时拉取可选模型列表 / 默认模型 / 兜底模型配置 / 默认提示词"""
    return {
        "models": MODEL_OPTIONS,
        "default_model": DEFAULT_MODEL,
        "fallback_model": FALLBACK_MODEL or None,
        "default_prompt": PROMPT_TEMPLATE,
    }


@app.get("/api/prompts")
def get_prompts():
    """列出所有已保存的提示词版本（全局共享，不区分用户），按版本号倒序"""
    return {"items": list_prompt_versions()}


@app.post("/api/prompts")
async def create_prompt(content: str = Form(...)):
    """
    显式保存一个新的提示词版本（不触发生成）。
    内容和已有版本完全一致时会直接复用那个版本号，不会产生重复版本。
    """
    version = get_or_create_prompt_version(content)
    return {"ok": True, "version": version}


@app.post("/api/generate")
async def generate(file: UploadFile = File(...), model: str = Form(None), prompt: str = Form(None)):
    """
    接收一张真实宠物照片 + 用户选择的模型 + 提示词，调用模型生成 Pixar 风格 3D 卡通图。
    若所选模型调用失败，且配置了兜底模型，则自动切换兜底模型重试一次。
    提示词若和已有版本内容不同，会自动保存为新版本；成功后落库历史记录
    （模型、耗时、token 消耗、预估费用、提示词版本），返回 base64 PNG。
    """
    image_bytes = await file.read()
    filename = file.filename or "upload.png"

    requested_model = (model or DEFAULT_MODEL).strip()
    prompt_version = get_or_create_prompt_version(prompt or PROMPT_TEMPLATE)
    prompt_text = prompt_version["content"]

    attempt_order = [requested_model]
    if FALLBACK_MODEL and FALLBACK_MODEL != requested_model:
        attempt_order.append(FALLBACK_MODEL)

    errors = []
    start = time.time()

    for idx, m in enumerate(attempt_order):
        try:
            b64, transparent, usage = _call_image_model(m, image_bytes, filename, prompt_text)
            elapsed = round(time.time() - start, 2)
            is_fallback = idx > 0

            # 历史记录写入失败（磁盘/数据库问题）不应该影响已经生成成功的图片返回给用户，
            # 这里单独兜底，写入失败就跳过历史统计字段，但依然把图片正常返回。
            try:
                record = save_history_record(
                    used_model=m,
                    requested_model=requested_model,
                    is_fallback=is_fallback,
                    elapsed_seconds=elapsed,
                    usage=usage,
                    background_transparent=transparent,
                    image_bytes=base64.b64decode(b64),
                    prompt_version=prompt_version,
                )
                history_saved = True
            except Exception as history_err:  # noqa: BLE001
                print(f"警告：历史记录写入失败：{history_err}")
                record = {
                    "id": None, "input_tokens": (usage or {}).get("input_tokens"),
                    "output_tokens": (usage or {}).get("output_tokens"),
                    "total_tokens": (usage or {}).get("total_tokens"),
                    "estimated_cost_usd": estimate_cost_usd(m, usage),
                }
                history_saved = False

            return JSONResponse(
                {
                    "ok": True,
                    "image_base64": b64,
                    "requested_model": requested_model,
                    "used_model": m,
                    "is_fallback": is_fallback,
                    "background_transparent": transparent,
                    "elapsed_seconds": elapsed,
                    "input_tokens": record["input_tokens"],
                    "output_tokens": record["output_tokens"],
                    "total_tokens": record["total_tokens"],
                    "estimated_cost_usd": record["estimated_cost_usd"],
                    "history_id": record["id"],
                    "history_saved": history_saved,
                    "prompt_version_id": prompt_version["id"],
                    "prompt_version_number": prompt_version["version_number"],
                }
            )
        except Exception as e:  # noqa: BLE001 - 记录后继续尝试下一个模型
            errors.append({"model": m, "error": str(e)})
            continue

    elapsed = round(time.time() - start, 2)
    return JSONResponse(
        {
            "ok": False,
            "requested_model": requested_model,
            "attempts": errors,
            "elapsed_seconds": elapsed,
            "error": errors[-1]["error"] if errors else "未知错误",
        },
        status_code=500,
    )


@app.get("/api/history")
def get_history(limit: int = 60):
    """
    返回全部历史生成记录（不区分用户，全局共享），按时间倒序。
    """
    limit = max(1, min(limit, 200))
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM history ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()

    items = []
    for r in rows:
        items.append(
            {
                "id": r["id"],
                "created_at": r["created_at"],
                "requested_model": r["requested_model"],
                "used_model": r["used_model"],
                "is_fallback": bool(r["is_fallback"]),
                "elapsed_seconds": r["elapsed_seconds"],
                "input_tokens": r["input_tokens"],
                "output_tokens": r["output_tokens"],
                "total_tokens": r["total_tokens"],
                "estimated_cost_usd": r["estimated_cost_usd"],
                "background_transparent": bool(r["background_transparent"]),
                "image_url": f"/api/history/image/{r['id']}",
                "prompt_version_number": r["prompt_version_number"],
                "prompt_text": r["prompt_text"],
            }
        )

    # 汇总统计：总调用次数、总耗时、总 token、总预估费用
    total_cost = sum(i["estimated_cost_usd"] or 0 for i in items)
    total_calls = len(items)

    return {
        "items": items,
        "summary": {
            "total_calls": total_calls,
            "total_estimated_cost_usd": round(total_cost, 6),
        },
    }


@app.get("/api/history/image/{item_id}")
def get_history_image(item_id: str):
    """按历史记录 id 返回对应的生成图片文件"""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT image_filename FROM history WHERE id = ?", (item_id,)
    ).fetchone()
    conn.close()

    if not row:
        return JSONResponse({"ok": False, "error": "记录不存在"}, status_code=404)

    file_path = HISTORY_DIR / row[0]
    if not file_path.exists():
        return JSONResponse({"ok": False, "error": "图片文件不存在"}, status_code=404)

    return FileResponse(file_path, media_type="image/png")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
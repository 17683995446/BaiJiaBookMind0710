"""FastAPI Web 服务：上传文件 → 配置 → 实时进度 → 预览/下载各类输出。

前端架构：hash 路由 SPA，主页为功能导航，各功能独立页面，可扩展。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    HTMLResponse, JSONResponse, FileResponse, StreamingResponse,
)
import time
import re

from .config import AppConfig, LLMConfig, PipelineConfig, BookType, OutputFormat
from .llm import LLMClient
from .orchestrator import run, rerun_from_archive
from . import outputs as out_mod

# ── 日志系统 ──
logger = logging.getLogger("book_digest")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_h)

app = FastAPI(title="拆书 Agent", version="0.2.0")

# ── .env 文件支持 ──
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv 可选，不影响运行

# ── CORS 配置 ──
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("BD_CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 速率限制（简易内存版） ──
RATE_LIMIT_WINDOW = 60  # 60 秒窗口
RATE_LIMIT_MAX = int(os.getenv("BD_RATE_LIMIT", "20"))  # 默认 20 次/分钟
_rate_store: dict[str, list[float]] = {}

@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """简易速率限制中间件：限制每个 IP 每分钟请求次数。"""
    # 仅限制昂贵的 API 端点
    path = request.url.path
    if path in ("/api/digest", "/api/qa", "/api/tts"):
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        if client_ip not in _rate_store:
            _rate_store[client_ip] = []
        # 清理过期记录
        _rate_store[client_ip] = [t for t in _rate_store[client_ip] if now - t < RATE_LIMIT_WINDOW]
        if len(_rate_store[client_ip]) >= RATE_LIMIT_MAX:
            return JSONResponse(
                {"detail": f"请求过于频繁，每分钟限 {RATE_LIMIT_MAX} 次"},
                status_code=429,
                headers={"Retry-After": str(RATE_LIMIT_WINDOW)},
            )
        _rate_store[client_ip].append(now)
    return await call_next(request)

# 上传临时目录
UPLOAD_DIR = Path(os.getenv("BD_UPLOAD_DIR", "/tmp/book_digest_uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
# 存档输出目录
ARCHIVE_DIR = Path(os.getenv("BD_OUTPUT_DIR", "output"))
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

# 任务进度存储（简易内存版，适合单机）
JOBS: dict[str, dict] = {}
# 任务超时时间（秒）
JOB_TIMEOUT = int(os.getenv("BD_JOB_TIMEOUT", "1800"))  # 30 分钟
# 任务过期清理间隔
JOB_CLEANUP_INTERVAL = 600  # 10 分钟
# 文件上传大小限制（字节，默认 50MB）
MAX_UPLOAD_SIZE = int(os.getenv("BD_MAX_UPLOAD_SIZE", str(50 * 1024 * 1024)))
# 允许的文件扩展名
ALLOWED_EXTENSIONS = {".txt", ".md", ".pdf", ".epub"}


def _load_result_by_job_id(job_id: str):
    """根据 job_id 加载拆书结果。
    支持两种模式：
    1. 普通 job_id：从内存 JOBS 字典查找
    2. archive:xxx 格式：从存档文件直接加载（解决服务器重启后 JOBS 丢失的问题）
    """
    from .cache import load_archive

    # 存档模式：archive:filename
    if job_id.startswith("archive:"):
        archive_name = job_id[len("archive:"):] + ".bda.json"
        archive_path = ARCHIVE_DIR / archive_name
        if not archive_path.exists():
            raise HTTPException(404, "存档不存在，请重新拆书或检查存档文件")
        return load_archive(archive_path)

    # 普通模式：从内存查找
    job = JOBS.get(job_id)
    if not job or job["status"] != "done":
        raise HTTPException(400, "拆书任务未完成或不存在")
    archive_path = job["files"].get("存档(.bda)")
    if not archive_path or not Path(archive_path).exists():
        raise HTTPException(404, "存档不存在")
    return load_archive(archive_path)


@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML


@app.get("/manifest.json")
async def manifest():
    """PWA manifest for installable web app."""
    return JSONResponse({
        "name": "拆书 Agent",
        "short_name": "拆书",
        "description": "集百家之所长的拆书工具",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0f0f1a",
        "theme_color": "#7c3aed",
        "icons": [
            {"src": "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Crect width='100' height='100' rx='20' fill='%237c3aed'/%3E%3Ctext x='50' y='65' font-size='50' text-anchor='middle' fill='white' font-family='sans-serif'%3E拆%3C/text%3E%3C/svg%3E", "sizes": "any", "type": "image/svg+xml"}
        ],
    })


# Service Worker JS (内联)
_SW_JS = """
const CACHE_NAME = 'book-digest-v1';
const CDN_CACHE = 'book-digest-cdn-v1';

// 需要缓存的本地路由
const LOCAL_URLS = ['/', '/manifest.json', '/health'];

// 安装时预缓存本地页面
self.addEventListener('install', function(event) {
  event.waitUntil(
    caches.open(CACHE_NAME).then(function(cache) {
      return cache.addAll(LOCAL_URLS);
    }).then(function() {
      return self.skipWaiting();
    })
  );
});

// 激活时清理旧缓存
self.addEventListener('activate', function(event) {
  event.waitUntil(
    caches.keys().then(function(names) {
      return Promise.all(names.map(function(name) {
        if (name !== CACHE_NAME && name !== CDN_CACHE) {
          return caches.delete(name);
        }
      }));
    }).then(function() {
      return self.clients.claim();
    })
  );
});

// 网络优先，失败则回退到缓存（适合 API 请求）
// 缓存优先，更新后台（适合静态资源）
self.addEventListener('fetch', function(event) {
  var url = new URL(event.request.url);

  // 跳过非 GET 请求
  if (event.request.method !== 'GET') return;

  // API 请求：网络优先
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(
      fetch(event.request).catch(function() {
        return caches.match(event.request);
      })
    );
    return;
  }

  // CDN 资源：缓存优先（Stale While Revalidate）
  if (url.hostname !== location.hostname) {
    event.respondWith(
      caches.open(CDN_CACHE).then(function(cache) {
        return cache.match(event.request).then(function(cached) {
          var fetchPromise = fetch(event.request).then(function(resp) {
            if (resp.ok) cache.put(event.request, resp.clone());
            return resp;
          }).catch(function() { return cached; });
          return cached || fetchPromise;
        });
      })
    );
    return;
  }

  // 本地页面：网络优先，失败回退缓存
  event.respondWith(
    fetch(event.request).then(function(resp) {
      if (resp.ok) {
        var respClone = resp.clone();
        caches.open(CACHE_NAME).then(function(cache) {
          cache.put(event.request, respClone);
        });
      }
      return resp;
    }).catch(function() {
      return caches.match(event.request).then(function(cached) {
        return cached || caches.match('/');
      });
    })
  );
});
"""


@app.get("/sw.js")
async def service_worker():
    """Service Worker for offline support."""
    from fastapi.responses import Response
    return Response(
        content=_SW_JS,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"}
    )


@app.get("/health")
async def health():
    """健康检查端点。"""
    active_jobs = sum(1 for j in JOBS.values() if j.get("status") == "running")
    return {
        "status": "healthy",
        "version": "0.2.0",
        "jobs_total": len(JOBS),
        "jobs_active": active_jobs,
        "has_api_key": bool(os.getenv("BD_API_KEY", "")),
    }


@app.post("/api/digest")
async def api_digest(
    file: UploadFile = File(...),
    api_key: str = Form(""),
    base_url: str = Form("https://api.siliconflow.cn/v1"),
    model: str = Form("Qwen/Qwen3.5-9B"),
    book_type: BookType = Form(BookType.NONFICTION),
    focus: str = Form(""),
    outputs: str = Form("all"),
    chunk_size: int = Form(2500),
    reviewers: int = Form(2),
    max_revisions: int = Form(2),
):
    """提交拆书任务，返回 job_id。"""
    # 环境变量作为兜底默认值（便于服务端预设 Key）
    if not api_key:
        api_key = os.getenv("BD_API_KEY", "")
    if not api_key:
        raise HTTPException(400, "缺少 API Key，请在表单中填写或设置 BD_API_KEY 环境变量")

    # 文件名安全检查
    if not file.filename:
        raise HTTPException(400, "文件名无效")
    filename = Path(file.filename).name  # 防止路径穿越
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"不支持的文件格式: {suffix}，仅支持 {', '.join(sorted(ALLOWED_EXTENSIONS))}")

    # 数值范围校验
    chunk_size = max(500, min(chunk_size, 10000))
    reviewers = max(1, min(reviewers, 5))
    max_revisions = max(0, min(max_revisions, 5))

    # 流式读取文件，检查大小
    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(413, f"文件过大: {len(content)} bytes，上限 {MAX_UPLOAD_SIZE} bytes")
    if len(content) < 10:
        raise HTTPException(400, "文件内容过少，可能是空文件")

    # 安全文件名（UUID + 原扩展名）
    safe_name = f"upload_{uuid.uuid4().hex[:8]}_{filename}"
    saved = UPLOAD_DIR / safe_name
    saved.write_bytes(content)
    logger.info(f"File uploaded: {filename} -> {safe_name} ({len(content)} bytes)")

    # 清理内容引用，释放内存
    del content

    cfg = AppConfig(
        llm=LLMConfig(api_key=api_key, base_url=base_url, model=model,
                      reviewers=reviewers, max_revisions=max_revisions),
        pipeline=PipelineConfig(chunk_size=chunk_size),
        book_type=book_type,
        focus_prompt=focus or None,
    )
    out_set = _parse(outputs)

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "pending", "stage": "排队中", "progress": 0, "files": {}}

    # 后台跑
    job_created_at = datetime.now()
    JOBS[job_id] = {
        "status": "pending", "stage": "排队中", "progress": 0, "files": {},
        "created_at": job_created_at.isoformat(),
    }
    logger.info(f"Job {job_id} created: file={saved.name}, model={model}")

    loop = asyncio.new_event_loop()
    try:
        loop.run_in_executor(
            None, _run_job, job_id, saved, cfg, out_set
        )
    finally:
        # 不关闭 loop，因为 run_in_executor 使用默认线程池
        pass
    return {"job_id": job_id}


def _run_job(job_id: str, path: Path, cfg: AppConfig, out_set: set[OutputFormat]):
    """后台执行拆书任务。"""
    start_time = datetime.now()

    def on_progress(stage, cur, total):
        if total > 0:
            JOBS[job_id]["stage"] = stage
            JOBS[job_id]["progress"] = int(cur / total * 100)
        JOBS[job_id]["status"] = "running"
        # 超时检查
        elapsed = (datetime.now() - start_time).total_seconds()
        if elapsed > JOB_TIMEOUT:
            raise TimeoutError(f"任务超时（{JOB_TIMEOUT}秒），当前阶段: {stage}")

    try:
        logger.info(f"Job {job_id} started: {path.name}")
        result, paths = run(path, cfg, out_set, on_progress=on_progress)
        JOBS[job_id]["status"] = "done"
        JOBS[job_id]["progress"] = 100
        JOBS[job_id]["files"] = {k: str(v) for k, v in paths.items()}
        JOBS[job_id]["stats"] = {
            "units": len(result.units),
            "clusters": len(result.graph.clusters),
            "cards": len(result.cards),
            "skills": len(result.skill_pack.get("skills", [])),
        }
        # 精简存储：只保留前100条单元和摘要数据，避免内存膨胀
        JOBS[job_id]["result"] = {
            "units": [{"id": u.id, "content": u.content[:500], "chapter": u.chapter, "tags": u.tags} for u in result.units[:200]],
            "units_total": len(result.units),
            "graph": {
                "clusters": {k: v for k, v in list(result.graph.clusters.items())[:20]},
                "nodes": [{"id": n.id, "label": n.label, "group": n.group, "size": n.size, "weight": n.weight} for n in result.graph.nodes[:300]],
                "edges": [{"from": e.source, "to": e.target, "weight": e.weight, "relation": e.relation} for e in result.graph.edges[:500]],
            },
            "book_title": result.book_title,
            "chapter_summaries": dict(list(result.chapter_summaries.items())[:30]),
            "overall_summary": result.overall_summary[:2000] if result.overall_summary else "",
            "cards": result.cards[:100],
            "skill_pack": result.skill_pack,
        }
        JOBS[job_id]["report"] = str(paths.get("综合报告", ""))
        elapsed = (datetime.now() - start_time).total_seconds()
        logger.info(f"Job {job_id} done in {elapsed:.1f}s: {JOBS[job_id]['stats']}")
    except TimeoutError as e:
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["error"] = str(e)
        logger.error(f"Job {job_id} timeout: {e}")
    except Exception as e:
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["error"] = str(e)
        logger.exception(f"Job {job_id} failed: {e}")
    finally:
        # 清理上传的临时文件
        try:
            if path.exists():
                path.unlink()
        except Exception:
            pass


def _cleanup_old_jobs():
    """清理过期任务，防止内存泄漏。"""
    now = datetime.now()
    expired = []
    for jid, job in JOBS.items():
        created_str = job.get("created_at")
        if not created_str:
            continue
        try:
            created = datetime.fromisoformat(created_str)
            # 已完成任务保留2小时，运行中任务保留24小时
            max_age = timedelta(hours=24) if job.get("status") == "running" else timedelta(hours=2)
            if now - created > max_age:
                expired.append(jid)
        except (ValueError, TypeError):
            continue
    for jid in expired:
        job = JOBS.pop(jid, None)
        if job:
            logger.info(f"Cleaned up expired job {jid} (status={job.get('status')})")
    if expired:
        logger.info(f"Cleaned up {len(expired)} expired jobs, {len(JOBS)} remaining")


@app.get("/api/status/{job_id}")
async def api_status(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404, "任务不存在")
    return JOBS[job_id]


@app.get("/api/preview/{job_id}")
async def api_preview(job_id: str):
    """返回综合报告 HTML 预览。"""
    job = JOBS.get(job_id)
    if not job or job["status"] != "done":
        raise HTTPException(400, "任务未完成")
    report = job.get("report")
    if not report or not Path(report).exists():
        raise HTTPException(404, "报告不存在")
    return HTMLResponse(Path(report).read_text("utf-8"))


@app.get("/api/download/{job_id}/{kind}")
async def api_download(job_id: str, kind: str):
    job = JOBS.get(job_id)
    if not job or job["status"] != "done":
        raise HTTPException(400, "任务未完成")
    path = job["files"].get(kind)
    if not path:
        raise HTTPException(404, f"无此输出：{kind}")
    return FileResponse(path, filename=Path(path).name)


# ════════════════════════════════════════════════════════════
# 新增功能 API：元数据 / Anki / 问答 / 听书 / Obsidian
# ════════════════════════════════════════════════════════════

@app.get("/api/voices")
async def api_voices():
    """获取可用 TTS 音色列表。"""
    from .tools_tts import list_voices
    return {"voices": list_voices()}


@app.post("/api/metadata")
async def api_metadata(file: UploadFile = File(...)):
    """提取书籍元数据（封面/作者/ISBN）。"""
    from .tools_export import extract_metadata
    suffix = Path(file.filename).suffix
    saved = UPLOAD_DIR / f"meta_{file.filename}"
    saved.write_bytes(await file.read())
    try:
        meta = extract_metadata(str(saved), output_dir=str(UPLOAD_DIR))
        return meta
    except Exception as e:
        raise HTTPException(500, f"元数据提取失败：{e}")


@app.post("/api/qa")
async def api_qa(
    job_id: str = Form(...),
    question: str = Form(...),
    api_key: str = Form(""),
    base_url: str = Form("https://api.siliconflow.cn/v1"),
    model: str = Form("Qwen/Qwen3.5-9B"),
):
    """基于拆书结果回答问题（RAG 问答）。支持普通 job_id 和 archive:xxx 格式。"""
    from .tools_qa import answer_question

    # 输入校验
    question = question.strip()
    if not question:
        raise HTTPException(400, "问题不能为空")
    if len(question) > 2000:
        raise HTTPException(400, "问题过长，请限制在 2000 字以内")

    result = _load_result_by_job_id(job_id)

    if not api_key:
        api_key = os.getenv("BD_API_KEY", "")
    cfg = AppConfig(llm=LLMConfig(api_key=api_key, base_url=base_url, model=model, enable_thinking=False))
    llm = LLMClient(cfg.llm)
    try:
        resp = answer_question(cfg, llm, result, question)
        return resp
    except Exception as e:
        raise HTTPException(500, f"问答失败：{e}")


@app.post("/api/anki/{job_id}")
async def api_anki(job_id: str):
    """导出 Anki 闪卡。支持普通 job_id 和 archive:xxx 格式。"""
    from .tools_export import export_anki

    result = _load_result_by_job_id(job_id)
    output_path = UPLOAD_DIR / f"{result.book_title}.apkg"
    try:
        path = export_anki(result, output_path)
        return FileResponse(str(path), filename=Path(path).name,
                            media_type="application/octet-stream")
    except Exception as e:
        raise HTTPException(500, f"Anki 导出失败：{e}")


@app.post("/api/obsidian/{job_id}")
async def api_obsidian(job_id: str):
    """导出 Obsidian Vault（ZIP）。支持普通 job_id 和 archive:xxx 格式。"""
    from .tools_export import export_obsidian
    import zipfile

    result = _load_result_by_job_id(job_id)
    vault_dir = UPLOAD_DIR / f"{result.book_title}_vault"
    try:
        export_obsidian(result, vault_dir)
        # 打包为 ZIP
        zip_path = UPLOAD_DIR / f"{result.book_title}_obsidian.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in vault_dir.rglob("*"):
                if f.is_file():
                    zf.write(f, f.relative_to(vault_dir))
        return FileResponse(str(zip_path), filename=zip_path.name,
                            media_type="application/zip")
    except Exception as e:
        raise HTTPException(500, f"Obsidian 导出失败：{e}")


@app.post("/api/tts")
async def api_tts(
    job_id: str = Form(...),
    mode: str = Form("summary"),
    voice: str = Form("zh-CN-YunxiNeural"),
):
    """生成听书音频。支持普通 job_id 和 archive:xxx 格式。"""
    from .tools_tts import generate_audiobook

    result = _load_result_by_job_id(job_id)
    try:
        resp = generate_audiobook(result, UPLOAD_DIR, voice=voice, mode=mode)
        # 返回第一个文件的下载链接
        files_info = [{"name": Path(f).name, "path": str(f)} for f in resp["files"]]
        return {"mode": resp["mode"], "files": files_info,
                "duration": resp.get("total_duration_est", "")}
    except Exception as e:
        raise HTTPException(500, f"听书生成失败：{e}")


@app.get("/api/tts/download/{filename}")
async def api_tts_download(filename: str):
    """下载听书音频文件。"""
    # 安全检查：防止路径穿越
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "非法文件名")
    path = (UPLOAD_DIR / filename).resolve()
    if not str(path).startswith(str(UPLOAD_DIR.resolve())):
        raise HTTPException(400, "非法路径")
    if not path.exists():
        raise HTTPException(404, "文件不存在")
    return FileResponse(str(path), filename=filename, media_type="audio/mpeg")


# ════════════════════════════════════════
# 知识海报 API（金句卡片 + AI 配图）
# ════════════════════════════════════════
# 海报图片缓存目录
POSTER_IMG_DIR = UPLOAD_DIR / "posters"
POSTER_IMG_DIR.mkdir(parents=True, exist_ok=True)
# 海报缓存目录（持久化到磁盘，修复问题2）
POSTER_CACHE_DIR = ARCHIVE_DIR / ".poster_cache"
POSTER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
# 内存缓存 + TTL（2小时），修复问题2/9
POSTER_CACHE_TTL = 7200  # 秒
POSTER_CACHE: dict[str, dict] = {}  # {job_id: {"cards": [...], "ts": timestamp}}
import threading as _poster_threading
POSTER_LOCK = _poster_threading.Lock()


def _poster_cache_path(job_id: str) -> Path:
    """海报缓存文件路径。"""
    safe = re.sub(r"[^\w\u4e00-\u9fa5]", "_", job_id)[:60]
    return POSTER_CACHE_DIR / f"{safe}.json"


def _poster_cache_get(job_id: str) -> list[dict] | None:
    """读取海报缓存（内存优先，磁盘兜底）。修复问题2。"""
    with POSTER_LOCK:
        entry = POSTER_CACHE.get(job_id)
        if entry:
            if time.time() - entry["ts"] < POSTER_CACHE_TTL:
                return entry["cards"]
            del POSTER_CACHE[job_id]
    # 磁盘兜底
    p = _poster_cache_path(job_id)
    if p.exists():
        try:
            import json as _json
            data = _json.loads(p.read_text("utf-8"))
            with POSTER_LOCK:
                POSTER_CACHE[job_id] = {"cards": data, "ts": time.time()}
            return data
        except Exception:
            pass
    return None


def _poster_cache_set(job_id: str, cards: list[dict]):
    """写入海报缓存（内存 + 磁盘）。修复问题2。"""
    with POSTER_LOCK:
        POSTER_CACHE[job_id] = {"cards": cards, "ts": time.time()}
    # 持久化到磁盘
    try:
        import json as _json
        _poster_cache_path(job_id).write_text(_json.dumps(cards, ensure_ascii=False), "utf-8")
    except Exception:
        pass


def _poster_cache_update_card(job_id: str, index: int, card: dict):
    """更新单张卡片的缓存。修复问题2。"""
    with POSTER_LOCK:
        entry = POSTER_CACHE.get(job_id)
        if entry and 0 <= index < len(entry["cards"]):
            entry["cards"][index] = card
    # 同步到磁盘
    try:
        import json as _json
        p = _poster_cache_path(job_id)
        if p.exists():
            data = _json.loads(p.read_text("utf-8"))
            if 0 <= index < len(data):
                data[index] = card
                p.write_text(_json.dumps(data, ensure_ascii=False), "utf-8")
    except Exception:
        pass


# 启动时清理过期图片（修复问题7）
from .tools_poster import cleanup_old_images
cleanup_old_images(POSTER_IMG_DIR)


@app.post("/api/poster/quotes/{job_id}")
async def api_poster_quotes(job_id: str, request: Request):
    """提取金句并生成卡片骨架（无图）。支持 archive:xxx 格式。
    修复问题1: 不再从前端接收 api_key，统一从环境变量获取
    返回: {"cards": [{"index", "quote", "chapter", "interpretation", "image_prompt", "image": null}, ...]}
    """
    from .tools_poster import extract_quotes

    # 检查缓存（修复问题2/9）
    cached = _poster_cache_get(job_id)
    if cached:
        return {"cards": cached, "cached": True}

    # 解析参数（不再接收 api_key）
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    n = int(body.get("n", 10))
    n = max(1, min(n, 20))  # 限制范围

    result = _load_result_by_job_id(job_id)

    # 修复问题1: 统一从环境变量获取 API Key
    api_key = os.getenv("BD_API_KEY", "")
    base_url = "https://api.siliconflow.cn/v1"
    model = "Qwen/Qwen3.5-9B"
    cfg = AppConfig(llm=LLMConfig(api_key=api_key, base_url=base_url, model=model, enable_thinking=False))
    llm = LLMClient(cfg.llm)

    try:
        quotes = extract_quotes(cfg, llm, result, n=n)
        cards = []
        for i, q in enumerate(quotes):
            cards.append({
                "index": i,
                "quote": q.get("quote", ""),
                "chapter": q.get("chapter", ""),
                "interpretation": q.get("interpretation", ""),
                "image_prompt": q.get("image_prompt", ""),  # 修复问题6: 保存 image_prompt
                "image": None,
                "book_title": result.book_title,
            })
        _poster_cache_set(job_id, cards)  # 修复问题2
        return {"cards": cards}
    except Exception as e:
        raise HTTPException(500, f"金句提取失败：{e}")


@app.post("/api/poster/image/{job_id}/{index}")
async def api_poster_image(job_id: str, index: int, request: Request):
    """为指定金句卡片生成 AI 配图。返回 {"image": "filename.png"} 或 {"image": null}。
    修复问题1: 不再从前端接收 api_key
    修复问题6: 使用 LLM 生成的 image_prompt 而非中文金句
    """
    from .tools_poster import generate_image

    # 从缓存获取卡片
    cards = _poster_cache_get(job_id)
    if not cards:
        raise HTTPException(400, "请先提取金句")
    if index < 0 or index >= len(cards):
        raise HTTPException(400, "卡片索引无效")

    card = cards[index]
    # 已有图片则直接返回
    if card.get("image"):
        return {"image": card["image"]}

    # 修复问题1: 统一从环境变量获取
    api_key = os.getenv("BD_API_KEY", "")
    base_url = "https://api.siliconflow.cn/v1"

    # 修复问题6: 优先使用 LLM 生成的 image_prompt
    image_prompt = card.get("image_prompt", "")
    if image_prompt:
        prompt = (
            f"{image_prompt}. "
            "Minimalist artistic illustration, soft pastel colors, "
            "inspirational mood, clean composition, no text, high quality."
        )
    else:
        # 兜底：用金句生成
        quote = card.get("quote", "")
        prompt = (
            f'Inspired by the concept of: "{quote}". '
            "Minimalist artistic illustration, soft pastel colors, "
            "inspirational mood, clean composition, no text, high quality."
        )

    safe_title = re.sub(r"[^\w\u4e00-\u9fa5]", "_", card.get("book_title", "book"))[:30]
    filename = f"poster_{safe_title}_{index}"

    try:
        image_name = generate_image(api_key, base_url, prompt, POSTER_IMG_DIR, filename)
        if image_name:
            card["image"] = image_name
            _poster_cache_update_card(job_id, index, card)  # 修复问题2
        return {"image": image_name}
    except Exception as e:
        raise HTTPException(500, f"配图生成失败：{e}")


@app.post("/api/poster/regenerate/{job_id}/{index}")
async def api_poster_regenerate(job_id: str, index: int):
    """重新生成单张配图（功能补充11）。"""
    from .tools_poster import generate_image

    cards = _poster_cache_get(job_id)
    if not cards:
        raise HTTPException(400, "请先提取金句")
    if index < 0 or index >= len(cards):
        raise HTTPException(400, "卡片索引无效")

    card = cards[index]
    api_key = os.getenv("BD_API_KEY", "")
    base_url = "https://api.siliconflow.cn/v1"

    image_prompt = card.get("image_prompt", "")
    prompt = (
        f"{image_prompt}. "
        "Minimalist artistic illustration, soft pastel colors, "
        "inspirational mood, clean composition, no text, high quality."
    ) if image_prompt else (
        f'Inspired by the concept of: "{card.get("quote", "")}". '
        "Minimalist artistic illustration, soft pastel colors, "
        "inspirational mood, clean composition, no text, high quality."
    )

    safe_title = re.sub(r"[^\w\u4e00-\u9fa5]", "_", card.get("book_title", "book"))[:30]
    filename = f"poster_{safe_title}_{index}_r"

    try:
        image_name = generate_image(api_key, base_url, prompt, POSTER_IMG_DIR, filename)
        if image_name:
            card["image"] = image_name
            _poster_cache_update_card(job_id, index, card)
        return {"image": image_name}
    except Exception as e:
        raise HTTPException(500, f"配图重新生成失败：{e}")


@app.get("/api/poster/files/{filename}")
async def api_poster_file(filename: str):
    """获取海报图片文件。"""
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "非法文件名")
    path = (POSTER_IMG_DIR / filename).resolve()
    if not str(path).startswith(str(POSTER_IMG_DIR.resolve())):
        raise HTTPException(400, "非法路径")
    if not path.exists():
        raise HTTPException(404, "图片不存在")
    return FileResponse(str(path), filename=filename, media_type="image/png")



# ════════════════════════════════════════
# 预设书库 API
# ════════════════════════════════════════
PRESET_BOOKS_DIR = Path(os.getenv("BD_PRESET_DIR", "/workspace/test_books"))


@app.get("/api/preset-books")
async def api_preset_books():
    """列出所有预设测试书籍。"""
    books = []
    if not PRESET_BOOKS_DIR.exists():
        return {"books": []}
    # 扫描目录下所有支持格式
    seen = {}  # title -> {formats: [...]}
    for f in sorted(PRESET_BOOKS_DIR.iterdir()):
        if f.suffix.lower() not in {".txt", ".md", ".pdf", ".epub"}:
            continue
        title = f.stem
        if title not in seen:
            seen[title] = {"title": title, "formats": [], "size": 0}
        size = f.stat().st_size
        # 估算字数（中文约3字节）
        est_chars = size // 3 if f.suffix in {".txt", ".md"} else size // 4
        seen[title]["formats"].append({
            "ext": f.suffix.lower().lstrip("."),
            "filename": f.name,
            "size": size,
            "est_chars": est_chars,
        })
        seen[title]["size"] += size
    books = list(seen.values())
    return {"books": books}


@app.get("/api/preset-books/{filename:path}")
async def api_preset_book_download(filename: str):
    """下载预设书籍文件。"""
    # 安全检查：防止路径穿越
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "非法文件名")
    path = (PRESET_BOOKS_DIR / filename).resolve()
    if not str(path).startswith(str(PRESET_BOOKS_DIR.resolve())):
        raise HTTPException(400, "非法路径")
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "预设书籍不存在")
    media_types = {
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".pdf": "application/pdf",
        ".epub": "application/epub+zip",
    }
    mt = media_types.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(str(path), filename=path.name, media_type=mt)


# ════════════════════════════════════════
# 存档管理 API（持久化知识库）
# ════════════════════════════════════════


@app.get("/api/archives")
async def api_list_archives():
    """列出所有已保存的 .bda.json 存档文件，供知识库页面加载。"""
    archives = []
    if not ARCHIVE_DIR.exists():
        return {"archives": []}
    for f in sorted(ARCHIVE_DIR.glob("*.bda.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text("utf-8"))
            stats = {
                "units": len(data.get("units", [])),
                "clusters": len(data.get("graph", {}).get("clusters", {})),
                "cards": len(data.get("cards", [])),
                "skills": len(data.get("skill_pack", {}).get("skills", [])),
            }
            archives.append({
                "filename": f.name,
                "book_title": data.get("book_title") or f.stem.replace(".bda", ""),
                "created_at": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                "stats": stats,
            })
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Skipping corrupt archive: {f} ({e})")
            continue
    return {"archives": archives}


@app.get("/api/archives/{filename}")
async def api_load_archive(filename: str):
    """加载指定存档文件的完整数据，用于二次查看/导出。"""
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "非法文件名")
    path = (ARCHIVE_DIR / filename).resolve()
    if not str(path).startswith(str(ARCHIVE_DIR.resolve())):
        raise HTTPException(400, "非法路径")
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "存档不存在")
    try:
        from .cache import load_archive
        result = load_archive(path)
        return {
            "book_title": result.book_title,
            "units": [{"id": u.id, "content": u.content[:500], "chapter": u.chapter, "tags": u.tags} for u in result.units[:200]],
            "units_total": len(result.units),
            "graph": {
                "clusters": {k: v for k, v in list(result.graph.clusters.items())[:20]},
                "nodes": [{"id": n.id, "label": n.label, "group": n.group, "size": n.size, "weight": n.weight} for n in result.graph.nodes[:300]],
                "edges": [{"from": e.source, "to": e.target, "weight": e.weight, "relation": e.relation} for e in result.graph.edges[:500]],
            },
            "chapter_summaries": dict(list(result.chapter_summaries.items())[:30]),
            "overall_summary": result.overall_summary[:2000] if result.overall_summary else "",
            "cards": result.cards[:100],
            "skill_pack": result.skill_pack,
            "stats": {
                "units": len(result.units),
                "clusters": len(result.graph.clusters),
                "cards": len(result.cards),
                "skills": len(result.skill_pack.get("skills", [])),
            },
        }
    except Exception as e:
        logger.error(f"Failed to load archive {filename}: {e}")
        raise HTTPException(500, f"加载存档失败: {e}")


@app.delete("/api/archives/{filename}")
async def api_delete_archive(filename: str):
    """删除指定存档文件。"""
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "非法文件名")
    path = (ARCHIVE_DIR / filename).resolve()
    if not str(path).startswith(str(ARCHIVE_DIR.resolve())):
        raise HTTPException(400, "非法路径")
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "存档不存在")
    try:
        path.unlink()
        logger.info(f"Archive deleted: {filename}")
        return {"ok": True}
    except OSError as e:
        raise HTTPException(500, f"删除失败: {e}")


def _parse(s: str) -> set[OutputFormat]:
    if s.strip().lower() == "all":
        return set(OutputFormat)
    result = set()
    for part in s.split(","):
        try:
            result.add(OutputFormat(part.strip().lower()))
        except ValueError:
            pass
    return result or set(OutputFormat)


# ════════════════════════════════════════════════════════════
# 前端 SPA：hash 路由 + 功能页面
# 架构：主页(功能导航) → 各功能独立页面
# 新增功能只需：1)在 FEATURES 数组加一项 2)加一个 <section data-page="xxx">
# ════════════════════════════════════════════════════════════

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#0f0f1a">
<meta name="description" content="拆书 Agent — 把电子书拆解为思维导图、知识图谱、对抗式总结、技能包、可视化卡片。支持 PDF/EPUB/MD/TXT，AI 驱动，SM-2 闪卡复习。">
<meta name="keywords" content="拆书,AI,知识图谱,思维导图,闪卡,SM-2,PDF,EPUB,电子书拆解,读书笔记">
<meta name="author" content="拆书 Agent">
<meta property="og:title" content="拆书 Agent · 集百家之所长">
<meta property="og:description" content="把电子书拆解为思维导图、知识图谱、对抗式总结、技能包、可视化卡片">
<meta property="og:type" content="website">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Crect width='24' height='24' rx='6' fill='%237c3aed'/%3E%3Cpath d='M5 4h10a2 2 0 0 1 2 2v14l-3-2-3 2-3-2-3 2V4z' fill='none' stroke='white' stroke-width='1.5' stroke-linejoin='round'/%3E%3C/svg%3E">
<link rel="apple-touch-icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Crect width='24' height='24' rx='6' fill='%237c3aed'/%3E%3Cpath d='M5 4h10a2 2 0 0 1 2 2v14l-3-2-3 2-3-2-3 2V4z' fill='none' stroke='white' stroke-width='1.5' stroke-linejoin='round'/%3E%3C/svg%3E">
<link rel="manifest" href="/manifest.json">
<title>拆书 Agent · 集百家之所长</title>
<!-- Stage T: JSON-LD 结构化数据 -->
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"WebApplication","name":"拆书 Agent","description":"把电子书拆解为思维导图、知识图谱、对抗式总结、技能包、可视化卡片","applicationCategory":"EducationApplication","operatingSystem":"Web","offers":{"@type":"Offer","price":"0","priceCurrency":"CNY"}}
</script>
<!-- ── Stage AA: 性能优化 — 资源预连接 ── -->
<link rel="preconnect" href="https://cdn.jsdelivr.net" crossorigin>
<link rel="dns-prefetch" href="https://cdn.jsdelivr.net">
<!-- 阶段1：Notyf Toast + SweetAlert2 弹窗 -->
<link href="https://cdn.jsdelivr.net/npm/notyf@3.10.0/notyf.min.css" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/sweetalert2@11/dist/sweetalert2.min.css" rel="stylesheet">
<!-- 阶段2：marked.js + DOMPurify Markdown 渲染 -->
<script src="https://cdn.jsdelivr.net/npm/marked@12/marked.min.js" defer></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3/dist/purify.min.js" defer></script>
<!-- 阶段3：Prism.js 代码高亮 -->
<link href="https://cdn.jsdelivr.net/npm/prismjs@1.29.0/themes/prism-tomorrow.min.css" rel="stylesheet">
<!-- 阶段4：FilePond 文件上传增强 -->
<link href="https://cdn.jsdelivr.net/npm/filepond@4/dist/filepond.min.css" rel="stylesheet">
<!-- 阶段5：Tom Select 下拉增强 + Lucide 图标 -->
<link href="https://cdn.jsdelivr.net/npm/tom-select@2/dist/css/tom-select.bootstrap5.min.css" rel="stylesheet">
<!-- 阶段A：Mermaid.js 流程图 + Chart.js 统计图表 -->
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js" defer></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js" defer></script>
<!-- 阶段E：vis-network 交互式知识图谱 -->
<script src="https://cdn.jsdelivr.net/npm/vis-network@9/standalone/umd/vis-network.min.js" defer></script>
<!-- 阶段G：jsPDF + html2canvas 浏览器端PDF导出 -->
<script src="https://cdn.jsdelivr.net/npm/jspdf@2/dist/jspdf.umd.min.js" defer></script>
<script src="https://cdn.jsdelivr.net/npm/html2canvas@1/dist/html2canvas.min.js" defer></script>
<!-- 阶段H：Fuse.js 模糊搜索 + intro.js 用户引导 -->
<script src="https://cdn.jsdelivr.net/npm/fuse.js@7/dist/fuse.min.js" defer></script>
<link href="https://cdn.jsdelivr.net/npm/intro.js@7/introjs.min.css" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/intro.js@7/intro.min.js" defer></script>
<!-- 阶段I：KaTeX 数学公式 + ECharts 高级可视化 -->
<link href="https://cdn.jsdelivr.net/npm/katex@0.16/dist/katex.min.css" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/katex@0.16/dist/katex.min.js" defer></script>
<script src="https://cdn.jsdelivr.net/npm/katex@0.16/dist/contrib/auto-render.min.js" defer></script>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js" defer></script>
<style>
:root{--bg:#0f0f1a;--card:#252538;--card-elevated:#2a2a40;--input-bg:#16162a;--accent:#7c3aed;--accent2:#ec4899;--accent-light:#a78bfa;--accent-bg:rgba(124,58,237,.12);--text:#e5e7eb;--muted:#a8b0bc;--ok:#34d399;--err:#f87171;--radius:14px;--radius-sm:8px;--radius-lg:18px;--border:#454568;--border-strong:#555580;--shadow-color:rgba(0,0,0,.4);--glow:rgba(124,58,237,.15);--skeleton1:#1e1e2e;--skeleton2:#2a2a3e;--touch-min:44px;--fs-body:14px;--fs-title:16px;--fs-large:20px;--tap-active:scale(.97);--space-xs:4px;--space-sm:8px;--space-md:16px;--space-lg:24px;--space-xl:32px}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{font-size:16px}
body{margin:0;background:var(--bg);color:var(--text);font-family:"Noto Sans CJK SC","PingFang SC","Microsoft YaHei",system-ui,sans-serif;line-height:1.6;font-weight:400;-webkit-font-smoothing:antialiased;overflow-x:hidden}

/* ── 顶部导航栏 ── */
/* ── Stage BB: Skip link 样式（从内联改为 CSS 类） ── */
.skip-link{position:absolute;left:-9999px;top:0;z-index:9999;padding:8px 16px;background:var(--accent);color:#fff;border-radius:0 0 8px 8px;text-decoration:none;font-size:.9rem}
.skip-link--visible{left:0!important}
#main-content:focus{outline:none}

.topbar{position:sticky;top:0;z-index:100;background:rgba(15,15,26,.92);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border-bottom:1px solid var(--border);padding:12px 16px;display:flex;align-items:center;gap:12px}
.topbar .back{width:36px;height:36px;border-radius:10px;background:var(--card);border:1px solid var(--border);color:var(--text);display:flex;align-items:center;justify-content:center;cursor:pointer;font-size:1.2rem;flex-shrink:0;transition:all .15s}
.topbar .back:active{transform:scale(.92);background:var(--accent)}
.topbar .back.hidden{visibility:hidden}
.topbar .logo{font-size:1.15rem;font-weight:700;background:linear-gradient(90deg,var(--accent-light),#f472b6);-webkit-background-clip:text;-webkit-text-fill-color:transparent;white-space:nowrap}

/* ── 页面容器 ── */
.page{display:none;transition:opacity .2s ease}
.page.active{display:block}
.page-loading{opacity:.5;pointer-events:none}
@keyframes fade{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
.wrap{max-width:680px;margin:0 auto;padding:24px 16px calc(72px + env(safe-area-inset-bottom))}

/* ── 主页：功能卡片网格 ── */
.hero{text-align:center;padding:32px 0 8px}
.hero h1{font-size:1.7rem;background:linear-gradient(135deg,var(--accent-light) 0%,var(--accent) 50%,var(--accent2) 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin:0}
.hero p{color:var(--text);opacity:.85;font-size:.92rem;margin-top:6px;font-weight:500;letter-spacing:.01em}
.feat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px;margin-top:28px}
/* 阶段3：≤480px 时保持 2 列（紧凑双列布局，更利于浏览） */
@media(max-width:480px){.feat-grid{grid-template-columns:repeat(2,1fr);gap:10px}}
@media(max-width:768px){.feat-grid{grid-template-columns:repeat(2,1fr)}}
@media(min-width:769px){.feat-grid{grid-template-columns:repeat(3,1fr)}}
@media(max-width:768px){.wrap{padding:0 12px}.stats-grid{grid-template-columns:repeat(2,1fr)!important}.review-controls{grid-template-columns:repeat(2,1fr)!important}.chart-container{height:250px!important}.echarts-container{height:250px!important}.graph-container{height:300px!important}}
.feat-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px;cursor:pointer;transition:all .2s;position:relative;overflow:hidden;min-height:var(--touch-min)}
.feat-card:active{transform:scale(.97)}
.feat-card:hover{border-color:var(--accent-light,var(--accent));background:rgba(124,58,237,.06);box-shadow:0 2px 12px var(--glow);transform:translateY(-1px)}
.feat-icon{width:48px;height:48px;border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:1.5rem;margin-bottom:14px;filter:drop-shadow(0 0 8px rgba(124,58,237,.3))}
.feat-card h3{font-size:1.05rem;margin:0 0 6px;color:var(--text)}
/* 阶段3：卡片描述字号 .82rem → .88rem，颜色加深为 var(--text) 0.75 透明度 */
/* 阶段2：描述行数统一为3行，避免视觉参差 */
.feat-card p{font-size:.86rem;color:var(--text);opacity:.72;margin:0;line-height:1.5;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;min-height:3.96rem}
.feat-card .badge{position:absolute;top:14px;right:14px;font-size:.68rem;padding:2px 8px;border-radius:var(--radius-sm);background:var(--accent-bg);color:var(--accent-light);border:1px solid rgba(124,58,237,.3)}
.feat-card .badge.soon{background:rgba(156,163,175,.15);color:var(--muted);border-color:rgba(156,163,175,.3)}
.feat-card .link{color:var(--accent);font-weight:600;transition:opacity .15s}
.feat-card:hover .link{opacity:.8}
/* 阶段9：主操作卡片（AI 拆书）视觉突出 */
.feat-card-primary{border-color:rgba(124,58,237,.4);background:linear-gradient(135deg,rgba(124,58,237,.08),var(--card) 60%)}
.feat-card-primary .feat-icon{box-shadow:0 0 20px rgba(124,58,237,.15)}
@media(max-width:768px){
  .feat-card-primary{border-color:rgba(124,58,237,.35)}
  .feat-card-primary .feat-icon{box-shadow:0 0 16px rgba(124,58,237,.12)}
}

/* 阶段3：底部渐变遮罩，提示可滚动（移动端固定底部淡出） */
.scroll-fade-hint{display:none}
@media(max-width:768px){
  .scroll-fade-hint{display:block;position:fixed;bottom:calc(64px + env(safe-area-inset-bottom));left:0;right:0;height:56px;pointer-events:none;z-index:195;background:linear-gradient(to top,var(--bg) 0%,color-mix(in srgb,var(--bg) 60%,transparent) 50%,transparent 100%);display:flex;align-items:flex-end;justify-content:center;padding-bottom:6px}
  .scroll-fade-hint span{font-size:.72rem;color:var(--muted);opacity:.8;animation:scrollHintBounce 1.8s ease-in-out infinite}
  @keyframes scrollHintBounce{0%,100%{transform:translateY(0);opacity:.5}50%{transform:translateY(-4px);opacity:.9}}
  /* 首页内容不超长时（视口够高）隐藏提示 */
  @media(min-height:740px){.scroll-fade-hint{display:none}}
}

/* ── 通用表单组件 ── */
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:20px 16px;margin:16px 0;box-shadow:0 0 0 1px var(--border-strong),0 4px 20px var(--glow)}
label{display:block;margin:8px 0 4px;font-size:.9rem;color:var(--muted);font-weight:600}
a{color:var(--accent-light,#a78bfa)}
input,select,textarea{width:100%;padding:10px 14px;min-height:44px;margin-bottom:8px;background:var(--input-bg);border:1px solid var(--border);border-radius:var(--radius-sm);color:var(--text);font-size:16px;font-family:inherit;-webkit-appearance:none;appearance:none}
select{background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath fill='%23a8b0bc' d='M6 8L0 0h12z'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 14px center;padding-right:36px}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 2px rgba(124,58,237,.2)}
input::placeholder,textarea::placeholder{color:#9ca3af}
/* P2修复：暗色模式下提亮placeholder对比度 */
#theme-light:disabled ~ * input::placeholder,#theme-light:disabled ~ * textarea::placeholder{color:#9ca3af}
/* ── 键盘焦点可见样式 (Stage W) ── */
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}
button:focus-visible,.feat-card:focus-visible,.preset-book-card:focus-visible,.collapsible-header:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
a:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
input:focus-visible,select:focus-visible,textarea:focus-visible{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(124,58,237,.15)}
[role="button"]:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
@media(max-width:520px){.row{grid-template-columns:1fr}}
@media(max-width:480px){.row{grid-template-columns:1fr!important}}
.hint{font-size:.75rem;color:#9ca3af;margin-top:4px}
/* P0修复：tooltip触摸区扩大至44px */
.tooltip-trigger{cursor:help;display:inline-flex;align-items:center;padding:16px;margin:-16px;position:relative;min-width:44px;min-height:44px;justify-content:center}
.section-title{font-size:1.1rem;font-weight:700;margin:0 0 4px;color:var(--text)}
.section-desc{font-size:.82rem;color:var(--muted);margin:0 0 12px}

/* ── 模型预设标签 ── */
.model-presets{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.preset{background:var(--input-bg);border:1px solid var(--border);color:var(--text);opacity:.7;padding:5px 12px;border-radius:20px;font-size:.78rem;cursor:pointer;transition:all .15s}
.preset:active{transform:scale(.95)}
.preset.active{border-color:var(--accent);background:rgba(124,58,237,.15);opacity:1;color:var(--accent-light)}

/* ── 折叠面板 ── */
.collapsible{overflow:hidden;transition:max-height .3s ease}
.collapsible.collapsed{max-height:0!important}
.collapsible-header{display:flex;justify-content:space-between;align-items:center;cursor:pointer;user-select:none;padding:12px 16px;min-height:var(--touch-min)}
.collapsible-header .arrow{transition:transform .3s;color:var(--muted);font-size:.8rem}
.collapsible-header .arrow.open{transform:rotate(90deg)}

/* ── 按钮 ── */
button{width:100%;padding:16px;background:linear-gradient(90deg,var(--accent),var(--accent2));border:1px solid var(--border);border-radius:var(--radius-sm);color:#fff;font-size:1.05rem;font-weight:700;cursor:pointer;margin-top:20px;transition:opacity .2s;letter-spacing:1px}
button:hover{opacity:.9}
button:active{transform:scale(.98)}
button[type="button"]:active,button[type="submit"]:active,.tool-btn:active,.primary:active,.secondary:active{transform:var(--tap-active)}
/* 阶段5：触摸反馈增强 - 所有可点击卡片 */
.feat-card:active{transform:var(--tap-active);transition:transform .1s}
.card[onclick]:active,.card[style*="cursor:pointer"]:active{transform:var(--tap-active)}
button:disabled{opacity:.4;cursor:wait}
button.secondary{background:var(--card);border:1px solid var(--border);color:var(--text);font-weight:600}

/* ── 拖拽上传区 ── */
.drop{border:2px dashed var(--border-strong);border-radius:12px;padding:32px 16px;text-align:center;color:var(--muted);cursor:pointer;transition:all .2s;min-height:100px;display:flex;align-items:center;justify-content:center}
.drop:active{background:rgba(124,58,237,.05)}
.drop.hover{border-color:var(--accent);color:var(--text);background:rgba(124,58,237,.05)}
.drop.has{border-color:var(--ok);color:var(--ok)}
.drop-text{font-size:.92rem;line-height:1.6}

/* ── 进度条 ── */
.progress{margin:18px 0}
.bar{height:8px;background:var(--input-bg);border-radius:4px;overflow:hidden;box-shadow:inset 0 1px 2px rgba(0,0,0,.08)}
.bar>div{height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));transition:width .4s;width:0;border-radius:3px;position:relative;overflow:hidden}
/* 阶段6：进度条流光动画 */
.bar>div::after{content:'';position:absolute;top:0;left:0;right:0;bottom:0;background:linear-gradient(90deg,transparent,rgba(255,255,255,.2),transparent);animation:barShimmer 1.5s linear infinite}
@keyframes barShimmer{from{transform:translateX(-100%)}to{transform:translateX(100%)}}
/* 阶段4：通用加载动画 */
@keyframes graphSpin{from{transform:rotate(0)}to{transform:rotate(360deg)}}
.skeleton-card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px;overflow:hidden;position:relative}
.skeleton-card::after{content:'';position:absolute;top:0;left:0;right:0;bottom:0;background:linear-gradient(90deg,transparent,rgba(124,58,237,.06),transparent);animation:barShimmer 1.5s linear infinite}
.skeleton-line{background:linear-gradient(90deg,var(--skeleton1),var(--skeleton2),var(--skeleton1));background-size:200% 100%;animation:skeleton-shimmer 1.5s infinite;border-radius:4px}
.skeleton-line:last-child{width:60%}
.stage{color:var(--muted);font-size:.85rem;margin-top:8px;text-align:center}

/* ── 结果区 ── */
.result{display:none}
.result.show{display:block}
.out-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-top:12px}
.out{background:var(--input-bg);border:1px solid var(--border);border-radius:10px;padding:12px;transition:border-color .2s}
.out:active{border-color:var(--accent)}
.out a{color:var(--accent-light);text-decoration:none;font-weight:600;font-size:.88rem}
.out .kind{font-size:.75rem;color:var(--muted);margin-top:4px}
.out small{color:#9ca3af}
/* P2-16：统计标签去除按钮外观，改为纯信息展示 */
.stat{display:inline-block;padding:4px 8px;margin:4px 4px 4px 0;font-size:.85rem;color:var(--muted)}
.stat b{color:var(--accent);font-size:1rem}
/* P1修复：可点击统计标签添加视觉指示 */
.stat-clickable{cursor:pointer;transition:all .15s;border:1px solid transparent;border-radius:8px}
.stat-clickable:hover,.stat-clickable:active{border-color:var(--accent);background:rgba(124,58,237,.06)}
.stat-clickable::after{content:'→';font-size:.7rem;opacity:.5;margin:0 4px}
.report-frame{width:100%;height:500px;border:1px solid var(--border);border-radius:12px;margin-top:12px;background:#fff}
.report-link{display:block;text-align:center;margin-top:8px;color:var(--accent-light);font-size:.88rem}
.err{color:var(--err);background:#2a1517;border:1px solid #5b2226;border-radius:10px;padding:14px;margin-top:12px;font-size:.9rem}
.err-row{display:flex;align-items:flex-start;gap:8px;flex-wrap:wrap}
.err-msg{flex:1;min-width:200px;line-height:1.5}
.err .secondary{font-size:.85rem!important;padding:8px 16px!important;margin-top:8px}

/* ── 工具箱 ── */
.toolbox{margin-top:28px;padding-top:20px;border-top:1px solid var(--border)}
.tool-row{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px}
@media(max-width:480px){.tool-row{grid-template-columns:1fr}}
.tool-btn{font-size:.9rem!important;padding:12px!important;margin-top:0!important}
.qa-box{margin-top:20px;background:var(--input-bg);border:1px solid var(--border);border-radius:12px;padding:16px}
.qa-input-row{display:flex;gap:8px}
.qa-input-row input{flex:1}
.qa-answer{margin-top:14px;padding:14px;background:var(--card);border-radius:10px;border:1px solid var(--border);font-size:.92rem;line-height:1.7}
.qa-answer.loading{color:var(--muted);font-style:italic}
.qa-answer.error{color:var(--err)}
.qa-answer h1,.qa-answer h2,.qa-answer h3{color:var(--accent-light);margin:12px 0 6px}
.qa-answer h1{font-size:1.15rem}.qa-answer h2{font-size:1.05rem}.qa-answer h3{font-size:1rem}
.qa-answer p{margin:8px 0}
.qa-answer ul,.qa-answer ol{margin:8px 0;padding-left:20px}
.qa-answer li{margin:4px 0}
.qa-answer code{background:#0d0d1a;padding:2px 6px;border-radius:4px;font-family:'Fira Code',Consolas,monospace;font-size:.85rem;color:#f472b6}
.qa-answer pre{background:#0d0d1a!important;border:1px solid var(--border);border-radius:var(--radius-sm);padding:12px;overflow-x:auto;margin:10px 0}
.qa-answer pre code{background:none!important;padding:0;color:var(--text);font-family:'Fira Code',Consolas,monospace;font-size:.85rem}
.qa-answer pre[class*="language-"]{background:#0d0d1a!important}
/* Prism 令牌颜色覆盖（适配暗色主题） */
.qa-answer .token.comment,.qa-answer .token.prolog,.qa-answer .token.doctype,.qa-answer .token.cdata{color:#9ca3af}
.qa-answer .token.punctuation{color:#9ca3af}
.qa-answer .token.property,.qa-answer .token.tag,.qa-answer .token.constant,.qa-answer .token.symbol,.qa-answer .token.deleted{color:#f472b6}
.qa-answer .token.boolean,.qa-answer .token.number{color:#fbbf24}
.qa-answer .token.selector,.qa-answer .token.attr-name,.qa-answer .token.string,.qa-answer .token.char,.qa-answer .token.builtin,.qa-answer .token.inserted{color:#34d399}
.qa-answer .token.operator,.qa-answer .token.entity,.qa-answer .token.url{color:var(--accent-light)}
.qa-answer .token.atrule,.qa-answer .token.attr-value,.qa-answer .token.keyword{color:var(--accent-light)}
.qa-answer .token.function,.qa-answer .token.class-name{color:#60a5fa}
.qa-answer .token.regex,.qa-answer .token.important,.qa-answer .token.variable{color:#fbbf24}
.qa-answer blockquote{border-left:3px solid var(--accent);margin:8px 0;padding:4px 12px;color:var(--muted)}
.qa-answer a{color:var(--accent-light);text-decoration:underline}
.qa-answer strong{color:#f9fafb}
.qa-answer table{border-collapse:collapse;width:100%;margin:10px 0;display:block;overflow-x:auto;-webkit-overflow-scrolling:touch}
.qa-answer th,.qa-answer td{border:1px solid var(--border);padding:6px 10px;text-align:left}
.qa-answer th{background:var(--input-bg);color:var(--accent-light)}
.qa-sources{margin-top:8px;font-size:.78rem;color:var(--muted)}
.qa-sources .src{display:inline-block;background:var(--input-bg);padding:2px 8px;border-radius:8px;margin:2px;border:1px solid var(--border);color:var(--accent-light);font-family:monospace}
.tts-status{margin-top:14px;padding:14px;border-radius:10px;text-align:center;font-size:.9rem}
.tts-status.loading{background:rgba(124,58,237,.1);border:1px solid var(--accent);color:var(--accent-light)}
.tts-status.done{background:rgba(52,211,153,.08);border:1px solid rgba(52,211,153,.3);color:var(--ok)}
.tts-status a{color:var(--accent-light);font-weight:600}

/* 知识海报样式 */
.poster-btn{background:linear-gradient(135deg,rgba(124,58,237,.15),rgba(236,72,153,.15))!important;border-color:rgba(124,58,237,.4)!important;color:var(--accent-light)!important;font-weight:600!important}
.poster-btn:hover{background:linear-gradient(135deg,rgba(124,58,237,.25),rgba(236,72,153,.25))!important}
.poster-overlay{position:fixed;inset:0;background:rgba(0,0,0,.7);backdrop-filter:blur(6px);z-index:9000;display:flex;align-items:flex-start;justify-content:center;padding:20px;overflow-y:auto}
.poster-modal{background:var(--bg);border:1px solid var(--border);border-radius:18px;max-width:1100px;width:100%;margin:auto;padding:24px;box-shadow:0 24px 60px rgba(0,0,0,.5)}
.poster-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
.poster-header h3{font-size:1.3rem;background:linear-gradient(90deg,var(--accent),var(--accent2));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.poster-close{background:none;border:none;font-size:28px;color:var(--muted);cursor:pointer;padding:0 8px;line-height:1;transition:color .2s;width:44px;height:44px;display:flex;align-items:center;justify-content:center;border-radius:8px}
.poster-close:hover{color:var(--err);background:rgba(248,113,113,.1)}
.poster-desc{color:var(--muted);font-size:.88rem;margin-bottom:20px}
.poster-footer{margin-top:16px;padding-top:12px;border-top:1px solid var(--border);text-align:center}
.poster-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:18px}
@media(max-width:640px){.poster-grid{grid-template-columns:1fr}}
.poster-card{background:var(--card);border:1px solid var(--border);border-radius:16px;overflow:hidden;transition:transform .3s,box-shadow .3s;display:flex;flex-direction:column}
.poster-card:hover{transform:translateY(-4px);box-shadow:0 12px 32px rgba(124,58,237,.2)}
.poster-card-img{width:100%;aspect-ratio:1;object-fit:cover;display:block;background:var(--input-bg)}
.poster-card-placeholder{width:100%;aspect-ratio:1;display:flex;align-items:center;justify-content:center;background:linear-gradient(135deg,var(--card),var(--input-bg));position:relative;overflow:hidden}
.poster-card-placeholder::after{content:'';position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(124,58,237,.06),transparent);background-size:200% 100%;animation:skeleton-shimmer 1.5s infinite}
.poster-card-placeholder .pc-icon{font-size:32px;color:var(--muted);opacity:.4;z-index:1}
.poster-card-placeholder .pc-loading-text{display:block;font-size:.78rem;color:var(--muted);margin-top:8px;z-index:1}
.poster-card-body{padding:18px 16px;flex:1;display:flex;flex-direction:column}
.poster-card-quote{font-size:1rem;line-height:1.6;font-weight:600;color:var(--text);margin-bottom:10px}
/* P0修复：移除伪元素引号(移动端裁切为C/D伪影)，改为内联引号 */
.poster-card-quote::before,.poster-card-quote::after{content:none}
.poster-quote-mark{color:var(--accent);font-size:1.3rem;font-weight:700;margin:0 1px}
.poster-card-interp{font-size:.82rem;color:var(--muted);line-height:1.5;margin-bottom:10px;flex:1}
.poster-card-meta{display:flex;align-items:center;justify-content:space-between;font-size:.75rem}
.poster-card-chapter{color:var(--muted);background:var(--input-bg);padding:3px 10px;border-radius:20px}
.poster-card-book{color:var(--accent);font-weight:500}
.poster-card-actions{display:flex;gap:8px;margin-top:10px;padding-top:10px;border-top:1px solid var(--border)}
.pc-action{background:none;border:1px solid var(--border);color:var(--muted);cursor:pointer;font-size:14px;padding:4px 10px;border-radius:8px;transition:all .2s;text-decoration:none;display:inline-flex;align-items:center}
.pc-action:hover{color:var(--accent);border-color:var(--accent)}
.poster-footer{text-align:center;margin-top:20px;padding:14px;font-size:.85rem;color:var(--muted);background:var(--input-bg);border-radius:10px}
.poster-loading-spinner{width:48px;height:48px;border:4px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin 1s linear infinite}
.poster-loading-text{color:var(--text);font-size:1rem}
.poster-progress{font-size:.85rem;color:var(--muted)}
@keyframes spin{to{transform:rotate(360deg)}}
/* 阶段8：海报引号在移动端缩小，避免大号引号形成"D"形伪影 */
@media(max-width:768px){
  .poster-card-quote::before,.poster-card-quote::after{font-size:1.2rem;vertical-align:-3px}
  .poster-card-body{padding:14px 12px}
  .poster-card-quote{font-size:.92rem;line-height:1.5}
  .poster-card-interp{font-size:.78rem}
  .poster-card-meta{font-size:.7rem}
  .poster-card-chapter{padding:2px 8px;font-size:.68rem}
}
h2{font-size:1.15rem;margin:24px 0 12px}
h3{font-size:1rem;margin:20px 0 8px;color:var(--accent-light)}

/* ── 主题切换按钮 ── */
.theme-toggle{width:36px;height:36px;min-height:var(--touch-min);min-width:var(--touch-min);border-radius:10px;background:rgba(124,58,237,.1);border:1px solid rgba(124,58,237,.3);color:var(--text);display:flex;align-items:center;justify-content:center;cursor:pointer;flex-shrink:0;transition:all .15s}
.theme-toggle:active{transform:scale(.92);background:var(--accent)}
.theme-toggle svg{width:20px;height:20px}

/* ── 搜索框 ── */
.search-box{position:relative;margin:16px 0}
.search-box input{padding-left:38px}
.search-box .search-icon{position:absolute;left:12px;top:50%;transform:translateY(-50%);color:var(--muted);width:18px;height:18px}
.search-results{margin-top:8px}
.search-result-item{padding:10px 14px;border-radius:8px;cursor:pointer;transition:all .15s;margin-bottom:4px}
.search-result-item:hover{background:rgba(124,58,237,.08)}
.search-result-item mark{background:rgba(251,191,36,.3);color:inherit;padding:1px 3px;border-radius:3px}

/* ── 统计仪表盘 ── */
.stats-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:12px;margin:16px 0}
.stat-card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px;text-align:center}
.stat-card .num{font-size:1.8rem;font-weight:700;background:linear-gradient(90deg,var(--accent-light),#f472b6);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.stat-card .label{font-size:.78rem;color:var(--muted);margin-top:4px}
.chart-container{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px;margin:12px 0;position:relative;height:280px}

/* ── 导出按钮组 ── */
.export-row{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px;margin-top:10px}

/* ── 键盘快捷键提示 ── */
.kbd-hint{position:fixed;bottom:calc(72px + env(safe-area-inset-bottom) + 8px);right:16px;background:var(--card);border:1px solid var(--border);border-radius:10px;padding:8px 12px;font-size:.75rem;color:var(--muted);z-index:50;display:flex;gap:12px;align-items:center}
.kbd-hint kbd{background:var(--input-bg);border:1px solid var(--border);border-radius:4px;padding:1px 6px;font-family:monospace;font-size:.7rem;color:var(--text)}
.kbd-hint .back-hint-mobile{display:none}
@media(max-width:480px){.kbd-hint{display:none}}
/* 阶段2：手机端（<=768px）整体隐藏 kbd-hint，移动端不展示键盘提示 */
@media(max-width:768px){.kbd-hint{display:none!important}}

/* ── 阶段2：底部 Tab 导航栏（移动端固定底部，毛玻璃效果） ── */
.bottom-tabs{display:none}
@media(max-width:768px){
  .bottom-tabs{display:flex;position:fixed;bottom:0;left:0;right:0;z-index:200;background:rgba(15,15,26,.85);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border-top:1px solid var(--border);box-shadow:0 -2px 12px var(--shadow-color,rgba(0,0,0,.15));padding:6px max(6px,env(safe-area-inset-left)) max(6px,env(safe-area-inset-bottom)) max(6px,env(safe-area-inset-right));justify-content:space-around;align-items:stretch}
  .btab{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;min-height:var(--touch-min);padding:6px 4px;color:var(--muted);text-decoration:none;font-size:.7rem;cursor:pointer;border-radius:10px;transition:color .15s,background .15s,transform .15s;-webkit-tap-highlight-color:transparent;opacity:.65}
  .btab i,.btab svg{width:22px;height:22px;stroke-width:2}
  .btab span{font-size:.65rem;line-height:1;letter-spacing:.02em}
  .btab.active{color:var(--accent);background:var(--accent-bg);opacity:1}
  .btab.active i,.btab.active svg{stroke-width:2.5;transform:scale(1.1);transition:transform .15s}
  .btab:active{background:rgba(124,58,237,.18);transform:scale(.92)}
  /* 悬浮元素上移避开底部 Tab 栏 */
  .qa-floating-bar{bottom:calc(64px + env(safe-area-inset-bottom))!important}
}

/* ── 阶段5：拆书结果页移动端 Tab 切换 + 问答悬浮栏 ── */
.result-tabs{display:none}
.rsect{display:block}
.qa-floating-bar{display:none}
@media(max-width:768px){
  /* 结果区 Tab 栏 */
  .result-tabs{display:flex;gap:6px;margin:16px 0 12px;background:var(--input-bg);border:1px solid var(--border);border-radius:12px;padding:5px}
  .result-tabs .rtab{flex:1;padding:10px 8px;font-size:.85rem;font-weight:600;background:transparent;border:none;color:var(--muted);border-radius:9px;cursor:pointer;margin:0;min-height:var(--touch-min);transition:all .15s}
  .result-tabs .rtab.active{background:var(--card);color:var(--accent);box-shadow:0 1px 3px rgba(0,0,0,.2),0 0 0 1px rgba(124,58,237,.3)}
  .result-tabs .rtab:active{transform:scale(.97)}
  /* 仅显示当前 active 的 rsect，其余隐藏 */
  .rsect{display:none}
  .rsect.active{display:block}
  /* 桌面端内联问答输入框在移动端隐藏（改用悬浮栏） */
  .qa-input-inline{display:none}
  /* 底部固定问答悬浮栏 */
  .qa-floating-bar{display:flex;position:fixed;left:0;right:0;bottom:calc(64px + env(safe-area-inset-bottom));z-index:180;gap:8px;padding:8px 12px;background:rgba(15,15,26,.92);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border-top:1px solid var(--border)}
  .qa-floating-bar input{flex:1;margin:0}
  .qa-floating-bar button{width:auto;flex-shrink:0;padding:12px 20px;margin:0;font-size:.9rem}
  /* 结果区底部留白避免被悬浮栏遮挡（P0修复：增大到150px） */
  .result.show{padding-bottom:calc(150px + env(safe-area-inset-bottom))}
}

/* ── 交互式知识图谱 (vis-network) ── */
.graph-container{background:var(--card);border:1px solid var(--border);border-radius:12px;margin:12px 0;height:400px;overflow:hidden;position:relative}
#vis-network{width:100%;height:100%}
/* 阶段9：图谱手势提示覆盖层 */
.graph-gesture-hint{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);background:rgba(0,0,0,.65);color:#fff;padding:8px 16px;border-radius:20px;font-size:.78rem;pointer-events:none;z-index:5;animation:hintPulse 2s ease-in-out infinite;transition:opacity .5s}
.graph-gesture-hint.hidden{opacity:0;pointer-events:none}
@keyframes hintPulse{0%,100%{opacity:.7}50%{opacity:1}}
@media(min-width:769px){.graph-gesture-hint{display:none}}
.graph-controls{position:absolute;top:8px;right:8px;display:flex;gap:6px;z-index:10}
.graph-controls button{width:auto;padding:8px 12px;font-size:.8rem;margin:0;background:var(--card);border:1px solid var(--border);border-radius:8px;color:var(--text);cursor:pointer;min-height:var(--touch-min);min-width:var(--touch-min)}
.graph-controls button:active{transform:scale(.92)}

/* ── 阶段6：图谱图例 + 搜索筛选区域 ── */
.graph-toolbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:10px}
.graph-search{flex:1;min-width:160px}
.graph-search input{padding:9px 12px;font-size:16px;margin-bottom:0}
.legend-toggle{display:none;background:var(--input-bg);border:1px solid var(--border);color:var(--text);padding:9px 14px;border-radius:8px;font-size:.85rem;cursor:pointer;min-height:var(--touch-min);transition:background .15s}
.legend-toggle:active{transform:var(--tap-active)}
.graph-legend{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.graph-legend .legend-item{display:inline-flex;align-items:center;gap:5px;font-size:.75rem;color:var(--muted);background:var(--input-bg);padding:4px 9px;border-radius:8px;border:1px solid var(--border)}
.graph-legend .legend-dot{width:10px;height:10px;border-radius:50%;display:inline-block}
@media(max-width:768px){
  /* 阶段6：移动端控制按钮改为顶部水平排列，避免遮挡节点 */
  .graph-controls{position:fixed;top:calc(56px + env(safe-area-inset-top));left:8px;right:8px;flex-direction:row;flex-wrap:wrap;gap:4px;z-index:50}
  .graph-controls button{padding:6px 10px;font-size:.78rem;min-height:var(--touch-min)}
  /* 图例区域紧凑化 */
  .graph-toolbar{flex-direction:column;align-items:stretch;gap:8px;margin-bottom:8px}
  .graph-legend .legend-item{font-size:.7rem;padding:3px 7px}
  /* 阶段9：移动端图例默认折叠，点击展开 */
  .legend-toggle{display:block;width:100%;text-align:left}
  .graph-legend{max-height:60px;overflow-y:auto;font-size:.72rem;transition:max-height .3s ease}
  .graph-legend.collapsed{max-height:0;overflow:hidden;margin-top:0!important}
  /* 阶段6：底部留白减少 */
  #page-graph .wrap{padding-bottom:calc(72px + env(safe-area-inset-bottom))}
  .graph-container{margin-bottom:8px}
}

/* ── 闪卡复习系统 ── */
.flashcard-container{perspective:1000px;margin:20px auto;max-width:500px;min-height:300px}
.flashcard{width:100%;height:280px;position:relative;transition:transform .6s;transform-style:preserve-3d;cursor:pointer}
.flashcard.flipped{transform:rotateY(180deg)}
.flashcard-side{position:absolute;width:100%;height:100%;backface-visibility:hidden;-webkit-backface-visibility:hidden;border-radius:16px;border:1px solid var(--border);background:var(--card);display:flex;flex-direction:column;align-items:flex-start;justify-content:flex-start;padding:24px;box-shadow:0 4px 24px rgba(0,0,0,.2);overflow-y:auto;-webkit-overflow-scrolling:touch}
.flashcard-back{transform:rotateY(180deg)}
.flashcard-front{background:linear-gradient(135deg,rgba(124,58,237,.06),rgba(236,72,153,.06))}
.flashcard-content{text-align:center;font-size:1rem;line-height:1.6;color:var(--text);margin:auto 0;width:100%;padding-bottom:30px}
.flashcard-hint{position:absolute;bottom:10px;left:50%;transform:translateX(-50%);font-size:.72rem;color:var(--muted);animation:hintPulse 2s ease-in-out infinite;pointer-events:none;z-index:2}
@keyframes hintPulse{0%,100%{opacity:.8}50%{opacity:1}}
.review-controls{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:20px}
.review-controls button{padding:10px 16px;font-size:.85rem;margin:0;border-radius:10px;min-height:var(--touch-min)}
.review-progress{display:flex;align-items:center;gap:12px;margin:16px 0}
.review-progress .bar{flex:1;background:rgba(124,58,237,.12)}
.review-progress .bar>div{box-shadow:0 0 8px rgba(124,58,237,.3)}
.review-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:16px 0}
.review-stat{text-align:center;padding:12px;background:var(--card);border:1px solid var(--border);border-radius:10px}
.review-stat .num{font-size:1.4rem;font-weight:700;color:var(--accent)}
.review-stat .label{font-size:.75rem;color:var(--muted)}

/* ── 阶段8：设置页移动端优化 ── */
.input-with-toggle{position:relative;display:flex;align-items:center}
.input-with-toggle input{width:100%;padding-right:46px}
.input-with-toggle input.has-copy-btn{padding-right:calc(46px + 44px)}
.pwd-toggle{position:absolute;right:6px;top:50%;transform:translateY(-50%);width:var(--touch-min);height:var(--touch-min);display:flex;align-items:center;justify-content:center;background:transparent;border:none;cursor:pointer;color:var(--muted);font-size:1.1rem;padding:0;border-radius:8px;z-index:2}
.pwd-toggle:nth-of-type(2){right:calc(6px + 44px)}
.pwd-toggle:active{transform:translateY(-50%) scale(.92)}
.pwd-toggle:hover{color:var(--text)}
select{width:100%;padding:12px;background:var(--input-bg);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:.9rem;cursor:pointer;-webkit-appearance:none;appearance:none;background-image:url("data:image/svg+xml;charset=US-ASCII,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20width%3D%2212%22%20height%3D%2212%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22%239ca3af%22%20stroke-width%3D%222%22%3E%3Cpolyline%20points%3D%226%209%2012%2015%2018%209%22%2F%3E%3C%2Fsvg%3E");background-repeat:no-repeat;background-position:right 14px center;padding-right:38px;min-height:var(--touch-min)}
select option{background:var(--card);color:var(--text)}
/* 滚动指示条 */
.scroll-indicator{position:fixed;top:0;left:0;right:0;height:3px;background:transparent;z-index:300;pointer-events:none}
.scroll-indicator-bar{height:100%;width:0;background:linear-gradient(90deg,var(--accent),var(--accent2));transition:width .1s}
/* 保存设置栏：桌面端普通块，移动端固定底部 */
.settings-save-bar{margin-top:24px;position:sticky;bottom:calc(64px + env(safe-area-inset-bottom) + 16px);z-index:50}
.settings-save-bar #save-settings-btn{width:100%;padding:10px 24px;font-size:1rem;font-weight:600;min-height:var(--touch-min);transition:all .15s;-webkit-tap-highlight-color:transparent}
.settings-save-bar #save-settings-btn:active{transform:scale(.97)}
.settings-save-bar #save-settings-btn[disabled]{opacity:.7;cursor:wait}
.settings-save-bar #save-settings-btn[style*="ok"]{box-shadow:0 4px 16px rgba(52,211,153,.3)}
@media(max-width:768px){
  .settings-save-bar{position:fixed;left:0;right:0;bottom:calc(64px + env(safe-area-inset-bottom));z-index:160;padding:12px 16px;background:rgba(15,15,26,.96);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border-top:1px solid var(--border);margin:0;box-shadow:0 -4px 20px rgba(0,0,0,.2)}
  .settings-save-bar #save-settings-btn{margin:0}
  /* 设置页底部留白避免被保存栏遮挡（增大至80px确保末尾表单可见） */
  #page-settings .wrap{padding-bottom:calc(80px + env(safe-area-inset-bottom) + 80px)}
  .scroll-indicator{display:block}
}
@media(min-width:769px){.scroll-indicator{display:none}}

/* ── ECharts 容器 ── */
.echarts-container{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px;margin:12px 0;height:320px}

/* ── 阶段7：闪卡复习移动端优化（底部弹出评分 + 滑动手势提示） ── */
@media(max-width:768px){
  /* 评分按钮作为底部弹出面板（固定底部，毛玻璃，避开底部 Tab 栏） */
  #fc-controls{position:fixed;left:0;right:0;bottom:calc(64px + env(safe-area-inset-bottom));z-index:170;display:flex!important;gap:8px;padding:12px;background:rgba(15,15,26,.94);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border-top:1px solid var(--border);transform:translateY(120%);opacity:0;transition:transform .28s ease,opacity .28s ease;pointer-events:none}
  #fc-controls.show{transform:translateY(0);opacity:1;pointer-events:auto}
  #fc-controls button{flex:1;margin:0}
  /* 闪卡复习页底部留白避免被评分面板遮挡 */
  #page-flashcards .wrap{padding-bottom:calc(80px + env(safe-area-inset-bottom) + 64px)}
  /* P1-5：闪卡进度条移动端加粗至6px，增强视觉反馈 */
  .review-progress .bar{height:6px;border-radius:3px}
  .review-progress .bar>div{border-radius:3px}
  /* 滑动手势提示文字 */
  .flashcard-hint{font-size:.7rem}
}

/* ── intro.js 暗色主题适配 + 阶段1全面修复 ── */
.introjs-tooltip{background-color:var(--card)!important;color:var(--text)!important;border:1px solid var(--border)!important;box-shadow:0 8px 32px rgba(0,0,0,.4)!important;border-radius:14px!important;min-width:min(300px,calc(100vw - 48px))!important;animation:introFadeIn .3s ease-out!important}
@media(max-width:768px){
  .introjs-tooltip{max-width:calc(100vw - 32px)!important;min-width:auto!important}
  .introjs-tooltipReferenceLayer{padding:0 8px!important}
  .introjs-button{padding:10px 18px!important;min-height:44px!important;font-size:15px!important}
  .introjs-skipbutton{font-size:14px!important;padding:8px 14px!important}
  .introjs-tooltipcontent{padding:10px 16px!important;font-size:15px!important}
  .introjs-bullets{padding:6px 16px!important}
}
/* 亮色模式覆盖 */
#theme-light:not(:disabled) ~ * .introjs-tooltip,
html[data-theme="light"] .introjs-tooltip{background-color:#ffffff!important;color:#1f2937!important;border:1px solid #e5e7eb!important}
@keyframes introFadeIn{from{opacity:0;transform:scale(.92)}to{opacity:1;transform:scale(1)}}
.introjs-tooltip-header{padding:10px 16px 0!important;display:flex!important;justify-content:space-between!important;align-items:center!important}
.introjs-tooltip-title{font-size:.8rem!important;color:var(--muted)!important;font-weight:600!important}
.introjs-tooltipcontent{padding:8px 16px!important;font-size:16px!important;line-height:1.6!important;color:#e5e7eb!important}
.introjs-tooltipcontent p{margin:0!important;font-size:16px!important;line-height:1.6!important}
#theme-light:not(:disabled) ~ * .introjs-tooltipcontent,
html[data-theme="light"] .introjs-tooltipcontent{color:#1f2937!important}
.introjs-bullets{padding:4px 16px!important}
.introjs-bullets ul li a{width:8px!important;height:8px!important;background:var(--border-strong)!important;border-radius:50%!important;transition:all .2s!important}
.introjs-bullets ul li a.active{background:var(--accent)!important;width:24px!important;border-radius:4px!important}
.introjs-progressbar{background-color:var(--accent)!important}
.introjs-progress{background:var(--input-bg)!important;height:4px!important;border-radius:2px!important}
.introjs-button{color:var(--text)!important;background:var(--card)!important;border:1px solid var(--border)!important;border-radius:8px!important;font-size:14px!important;padding:8px 16px!important;min-height:44px!important;cursor:pointer!important;transition:all .15s!important;text-shadow:none!important;-webkit-tap-highlight-color:transparent}
#theme-light:not(:disabled) ~ * .introjs-button,
html[data-theme="light"] .introjs-button{color:#1f2937!important;background:#fff!important;border:1px solid #d1d5db!important}
.introjs-button:hover{background:var(--accent)!important;color:#fff!important;border-color:var(--accent)!important}
.introjs-button:active{transform:scale(.95)!important;background:rgba(124,58,237,.15)!important}
.introjs-button:focus{box-shadow:0 0 0 2px rgba(124,58,237,.3)!important;outline:none!important}
.introjs-skipbutton{color:var(--muted)!important;font-size:14px!important;font-weight:700!important;padding:8px 12px!important;min-height:44px!important;min-width:44px!important;border-radius:8px!important;opacity:.8!important;transition:all .15s!important;float:none!important;position:static!important;background:rgba(248,113,113,.08)!important}
.introjs-skipbutton:hover{color:var(--err)!important;opacity:1!important;background:rgba(248,113,113,.1)!important}
.introjs-skipbutton:active{transform:scale(.92)!important}
.introjs-nextbutton{background:var(--accent)!important;color:#fff!important;border-color:var(--accent)!important;font-weight:600!important}
.introjs-nextbutton:hover{background:#6d28d9!important;color:#fff!important}
.introjs-prevbutton{margin-right:8px!important}
.introjs-tooltipReferenceLayer{font-family:inherit!important}
.introjs-arrow.top{border-bottom-color:var(--card)!important}
.introjs-arrow.bottom{border-top-color:var(--card)!important}
.introjs-overlay{background:rgba(0,0,0,.5)!important;backdrop-filter:blur(2px)!important;-webkit-backdrop-filter:blur(2px)!important;animation:overlayFadeIn .3s ease!important}
@keyframes overlayFadeIn{from{opacity:0}to{opacity:1}}
.introjs-helperLayer{box-shadow:0 0 0 4px rgba(124,58,237,.4),0 0 20px rgba(124,58,237,.2)!important;border-radius:8px!important}
/* SweetAlert2 暗色适配增强 */
.swal2-popup{background:var(--card)!important;color:var(--text)!important;border:1px solid var(--border)!important;border-radius:14px!important;font-family:inherit!important;animation:swalFadeIn .3s ease-out!important}
@keyframes swalFadeIn{from{opacity:0;transform:scale(.92)}to{opacity:1;transform:scale(1)}}
.swal2-title{color:var(--text)!important;font-size:1.2rem!important}
.swal2-html-container{color:var(--muted)!important;font-size:16px!important;line-height:1.5!important}
.swal2-confirm{background:var(--accent)!important;border-radius:10px!important;font-weight:600!important;min-height:44px!important;padding:10px 24px!important;transition:all .15s!important}
.swal2-confirm:hover{background:#6d28d9!important}
.swal2-confirm:active{transform:scale(.95)!important}
.swal2-cancel{background:var(--input-bg)!important;color:var(--text)!important;border:1px solid var(--border-strong)!important;border-radius:10px!important;min-height:44px!important;padding:10px 24px!important}
.swal2-cancel:active{transform:scale(.95)!important}
.swal2-backdrop-show{background:rgba(0,0,0,.5)!important;backdrop-filter:blur(2px)!important}
/* P1: 统一弹层层级 SweetAlert(10000) > Toast(1000) > intro.js(999) > 底部Tab(200) > 返回顶部(195) > 悬浮问答栏(180) */
.notyf{z-index:1000!important}
.introjs-overlay,.introjs-helperLayer,.introjs-tooltipReferenceLayer,.introjs-tooltip{z-index:999!important}
.swal2-container{z-index:10000!important}

/* ── vis-network 暗色适配 ── */
.vis-network{outline:none!important}
.vis-tooltip{background:var(--card)!important;color:var(--text)!important;border:1px solid var(--border)!important;border-radius:8px!important}

/* ── 预设书库 ── */
.preset-book-card{background:var(--input-bg);border:1px solid var(--border);border-radius:12px;padding:14px;transition:all .2s;cursor:pointer}
.preset-book-card:active{transform:scale(.98)}
.preset-book-card:hover{border-color:var(--accent);background:rgba(124,58,237,.06)}
.preset-book-card .pb-title{font-weight:700;font-size:.92rem;color:var(--text);margin-bottom:6px}
.preset-book-card .pb-info{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.preset-book-card .pb-format{font-size:.72rem;padding:3px 8px;border-radius:6px;border:1px solid var(--border);color:var(--muted);background:var(--card);cursor:pointer;transition:all .15s}
.preset-book-card .pb-format:hover{border-color:var(--accent);color:var(--accent-light);background:rgba(124,58,237,.1)}
.preset-book-card .pb-format.active{border-color:var(--accent);color:var(--accent-light);background:rgba(124,58,237,.15)}
.preset-book-card .pb-chars{font-size:.72rem;color:var(--muted)}
.preset-book-card.selected{border-color:var(--accent);background:rgba(124,58,237,.08);box-shadow:0 0 0 2px rgba(124,58,237,.2)}
.preset-book-card .pb-selected-tag{font-size:.7rem;color:var(--ok);margin-top:6px;font-weight:600;display:flex;align-items:center;gap:4px}

/* ── 移动端全面优化 (手机优先) ── */
@media(max-width:768px){
  /* 阶段1：触控热区最小 44px（所有可点击元素） */
  button,.feat-card,.preset,.preset-book-card,.tool-btn,.out,.search-result-item,.pc-action,.poster-close,.collapsible-header,#backBtn,.theme-toggle,#onboardingBtn,.review-controls button,.graph-controls button,.pb-format,.stat-clickable,.lib-item,.flashcard,.graph-legend-item,.qa-tab,.swal2-styled,.filepond--button{min-height:var(--touch-min);min-width:var(--touch-min)}
  /* P1-7：密码切换/复制按钮移动端增加可视触摸区背景 */
  .pwd-toggle{background:var(--input-bg);font-size:1.25rem}
  input[type="checkbox"],input[type="radio"]{min-width:var(--touch-min);min-height:var(--touch-min)}
  /* 阶段1：字体体系（最小正文 14px / 标题 16px / 大标题 20px） */
  body{font-size:var(--fs-body)}
  .section-title{font-size:var(--fs-title)!important}
  .feat-card h3{font-size:var(--fs-title)}
  h2{font-size:var(--fs-large)!important}
  .hero h2{font-size:var(--fs-large)!important}
  .hero p{font-size:var(--fs-body)}
  /* 阶段1：按钮 pressed 反馈态 */
  button:active,.feat-card:active{transform:var(--tap-active)}
  /* 阶段1：顶部导航栏图标 padding 扩展热区到 44px */
  .topbar .back,.theme-toggle{padding:10px;min-width:var(--touch-min);min-height:var(--touch-min);display:flex;align-items:center;justify-content:center}

  /* 安全区适配（刘海屏/底部横条） */
  .topbar{padding-top:max(12px,env(safe-area-inset-top));padding-left:max(16px,env(safe-area-inset-left));padding-right:max(16px,env(safe-area-inset-right))}
  .wrap{padding-bottom:max(80px,calc(80px + env(safe-area-inset-bottom)))}

  /* 顶部导航栏紧凑化 */
  .topbar{padding:8px 12px;padding-top:max(12px,env(safe-area-inset-top));padding-left:max(16px,env(safe-area-inset-left));padding-right:max(16px,env(safe-area-inset-right));gap:8px}
  .topbar .back{width:40px;height:40px;border-radius:10px}
  .topbar .logo{font-size:1rem}
  .theme-toggle{width:40px;height:40px}
  .theme-toggle svg{width:18px;height:18px}

  /* 主页 Hero 缩小 */
  .hero{padding:32px 16px 20px}
  .hero h2{font-size:1.5rem!important}
  .hero p{font-size:.82rem;margin-bottom:6px}

  /* 功能卡片紧凑化 */
  .feat-card{padding:12px 14px}
  .feat-icon{width:40px;height:40px;font-size:1.25rem;margin-bottom:10px}
  /* P2-10：NEW标签移动端统一定位 */
  .feat-card .badge{top:10px;right:10px}
  .feat-card h3{font-size:.95rem}
  .feat-card p{font-size:.88rem;opacity:.75}
  .feat-grid{gap:10px;margin-top:12px}

  /* 表单卡片减少内边距 */
  .card{padding:16px 12px;margin:12px 0}
  label{margin:8px 0 4px;font-size:.85rem}
  input,select,textarea{padding:11px 12px;font-size:16px;border-radius:10px}
  .section-title{font-size:1.02rem}
  .section-desc{font-size:.8rem}

  /* 按钮触摸优化 */
  button{padding:14px 16px;font-size:1rem;border-radius:10px;margin-top:16px}
  button.secondary{padding:12px 14px}

  /* P0-3：拆书提交按钮移动端 sticky 定位，始终可见 */
  #page-digest #btn{position:sticky;bottom:calc(72px + env(safe-area-inset-bottom));z-index:150;box-shadow:0 -2px 12px rgba(0,0,0,.15);border-radius:12px;margin-bottom:8px}

  /* 预设书库卡片紧凑化 */
  .preset-book-card{padding:12px}
  .preset-book-card .pb-title{font-size:.88rem}
  .preset-book-card .pb-format{font-size:.7rem;padding:10px 14px;display:inline-flex;align-items:center;justify-content:center}
  .preset-book-card .pb-chars{font-size:.7rem}

  /* 拖拽区缩小 */
  .drop{padding:24px 12px;min-height:80px}
  .drop-text{font-size:.85rem}

  /* 结果输出网格 */
  .out-grid{grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px}
  .out{padding:10px}
  .out a{font-size:.82rem}

  /* 统计标签 */
  .stat{padding:6px 10px;font-size:.8rem;margin:2px}
  .stat b{font-size:1rem}
  .stat-clickable{display:inline-flex;align-items:center;justify-content:center;padding:10px 14px;margin:2px}

  /* 问答区优化 */
  .qa-box{padding:12px}
  .qa-input-row{flex-direction:column;gap:8px}
  .qa-input-row button{margin-top:0;font-size:.9rem;padding:12px}
  .qa-answer{padding:12px;font-size:.9rem;line-height:1.65}
  .qa-answer h1{font-size:1.05rem}.qa-answer h2{font-size:1rem}.qa-answer h3{font-size:.95rem}
  .qa-answer pre{padding:10px;font-size:.8rem}
  .qa-answer ul,.qa-answer ol{padding-left:16px}

  /* 图表容器高度调整 */
  .chart-container{height:220px!important;padding:12px}
  .echarts-container{height:220px!important;padding:12px}
  .graph-container{min-height:320px!important}

  /* 报告 iframe 缩小 */
  .report-frame{height:400px}

  /* 导出按钮紧凑化 */
  .export-row{grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:6px}
  .tool-btn{font-size:.82rem!important;padding:10px!important}
  .tool-row{gap:8px}

  /* 闪卡优化 */
  .flashcard{height:240px}
  .flashcard-side{padding:18px}
  .flashcard-content{font-size:.92rem;padding:20px}
  .flashcard-container{max-width:100%}
  /* P1-6：闪卡边缘预览暗示可滑动 */
  .flashcard-container::after{content:'';position:absolute;left:8px;right:8px;bottom:-6px;height:12px;background:var(--card);border:1px solid var(--border);border-radius:0 0 12px 12px;z-index:-1;opacity:.5}

  /* 阶段7：闪卡评分按钮改为 3 列底部弹出面板（生疏/一般/熟练） */
  .review-controls{grid-template-columns:repeat(4,1fr)!important;gap:6px}
  .review-controls button{padding:14px 8px;font-size:.88rem;min-height:var(--touch-min)}
  .review-stats{gap:8px}
  .review-stat{padding:10px}
  .review-stat .num{font-size:1.2rem}
  .review-stat .label{font-size:.7rem}

  /* 统计卡片 */
  .stats-grid{grid-template-columns:repeat(2,1fr)!important;gap:8px}
  .stat-card{padding:12px}
  .stat-card .num{font-size:1.4rem}
  .stat-card .label{font-size:.72rem}

  /* 搜索框 */
  .search-box input{padding-left:36px}
  .search-result-item{padding:10px 12px;font-size:.88rem}

  /* 知识图谱控制按钮 */
  .graph-controls button{padding:6px 10px;font-size:.78rem;min-height:var(--touch-min)}

  /* h2/h3 标题缩小 */
  h2{font-size:1.05rem;margin:18px 0 10px}
  h3{font-size:.95rem;margin:16px 0 6px}

  /* 模型预设标签 */
  .model-presets{gap:5px}
  .preset{padding:5px 10px;font-size:.75rem}
}

/* ── 阶段4：拆书输入页移动优化 ── */
@media(max-width:768px){
  /* 表单双列 .row 在手机端强制单列（覆盖 max-width:520px 阈值，统一 768px 以下单列） */
  .row{grid-template-columns:1fr!important}
  /* 隐藏 FilePond 的"拖放文件或"文字，仅保留"点击选择文件" */
  .filepond--drop-label .fp-drop-text{display:none}
  /* 模型预设改为 2×2 网格 */
  .model-presets{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}
  .model-presets .preset{padding:12px 10px;font-size:.82rem;text-align:center;border-radius:12px;display:flex;align-items:center;justify-content:center;min-height:var(--touch-min)}
  .model-presets .preset.active{background:rgba(124,58,237,.18);border-color:var(--accent)}
  /* P0修复：拆书提交按钮移动端粘性定位，确保始终可见且不被悬浮栏遮挡 */
  #btn{position:sticky!important;bottom:calc(64px + env(safe-area-inset-bottom) + 80px)!important;z-index:190!important;box-shadow:0 -2px 12px rgba(0,0,0,.15)!important;border-radius:12px!important}
}
@media(max-width:380px){
  .model-presets{display:flex;flex-wrap:wrap;gap:6px}
  .model-presets .preset{padding:10px 8px;font-size:.78rem;flex:1 1 auto}
}

@media(max-width:380px){
  /* 超小屏（iPhone SE 等） */
  .hero h2{font-size:1.3rem!important}
  .feat-grid{grid-template-columns:1fr!important;gap:8px}
  .feat-card{padding:14px 12px}
  .feat-icon{width:36px;height:36px;font-size:1.1rem;margin-bottom:8px}
  .feat-card h3{font-size:.9rem}
  .feat-card p{font-size:.85rem;opacity:.75}
  .card{padding:14px 10px}
  .topbar{padding:6px 10px;gap:6px}
  .topbar .logo{font-size:.92rem}
  .topbar .back{width:36px;height:36px}
  .theme-toggle{width:36px;height:36px}
  .flashcard{height:200px}
  .review-controls{gap:6px}
  .review-controls button{padding:12px 6px;font-size:.82rem}
  .stats-grid{gap:6px}
  .stat-card{padding:10px}
  .stat-card .num{font-size:1.2rem}
  .graph-container{height:240px!important}
  .chart-container{height:180px!important}
  .echarts-container{height:180px!important}
  .report-frame{height:350px}
  button{padding:13px 14px;font-size:.95rem}
  .preset{font-size:.7rem;padding:4px 8px}
  .qa-answer{font-size:.85rem}
  .qa-answer pre{font-size:.75rem}
}

/* ── 触摸交互优化 ── */
@media(hover:none) and (pointer:coarse){
  /* 触摸设备：移除 hover 效果，增大触摸区域 */
  .feat-card:hover{border-color:var(--border);background:var(--card);transform:none}
  .preset-book-card:hover{border-color:var(--border);background:var(--input-bg)}
  .preset:hover{border-color:var(--border)}
  .search-result-item:hover{background:transparent}
  .out:hover{border-color:var(--border)}
  /* 触摸反馈用 active 替代 hover */
  .feat-card:active{transform:scale(.97);border-color:var(--accent)}
  .preset-book-card:active{transform:scale(.98);border-color:var(--accent)}
  /* #026/#034：触摸设备隐藏 Esc 键盘提示（无物理键盘，提示无意义） */
  .kbd-hint{display:none!important}
}

/* ── P0修复：全局焦点状态设计（无障碍）── */
*:focus-visible{outline:2px solid var(--accent)!important;outline-offset:2px!important;border-radius:4px}
button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:2px solid var(--accent)!important;outline-offset:2px!important}
.btab:focus-visible,.feat-card:focus-visible,.preset:focus-visible{outline:2px solid var(--accent)!important;outline-offset:-2px!important}
/* ── 阶段CC: 动画无障碍（prefers-reduced-motion）── */
@media (prefers-reduced-motion: reduce){
  *{animation-duration:0.01ms!important;animation-iteration-count:1!important;transition-duration:0.01ms!important;scroll-behavior:auto!important}
  .page{animation:none!important}
  .skeleton-line,.skeleton-card{animation:none!important;background:var(--skeleton1)!important}
  .flashcard{transition:none!important}
  .bar>div{transition:none!important}
  .collapsible{transition:none!important}
  .collapsible-header .arrow{transition:none!important}
  .feat-card,.preset-book-card,.preset,.search-result-item,.out{transition:none!important}
}

/* ── 阶段CC: 打印样式（@media print）── */
@media print{
  :root{--bg:#fff!important;--card:#fff!important;--text:#000!important;--muted:#555!important;--border:#ccc!important;--accent:#7c3aed!important;--accent2:#ec4899!important}
  body{background:#fff!important;color:#000!important;font-size:12pt!important;line-height:1.5!important}
  .topbar,.skip-link,#onboardingBtn,.theme-toggle,.progress,.err,#btn,.drop,.filepond--root,.model-presets,.collapsible-header,.graph-controls,.flashcard-hint,#fc-controls,.review-progress,#offline-banner,.kbd-hint,.footer,#preset-header,.bottom-tabs{display:none!important}
  .page:not(.active){display:none!important}
  .page.active{display:block!important;animation:none!important}
  .collapsible{max-height:none!important;overflow:visible!important}
  .collapsible[aria-hidden="true"]{display:block!important}
  .card{box-shadow:none!important;border:1px solid #ccc!important;break-inside:avoid;page-break-inside:avoid}
  .wrap{max-width:100%!important;padding:0!important}
  .report-frame{height:auto!important;overflow:visible!important}
  .chart-container,.echarts-container,.graph-container{height:300px!important;page-break-inside:avoid}
  a[href]:after{content:" (" attr(href) ")";font-size:.8em;color:#555}
  a[href^="#"]:after,a[href^="javascript:"]:after,a[href^="/api"]:after{content:""}
  *{-webkit-print-color-adjust:exact!important;print-color-adjust:exact!important}
  h1,h2,h3{page-break-after:avoid}
  pre,blockquote,table{page-break-inside:avoid}
}

/* ── 阶段EE: 输入验证样式 ── */
.input-invalid{border-color:var(--err)!important;box-shadow:0 0 0 3px rgba(248,113,113,.12)!important}
.input-valid{border-color:var(--ok)!important}
.input-hint{font-size:.75rem;margin-top:4px;min-height:1em;transition:opacity .15s}
.input-hint.error{color:var(--err)}
.input-hint.ok{color:var(--ok)}
.input-hint.warn{color:#fbbf24}
.file-size-info{font-size:.75rem;color:var(--muted);margin-top:4px}
.file-size-info.too-large{color:var(--err)}

/* ── 阶段FF: CDN 加载失败提示 ── */
.cdn-error-banner{position:fixed;bottom:0;left:0;right:0;background:rgba(248,113,113,.95);color:#fff;padding:10px 16px;font-size:.85rem;z-index:9998;display:none;align-items:center;gap:8px;backdrop-filter:blur(8px)}
.cdn-error-banner.show{display:flex}
.cdn-error-banner .cdn-close{margin-left:auto;cursor:pointer;background:rgba(255,255,255,.2);border:none;color:#fff;padding:4px 10px;border-radius:6px;font-size:.8rem;flex-shrink:0}
.cdn-error-banner .cdn-close:hover{background:rgba(255,255,255,.3)}

/* ── P1 修复：触摸反馈 / 评分动画 / 表单错误 / 设置分组 / 上传热区 ── */
/* #032：暂停/评分按钮激活态 */
.review-controls button.active,#fc-pause-btn.active{background:var(--accent);color:#fff!important;border-color:var(--accent)!important}
/* #033：评分按钮即时视觉反馈 */
.review-controls button{transition:transform .1s,background .15s}
.review-controls button:active{transform:scale(.9)}
.review-controls button.rated{animation:ratedFlash .3s ease}
@keyframes ratedFlash{0%{background:var(--accent);color:#fff}100%{}}
/* #039：设置页分组视觉分隔 */
.settings-section{border-bottom:1px solid var(--border);margin-bottom:16px;padding-bottom:16px}
/* 知识库列表项紧凑间距（Stage 4 #6） */
.card.lib-item{padding:12px 16px}
/* #041：表单校验错误样式 */
.input-error{border-color:var(--err)!important;box-shadow:0 0 0 2px rgba(248,113,113,.2)!important}
.error-msg{color:var(--err);font-size:.78rem;margin-top:4px;display:none}
/* #057：上传区域整体可点击热区 */
.filepond--root,.filepond--drop-label,.filepond--drop-label label{cursor:pointer}
</style>
<!-- 阶段C/R：主题切换支持（必须放在主样式之后，确保CSS层叠优先级正确） -->
<style id="theme-light" disabled>
:root{--bg:#f8f9fa;--card:#ffffff;--input-bg:#f3f4f6;--accent:#7c3aed;--accent2:#ec4899;--accent-light:#7c3aed;--accent-bg:rgba(124,58,237,.1);--text:#1f2937;--muted:#4b5563;--ok:#10b981;--err:#ef4444;--radius:14px;--radius-sm:8px;--radius-lg:18px;--border:#e5e7eb;--border-strong:#d1d5db;--shadow-color:rgba(0,0,0,.15);--skeleton1:#e5e7eb;--skeleton2:#f3f4f6}
body{background:var(--bg);color:var(--text);font-weight:400}
.topbar{background:rgba(248,249,250,.92)!important}
/* Stage W: 浅色主题下加深 hero 渐变文字以保证 WCAG AA 对比度 */
  .hero h2{background:linear-gradient(90deg,#7c3aed,#be185d)!important;-webkit-background-clip:text!important;-webkit-text-fill-color:transparent!important;background-clip:text!important}
/* 浅色主题下加深 hint 文字 */
.hint{color:#6b7280!important}
.out small{color:#6b7280!important}
/* P1: 浅色主题下底部 Tab 未选中态使用中性灰，保证图标(currentColor)与文字对比度 */
.btab{color:#6b7280!important}
/* 浅色主题下 FilePond/TomSelect 文字色适配 */
.filepond--drop-label{color:#4b5563!important}
.ts-dropdown{color:var(--text)!important}
/* 阶段4：浅色主题下修复硬编码暗色背景的元素 */
.bottom-tabs{background:rgba(255,255,255,.92)!important}
.qa-floating-bar{background:rgba(248,249,250,.95)!important}
.settings-save-bar{background:rgba(248,249,250,.97)!important}
#fc-controls{background:rgba(248,249,250,.95)!important}
.scroll-fade-hint{background:linear-gradient(to top,var(--bg) 0%,rgba(248,249,250,.6) 50%,transparent 100%)!important}
.kbd-hint{background:var(--card)!important;color:var(--muted)!important}
.feat-card .badge{color:var(--accent-light)!important;background:var(--accent-bg)!important;border-color:rgba(124,58,237,.3)!important}
.feat-card .badge.soon{color:#6b7280!important}
.feat-icon{filter:none!important}
.theme-toggle{background:rgba(124,58,237,.05);border:1px solid rgba(124,58,237,.15)}
select{background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath fill='%234b5563' d='M6 8L0 0h12z'/%3E%3C/svg%3E")!important}
/* 阶段4：浅色主题下代码块背景适配 */
.qa-answer code{background:#f3f4f6!important;color:#be185d!important}
.qa-answer pre{background:#1e1e2e!important}
.qa-answer pre code{color:#e5e7eb!important}
.qa-answer pre[class*="language-"]{background:#1e1e2e!important}
</style>
</head>
<body>

<!-- 跳至主要内容（无障碍） -->
<a href="#main-content" class="skip-link" onfocus="this.classList.add('skip-link--visible')" onblur="this.classList.remove('skip-link--visible')">跳至主要内容</a>

<!-- 顶部导航栏（Stage S: 语义化为 header landmark） -->
<header class="topbar" role="banner">
  <div class="back hidden" id="backBtn" onclick="navigate('')" role="button" tabindex="0" aria-label="返回主页" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();navigate('')}">&#8592;</div>
  <div class="logo" role="heading" aria-level="1">拆书 Agent</div>
  <div style="flex:1"></div>
  <div class="theme-toggle" id="themeToggle" onclick="toggleTheme()" title="当前：暗色模式 | 点击切换" role="button" tabindex="0" aria-label="当前：暗色模式，点击切换到亮色" onkeydown="if(event.key==='Enter'){event.preventDefault();toggleTheme()}">
    <svg id="theme-icon-dark" xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
    <svg id="theme-icon-light" xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display:none" aria-hidden="true"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>
  </div>
  <div class="theme-toggle" id="onboardingBtn" onclick="startOnboarding()" title="使用引导" style="margin-left:4px" role="button" tabindex="0" aria-label="使用引导" onkeydown="if(event.key==='Enter'){event.preventDefault();startOnboarding()}">
    <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
  </div>
</header>

<!-- Stage T: noscript 回退提示 -->
<noscript>
<div style="padding:24px;text-align:center;color:#e5e7eb;background:#0f0f1a;min-height:100vh;display:flex;align-items:center;justify-content:center;flex-direction:column">
<p style="color:#a78bfa;font-size:1.5rem;font-weight:700;margin:0 0 12px">拆书 Agent</p>
<p style="color:#9ca3af;max-width:400px">本应用需要 JavaScript 才能运行。请在浏览器设置中启用 JavaScript 后刷新页面。</p>
</div>
</noscript>

<!-- ════════ 主内容区（Stage S: 语义化为 main landmark） ════════ -->
<main id="main-content" role="main" tabindex="-1">

<!-- ════════ 主页：功能导航 ════════ -->
<div class="page active" id="page-home">
  <div class="wrap">
    <div class="hero">
      <h2 style="font-size:2.2rem;font-weight:800;background:linear-gradient(135deg,#a78bfa 0%,#7c3aed 50%,#ec4899 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin:0 0 8px">集百家之所长</h2>
      <p>思维导图 · 知识图谱 · 对抗式总结 · 技能包 · 可视化卡片</p>
    </div>
    <div class="feat-grid" id="featGrid"></div>
    <!-- 阶段5：最近拆书活动 -->
    <div id="recent-activity" style="display:none;margin-top:28px">
      <div class="section-title" style="font-size:1rem;margin-bottom:12px">最近拆书</div>
      <div id="recent-list" style="display:flex;flex-direction:column;gap:8px"></div>
    </div>
    <!-- 阶段3：底部渐变遮罩，提示可向下滚动 -->
    <div class="scroll-fade-hint" aria-hidden="true"><span>向下滑动查看更多</span></div>
  </div>
</div>

<!-- ════════ 拆书页 ════════ -->
<div class="page" id="page-digest" aria-hidden="true">
  <div class="wrap">

    <div class="card">
      <div class="section-title">选择书籍</div>
      <p class="section-desc">上传电子书，AI 自动拆解为多种知识形态</p>
      <div class="hint" style="margin-top:6px">PDF · EPUB · MD · TXT · ≤50MB</div>
      <input type="file" id="file" accept=".pdf,.epub,.md,.txt" aria-label="选择电子书文件">

      <!-- 预设书库 -->
      <div class="collapsible-header" id="preset-header" onclick="togglePresetLibrary()" style="margin-top:16px" role="button" tabindex="0" aria-label="展开/收起预设书库" aria-expanded="false" aria-controls="preset-library" onkeydown="if(event.key==='Enter'){event.preventDefault();togglePresetLibrary()}">
        <label style="margin:0;cursor:pointer">📚 预设书库</label>
        <span class="arrow" id="preset-arrow">&#9654;</span>
      </div>
      <div class="collapsible collapsed" id="preset-library" style="max-height:0" aria-hidden="true">
        <div style="padding-top:12px">
          <p class="hint" style="margin-bottom:10px">选择一本预设书籍自动填入，支持 TXT / MD / PDF / EPUB，每本 10 万字+</p>
          <div id="preset-books-list" style="display:grid;gap:10px"></div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="section-title">模型配置</div>
      <p class="section-desc">默认使用 SiliconFlow <span style="font-size:.68rem;padding:1px 6px;border-radius:4px;background:rgba(52,211,153,.12);color:rgba(52,211,153,.9);vertical-align:middle">免费</span></p>
      <div class="model-presets" id="presets">
        <span class="preset active" data-model="Qwen/Qwen3.5-9B" data-url="https://api.siliconflow.cn/v1" title="免费模型 · 速度最快 · 适合快速预览">Qwen3.5-9B <span style="font-size:.68rem;padding:1px 6px;border-radius:4px;background:rgba(52,211,153,.12);color:rgba(52,211,153,.9)">免费</span></span>
        <span class="preset" data-model="deepseek-ai/DeepSeek-V3" data-url="https://api.siliconflow.cn/v1" title="性价比高 · 推理强 · 推荐大多数书籍">DeepSeek-V3</span>
        <span class="preset" data-model="zai-org/GLM-5.2" data-url="https://api.siliconflow.cn/v1" title="中文优化 · 总结精准 · 适合中文书籍">GLM-5.2</span>
        <span class="preset" data-model="gpt-4o-mini" data-url="https://api.openai.com/v1" title="质量最高 · 成本较高 · 需要 OpenAI Key">OpenAI</span>
      </div>
      <div class="row" style="margin-top:8px">
        <div><label for="api_key" style="display:flex;align-items:center;gap:6px">API Key<a href="https://siliconflow.cn" target="_blank" rel="noopener" style="font-size:.78rem;color:var(--accent);font-weight:400">如何获取？</a></label><div style="position:relative"><input id="api_key" type="password" placeholder="sk-..." value="" aria-label="API Key" style="padding-right:44px"><button type="button" class="pwd-toggle" onclick="var i=document.getElementById('api_key');i.type=i.type==='password'?'text':'password';this.textContent=i.type==='password'?'显':'隐'" style="color:var(--text);font-weight:600" aria-label="切换API Key可见性" title="显示/隐藏">显</button></div></div>
        <div><label for="book_type">书籍类型</label><select id="book_type" aria-label="书籍类型"><option value="nonfiction">社科/非虚构</option><option value="fiction">小说/虚构</option><option value="technical">技术/学术</option></select></div>
      </div>
      <!-- 阶段5：模型选择统一为预设按钮，手动输入移入高级设置 -->
      <div class="row">
        <div><label for="model">当前模型 <small style="font-weight:400;color:var(--muted)">（只读）</small></label><input id="model" value="Qwen/Qwen3.5-9B" aria-label="模型名称" readonly title="Qwen/Qwen3.5-9B" style="opacity:.85;cursor:default;font-size:16px;text-overflow:ellipsis;overflow:hidden;white-space:nowrap;background:var(--input-bg);border:1px dashed var(--border)"></div>
      </div>

      <div class="collapsible-header" id="adv-header" onclick="toggleAdvanced()" style="margin-top:16px" role="button" tabindex="0" aria-label="展开/收起高级设置" aria-expanded="false" aria-controls="advanced" onkeydown="if(event.key==='Enter'){event.preventDefault();toggleAdvanced()}">
        <label style="margin:0;cursor:pointer">高级设置</label>
        <span class="arrow" id="adv-arrow">&#9654;</span>
      </div>
      <div class="collapsible collapsed" id="advanced" style="max-height:0" aria-hidden="true">
        <div style="padding-top:12px">
          <label for="base_url">API Base URL</label>
          <input id="base_url" value="https://api.siliconflow.cn/v1" aria-label="API Base URL">
          <label for="custom_model" style="margin-top:8px">自定义模型名称 <small style="font-weight:400;color:var(--muted)">（覆盖上方预设）</small></label>
          <input id="custom_model" placeholder="如：gpt-4o, claude-3-opus 等" aria-label="自定义模型名称" oninput="if(this.value){var m=document.getElementById('model');m.value=this.value;m.title=this.value}">
          <label for="focus" style="margin-top:8px">侧重点（可选）</label>
          <input id="focus" placeholder="如：关注系统设计权衡" aria-label="拆书侧重点">
          <div class="row" style="margin-top:8px">
            <div><label for="chunk_size">分块大小(token) <span class="tooltip-trigger" title="每块文本的 token 数量。推荐 2000-3000">ⓘ</span></label><input id="chunk_size" type="number" value="2500" aria-label="分块大小"></div>
            <div><label for="reviewers">审稿人数 <span class="tooltip-trigger" title="1=快速，2=平衡(推荐)，3-4=高质量">ⓘ</span></label><input id="reviewers" type="number" value="2" min="1" max="4" aria-label="审稿人数"></div>
          </div>
          <label for="max_revisions">最大修订轮次 <span class="tooltip-trigger" title="AI迭代改进次数。0=不修订（最快），2=平衡（推荐），4=最高质量（耗时）">ⓘ</span></label>
          <input id="max_revisions" type="number" value="2" min="0" max="4" aria-label="最大修订轮次">
          <div class="hint">审稿人越多、修订越多，质量越高但耗时和 token 消耗也越大</div>
        </div>
      </div>
    </div>

    <button type="button" id="btn" onclick="submit()" style="min-height:48px;font-size:1rem">开始拆书</button>

    <div class="progress" id="prog" style="display:none">
      <div class="bar"><div id="bar"></div></div>
      <div class="stage" id="stage" role="status" aria-live="polite" aria-atomic="true">排队中…</div>
    </div>

    <div class="err" id="err" style="display:none"></div>

    <div class="result" id="result">
      <!-- 统计仪表盘（合并拆书完成标题，去除重复统计） -->
      <div id="dashboard" style="margin-top:16px">
        <h2 style="color:var(--ok);margin-bottom:12px">拆书完成</h2>
        <div class="stats-grid" id="stats-grid"></div>
        <div class="chart-container"><canvas id="chart-clusters"></canvas></div>
        <div class="chart-container"><canvas id="chart-chapters"></canvas></div>
        <div class="echarts-container" id="echarts-radar-dashboard" style="display:none"></div>
      </div>

      <!-- 全文搜索 -->
      <div id="search-section" style="display:none;margin-top:16px">
        <h3>知识搜索</h3>
        <div class="search-box">
          <svg class="search-icon" xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
          <input id="search-input" placeholder="搜索知识单元…" oninput="debounceSearch(this.value)" aria-label="搜索知识单元">
        </div>
        <div class="search-results" id="search-results"></div>
      </div>

      <!-- 阶段5：移动端结果区 Tab 切换栏（输出文件 / 知识工具箱 / 问答） -->
      <div class="result-tabs" id="resultTabs" role="tablist" aria-label="结果区切换">
        <button type="button" class="rtab active" data-rtab="outputs" role="tab" aria-selected="true" onclick="switchResultTab('outputs')">输出文件</button>
        <button type="button" class="rtab" data-rtab="toolbox" role="tab" aria-selected="false" onclick="switchResultTab('toolbox')">工具箱</button>
        <button type="button" class="rtab" data-rtab="qa" role="tab" aria-selected="false" onclick="switchResultTab('qa')">问答</button>
      </div>

      <div class="rsect active" data-rtab="outputs" role="tabpanel">
      <h3>输出文件</h3>
      <div class="out-grid" id="outs"></div>

      <!-- 扩展导出 -->
      <div class="export-row" id="export-extras" style="display:none">
        <button type="button" class="secondary tool-btn" onclick="exportCSV()">导出 CSV</button>
        <button type="button" class="secondary tool-btn" onclick="exportNotion()">导出 Notion</button>
        <button type="button" class="secondary tool-btn" onclick="exportMermaid()">Mermaid 图表</button>
        <button type="button" class="secondary tool-btn" onclick="exportPDF()">导出 PDF</button>
      </div>

      <h3>报告预览</h3>
      <iframe class="report-frame" id="preview"></iframe>
      <a class="report-link" id="report-full" href="#" target="_blank">在新窗口打开完整报告 &#8594;</a>
      </div><!-- /.rsect outputs -->

      <!-- 工具箱：拆书后的增值功能 -->
      <div class="rsect toolbox" data-rtab="toolbox" id="toolbox" role="tabpanel">
        <h3>知识工具箱</h3>
        <p class="section-desc" style="margin-bottom:16px">基于本次拆书结果的增值功能</p>

        <div class="tool-row">
          <button type="button" class="secondary tool-btn" onclick="exportAnki()">Anki 闪卡</button>
          <button type="button" class="secondary tool-btn" onclick="exportObsidian()">Obsidian 导出</button>
        </div>
        <div class="tool-row">
          <button type="button" class="secondary tool-btn" onclick="generateTTS('summary')">听书(总结)</button>
          <button type="button" class="secondary tool-btn" onclick="generateTTS('full')">听书(完整)</button>
        </div>
        <div class="tool-row">
          <button type="button" id="poster-trigger-btn" class="secondary tool-btn poster-btn" onclick="openPoster()" title="完成拆书后可生成知识海报">知识海报</button>
        </div>

        <div class="tts-status" id="tts-status" style="display:none"></div>
      </div><!-- /.rsect toolbox -->

      <!-- 阶段5：问答独立 Tab 区（桌面端与工具箱并列展示，移动端在 Tab 中切换） -->
      <div class="rsect" data-rtab="qa" id="qa-section" role="tabpanel">
        <div class="qa-box">
          <h3>书籍问答</h3>
          <p class="section-desc">基于拆书结果回答你的问题</p>
          <!-- 桌面端内联输入框（移动端隐藏，改用底部悬浮栏） -->
          <div class="qa-input-row qa-input-inline">
            <input id="qa-question" placeholder="如：这本书讲的核心方法论是什么？" aria-label="提问输入框：输入你想问的关于这本书的问题" onkeydown="if(event.key==='Enter')askQuestion()">
            <button type="button" class="secondary" style="width:auto;padding:12px 20px;margin-top:0;flex-shrink:0" onclick="askQuestion()">问</button>
          </div>
          <div class="qa-answer" id="qa-answer" style="display:none"></div>
          <div class="qa-sources" id="qa-sources" style="display:none"></div>
        </div>
      </div><!-- /.rsect qa -->

      <!-- 阶段5：移动端底部固定问答悬浮栏（桌面端隐藏） -->
      <div class="qa-floating-bar" id="qaFloatingBar" aria-label="快捷提问">
        <input id="qa-question-mobile" placeholder="问一个问题…" aria-label="移动端提问输入框" onkeydown="if(event.key==='Enter')askQuestionMobile()">
        <button type="button" class="secondary" onclick="askQuestionMobile()" aria-label="发送提问">问</button>
      </div>
    </div>
  </div>
</div>

<!-- ════════ 全局设置页 ════════ -->
<div class="page" id="page-settings" aria-hidden="true">
  <div class="wrap">
    <!-- 阶段8：滚动指示条 -->
    <div class="scroll-indicator" aria-hidden="true"><div class="scroll-indicator-bar" id="settings-scroll-bar"></div></div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px;align-items:center"><a href="#settings-api" style="font-size:.8rem;color:var(--accent-light)">API配置</a> · <a href="#settings-model" style="font-size:.8rem;color:var(--accent-light)">模型设置</a> · <a href="#settings-data" style="font-size:.8rem;color:var(--accent-light)">数据管理</a></div>
    <div class="card settings-section" id="settings-api">
      <div class="section-title">API 配置</div>
      <p class="section-desc">设置默认 API Key 和模型，所有功能页面共享</p>
      <label for="set-api-key">默认 API Key</label>
      <div class="input-with-toggle">
        <input id="set-api-key" type="password" class="has-copy-btn" placeholder="sk-..." aria-label="默认 API Key">
        <button type="button" class="pwd-toggle" id="set-api-key-copy" onclick="copyApiKeyToClipboard()" aria-label="复制 API Key" title="复制">📋</button>
        <button type="button" class="pwd-toggle" id="set-api-key-toggle" onclick="togglePwdVisibility('set-api-key', this)" aria-label="显示/隐藏 API Key" title="显示/隐藏" style="color:var(--text);font-weight:600">显</button>
      </div>
      <p style="font-size:.76rem;color:var(--muted);margin:4px 0 0">💡 免费模型无需Key · <a href="https://cloud.siliconflow.cn" target="_blank" style="color:var(--accent-light)">获取 SiliconFlow API Key→</a></p>
      <label for="set-model">默认模型</label>
      <select id="set-model" aria-label="默认模型">
        <option value="Qwen/Qwen3.5-9B">Qwen3.5-9B（免费 · 速度最快）</option>
        <option value="deepseek-ai/DeepSeek-V3">DeepSeek-V3（性价比高 · 推理强）</option>
        <option value="zai-org/GLM-5.2">GLM-5.2（中文优化）</option>
        <option value="gpt-4o-mini">gpt-4o-mini (OpenAI · 质量最高)</option>
        <option value="__custom__">自定义模型…</option>
      </select>
      <input id="set-model-custom" placeholder="自定义模型名称（选择&ldquo;自定义模型&rdquo;后填写）" aria-label="自定义模型名称" style="display:none;margin-top:8px">
      <p style="font-size:.78rem;color:var(--muted);margin:6px 0 0">💡 快捷选择已同步上方下拉框</p>
      <div class="model-presets" id="set-presets">
        <span class="preset active" data-model="Qwen/Qwen3.5-9B" data-url="https://api.siliconflow.cn/v1" title="免费模型 · 速度最快 · 适合快速预览">Qwen3.5-9B <span style="font-size:.68rem;padding:1px 6px;border-radius:4px;background:rgba(52,211,153,.12);color:rgba(52,211,153,.9)">免费</span></span>
        <span class="preset" data-model="deepseek-ai/DeepSeek-V3" data-url="https://api.siliconflow.cn/v1" title="性价比高 · 推理强 · 推荐大多数书籍">DeepSeek-V3</span>
        <span class="preset" data-model="zai-org/GLM-5.2" data-url="https://api.siliconflow.cn/v1" title="中文优化 · 总结精准 · 适合中文书籍">GLM-5.2</span>
        <span class="preset" data-model="gpt-4o-mini" data-url="https://api.openai.com/v1" title="质量最高 · 成本较高 · 需要 OpenAI Key">OpenAI</span>
      </div>
      <!-- 阶段9：Base URL 收入折叠区 -->
      <div class="collapsible-header" onclick="var el=this.nextElementSibling; var arrow=this.querySelector('.arrow'); if(el.style.maxHeight==='0px'||el.style.maxHeight===''){el.style.maxHeight='200px';el.style.paddingTop='12px';arrow.innerHTML='&#9660;';this.setAttribute('aria-expanded','true')}else{el.style.maxHeight='0';el.style.paddingTop='0';arrow.innerHTML='&#9654;';this.setAttribute('aria-expanded','false')}" role="button" tabindex="0" aria-expanded="false" aria-label="展开/收起 Base URL" onkeydown="if(event.key==='Enter'){event.preventDefault();this.click()}" style="margin-top:12px;cursor:pointer">
        <label style="margin:0;cursor:pointer">API Base URL</label>
        <span class="arrow" id="set-base-url-arrow">&#9654;</span>
      </div>
      <div class="collapsible" id="set-base-url-wrap" style="max-height:0;overflow:hidden;transition:max-height .3s" aria-hidden="true">
        <input id="set-base-url" value="https://api.siliconflow.cn/v1" aria-label="默认 Base URL" style="margin-top:0">
      </div>
    </div>

    <div class="card settings-section" id="settings-model">
      <div class="section-title">拆书默认参数</div>
      <p class="section-desc">设置拆书页的默认参数值</p>
      <div class="row">
        <div><label for="set-book-type">默认书籍类型</label>
          <select id="set-book-type" aria-label="默认书籍类型">
            <option value="nonfiction">社科/非虚构</option>
            <option value="fiction">小说/虚构</option>
            <option value="technical">技术/学术</option>
          </select>
        </div>
        <div><label for="set-chunk-size">默认分块大小(token) <span class="tooltip-trigger" title="每块文本的 token 数量。越大越快但可能遗漏细节，越小越精细但耗时更长。推荐 2000-3000">ⓘ</span></label><input id="set-chunk-size" type="number" value="2500" aria-label="默认分块大小"></div>
      </div>
      <div class="row">
        <div><label for="set-reviewers">默认审稿人数 <span class="tooltip-trigger" title="AI 审稿轮数。1=快速，2=平衡(推荐)，3-4=高质量但耗时">ⓘ</span></label><input id="set-reviewers" type="number" value="2" min="1" max="4" aria-label="默认审稿人数"></div>
        <div><label for="set-max-revisions">默认修订轮次 <span class="tooltip-trigger" title="AI迭代改进次数。0=不修订（最快），2=平衡（推荐），4=最高质量（耗时）">ⓘ</span></label><input id="set-max-revisions" type="number" value="2" min="0" max="4" aria-label="默认修订轮次"></div>
      </div>
    </div>

    <div class="card settings-section" id="settings-data">
      <div class="section-title" style="font-size:1.15rem;border-left:3px solid var(--accent);padding-left:10px">数据管理</div>
      <p class="section-desc" style="font-size:.82rem;margin-bottom:14px">管理本地存储的设置和历史数据</p>
      <div class="tool-row" style="gap:12px;margin-bottom:14px">
        <button type="button" class="secondary tool-btn" style="min-height:44px" onclick="exportSettings()">导出配置</button>
        <button type="button" class="secondary tool-btn" style="min-height:44px" onclick="importSettings()">导入配置</button>
      </div>
      <button type="button" class="secondary tool-btn" style="border-color:rgba(248,113,113,.4);color:var(--err);background:rgba(248,113,113,.06);min-height:44px;font-weight:600" onclick="clearAllData()">🗑️ 清除所有本地数据</button>
      <input type="file" id="import-file" accept=".json" hidden aria-label="导入配置文件" aria-hidden="true">
    </div>

    <!-- 阶段8：底部固定保存设置栏 -->
    <div class="settings-save-bar">
      <button type="button" id="save-settings-btn" onclick="saveSettings()">保存设置</button>
    </div>
  </div>
</div>

<!-- ════════ 知识库页 ════════ -->
<div class="page" id="page-library" aria-hidden="true">
  <div class="wrap">
    <div class="card">
      <div class="section-title">拆书历史</div>
      <p class="section-desc" style="color:var(--text);opacity:.7">查看历史拆书记录，重新下载或继续操作</p>
      <!-- 阶段6：知识库搜索 -->
      <div class="search-box" id="lib-search-box" style="display:none">
        <svg class="search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
        <input id="lib-search-input" type="text" placeholder="搜索书名…" oninput="filterLibrary(this.value);var cb=document.getElementById('lib-search-clear');if(cb){cb.style.display=this.value?'flex':'none'}" aria-label="搜索书名" style="padding-right:40px">
        <button type="button" id="lib-search-clear" onclick="var si=document.getElementById('lib-search-input');si.value='';filterLibrary('');this.style.display='none';si.focus()" style="position:absolute;right:8px;top:50%;transform:translateY(-50%);background:none;border:none;cursor:pointer;font-size:1.1rem;color:var(--muted);display:none;min-height:36px;min-width:36px;align-items:center;justify-content:center" aria-label="清除搜索">✕</button>
      </div>
      <div id="lib-list"><div style="text-align:center;padding:48px 16px"><div style="font-size:48px;opacity:.3;margin-bottom:16px">📚</div><p style="color:var(--muted);font-size:.9rem;margin-bottom:16px">暂无拆书记录</p><button type="button" class="secondary" style="width:auto;padding:10px 24px;margin:0 auto" onclick="navigate(&quot;digest&quot;)">去拆书 →</button></div></div>
    </div>
  </div>
</div>

<!-- ════════ 书书对比页 ════════ -->
<div class="page" id="page-compare" aria-hidden="true">
  <div class="wrap">
    <div class="card">
      <div class="section-title">书籍对比</div>
      <p class="section-desc">选择两本已拆解的书籍，对比知识结构异同</p>
      <div class="row">
        <div><label for="cmp-book-a">书籍 A</label><select id="cmp-book-a" aria-label="选择书籍A"><option value="">请选择…</option></select></div>
        <div><label for="cmp-book-b">书籍 B</label><select id="cmp-book-b" aria-label="选择书籍B"><option value="">请选择…</option></select></div>
      </div>
      <!-- 阶段5：交换 A/B 按钮 -->
      <button type="button" id="cmp-swap-btn" class="secondary" style="width:auto;padding:8px 16px;margin:0 auto 8px;display:block;font-size:.85rem;opacity:.4;cursor:not-allowed" onclick="swapCompareBooks()" aria-label="交换书籍A和B" disabled>⇄ 交换 A/B</button>
      <button type="button" id="cmp-btn" onclick="runCompare()" disabled style="opacity:.55;cursor:not-allowed;background:var(--input-bg);color:var(--muted);border:1px dashed var(--border-strong);font-weight:500">开始对比</button>
      <p id="cmp-hint" style="font-size:.8rem;color:var(--muted);text-align:center;margin-top:8px">请先选择两本不同的书籍</p>
    </div>
    <div id="cmp-result" style="display:none"></div>
  </div>
</div>

<!-- ════════ 知识图谱页 ════════ -->
<div class="page" id="page-graph" aria-hidden="true">
  <div class="wrap">
    <div class="card">
      <div class="section-title">交互式知识图谱</div>
      <p class="section-desc">可视化探索知识单元间的关联，支持拖拽、缩放、点击节点查看详情</p>
      <div id="graph-empty" style="text-align:center;padding:48px 16px">
        <div style="font-size:48px;opacity:.3;margin-bottom:16px">🕸️</div>
        <p style="color:var(--text);font-size:1.05rem;font-weight:600;margin-bottom:8px">暂无图谱</p>
        <p style="color:var(--muted);font-size:.9rem;margin-bottom:16px">拆书后可在此查看知识关联</p>
        <button type="button" class="secondary" style="width:auto;padding:10px 24px;margin:0 auto" onclick="navigate(&quot;digest&quot;)">去拆书 →</button>
      </div>
      <div id="graph-wrapper" style="display:none">
        <!-- 阶段6：图例 + 搜索筛选区域 -->
        <div class="graph-toolbar">
          <div class="graph-search">
            <input id="graph-filter" placeholder="筛选节点标签…" aria-label="筛选知识图谱节点" oninput="filterGraphNodes(this.value)">
          </div>
          <button type="button" class="legend-toggle" id="legend-toggle" onclick="toggleGraphLegend()" aria-expanded="true">聚类图例 ▾</button>
          <div class="graph-legend" id="graph-legend" aria-label="聚类图例"></div>
        </div>
        <div class="graph-container">
          <div id="vis-network"></div>
          <div class="graph-gesture-hint" id="graph-gesture-hint">
            <span>双指缩放 · 拖拽移动 · 点击节点查看详情</span>
          </div>
          <div class="graph-controls">
            <button type="button" onclick="graphZoomIn()" title="放大">+</button>
            <button type="button" onclick="graphZoomOut()" title="缩小">−</button>
            <button type="button" onclick="graphFit()" title="适应">⤢</button>
          </div>
        </div>
        <div id="graph-detail" style="display:none;margin-top:12px;padding:14px;background:var(--card);border:1px solid var(--border);border-radius:10px"></div>
        <div class="echarts-container" id="echarts-wordcloud-wrap" style="display:none"></div>
        <div class="echarts-container" id="echarts-radar-wrap" style="display:none"></div>
      </div>
    </div>
  </div>
</div>

<!-- ════════ 闪卡复习页 ════════ -->
<div class="page" id="page-flashcards" aria-hidden="true">
  <div class="wrap">
    <div class="card">
      <div class="section-title" style="display:flex;justify-content:space-between;align-items:center">闪卡复习
        <button type="button" id="fc-pause-btn" class="secondary" style="width:auto;padding:6px 14px;margin:0;font-size:.8rem;display:none" onclick="pauseReview()">⏸️ 暂停</button>
      </div>
      <p class="section-desc">科学记忆 · 间隔复习</p>
      <div id="fc-empty" style="color:var(--muted);text-align:center;padding:40px 0">
        <div style="font-size:48px;opacity:.3;margin-bottom:16px">🎴</div>
        <p style="color:var(--text);font-size:1.05rem;font-weight:600;margin-bottom:8px">暂无闪卡</p>
        <p style="margin-bottom:16px">完成拆书后，闪卡将自动生成</p>
        <button type="button" class="primary" style="width:auto;padding:12px 28px;margin:0 auto;min-height:44px" onclick="navigate('digest')">去拆书 →</button>
      </div>
      <div id="fc-review" style="display:none">
        <div class="review-stats" id="fc-stats"></div>
        <div class="review-progress">
          <span id="fc-progress-text" style="font-size:.85rem;color:var(--muted);white-space:nowrap">第 1 张，共 0 张</span>
          <div class="bar"><div id="fc-bar"></div></div>
        </div>
        <div class="flashcard-container">
          <div class="flashcard" id="fc-card" onclick="flipCard()" role="button" tabindex="0" aria-label="闪卡：点击或上滑翻转，左滑下一张" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();flipCard()}">
            <div class="flashcard-side flashcard-front">
              <div class="flashcard-content" id="fc-front"></div>
              <div class="flashcard-hint"><span style="font-size:1rem;vertical-align:middle">👆</span> 点击翻转 · <span style="font-size:1rem;vertical-align:middle">👉</span> 滑动下一张</div>
            </div>
            <div class="flashcard-side flashcard-back">
              <div class="flashcard-content" id="fc-back"></div>
              <div class="flashcard-hint"><span style="font-size:1rem;vertical-align:middle">⬇️</span> 在下方选择评分</div>
            </div>
          </div>
        </div>
        <!-- P0修复：4级评分按钮（生疏/困难/良好/熟练），符合SM-2标准 -->
        <!-- 阶段5：添加SM-2间隔提示 -->
        <div class="review-controls" id="fc-controls" style="display:none">
          <button type="button" class="secondary fc-rate" data-rate="1" style="background:rgba(248,113,113,.12);border-color:rgba(248,113,113,.3);color:#f87171" onclick="reviewCard(1,this)">生疏<br><small style="font-size:.68rem;opacity:.7">重来</small></button>
          <button type="button" class="secondary fc-rate" data-rate="2" style="background:rgba(251,191,36,.12);border-color:rgba(251,191,36,.3);color:#fbbf24" onclick="reviewCard(2,this)">困难<br><small style="font-size:.68rem;opacity:.7">1天</small></button>
          <button type="button" class="secondary fc-rate" data-rate="3" style="background:rgba(96,165,250,.12);border-color:rgba(96,165,250,.3);color:#60a5fa" onclick="reviewCard(3,this)">良好<br><small style="font-size:.68rem;opacity:.7">6天</small></button>
          <button type="button" class="secondary fc-rate" data-rate="4" style="background:rgba(52,211,153,.12);border-color:rgba(52,211,153,.3);color:#34d399" onclick="reviewCard(4,this)">熟练<br><small style="font-size:.68rem;opacity:.7">更久</small></button>
        </div>
        <!-- 阶段9：撤销提示 toast（P1-9：移至顶部避免与底部导航冲突） -->
        <div id="fc-undo-toast" style="display:none;position:fixed;top:calc(56px + env(safe-area-inset-top));left:50%;transform:translateX(-50%);background:rgba(0,0,0,.8);color:#fff;padding:10px 20px;border-radius:24px;font-size:.85rem;z-index:100;align-items:center;gap:10px;box-shadow:0 4px 20px rgba(0,0,0,.3)">
          <span id="fc-undo-msg">已评分</span>
          <button type="button" onclick="undoReviewCard()" style="background:rgba(124,58,237,.3);border:1px solid rgba(124,58,237,.5);color:#c4b5fd;padding:10px 20px;border-radius:12px;font-size:.85rem;cursor:pointer;min-height:44px;min-width:72px;-webkit-tap-highlight-color:transparent">撤销</button>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- 知识海报模态框 -->
<div class="poster-overlay" id="poster-overlay" style="display:none" aria-hidden="true">
  <div class="poster-modal" role="dialog" aria-label="知识海报">
    <div class="poster-header">
      <h3>知识海报</h3>
      <button type="button" class="poster-close" onclick="closePoster()" aria-label="关闭">&times;</button>
    </div>
    <div class="poster-desc" id="poster-desc">AI 提炼书中金句，配以生成插画，制作知识卡片<span id="poster-scroll-hint" style="display:none;color:#c4b5fd;font-size:.78rem;margin-left:8px">↓ 向下滑动查看更多</span></div>
    <div class="poster-grid" id="poster-grid"></div>
    <div class="poster-footer" id="poster-footer" style="display:none"></div>
  </div>
</div>

<!-- 阶段2：底部 Tab 导航（移动端固定底部，毛玻璃效果） -->
<nav class="bottom-tabs" id="bottomTabs" role="navigation" aria-label="底部主导航">
  <a class="btab" data-route="home" onclick="navigate('')" role="button" tabindex="0" aria-label="首页" onkeydown="if(event.key==='Enter'){event.preventDefault();navigate('')}">
    <i data-lucide="home"></i><span>首页</span>
  </a>
  <a class="btab" data-route="library" onclick="navigate('library')" role="button" tabindex="0" aria-label="知识库" onkeydown="if(event.key==='Enter'){event.preventDefault();navigate('library')}">
    <i data-lucide="library"></i><span>知识库</span>
  </a>
  <a class="btab" data-route="graph" onclick="navigate('graph')" role="button" tabindex="0" aria-label="知识图谱" onkeydown="if(event.key==='Enter'){event.preventDefault();navigate('graph')}">
    <i data-lucide="git-fork"></i><span>图谱</span>
  </a>
  <a class="btab" data-route="flashcards" onclick="navigate('flashcards')" role="button" tabindex="0" aria-label="闪卡复习" onkeydown="if(event.key==='Enter'){event.preventDefault();navigate('flashcards')}">
    <i data-lucide="layers"></i><span>闪卡</span>
  </a>
  <a class="btab" data-route="settings" onclick="navigate('settings')" role="button" tabindex="0" aria-label="设置" onkeydown="if(event.key==='Enter'){event.preventDefault();navigate('settings')}">
    <i data-lucide="settings"></i><span>设置</span>
  </a>
</nav>

<!-- #100：返回顶部按钮 -->
<button id="back-to-top" onclick="window.scrollTo({top:0,behavior:'smooth'})" style="position:fixed;bottom:calc(72px + env(safe-area-inset-bottom));right:16px;width:44px;height:44px;border-radius:50%;background:var(--card);border:1px solid var(--border);box-shadow:0 2px 12px var(--shadow-color,rgba(0,0,0,.2));cursor:pointer;z-index:195;display:none;align-items:center;justify-content:center;font-size:1.2rem;color:var(--accent)" aria-label="返回顶部">↑</button>

</main>

<!-- 底部信息（Stage S: 语义化为 footer landmark） -->
<footer class="footer" role="contentinfo" style="text-align:center;padding:16px;color:var(--muted);font-size:.8rem;border-top:1px solid var(--border);margin-top:auto">
  <span style="font-size:.78rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:inline-block;max-width:100%">拆书 Agent · v0.2.0</span>
</footer>

<script>
// ════════════════════════════════════════
// 功能注册表 —— 新增功能只需在此加一项
// ════════════════════════════════════════
const FEATURES = [
  {
    id: 'digest',
    title: 'AI 拆书',
    desc: '上传电子书，AI自动拆解',
    icon: 'book-open',
    iconBg: 'rgba(124,58,237,.15)',
    iconColor: '#a78bfa',
    badge: null,
    primary: true,
  },
  {
    id: 'library',
    title: '知识库',
    desc: '回顾历史拆书，二次导出',
    icon: 'library',
    iconBg: 'rgba(6,182,212,.12)',
    iconColor: '#06b6d4',
    badge: null,
  },
  {
    id: 'compare',
    title: '书书对比',
    desc: '对比两本书的知识结构',
    icon: 'scale',
    iconBg: 'rgba(6,182,212,.12)',
    iconColor: '#06b6d4',
    badge: null,
  },
  {
    id: 'graph',
    title: '知识图谱',
    desc: '可视化探索概念关联',
    icon: 'share-2',
    iconBg: 'rgba(236,72,153,.12)',
    iconColor: 'var(--accent2)',
    badge: 'NEW',
  },
  {
    id: 'flashcards',
    title: '闪卡复习',
    desc: '科学记忆 · 间隔复习',
    icon: 'layers',
    iconBg: 'rgba(124,58,237,.12)',
    iconColor: 'var(--accent)',
    badge: 'NEW',
  },
  {
    id: 'settings',
    title: '全局设置',
    desc: 'API Key、模型、输出偏好',
    icon: 'settings',
    iconBg: 'rgba(124,58,237,.12)',
    iconColor: 'var(--accent)',
    badge: null,
  },
];

// ════════════════════════════════════════
// 路由系统：hash 路由切换页面
// ════════════════════════════════════════
function navigate(hash){
  location.hash = hash;
}
// ── 阶段DD: 页面导航资源清理 ──
var _activeAbortController = null;
function cleanupPageResources(){
  // 销毁 Chart.js 实例
  if(typeof chartClusters !== 'undefined' && chartClusters){ try{ chartClusters.destroy(); }catch(e){} chartClusters = null; }
  if(typeof chartChapters !== 'undefined' && chartChapters){ try{ chartChapters.destroy(); }catch(e){} chartChapters = null; }
  // 销毁 ECharts 实例
  if(typeof echartsRadarInstance !== 'undefined' && echartsRadarInstance){ try{ echartsRadarInstance.dispose(); }catch(e){} echartsRadarInstance = null; }
  if(typeof echartsWordcloudInstance !== 'undefined' && echartsWordcloudInstance){ try{ echartsWordcloudInstance.dispose(); }catch(e){} echartsWordcloudInstance = null; }
  // 销毁 vis-network 实例
  if(typeof visNetworkInstance !== 'undefined' && visNetworkInstance){ try{ visNetworkInstance.destroy(); }catch(e){} visNetworkInstance = null; }
  // 中止正在进行的 fetch 请求（如 poll 轮询）
  if(_activeAbortController){
    try{ _activeAbortController.abort(); }catch(e){}
    _activeAbortController = null;
  }
}
function router(){
  // 阶段DD: 切换页面前清理上一页资源
  cleanupPageResources();
  const hash = location.hash.replace('#','');
  const page = hash || 'home';
  // 隐藏所有页面并标记 aria-hidden（Stage S: 无障碍增强）
  document.querySelectorAll('.page').forEach(p=>{
    p.classList.remove('active');
    p.setAttribute('aria-hidden','true');
  });
  // 显示目标页面并移除 aria-hidden
  const target = document.getElementById('page-'+page) || document.getElementById('page-home');
  if(target){
    target.classList.add('active');
    target.removeAttribute('aria-hidden');
    // #097/#098：页面切换淡入动画 + 短暂加载态（200ms）
    target.style.opacity='0';
    target.classList.add('page-loading');
    setTimeout(function(){ target.style.opacity='1'; }, 10);
    setTimeout(function(){ target.classList.remove('page-loading'); }, 200);
  }
  // 返回按钮：非首页显示
  const back = document.getElementById('backBtn');
  if(page==='home'||page===''){back.classList.add('hidden')}
  else{back.classList.remove('hidden')}
  // 阶段2：同步底部 Tab 栏 active 态
  // 流程连贯性修复：AI拆书和书书对比是首页的二级页面，进入时高亮"首页"Tab
  var routeMap = {'digest':'home', 'compare':'home'};
  const routeName = routeMap[page] || (page==='home'||page==='' ? 'home' : page);
  document.querySelectorAll('.btab').forEach(function(t){
    if(t.getAttribute('data-route')===routeName){ t.classList.add('active'); }
    else{ t.classList.remove('active'); }
  });
  // 重新渲染底部 Tab 栏的 Lucide 图标（首次插入时未渲染）
  if(typeof lucide!=='undefined'){ try{ lucide.createIcons(); }catch(e){} }
  // 滚动到顶部
  window.scrollTo(0,0);
  // 进入图谱页时自动加载最近归档
  if(page==='graph' && typeof autoLoadGraph==='function'){ autoLoadGraph(); }
  // 流程连贯性修复：进入设置页时初始化更改跟踪器
  if(page==='settings' && typeof initSettingsChangeTracker==='function'){ initSettingsChangeTracker(); }
  // 阶段2：设置页骨架屏加载效果
  if(page==='settings'){
    var settingsCards = target ? target.querySelectorAll('.card') : [];
    settingsCards.forEach(function(card){
      card.style.opacity='0';
      card.style.transform='translateY(8px)';
    });
    requestAnimationFrame(function(){
      settingsCards.forEach(function(card,i){
        setTimeout(function(){
          card.style.transition='opacity .3s ease, transform .3s ease';
          card.style.opacity='1';
          card.style.transform='translateY(0)';
        }, i*80);
      });
    });
  }
}
window.addEventListener('hashchange',router);

// ════════════════════════════════════════
// 主页：渲染功能卡片
// ════════════════════════════════════════
function renderFeatures(){
  const grid = document.getElementById('featGrid');
  grid.innerHTML = FEATURES.map(f=>{
    const badge = f.badge==='soon' ? '<span class="badge soon">即将上线</span>' : (f.badge?'<span class="badge">'+f.badge+'</span>':'');
    const clickable = f.badge==='soon' ? '' : 'onclick="navigate(\\''+f.id+'\\')" role="button" tabindex="0" aria-label="'+f.title+': '+f.desc+'" onkeydown="if(event.key===\\'Enter\\'){event.preventDefault();navigate(\\''+f.id+'\\')}"';
    const primaryCls = f.primary ? ' feat-card-primary' : '';
    const cta = f.primary ? '<div class="link" style="margin-top:12px;padding-top:10px;border-top:1px solid rgba(124,58,237,.2);font-size:.82rem">开始拆书 →</div>' : '';
    return '<div class="feat-card'+primaryCls+'" '+clickable+'>'+
      '<div class="feat-icon" style="background:'+f.iconBg+';color:'+f.iconColor+'"><i data-lucide="'+f.icon+'"></i></div>'+
      badge+
      '<h3>'+f.title+'</h3>'+
      '<p>'+f.desc+'</p>'+
      cta+
    '</div>';
  }).join('');
  // 渲染 Lucide 图标
  if(typeof lucide!=='undefined'){ lucide.createIcons(); }
  // 阶段5：加载最近拆书活动
  loadRecentActivity();
}

// 阶段5：加载最近拆书记录到主页
function loadRecentActivity(){
  fetch('/api/archives').then(r=>r.json()).then(data=>{
    var items = data.items || data.archives || data || [];
    if(!Array.isArray(items) || items.length===0) return;
    var recent = items.slice(0,3);
    var container = document.getElementById('recent-list');
    var section = document.getElementById('recent-activity');
    if(!container || !section) return;
    container.innerHTML = recent.map(function(a){
      var title = (typeof _cleanBookTitle==='function') ? _cleanBookTitle(a.book_title||a.filename||'未知') : (a.book_title||a.filename||'未知');
      var dateStr = a.created_at || a.date || '';
      var timeLabel = '';
      if(dateStr && typeof formatRelativeTime==='function'){
        timeLabel = formatRelativeTime(dateStr);
      }
      return '<div class="card" style="padding:12px 16px;margin:0;cursor:pointer;display:flex;align-items:center;gap:12px" onclick="openArchive(\\''+(a.filename||a.id||'')+'\\')">'+
        '<div style="width:36px;height:36px;border-radius:8px;background:rgba(124,58,237,.12);display:flex;align-items:center;justify-content:center;font-size:1.1rem;flex-shrink:0">📖</div>'+
        '<div style="flex:1;min-width:0">'+
          '<div style="font-weight:600;font-size:.9rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'+pEscape(title)+'</div>'+
          (timeLabel?'<small style="color:var(--muted);font-size:.75rem">'+timeLabel+'</small>':'')+
        '</div>'+
        '<span style="color:var(--muted);font-size:.8rem;flex-shrink:0">→</span>'+
      '</div>';
    }).join('');
    if(recent.length === 1){
      container.innerHTML += '<div style="text-align:center;padding:8px;font-size:.78rem;color:var(--muted);opacity:.6">— 更多记录在知识库 —</div>';
    }
    section.style.display = 'block';
  }).catch(function(){});
}

// Stage S: 为初始隐藏元素添加 aria-hidden（无障碍增强）
document.querySelectorAll('[style*="display:none"]').forEach(function(el){
  if(!el.getAttribute('aria-hidden')) el.setAttribute('aria-hidden','true');
});
// 监听显示/隐藏变化，自动同步 aria-hidden
if(typeof MutationObserver!=='undefined'){
  new MutationObserver(function(muts){
    muts.forEach(function(m){
      if(m.attributeName==='style'&&m.target.style){
        m.target.setAttribute('aria-hidden', m.target.style.display==='none'?'true':'false');
      }
    });
  }).observe(document.body,{subtree:true,attributes:true,attributeFilter:['style']});
}

// ════════════════════════════════════════
// 拆书页逻辑
// ════════════════════════════════════════
// 文件输入引用（FilePond 初始化后由 pond 管理）
const fileInput=document.getElementById('file');

document.querySelectorAll('.preset').forEach(p=>{
  p.onclick=()=>{
    document.querySelectorAll('.preset').forEach(x=>x.classList.remove('active'));
    p.classList.add('active');
    document.getElementById('model').value=p.dataset.model;
    document.getElementById('model').title=p.dataset.model;
    document.getElementById('base_url').value=p.dataset.url;
    // P2-14：API Key 与模型匹配校验
    checkApiKeyMatch();
  };
});

// P2-14：选中付费模型但无 API Key 时显示内联提示
// P1修复：提示样式改为紧凑内联，避免占据过多屏幕空间
function checkApiKeyMatch(){
  var apiKeyInput = document.getElementById('api_key');
  var hasKey = apiKeyInput.value.trim() || apiKeyInput.getAttribute('data-server-key')==='1';
  var activePreset = document.querySelector('#presets .preset.active');
  var modelLabel = activePreset ? activePreset.textContent : '';
  var isFree = modelLabel.indexOf('免费') >= 0 || modelLabel.indexOf('Free') >= 0;
  var hint = document.getElementById('api-key-hint');
  if(!hint){
    hint = document.createElement('p');
    hint.id = 'api-key-hint';
    hint.style.cssText = 'font-size:.76rem;color:var(--warn);margin:2px 0 0;padding:2px 6px;display:none;border-left:2px solid var(--warn);background:rgba(250,204,21,.06)';
    apiKeyInput.parentNode.appendChild(hint);
  }
  if(!hasKey && !isFree){
    hint.innerHTML = '⚠ 需要API Key · <a href="https://cloud.siliconflow.cn" target="_blank" style="color:var(--accent-light)">获取→</a>';
    hint.style.display = 'block';
  } else {
    hint.style.display = 'none';
  }
}
// API Key 输入时也检查
document.getElementById('api_key').addEventListener('input', function(){
  if(this.getAttribute('data-server-key')==='1') this.removeAttribute('data-server-key');
  checkApiKeyMatch();
});

function toggleAdvanced(){
  const adv=document.getElementById('advanced'),arrow=document.getElementById('adv-arrow'),hdr=document.getElementById('adv-header');
  adv.classList.toggle('collapsed');
  arrow.classList.toggle('open');
  const expanded=!adv.classList.contains('collapsed');
  if(expanded){adv.style.maxHeight='400px';adv.setAttribute('aria-hidden','false');hdr.setAttribute('aria-expanded','true')}
  else{adv.style.maxHeight='0';adv.setAttribute('aria-hidden','true');hdr.setAttribute('aria-expanded','false')}
}

// ════════════════════════════════════════
// 预设书库
// ════════════════════════════════════════
let presetBooksLoaded = false;
let selectedPresetFile = null;

function togglePresetLibrary(){
  const lib=document.getElementById('preset-library'),arrow=document.getElementById('preset-arrow'),hdr=document.getElementById('preset-header');
  lib.classList.toggle('collapsed');
  arrow.classList.toggle('open');
  const expanded=!lib.classList.contains('collapsed');
  if(expanded){
    lib.style.maxHeight='600px';
    lib.setAttribute('aria-hidden','false');
    hdr.setAttribute('aria-expanded','true');
    if(!presetBooksLoaded) loadPresetBooks();
  } else {
    lib.style.maxHeight='0';
    lib.setAttribute('aria-hidden','true');
    hdr.setAttribute('aria-expanded','false');
  }
}

const _PRESET_PAGE_SIZE=12;
let _presetBooksCache=[];
let _presetPage=1;

function _buildPresetCard(book){
  const safeId=book.title.replace(/[^a-zA-Z0-9]/g,'_');
  const fmts=book.formats.map(f=>{
    const kb=Math.round(f.size/1024);
    const charsK=Math.round(f.est_chars/1000);
    return '<span class="pb-format" role="button" tabindex="0" data-title="'+book.title+'" data-filename="'+f.filename+'" data-ext="'+f.ext+'" title="'+kb+'KB | ~'+charsK+'K字" aria-label="选择 '+book.title+' '+f.ext.toUpperCase()+' 格式">'+f.ext.toUpperCase()+' '+kb+'KB</span>';
  }).join('');
  const totalChars=Math.round(book.formats.reduce((s,f)=>s+f.est_chars,0)/book.formats.length/1000);
  return '<div class="preset-book-card" id="pbc-'+safeId+'">'+
    '<div class="pb-title">'+book.title+'</div>'+
    '<div class="pb-info">'+fmts+'<span class="pb-chars">~'+totalChars+'K字</span></div>'+
    '<div class="pb-selected-tag" id="pbst-'+safeId+'" style="display:none">已选择</div>'+
  '</div>';
}

function _renderPresetBooks(){
  const list=document.getElementById('preset-books-list');
  if(!list) return;
  // #101：分页渲染，每次仅展示前 _presetPage * 12 本
  const visible=_presetBooksCache.slice(0, _presetPage*_PRESET_PAGE_SIZE);
  let html=visible.map(_buildPresetCard).join('');
  if(_presetBooksCache.length > _presetPage*_PRESET_PAGE_SIZE){
    html+='<button type="button" id="preset-load-more" style="width:100%;padding:10px;background:var(--card);border:1px solid var(--border);color:var(--accent);cursor:pointer;border-radius:var(--radius-sm);margin-top:4px">加载更多 ('+(_presetBooksCache.length-_presetPage*_PRESET_PAGE_SIZE)+' 本)</button>';
  }
  list.innerHTML=html;
  // 绑定格式按钮点击事件
  document.querySelectorAll('.pb-format').forEach(btn=>{
    btn.onclick=function(e){
      e.stopPropagation();
      selectPresetBook(this.dataset.title, this.dataset.filename, this.dataset.ext, this);
    };
    btn.onkeydown=function(e){
      if(e.key==='Enter'||e.key===' '){
        e.preventDefault();
        e.stopPropagation();
        selectPresetBook(this.dataset.title, this.dataset.filename, this.dataset.ext, this);
      }
    };
  });
  // 绑定"加载更多"按钮
  const moreBtn=document.getElementById('preset-load-more');
  if(moreBtn){ moreBtn.onclick=function(){ _presetPage++; _renderPresetBooks(); }; }
  // 重新应用已选中书籍的高亮状态（避免分页重渲染后丢失）
  if(typeof selectedPresetFile!=='undefined' && selectedPresetFile){
    const safeId=selectedPresetFile.title.replace(/[^a-zA-Z0-9]/g,'_');
    const card=document.getElementById('pbc-'+safeId);
    if(card) card.classList.add('selected');
    const tag=document.getElementById('pbst-'+safeId);
    if(tag){tag.style.display='block';tag.textContent='已选择：'+selectedPresetFile.filename;}
    if(selectedPresetFile.filename){
      document.querySelectorAll('.pb-format').forEach(function(f){
        if(f.dataset.filename===selectedPresetFile.filename) f.classList.add('active');
      });
    }
  }
}

async function loadPresetBooks(){
  // 显示骨架屏
  const list=document.getElementById('preset-books-list');
  list.innerHTML='<div class="skeleton-card"><div class="skeleton-line" style="width:60%;height:14px;margin-bottom:8px"></div><div class="skeleton-line" style="width:40%;height:12px"></div></div><div class="skeleton-card"><div class="skeleton-line" style="width:60%;height:14px;margin-bottom:8px"></div><div class="skeleton-line" style="width:40%;height:12px"></div></div><div class="skeleton-card"><div class="skeleton-line" style="width:60%;height:14px;margin-bottom:8px"></div><div class="skeleton-line" style="width:40%;height:12px"></div></div>';
  try{
    const r=await fetch('/api/preset-books');
    const d=await r.json();
    if(!d.books||d.books.length===0){
      list.innerHTML='<p style="color:var(--muted);text-align:center;padding:20px">暂无预设书籍。请将测试书籍放入服务器 test_books 目录。</p>';
      return;
    }
    _presetBooksCache=d.books;
    _presetPage=1;
    _renderPresetBooks();
    presetBooksLoaded=true;
  }catch(e){
    list.innerHTML='<p style="color:var(--err)">加载失败：'+e.message+'</p>';
  }
}

async function selectPresetBook(title, filename, ext, btnEl){
  try{
    // 从后端下载文件
    const r=await fetch('/api/preset-books/'+encodeURIComponent(filename));
    if(!r.ok) throw new Error('下载失败');
    const blob=await r.blob();
    // 根据扩展名设置正确的 MIME 类型（避免 FilePond 类型验证失败）
    const mimeMap={'pdf':'application/pdf','epub':'application/epub+zip','txt':'text/plain','md':'text/markdown'};
    const mime=mimeMap[ext.toLowerCase()]||blob.type||'application/octet-stream';
    const file=new File([blob], filename, {type: mime});

    // 设置到 FilePond（程序化添加文件时临时关闭类型验证，避免 FilePond 内部检测与 file.type 不一致）
    if(typeof pond!=='undefined' && pond){
      pond.removeFile();
      const FP_TYPES=['application/pdf','application/epub+zip','text/plain','text/markdown'];
      try{ pond.setOptions({acceptedFileTypes:null}); }catch(e){}
      try{
        await pond.addFile(file);
      }catch(fpErr){
        // FilePond 添加失败时降级到原生 input
        console.warn('FilePond addFile failed, falling back:', fpErr);
        try{ pond.removeFile(); }catch(e2){}
        const dt=new DataTransfer();
        dt.items.add(file);
        const fi2=document.getElementById('file');
        if(fi2) fi2.files=dt.files;
      }finally{
        try{ pond.setOptions({acceptedFileTypes:FP_TYPES}); }catch(e){}
      }
    } else {
      // 降级：设置到原生 input（用 DataTransfer）
      const dt=new DataTransfer();
      dt.items.add(file);
      const fi3=document.getElementById('file');
      if(fi3) fi3.files=dt.files;
    }

    selectedPresetFile={title, filename, ext};

    // 更新 UI 高亮
    document.querySelectorAll('.preset-book-card').forEach(c=>c.classList.remove('selected'));
    document.querySelectorAll('.pb-selected-tag').forEach(t=>t.style.display='none');
    document.querySelectorAll('.pb-format').forEach(f=>f.classList.remove('active'));

    const safeId=title.replace(/[^a-zA-Z0-9]/g,'_');
    const card=document.getElementById('pbc-'+safeId);
    if(card) card.classList.add('selected');
    const tag=document.getElementById('pbst-'+safeId);
    if(tag){tag.style.display='block';tag.textContent='已选择：'+filename;}
    if(btnEl) btnEl.classList.add('active');

    if(typeof toast==='function') toast('已加载：'+filename, 'success');
  }catch(e){
    if(typeof toastError==='function') toastError('加载预设书籍失败：'+e.message);
  }
}

// Ctrl+Enter 提交拆书
document.addEventListener('keydown', function(e){
  if((e.ctrlKey||e.metaKey) && e.key==='Enter' && document.getElementById('page-digest').style.display!=='none'){
    e.preventDefault();
    submit();
  }
});

// 检查服务器是否已配置 API Key
fetch('/health').then(r=>r.json()).then(d=>{
  if(d.has_api_key){
    var apiKeyInput = document.getElementById('api_key');
    if(apiKeyInput && !apiKeyInput.value){
      apiKeyInput.placeholder = '已通过环境变量配置，留空则使用服务器默认';
      apiKeyInput.setAttribute('data-server-key', '1');
    }
  }
}).catch(function(){});

// ── 阶段EE: 输入验证与表单 UX ──
function showInputHint(inputId, msg, type){
  var el = document.getElementById(inputId+'-hint');
  if(!el){
    el = document.createElement('div');
    el.id = inputId+'-hint';
    el.className = 'input-hint';
    var input = document.getElementById(inputId);
    if(input && input.parentNode) input.parentNode.appendChild(el);
  }
  el.textContent = msg;
  el.className = 'input-hint ' + (type||'');
}
function clearInputHint(inputId){
  var el = document.getElementById(inputId+'-hint');
  if(el){ el.textContent = ''; el.className = 'input-hint'; }
}
function setValid(inputId, valid){
  var input = document.getElementById(inputId);
  if(!input) return;
  input.classList.remove('input-valid','input-invalid');
  if(valid === true) input.classList.add('input-valid');
  else if(valid === false) input.classList.add('input-invalid');
}
function validateApiKey(){
  var input = document.getElementById('api_key');
  if(!input) return true;
  var val = input.value.trim();
  if(!val){
    if(input.getAttribute('data-server-key') === '1'){
      clearInputHint('api_key'); setValid('api_key', null); return true;
    }
    setValid('api_key', false);
    showInputHint('api_key', '请填写 API Key', 'error');
    return false;
  }
  if(val.length < 10){
    setValid('api_key', false);
    showInputHint('api_key', 'API Key 长度似乎过短', 'warn');
    return false;
  }
  setValid('api_key', true);
  showInputHint('api_key', '✓ 格式正确', 'ok');
  return true;
}
function validateBaseUrl(){
  var input = document.getElementById('base_url');
  if(!input) return true;
  var val = input.value.trim();
  if(!val){ setValid('base_url', false); showInputHint('base_url', '请填写 Base URL', 'error'); return false; }
  try{
    var u = new URL(val);
    if(u.protocol !== 'https:' && u.protocol !== 'http:'){
      setValid('base_url', false); showInputHint('base_url', 'URL 需以 http:// 或 https:// 开头', 'error'); return false;
    }
    setValid('base_url', true); clearInputHint('base_url'); return true;
  }catch(e){
    setValid('base_url', false); showInputHint('base_url', 'URL 格式不正确', 'error'); return false;
  }
}
function validateModel(){
  var input = document.getElementById('model');
  if(!input) return true;
  var val = input.value.trim();
  if(!val){ setValid('model', false); showInputHint('model', '请填写模型名称', 'error'); return false; }
  setValid('model', true); clearInputHint('model'); return true;
}
function validateNumber(inputId, min, max, label){
  var input = document.getElementById(inputId);
  if(!input) return true;
  var val = parseInt(input.value, 10);
  if(isNaN(val)){ setValid(inputId, false); showInputHint(inputId, label+'必须是数字', 'error'); return false; }
  if(val < min){ setValid(inputId, false); showInputHint(inputId, label+'不能小于 '+min, 'error'); return false; }
  if(val > max){ setValid(inputId, false); showInputHint(inputId, label+'不能大于 '+max, 'error'); return false; }
  setValid(inputId, true); clearInputHint(inputId); return true;
}
function validateForm(){
  var ok = true;
  if(!validateApiKey()) ok = false;
  if(!validateBaseUrl()) ok = false;
  if(!validateModel()) ok = false;
  if(!validateNumber('chunk_size', 500, 10000, '分块大小')) ok = false;
  if(!validateNumber('reviewers', 1, 4, '审稿人数')) ok = false;
  if(!validateNumber('max_revisions', 0, 4, '修订轮次')) ok = false;
  return ok;
}
// 绑定实时验证
document.addEventListener('DOMContentLoaded', function(){
  var apiKey = document.getElementById('api_key');
  if(apiKey) apiKey.addEventListener('blur', validateApiKey);
  var baseUrl = document.getElementById('base_url');
  if(baseUrl) baseUrl.addEventListener('blur', validateBaseUrl);
  var model = document.getElementById('model');
  if(model) model.addEventListener('blur', validateModel);
  ['chunk_size','reviewers','max_revisions'].forEach(function(id){
    var el = document.getElementById(id);
    if(el) el.addEventListener('blur', function(){ validateNumber(id, id==='chunk_size'?500:0, id==='chunk_size'?10000:4, el.previousElementSibling?el.previousElementSibling.textContent.replace(/（.*）/,'').trim():id); });
  });
});

async function submit(){
  // 从 FilePond 获取文件，降级到原生 input
  let selectedFile = null;
  if(typeof pond!=='undefined' && pond && pond.getFiles().length>0){
    selectedFile = pond.getFile().file;
  } else if(fileInput.files[0]){
    selectedFile = fileInput.files[0];
  }
  if(!selectedFile){
    toastError('请先选择文件');
    // P0修复：文件上传区内联视觉反馈
    var fp = document.querySelector('.filepond--root');
    if(fp){
      fp.style.outline = '2px solid var(--err)';
      fp.style.outlineOffset = '2px';
      setTimeout(function(){ fp.style.outline = ''; fp.style.outlineOffset = ''; }, 3000);
    }
    return;
  }
  // 阶段EE: 提交前表单验证
  if(!validateForm()){toastError('请修正表单中的错误');return}
  const key=document.getElementById('api_key').value.trim();
  if(!key && document.getElementById('api_key').getAttribute('data-server-key')!=='1'){toastError('请填写 API Key');return}
  // 阶段EE: 文件大小检查
  if(selectedFile.size > 50 * 1024 * 1024){toastError('文件超过 50MB 限制');return}
  const btn=document.getElementById('btn');
  if(btn){btn.disabled=true;btn.textContent='处理中…';btn.style.opacity=0.7;}
  document.getElementById('prog').style.display='block';
  document.getElementById('err').style.display='none';
  document.getElementById('result').classList.remove('show');
  const fd=new FormData();
  fd.append('file',selectedFile);
  fd.append('api_key',key);
  fd.append('base_url',document.getElementById('base_url').value);
  fd.append('model',document.getElementById('model').value);
  fd.append('book_type',document.getElementById('book_type').value);
  fd.append('focus',document.getElementById('focus').value);
  fd.append('chunk_size',document.getElementById('chunk_size').value||2500);
  fd.append('reviewers',document.getElementById('reviewers').value||2);
  fd.append('max_revisions',document.getElementById('max_revisions').value||2);
  try{
    // 阶段DD: 使用 AbortController 支持取消请求
    _activeAbortController = new AbortController();
    const r=await fetch('/api/digest',{method:'POST',body:fd,signal:_activeAbortController.signal});
    if(!r.ok){
      const text=await r.text().catch(()=>'');
      fail('提交失败 (HTTP '+r.status+')'+(text?': '+text.substring(0,100):''));return
    }
    const ct=r.headers.get('content-type')||'';
    if(!ct.includes('application/json')){
      const text=await r.text().catch(()=>'未知错误');
      fail('服务器返回异常：'+text.substring(0,100));return
    }
    const d=await r.json();
    btn.textContent='拆书中…';
    poll._retries=0;
    poll(d.job_id);
  }catch(e){
    if(e.name==='AbortError') return; // 阶段DD: 导航取消，静默退出
    fail('网络错误：'+e.message)
  }
}
function fail(msg){
  // 阶段DD: 错误发生时清理 AbortController
  _activeAbortController = null;
  var errEl=document.getElementById('err');
  errEl.innerHTML='<div class="err-row"><span class="err-msg">'+msg+'</span></div>';
  errEl.style.display='block';
  // 阶段Y：添加重试按钮
  var retryBtn=document.createElement('button');
  retryBtn.className='secondary';
  retryBtn.textContent='重试';
  retryBtn.style.marginTop='8px';
  retryBtn.style.fontSize='.85rem';
  retryBtn.style.padding='8px 16px';
  retryBtn.onclick=function(){
    errEl.style.display='none';
    var lastFile=null;
    if(typeof pond!=='undefined' && pond && pond.getFiles().length>0){ lastFile=pond.getFile().file; }
    else if(fileInput.files[0]){ lastFile=fileInput.files[0]; }
    if(lastFile){ submit(); }
    else { toastError('请重新选择文件后重试'); }
  };
  errEl.appendChild(retryBtn);
  var btn=document.getElementById('btn');if(btn){btn.disabled=false;btn.textContent='开始拆书';btn.style.opacity=1;}
  document.getElementById('prog').style.display='none';
}
async function poll(jobId){
  // 阶段DD: 检查是否已被取消（导航离开时）
  if(!_activeAbortController) return;
  try{
    const r=await fetch('/api/status/'+jobId,{signal:_activeAbortController.signal});
    if(!r.ok){
      const text=await r.text().catch(()=>'');
      if(r.status===404){fail('任务不存在，可能服务器已重启');return}
      if(r.status>=500){fail('服务器内部错误，请稍后重试');return}
      fail('查询失败 (HTTP '+r.status+')'+(text?': '+text.substring(0,100):''));return
    }
    const ct=r.headers.get('content-type')||'';
    if(!ct.includes('application/json')){
      const text=await r.text().catch(()=>'未知错误');
      // 可能是代理返回的纯文本错误（如 "no healthy upstream"）
      if(text.includes('no healthy upstream')||text.includes('upstream')){
        fail('服务器暂时不可用，请稍后重试');return
      }
      fail('服务器返回异常：'+text.substring(0,100));return
    }
    const d=await r.json();
    if(d.status==='done'){
      _activeAbortController = null; // 阶段DD: 轮询结束，清理
      document.getElementById('bar').style.width='100%';
      document.getElementById('stage').textContent='完成！';
      showResult(jobId,d);
      const btn=document.getElementById('btn');if(btn){btn.disabled=false;btn.textContent='开始拆书';btn.style.opacity=1;}
    }else if(d.status==='error'){
      _activeAbortController = null; // 阶段DD: 轮询结束，清理
      fail(d.error||'拆书失败');
    }else{
      document.getElementById('bar').style.width=d.progress+'%';
      document.getElementById('stage').textContent=d.stage+' ('+d.progress+'%)';
      setTimeout(()=>{
        // 阶段DD: 检查是否已被取消
        if(_activeAbortController) poll(jobId);
      },2500);
    }
  }catch(e){
    // 阶段DD: 导航取消时静默退出
    if(e.name==='AbortError') return;
    // 网络错误或 JSON 解析失败，重试一次
    if(!poll._retries) poll._retries=0;
    poll._retries++;
    if(poll._retries<=3){
      document.getElementById('stage').textContent='网络波动，重试中 ('+poll._retries+'/3)...';
      setTimeout(()=>{
        if(_activeAbortController) poll(jobId);
      },3000);
    } else {
      fail('查询状态失败：'+e.message+'（已重试3次，请检查网络或重启服务）');
    }
  }
}
async function showResult(jobId,d){
  document.getElementById('result').classList.add('show');
  // 阶段5：结果展示后重置 Tab 到"输出文件"
  if (typeof switchResultTab === 'function') { try{ switchResultTab('outputs'); }catch(e){} }
  const s=d.stats||{};
  // 统计数据由 renderDashboard 统一渲染到 stats-grid，无需重复设置
  let h='';
  for(const[k,v]of Object.entries(d.files)){const fn=v.split('/').pop();h+='<div class="out"><a href="/api/download/'+jobId+'/'+encodeURIComponent(k)+'" target="_blank">下载</a><div class="kind">'+k+'<br><small>'+fn+'</small></div></div>'}
  document.getElementById('outs').innerHTML=h;
  const pv=document.getElementById('preview');pv.src='/api/preview/'+jobId;
  document.getElementById('report-full').href='/api/preview/'+jobId;
}

// ════════════════════════════════════════
// 工具箱功能：Anki / Obsidian / TTS / 问答
// ════════════════════════════════════════
var currentJobId = null; // 阶段DD: var 避免 TDZ 错误

// 流程连贯性修复：更新海报入口按钮的禁用状态
function updatePosterBtnState(){
  var btn = document.getElementById('poster-trigger-btn');
  if(!btn) return;
  if(currentJobId){
    btn.disabled = false;
    btn.style.opacity = '1';
    btn.style.cursor = 'pointer';
    btn.title = '生成知识海报';
  } else {
    btn.disabled = false; // 允许点击以显示空状态引导
    btn.style.opacity = '.6';
    btn.title = '需先完成拆书 — 点击查看说明';
  }
}

// 在 showResult 中保存 jobId + 写入知识库历史 + 渲染仪表盘
const _origShowResult = showResult;
showResult = async function(jobId, d) {
  currentJobId = jobId;
  await _origShowResult(jobId, d);
  // 流程连贯性修复：拆书完成后启用海报按钮
  updatePosterBtnState();
  // 保存到知识库历史
  if(d.stats){
    const title = document.querySelector('.filepond--file-info-main')?.textContent || 
                  d.files?.[Object.keys(d.files)[0]]?.split('/').pop() || '未知书籍';
    saveToHistory(jobId, d.stats, title.replace(/\.[^.]+$/, ''));
  }
  // 渲染统计仪表盘
  if(d.stats){
    renderDashboard(d.stats, d.result||null);
  }
  // 设置搜索数据 + Fuse.js 初始化
  if(d.result && d.result.units){
    setSearchData(d.result.units);
    initFuseSearch(d.result.units);
  }
  // 渲染交互式知识图谱
  if(d.result){
    renderInteractiveGraph(d.result);
  }
  // 导入闪卡数据
  if(d.result){
    const added = importFlashcardsFromResult(d.result);
    if(added > 0) toast(added+' 张闪卡已导入复习系统', 'info');
  }
  // 渲染 ECharts 可视化
  if(d.result){
    renderEChartsVisualizations(d.result);
  }
  // 渲染数学公式
  if(typeof renderMathInElement !== 'undefined'){
    const qaAns = document.getElementById('qa-answer');
    if(qaAns) renderMath(qaAns);
  }
};

async function exportAnki() {
  if (!currentJobId) {toastError('请先完成拆书');return}
  try {
    const r = await fetch('/api/anki/' + currentJobId, {method:'POST'});
    if (!r.ok) {const e = await r.json(); throw new Error(e.detail||'导出失败')}
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'anki_deck.apkg';
    a.click();
    toastSuccess('Anki 闪卡已导出');
  } catch(e) {toastError('Anki 导出失败：'+e.message)}
}

async function exportObsidian() {
  if (!currentJobId) {toastError('请先完成拆书');return}
  try {
    const r = await fetch('/api/obsidian/' + currentJobId, {method:'POST'});
    if (!r.ok) {const e = await r.json(); throw new Error(e.detail||'导出失败')}
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'obsidian_vault.zip';
    a.click();
    toastSuccess('Obsidian Vault 已导出');
  } catch(e) {toastError('Obsidian 导出失败：'+e.message)}
}

async function generateTTS(mode) {
  if (!currentJobId) {toastError('请先完成拆书');return}
  const status = document.getElementById('tts-status');
  status.style.display = 'block';
  status.className = 'tts-status loading';
  status.textContent = '正在生成听书音频，请稍候…';
  try {
    const fd = new FormData();
    fd.append('job_id', currentJobId);
    fd.append('mode', mode);
    fd.append('voice', 'zh-CN-YunxiNeural');
    const r = await fetch('/api/tts', {method:'POST', body:fd, signal:(_activeAbortController?_activeAbortController.signal:undefined)});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail||'生成失败');
    let html = '生成完成！估算时长 ' + d.duration + '<br>';
    d.files.forEach(f => {
      html += '<a href="/api/tts/download/' + encodeURIComponent(f.name) + '" target="_blank">' + f.name + '</a><br>';
    });
    status.className = 'tts-status done';
    status.innerHTML = html;
  } catch(e) {
    if(e.name==='AbortError') return;
    status.className = 'tts-status';
    status.style.background = 'rgba(248,113,113,.08)';
    status.style.border = '1px solid rgba(248,113,113,.3)';
    status.style.color = 'var(--err)';
    status.textContent = '听书生成失败：' + e.message;
  }
}

// ═══ 知识海报功能 ═══
// P0修复：清理调试文件名，显示用户友好的书名
function _cleanBookTitle(title){
  if(!title) return '';
  // 移除文件扩展名
  title = title.replace(/\.(pdf|epub|md|txt|mobi|azw3)$/i, '');
  // 移除 test_ 前缀
  title = title.replace(/^test_/i, '');
  // 移除日期后缀 (2026/7/3 等)
  title = title.replace(/\s*\(\d{4}\/\d+\/\d+.*?\)\s*/g, '');
  // 下划线替换为空格
  title = title.replace(/_/g, ' ');
  return title.trim();
}

var posterCards = [];
var posterImageLoading = {};
var posterAbortFlag = false; // 修复问题4: 关闭模态框时中断
var POSTER_CONCURRENCY = 3; // 修复问题3: 并发数

async function generatePoster() {
  if (!currentJobId) {toastError('请先完成拆书');return}
  posterAbortFlag = false;
  var overlay = document.getElementById('poster-overlay');
  var grid = document.getElementById('poster-grid');
  var footer = document.getElementById('poster-footer');
  overlay.style.display = 'flex';
  overlay.setAttribute('aria-hidden','false');
  grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:40px;color:var(--muted)"><div class="poster-loading-spinner" style="margin:0 auto 16px"></div>正在提取书中金句…</div>';
  footer.style.display = 'none';
  // P0-2：加载时隐藏滚动提示
  var _sh = document.getElementById('poster-scroll-hint');
  if(_sh) _sh.style.display = 'none';

  try {
    // 修复问题1: 不再传 api_key
    var body = JSON.stringify({n: 10});
    var r = await fetch('/api/poster/quotes/'+encodeURIComponent(currentJobId), {
      method:'POST', headers:{'Content-Type':'application/json'}, body:body
    });
    var d = await r.json();
    if(!r.ok) throw new Error(d.detail||'提取失败');
    posterCards = d.cards || [];
    renderPosterGrid();
    // 修复问题3: 并发生成配图
    generatePosterImages();
  } catch(e) {
    grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:40px;color:var(--err)">金句提取失败：'+pEscape(e.message)+'</div>';
  }
}

function renderPosterGrid() {
  var grid = document.getElementById('poster-grid');
  var html = '';
  for(var i=0; i<posterCards.length; i++){
    var c = posterCards[i];
    var imgHtml = '';
    if(c.image){
      imgHtml = '<img class="poster-card-img" src="/api/poster/files/'+encodeURIComponent(c.image)+'" alt="配图" loading="lazy" onerror="this.style.display=&#39;none&#39;;var ph=this.nextElementSibling;if(ph){ph.style.display=&#39;flex&#39;}">';
      // P0修复：配图加载失败时显示重试按钮 + 渐变背景
      imgHtml += '<div class="poster-card-placeholder poster-img-error" style="display:none;flex-direction:column;gap:8px;background:linear-gradient(135deg,var(--card),rgba(248,113,113,.08))"><span class="pc-icon" style="font-size:28px">🖼️</span><span class="pc-loading-text">配图加载失败</span><button type="button" class="secondary" style="width:auto;padding:8px 16px;margin:0 auto;font-size:.78rem;min-height:36px" onclick="retryPosterImage('+i+')">点击重试</button></div>';
    } else {
      imgHtml = '<div class="poster-card-placeholder" id="poster-ph-'+i+'"><span class="pc-icon">🎨</span><span class="pc-loading-text">正在生成配图…</span></div>';
    }
    html += '<div class="poster-card" id="poster-card-'+i+'">'+
      imgHtml+
      '<div class="poster-card-body">'+
        '<div class="poster-card-quote"><span class="poster-quote-mark">\u201C</span>'+pEscape(c.quote)+'<span class="poster-quote-mark">\u201D</span></div>'+
        (c.interpretation ? '<div class="poster-card-interp">'+pEscape(c.interpretation)+'</div>' : '')+
        '<div class="poster-card-meta">'+
          (c.chapter ? '<span class="poster-card-chapter">'+pEscape(c.chapter)+'</span>' : '<span></span>')+
          // P0修复：清理调试文件名，显示用户友好的书名
          '<span class="poster-card-book">'+pEscape(_cleanBookTitle(c.book_title||''))+'</span>'+
        '</div>'+
        '<div class="poster-card-actions">'+
          (c.image ? '<button class="pc-action" onclick="regeneratePosterImage('+i+')" title="重新生成配图">🔄</button>' : '')+
          (c.image ? '<a class="pc-action" href="/api/poster/files/'+encodeURIComponent(c.image)+'" download title="下载配图">⬇</a>' : '')+
          // P0修复：添加分享按钮（复制金句到剪贴板）
          '<button class="pc-action" onclick="sharePosterQuote('+i+')" title="复制金句">🔗</button>'+
        '</div>'+
      '</div>'+
    '</div>';
  }
  grid.innerHTML = html;
  // P0-2：内容加载完成后显示滚动提示
  var scrollHint = document.getElementById('poster-scroll-hint');
  if(scrollHint && posterCards.length > 0){ scrollHint.style.display = 'inline'; }
  // 阶段9：更新海报计数指示器
  var footer = document.getElementById('poster-footer');
  if(footer){
    var loaded = posterCards.filter(function(c){return c.image}).length;
    footer.style.display = 'block';
    footer.innerHTML = '<span style="color:var(--muted);font-size:.85rem">📖 共 '+posterCards.length+' 张知识卡片 · 已生成 '+loaded+' 张配图</span>';
  }
}

function pEscape(s){
  if(!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// 修复问题3: 并发生成配图（POSTER_CONCURRENCY 张同时）
async function generatePosterImages(){
  var footer = document.getElementById('poster-footer');
  var total = posterCards.length;
  var completed = 0;
  footer.style.display = 'block';
  footer.textContent = '正在生成 AI 配图… 0 / '+total;

  // 筛选需要生成图片的卡片
  var pending = [];
  for(var i=0; i<posterCards.length; i++){
    if(!posterCards[i].image && !posterImageLoading[i]){
      pending.push(i);
    } else if(posterCards[i].image){
      completed++;
    }
  }
  updatePosterProgress(completed,total);

  // 分批并发
  while(pending.length > 0){
    // 修复问题4: 检查中断标志
    if(posterAbortFlag){footer.textContent='已中断（已生成 '+completed+' / '+total+'）';return}
    var batch = pending.splice(0, POSTER_CONCURRENCY);
    var promises = batch.map(function(idx){
      posterImageLoading[idx] = true;
      return fetchPosterImage(idx).then(function(){
        posterImageLoading[idx] = false;
        completed++;
        updatePosterProgress(completed,total);
      }).catch(function(){
        posterImageLoading[idx] = false;
        completed++;
        updatePosterProgress(completed,total);
      });
    });
    await Promise.all(promises);
  }
  footer.textContent = '配图生成完成！共 '+completed+' / '+total+' 张';
}

async function fetchPosterImage(i){
  // 修复问题4: 检查中断
  if(posterAbortFlag) return;
  try {
    var body = JSON.stringify({}); // 修复问题1: 不再传 api_key
    var r = await fetch('/api/poster/image/'+encodeURIComponent(currentJobId)+'/'+i, {
      method:'POST', headers:{'Content-Type':'application/json'}, body:body
    });
    var d = await r.json();
    if(r.ok && d.image){
      posterCards[i].image = d.image;
      var ph = document.getElementById('poster-ph-'+i);
      if(ph){
        var img = document.createElement('img');
        img.className = 'poster-card-img';
        img.src = '/api/poster/files/'+encodeURIComponent(d.image);
        img.alt = '配图';
        img.loading = 'lazy';
        ph.replaceWith(img);
        // 添加操作按钮
        var card = document.getElementById('poster-card-'+i);
        if(card){
          var actions = card.querySelector('.poster-card-actions');
          if(actions){
            actions.innerHTML = '<button class="pc-action" onclick="regeneratePosterImage('+i+')" title="重新生成配图">🔄</button><a class="pc-action" href="/api/poster/files/'+encodeURIComponent(d.image)+'" download title="下载配图">⬇</a>';
          }
        }
      }
    }
  } catch(e){
    console.warn('Image '+i+' failed:', e);
  }
}

// 功能补充11: 重新生成单张配图
// P0修复：分享金句（复制到剪贴板）
async function sharePosterQuote(i){
  if(!posterCards[i]) return;
  var c = posterCards[i];
  var text = '"' + c.quote + '"';
  if(c.book_title) text += ' —— ' + _cleanBookTitle(c.book_title);
  try {
    await navigator.clipboard.writeText(text);
    if(typeof Swal !== 'undefined'){
      Swal.fire({title:'已复制',text:'金句已复制到剪贴板',icon:'success',timer:1500,showConfirmButton:false});
    } else { toastSuccess('金句已复制'); }
  } catch(e){
    // 降级方案
    var ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); toastSuccess('金句已复制'); } catch(e2){ toastError('复制失败'); }
    document.body.removeChild(ta);
  }
}

// P0修复：配图加载失败后重试（重新请求图片URL）
async function retryPosterImage(i){
  if(!posterCards[i] || !posterCards[i].image) return;
  var card = document.getElementById('poster-card-'+i);
  if(!card) return;
  var errPh = card.querySelector('.poster-img-error');
  if(errPh) errPh.style.display = 'none';
  var img = document.createElement('img');
  img.className = 'poster-card-img';
  img.src = '/api/poster/files/'+encodeURIComponent(posterCards[i].image)+'?t='+Date.now();
  img.alt = '配图';
  img.loading = 'lazy';
  img.onerror = function(){ this.style.display='none'; var ph=this.nextElementSibling; if(ph) ph.style.display='flex'; };
  if(errPh) errPh.parentNode.insertBefore(img, errPh);
  else card.insertBefore(img, card.firstChild);
}

async function regeneratePosterImage(i){
  if(!posterCards[i]) return;
  // 显示加载状态
  var card = document.getElementById('poster-card-'+i);
  var imgEl = card ? card.querySelector('.poster-card-img') : null;
  if(imgEl){
    var ph = document.createElement('div');
    ph.className = 'poster-card-placeholder';
    ph.id = 'poster-ph-'+i;
    ph.innerHTML = '<span class="pc-icon">🎨</span>';
    imgEl.replaceWith(ph);
  }
  try {
    var r = await fetch('/api/poster/regenerate/'+encodeURIComponent(currentJobId)+'/'+i, {method:'POST'});
    var d = await r.json();
    if(r.ok && d.image){
      posterCards[i].image = d.image;
      var ph2 = document.getElementById('poster-ph-'+i);
      if(ph2){
        var img = document.createElement('img');
        img.className = 'poster-card-img';
        img.src = '/api/poster/files/'+encodeURIComponent(d.image);
        img.alt = '配图';
        ph2.replaceWith(img);
      }
      toast('配图已重新生成');
    } else {
      toastError('重新生成失败');
    }
  } catch(e){
    toastError('重新生成失败：'+e.message);
  }
}

function updatePosterProgress(done,total){
  var footer = document.getElementById('poster-footer');
  footer.textContent = '正在生成 AI 配图… '+done+' / '+total;
}

function closePoster(){
  posterAbortFlag = true; // 修复问题4: 中断配图生成
  var overlay = document.getElementById('poster-overlay');
  overlay.style.display = 'none';
  overlay.setAttribute('aria-hidden','true');
}

// 阶段1：openPoster 入口函数 — 无 jobId 时显示空状态引导
function openPoster(){
  var overlay = document.getElementById('poster-overlay');
  var grid = document.getElementById('poster-grid');
  var footer = document.getElementById('poster-footer');
  overlay.style.display = 'flex';
  overlay.setAttribute('aria-hidden','false');
  footer.style.display = 'none';
  // 隐藏滚动提示
  var _sh = document.getElementById('poster-scroll-hint');
  if(_sh) _sh.style.display = 'none';
  if(!currentJobId){
    // 无拆书任务：显示空状态引导
    grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:48px 16px">'+
      '<div style="font-size:48px;opacity:.3;margin-bottom:16px">🖼️</div>'+
      '<p style="color:var(--muted);font-size:.9rem;margin-bottom:16px">完成拆书后即可生成知识海报</p>'+
      '<button type="button" class="secondary" style="width:auto;padding:10px 24px;margin:0 auto" onclick="closePoster();navigate(&quot;digest&quot;)">去拆书 →</button>'+
    '</div>';
  } else {
    // 有 jobId：正常生成
    generatePoster();
  }
}

// 点击遮罩关闭 + Esc 关闭（功能补充13）
document.addEventListener('DOMContentLoaded', function(){
  var overlay = document.getElementById('poster-overlay');
  if(overlay){
    overlay.addEventListener('click', function(e){
      if(e.target === overlay) closePoster();
    });
  }
  // Esc 关闭
  document.addEventListener('keydown', function(e){
    if(e.key === 'Escape'){
      var ov = document.getElementById('poster-overlay');
      if(ov && ov.style.display !== 'none') closePoster();
    }
  });
  // 流程连贯性修复：初始化海报按钮状态
  updatePosterBtnState();
});


async function askQuestion() {
  if (!currentJobId) {toastError('请先完成拆书');return}
  // 阶段5：优先读取桌面端输入框，为空则回退到移动端悬浮栏输入
  const deskInput = document.getElementById('qa-question');
  const mobInput = document.getElementById('qa-question-mobile');
  let q = (deskInput && deskInput.value ? deskInput.value : '').trim();
  if (!q && mobInput) { q = mobInput.value.trim(); }
  if (deskInput) deskInput.value = q;
  if (mobInput) mobInput.value = q;
  if (!q) return;
  const answerEl = document.getElementById('qa-answer');
  const srcEl = document.getElementById('qa-sources');
  answerEl.style.display = 'block';
  answerEl.className = 'qa-answer loading';
  answerEl.textContent = '思考中…';
  srcEl.style.display = 'none';
  try {
    const apiKey = document.getElementById('api_key').value.trim();
    const fd = new FormData();
    fd.append('job_id', currentJobId);
    fd.append('question', q);
    if (apiKey) fd.append('api_key', apiKey);
    fd.append('base_url', document.getElementById('base_url').value);
    fd.append('model', document.getElementById('model').value);
    const r = await fetch('/api/qa', {method:'POST', body:fd, signal:(_activeAbortController?_activeAbortController.signal:undefined)});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail||'问答失败');
    answerEl.className = 'qa-answer';
    answerEl.innerHTML = renderMarkdown(d.answer);
    highlightCode(answerEl);
    renderMath(answerEl);
    if (d.sources && d.sources.length) {
      srcEl.style.display = 'block';
      srcEl.innerHTML = '引用：' + d.sources.map(s => '<span class="src">'+s+'</span>').join('');
    }
    // 阶段5：移动端提交后自动切换到问答 Tab 展示答案
    if (window.matchMedia('(max-width:768px)').matches) {
      switchResultTab('qa');
    }
  } catch(e) {
    if(e.name==='AbortError') return;
    answerEl.className = 'qa-answer error';
    answerEl.textContent = '问答失败：' + e.message;
  }
}

// 阶段5：移动端悬浮栏提问（同步到主输入框后调用 askQuestion）
function askQuestionMobile(){
  const mobInput = document.getElementById('qa-question-mobile');
  const deskInput = document.getElementById('qa-question');
  if (mobInput && deskInput) deskInput.value = mobInput.value;
  askQuestion();
}

// 阶段5：结果区 Tab 切换
function switchResultTab(tab){
  document.querySelectorAll('#resultTabs .rtab').forEach(function(b){
    const on = (b.getAttribute('data-rtab') === tab);
    b.classList.toggle('active', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  document.querySelectorAll('.rsect').forEach(function(s){
    s.classList.toggle('active', s.getAttribute('data-rtab') === tab);
  });
}

// ════════════════════════════════════════
// 全局设置页逻辑
// ════════════════════════════════════════
const SETTINGS_KEY = 'bda_settings';

function getDefaultSettings(){
  return {
    apiKey: '',
    model: 'Qwen/Qwen3.5-9B',
    baseUrl: 'https://api.siliconflow.cn/v1',
    bookType: 'nonfiction',
    chunkSize: 2500,
    reviewers: 2,
    maxRevisions: 2
  };
}

function loadSettings(){
  try{
    const raw = localStorage.getItem(SETTINGS_KEY);
    if(raw) return Object.assign(getDefaultSettings(), JSON.parse(raw));
  }catch(e){}
  return getDefaultSettings();
}

// 阶段8：统一填充模型下拉（处理选项内 / 自定义模型两种情况）
function setModelSelectValue(model){
  const sel = document.getElementById('set-model');
  const customInput = document.getElementById('set-model-custom');
  if(!sel) return;
  // 检查模型是否在下拉选项中
  let found = false;
  for(let i=0;i<sel.options.length;i++){
    if(sel.options[i].value === model){ found = true; break; }
  }
  if(found){
    sel.value = model;
    if(customInput){ customInput.style.display='none'; customInput.value=''; }
  } else {
    // 不在选项中 → 切换到"自定义模型"并填入输入框
    sel.value = '__custom__';
    if(customInput){ customInput.style.display=''; customInput.value = model || ''; }
  }
}

// 阶段8：API Key 显示/隐藏切换
function togglePwdVisibility(inputId, btn){
  const input = document.getElementById(inputId);
  if(!input) return;
  if(input.type === 'password'){
    input.type = 'text';
    if(btn){ btn.setAttribute('aria-label','隐藏'); btn.textContent = '隐'; }
  } else {
    input.type = 'password';
    if(btn){ btn.setAttribute('aria-label','显示'); btn.textContent = '显'; }
  }
}

// 阶段1：复制 API Key 到剪贴板
function copyApiKeyToClipboard(){
  var input = document.getElementById('set-api-key');
  if(!input || !input.value.trim()){
    toastError('API Key 为空，无法复制');
    return;
  }
  try{
    input.select();
    input.setSelectionRange(0, 99999);
    if(navigator.clipboard){
      navigator.clipboard.writeText(input.value).then(function(){
        toastSuccess('API Key 已复制到剪贴板');
      }).catch(function(){
        document.execCommand('copy');
        toastSuccess('API Key 已复制');
      });
    } else {
      document.execCommand('copy');
      toastSuccess('API Key 已复制');
    }
  }catch(e){
    toastError('复制失败：' + e.message);
  }
}

// 阶段8：设置页滚动指示条更新
function updateSettingsScrollIndicator(){
  const bar = document.getElementById('settings-scroll-bar');
  if(!bar) return;
  const st = window.scrollY || document.documentElement.scrollTop;
  const sh = document.documentElement.scrollHeight - document.documentElement.clientHeight;
  const pct = sh > 0 ? Math.min(100, (st / sh) * 100) : 0;
  bar.style.width = pct + '%';
}
// 阶段8：注册滚动监听（仅在设置页可见时更新指示条）
window.addEventListener('scroll', updateSettingsScrollIndicator, {passive:true});
window.addEventListener('resize', updateSettingsScrollIndicator, {passive:true});

// 流程连贯性修复：未保存更改指示器
function initSettingsChangeTracker(){
  var settingsCard = document.querySelector('#page-settings');
  if(!settingsCard || settingsCard.dataset.changeTrackerInit) return;
  settingsCard.dataset.changeTrackerInit = '1';
  settingsCard.querySelectorAll('input, select').forEach(function(el){
    el.addEventListener('input', function(){
      var btn = document.getElementById('save-settings-btn');
      if(btn && btn.textContent.indexOf('保存中') < 0 && btn.textContent.indexOf('已保存') < 0){
        btn.style.boxShadow = '0 0 0 3px rgba(250,204,21,.4)';
        btn.dataset.dirty = '1';
      }
    });
  });
}

function saveSettings(){
  var btn=document.getElementById('save-settings-btn')||document.querySelector('button[onclick="saveSettings()"]');
  if(btn){btn.disabled=true;var origText=btn.textContent;btn.textContent='保存中…';btn.style.boxShadow='';}
  // 阶段8：模型下拉支持"自定义模型"
  var modelSel = document.getElementById('set-model');
  var modelVal = modelSel.value;
  if(modelVal === '__custom__'){
    var customVal = (document.getElementById('set-model-custom').value || '').trim();
    if(!customVal){
      toastError('请填写自定义模型名称');
      if(btn){btn.disabled=false;btn.textContent=origText||'保存设置';}
      return;
    }
    modelVal = customVal;
  }
  const s = {
    apiKey: document.getElementById('set-api-key').value.trim(),
    model: modelVal,
    baseUrl: document.getElementById('set-base-url').value.trim(),
    bookType: document.getElementById('set-book-type').value,
    chunkSize: parseInt(document.getElementById('set-chunk-size').value)||2500,
    reviewers: parseInt(document.getElementById('set-reviewers').value)||2,
    maxRevisions: parseInt(document.getElementById('set-max-revisions').value)||2
  };
  try{
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(s));
    toastSuccess('设置已保存');
    // 流程连贯性修复：按钮即时反馈（延迟恢复，避免被finally覆盖）
    if(btn){
      btn.style.background='var(--ok)';
      btn.textContent='✓ 已保存';
    }
    applySettingsToDigest();
    // 1.5秒后恢复按钮状态
    setTimeout(function(){
      if(btn){ btn.style.background=''; btn.textContent=origText||'保存设置'; btn.dataset.dirty=''; btn.style.boxShadow=''; }
    },1500);
  }catch(e){
    toastError('保存失败：'+e.message);
  }finally{
    // 仅恢复 disabled 状态，文本恢复由 setTimeout 延迟处理
    if(btn){ btn.disabled=false; }
  }
}

function applySettingsToDigest(){
  const s = loadSettings();
  const ak=document.getElementById('api_key');
  const md=document.getElementById('model');
  const bu=document.getElementById('base_url');
  const bt=document.getElementById('book_type');
  const cs=document.getElementById('chunk_size');
  const rv=document.getElementById('reviewers');
  const mr=document.getElementById('max_revisions');
  if(ak && !ak.value) ak.value=s.apiKey;
  if(md && !md.dataset.userSet) md.value=s.model;
  if(bu && !bu.dataset.userSet) bu.value=s.baseUrl;
  if(bt) bt.value=s.bookType;
  if(cs) cs.value=s.chunkSize;
  if(rv) rv.value=s.reviewers;
  if(mr) mr.value=s.maxRevisions;
}

function initSettingsPage(){
  const s = loadSettings();
  const setAK=document.getElementById('set-api-key');
  const setMD=document.getElementById('set-model');
  const setBU=document.getElementById('set-base-url');
  const setBT=document.getElementById('set-book-type');
  const setCS=document.getElementById('set-chunk-size');
  const setRV=document.getElementById('set-reviewers');
  const setMR=document.getElementById('set-max-revisions');
  if(setAK) setAK.value=s.apiKey;
  // 阶段8：模型下拉填充（若保存的模型不在选项中，则切换到"自定义模型"并填入输入框）
  setModelSelectValue(s.model);
  if(setBU) setBU.value=s.baseUrl;
  if(setBT) setBT.value=s.bookType;
  if(setCS) setCS.value=s.chunkSize;
  if(setRV) setRV.value=s.reviewers;
  if(setMR) setMR.value=s.maxRevisions;
  // 阶段8：模型下拉变化时切换自定义输入框显示
  if(setMD){
    setMD.onchange = function(){
      var customInput = document.getElementById('set-model-custom');
      if(this.value === '__custom__'){
        if(customInput){ customInput.style.display=''; customInput.focus(); }
      } else {
        if(customInput){ customInput.style.display='none'; }
      }
    };
  }
  // 预设按钮
  document.querySelectorAll('#set-presets .preset').forEach(p=>{
    p.onclick=()=>{
      document.querySelectorAll('#set-presets .preset').forEach(x=>x.classList.remove('active'));
      p.classList.add('active');
      // 阶段8：使用统一的下拉填充逻辑
      setModelSelectValue(p.dataset.model);
      document.getElementById('set-base-url').value=p.dataset.url;
    };
  });
  // 导入文件
  const impFile=document.getElementById('import-file');
  if(impFile) impFile.onchange=(e)=>{
    const f=e.target.files[0]; if(!f) return;
    const reader=new FileReader();
    reader.onload=()=>{
      try{
        const data=JSON.parse(reader.result);
        localStorage.setItem(SETTINGS_KEY, JSON.stringify(data));
        initSettingsPage();
        toastSuccess('配置已导入');
      }catch(err){ toastError('导入失败：文件格式错误'); }
    };
    reader.readAsText(f);
  };
}

function exportSettings(){
  const s = loadSettings();
  const blob = new Blob([JSON.stringify(s,null,2)], {type:'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'bda_settings.json';
  a.click();
  toastSuccess('配置已导出');
}

function importSettings(){
  const f = document.getElementById('import-file');
  if(f) f.click();
}

function clearAllData(){
  if(typeof Swal!=='undefined'){
    Swal.fire({
      title:'⚠️ 确认清除所有数据',
      html:'<b>此操作将永久删除所有本地设置和拆书历史，且不可撤销！</b><br>确定要继续吗？',
      icon:'warning',
      showCancelButton:true,
      confirmButtonColor:'#ef4444',
      confirmButtonText:'⚠️ 确认清除',
      cancelButtonText:'取消'
    }).then(r=>{
      if(r.isConfirmed){
        localStorage.clear();
        toastSuccess('所有本地数据已清除');
        setTimeout(()=>location.reload(), 1000);
      }
    });
  }else{
    if(confirm('⚠️ 确定要清除所有本地数据吗？此操作不可撤销！')){
      localStorage.clear();
      location.reload();
    }
  }
}

// ════════════════════════════════════════
// 知识库页逻辑
// ════════════════════════════════════════
const HISTORY_KEY = 'bda_history';

function loadHistory(){
  try{
    const raw = localStorage.getItem(HISTORY_KEY);
    if(raw) return JSON.parse(raw);
  }catch(e){}
  return [];
}

function saveToHistory(jobId, stats, bookTitle){
  const history = loadHistory();
  history.unshift({
    jobId, 
    title: bookTitle || '未知书籍',
    date: new Date().toLocaleString('zh-CN'),
    stats: stats || {}
  });
  // 最多保存50条
  if(history.length > 50) history.pop();
  try{ localStorage.setItem(HISTORY_KEY, JSON.stringify(history)); }catch(e){}
}

// 阶段5：相对时间格式化（近期显示"x天前"，超过7天显示日期）
function formatRelativeTime(dateStr){
  if(!dateStr) return '';
  var d = new Date(dateStr);
  if(isNaN(d.getTime())) return dateStr;
  var now = new Date();
  var diffMs = now - d;
  var diffMin = Math.floor(diffMs / 60000);
  var diffHr = Math.floor(diffMin / 60);
  var diffDay = Math.floor(diffHr / 24);
  if(diffMin < 1) return '刚刚';
  if(diffMin < 60) return diffMin + ' 分钟前';
  if(diffHr < 24) return diffHr + ' 小时前';
  if(diffDay < 7) return diffDay + ' 天前';
  return d.toLocaleString('zh-CN', {year:'numeric',month:'2-digit',day:'2-digit'});
}

function renderLibrary(){
  const list = document.getElementById('lib-list');
  if(!list) return;
  // 阶段4：骨架屏加载状态
  list.innerHTML = '<div class="skeleton-card" style="margin:8px 0"><div class="skeleton-line" style="width:70%"></div><div class="skeleton-line" style="width:40%"></div></div><div class="skeleton-card" style="margin:8px 0"><div class="skeleton-line" style="width:60%"></div><div class="skeleton-line" style="width:35%"></div></div>';
  // 从服务端加载持久化存档
  fetch('/api/archives').then(r=>r.json()).then(data=>{
    const archives = data.archives || [];
    const localHistory = loadHistory();
    // 合并：服务端存档 + 本地历史中不在服务端的条目
    const archiveNames = new Set(archives.map(a=>a.filename));
    const merged = [];
    // 1. 先添加服务端存档（持久化、可靠）
    archives.forEach(a=>{
      merged.push({
        type: 'archive',
        filename: a.filename,
        title: _cleanBookTitle(a.book_title || a.filename),
        date: new Date(a.created_at).toLocaleString('zh-CN', {year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}),
        stats: a.stats || {}
      });
    });
    // 2. 再添加本地历史中对应的服务端存档（按标题匹配）
    // 本地历史仅作为补充显示，实际数据以服务端为准
    if(merged.length === 0 && localHistory.length > 0){
      localHistory.forEach(h=>{
        merged.push({
          type: 'local',
          jobId: h.jobId,
          title: h.title,
          date: h.date,
          stats: h.stats || {}
        });
      });
    }
    if(merged.length === 0){
      list.innerHTML = '<div style="text-align:center;padding:48px 16px"><div style="font-size:48px;opacity:.3;margin-bottom:16px">📚</div><p style="color:var(--muted);font-size:.9rem;margin-bottom:16px">暂无拆书记录</p><button type="button" class="secondary" style="width:auto;padding:10px 24px;margin:0 auto" onclick="navigate(&quot;digest&quot;)">去拆书 →</button></div>';
      var sb = document.getElementById('lib-search-box');
      if(sb) sb.style.display = 'none';
      return;
    }
    // 阶段6：存储数据并显示搜索框
    _libData = merged;
    var sb = document.getElementById('lib-search-box');
    if(sb) sb.style.display = 'block';
    renderLibraryList(merged);
  }).catch(err=>{
    list.innerHTML = '<p style="color:var(--err);text-align:center;padding:20px 0">加载失败：'+err.message+'</p>';
  });
}

// 阶段6：知识库搜索过滤
var _libData = [];
function filterLibrary(query){
  if(!_libData || _libData.length === 0) return;
  var q = (query || '').toLowerCase().trim();
  if(!q){
    renderLibraryList(_libData);
    return;
  }
  var filtered = _libData.filter(function(h){
    return (h.title || '').toLowerCase().indexOf(q) >= 0;
  });
  if(filtered.length === 0){
    document.getElementById('lib-list').innerHTML = '<p style="color:var(--muted);text-align:center;padding:32px 0">未找到匹配的书籍</p>';
    return;
  }
  renderLibraryList(filtered);
}

function renderLibraryList(items){
  var list = document.getElementById('lib-list');
  if(!list) return;
  // P0修复：空状态设计
  if(!items || items.length === 0){
    list.innerHTML = '<div style="text-align:center;padding:48px 16px">'+
      '<div style="font-size:3rem;margin-bottom:16px;opacity:.4">📚</div>'+
      '<p style="color:var(--text);font-size:1.05rem;font-weight:600;margin-bottom:8px">知识库是空的</p>'+
      '<p style="color:var(--muted);font-size:.85rem;margin-bottom:24px">上传电子书，开始第一次拆书</p>'+
      '<button type="button" class="primary" style="min-height:44px;padding:10px 24px;border-radius:10px" onclick="navigate(\\\'digest\\\')">开始拆书 →</button>'+
      '</div>';
    return;
  }
  list.innerHTML = items.map(function(h,i){
      var s = h.stats || {};
      var badge = h.type === 'archive'
        ? '<span style="font-size:.7rem;color:var(--ok);border:1px solid var(--ok);padding:1px 6px;border-radius:4px;margin-left:6px">已完成</span>'
        : '<span style="font-size:.7rem;color:var(--muted);border:1px solid var(--border);padding:1px 6px;border-radius:4px;margin-left:6px">仅本地</span>';
      var openBtn = h.type === 'archive'
        ? '<button type="button" class="tool-btn lib-view-btn lib-open-btn" data-filename="'+h.filename+'" style="width:auto;padding:10px 16px;margin:0 4px 0 0;min-height:44px;border-color:rgba(124,58,237,.4);background:rgba(124,58,237,.1);color:var(--primary)">查看</button>'
        : '';
      var deleteBtn = h.type === 'archive'
        ? '<button type="button" class="tool-btn lib-del-btn" data-filename="'+h.filename+'" style="width:auto;padding:10px 14px;margin:0;background:transparent;border:1px solid var(--border);color:var(--muted);min-height:44px;min-width:44px;border-radius:10px" aria-label="删除">🗑️</button>'
        : '<button type="button" class="tool-btn lib-del-local-btn" data-idx="'+i+'" style="width:auto;padding:10px 14px;margin:0;background:transparent;border:1px solid var(--border);color:var(--muted);min-height:44px;min-width:44px;border-radius:10px" aria-label="删除">🗑️</button>';
      var titleStyle = 'margin:0 0 4px;color:var(--text);font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
      if(/^[a-z0-9\s.\-_,]+$/.test(h.title) && /[a-z]/.test(h.title)){ titleStyle += ';text-transform:capitalize'; }
      return '<div class="card lib-item" style="margin:8px 0">'+
        '<div style="display:flex;justify-content:space-between;align-items:start;gap:8px">'+
          '<div style="min-width:0;flex:1"><h3 style="'+titleStyle+'" title="'+h.title+'">'+h.title+badge+'</h3>'+
          '<small style="color:var(--muted);font-size:.8rem;opacity:.8">'+formatRelativeTime(h.date)+'</small></div>'+
          '<div style="display:flex;gap:4px;flex-shrink:0">'+openBtn+deleteBtn+'</div>'+
        '</div>'+
        '<div style="margin-top:10px;display:flex;flex-wrap:wrap;gap:4px">'+
          (s.units?'<span class="stat stat-clickable" onclick="navigate(&quot;graph&quot;)" title="从书中提取的知识点">知识单元 <b>'+s.units+'</b></span>':'')+
          (s.clusters?'<span class="stat stat-clickable" onclick="navigate(&quot;graph&quot;)" title="知识单元的分组">主题 <b>'+s.clusters+'</b></span>':'')+
          (s.cards?'<span class="stat stat-clickable" onclick="navigate(&quot;flashcards&quot;)" title="可复习的闪卡">卡片 <b>'+s.cards+'</b></span>':'')+
          (s.skills?'<span class="stat stat-clickable" onclick="navigate(&quot;flashcards&quot;)" title="实践技能包">技能 <b>'+s.skills+'</b></span>':'')+
        '</div>'+
      '</div>';
    }).join('');
}

// 事件委托：处理知识库列表中的按钮点击
document.addEventListener('click', function(e){
  var btn = e.target.closest('.lib-open-btn');
  if(btn){ openArchive(btn.dataset.filename); return; }
  btn = e.target.closest('.lib-del-btn');
  if(btn){ deleteArchive(btn.dataset.filename); return; }
  btn = e.target.closest('.lib-del-local-btn');
  if(btn){ deleteHistory(parseInt(btn.dataset.idx, 10)); return; }
});

async function openArchive(filename){
  try{
    const r = await fetch('/api/archives/'+encodeURIComponent(filename));
    if(!r.ok){ toastError('加载存档失败 (HTTP '+r.status+')'); return; }
    const d = await r.json();
    // 设置 currentJobId 为存档文件名（去掉 .bda.json 后缀）
    currentJobId = filename.replace(/\.bda\.json$/, '');
    // 标记为存档模式，后端 API 可据此从文件加载
    currentJobId = 'archive:' + currentJobId;
    // 切换到拆书结果页并渲染
    location.hash = '#digest';
    setTimeout(()=>{
      document.getElementById('result').classList.add('show');
      // 阶段5：从历史加载结果时重置 Tab 到"输出文件"
      if (typeof switchResultTab === 'function') { try{ switchResultTab('outputs'); }catch(e){} }
      const s = d.stats || {};
      // 统计数据由 renderDashboard 统一渲染到 stats-grid，无需重复设置
      // 渲染仪表盘
      if(d.stats) renderDashboard(d.stats, d);
      // 设置搜索数据
      if(d.units) setSearchData(d.units);
      if(d.units && typeof Fuse!=='undefined') initFuseSearch(d.units);
      // 渲染知识图谱
      renderInteractiveGraph(d);
      // 导入闪卡
      const added = importFlashcardsFromResult(d);
      if(added > 0) toast(added+' 张闪卡已导入复习系统', 'info');
      // 渲染 ECharts
      renderEChartsVisualizations(d);
      // 渲染数学公式
      if(typeof renderMathInElement !== 'undefined'){
        const qaAns = document.getElementById('qa-answer');
        if(qaAns) renderMath(qaAns);
      }
      toastSuccess('已加载存档：'+(d.book_title||filename));
    }, 300);
  }catch(e){
    toastError('加载存档失败：'+e.message);
  }
}

async function deleteArchive(filename){
  if(typeof Swal!=='undefined'){
    const r = await Swal.fire({
      title:'确认删除',
      text:'删除后不可恢复',
      showCancelButton:true,
      confirmButtonText:'删除',
      cancelButtonText:'取消'
    });
    if(!r.isConfirmed) return;
  }
  try{
    const resp = await fetch('/api/archives/'+encodeURIComponent(filename), {method:'DELETE'});
    if(!resp.ok){ toastError('删除失败 (HTTP '+resp.status+')'); return; }
    renderLibrary();
    toastSuccess('已删除');
  }catch(e){
    toastError('删除失败：'+e.message);
  }
}

function deleteHistory(idx){
  if(typeof Swal!=='undefined'){
    Swal.fire({
      title:'确认删除',
      text:'删除后不可恢复',
      showCancelButton:true,
      confirmButtonText:'删除',
      cancelButtonText:'取消'
    }).then(r=>{
      if(r.isConfirmed){
        const history = loadHistory();
        history.splice(idx, 1);
        localStorage.setItem(HISTORY_KEY, JSON.stringify(history));
        renderLibrary();
        toastSuccess('已删除');
      }
    });
  }
}

// ════════════════════════════════════════
// 书书对比页逻辑
// ════════════════════════════════════════
// 阶段5：交换书籍 A/B
function swapCompareBooks(){
  var selA = document.getElementById('cmp-book-a');
  var selB = document.getElementById('cmp-book-b');
  if(!selA || !selB) return;
  // 流程连贯性修复：两本书都未选时不执行交换
  if(!selA.value || !selB.value) return;
  var tmp = selA.value;
  selA.value = selB.value;
  selB.value = tmp;
  // 触发 change 事件更新按钮状态
  selA.dispatchEvent(new Event('change'));
}

function initComparePage(){
  const selA = document.getElementById('cmp-book-a');
  const selB = document.getElementById('cmp-book-b');
  if(!selA || !selB) return;
  selA.innerHTML = '<option value="">加载中…</option>';
  selB.innerHTML = '<option value="">加载中…</option>';
  // 阶段9：选择变更时更新按钮状态
  function updateCmpBtn(){
    var btn = document.getElementById('cmp-btn');
    var hint = document.getElementById('cmp-hint');
    var swapBtn = document.getElementById('cmp-swap-btn');
    if(!btn) return;
    var ready = selA.value && selB.value && selA.value !== selB.value;
    btn.disabled = !ready;
    // 流程连贯性修复：交换按钮仅在两本书都已选择时启用
    if(swapBtn){
      var bothSelected = selA.value && selB.value;
      swapBtn.disabled = !bothSelected;
      swapBtn.style.opacity = bothSelected ? '1' : '.4';
      swapBtn.style.cursor = bothSelected ? 'pointer' : 'not-allowed';
    }
    if(ready){
      btn.style.opacity = '1';
      btn.style.cursor = 'pointer';
      btn.style.background = '';
      btn.style.color = '';
      btn.style.border = '';
      if(hint){ hint.textContent = '✓ 已选择两本书，点击开始对比'; hint.style.color = 'var(--ok)'; }
    } else {
      btn.style.opacity = '.55';
      btn.style.cursor = 'not-allowed';
      btn.style.background = 'var(--input-bg)';
      btn.style.color = 'var(--muted)';
      btn.style.border = '1px dashed var(--border-strong)';
      if(hint){
        if(!selA.value || !selB.value){
          hint.textContent = '请先选择两本不同的书籍';
        } else {
          hint.textContent = '请选择两本不同的书籍（当前选择相同）';
        }
        hint.style.color = 'var(--muted)';
      }
    }
  }
  selA.addEventListener('change', updateCmpBtn);
  selB.addEventListener('change', updateCmpBtn);
  // 从服务端加载存档列表
  fetch('/api/archives').then(r=>r.json()).then(data=>{
    const archives = data.archives || [];
    // 流程连贯性修复：截断过长标题，日期用短格式，避免下拉框截断
    const opts = '<option value="">请选择…</option>' + 
      archives.map(function(a){
        var title = _cleanBookTitle(a.book_title || a.filename || '未知');
        var dateStr = new Date(a.created_at).toLocaleDateString('zh-CN',{month:'2-digit',day:'2-digit'});
        var displayTitle = title.length > 20 ? title.substring(0,18)+'…' : title;
        return '<option value="'+a.filename+'" title="'+title+'">'+displayTitle+' ('+dateStr+')</option>';
      }).join('');
    selA.innerHTML = opts;
    selB.innerHTML = opts;
  }).catch(()=>{
    selA.innerHTML = '<option value="">加载失败</option>';
    selB.innerHTML = '<option value="">加载失败</option>';
  });
}

async function runCompare(){
  const btn = document.getElementById('cmp-btn');
  if(btn && btn.disabled) return;
  const fnA = document.getElementById('cmp-book-a').value;
  const fnB = document.getElementById('cmp-book-b').value;
  if(!fnA || !fnB){ toastError('请选择两本书'); return; }
  if(fnA === fnB){ toastError('请选择不同的两本书'); return; }
  try{
    const [rA, rB] = await Promise.all([
      fetch('/api/archives/'+encodeURIComponent(fnA)).then(r=>r.json()),
      fetch('/api/archives/'+encodeURIComponent(fnB)).then(r=>r.json())
    ]);
    renderCompareResult(rA, rB);
  }catch(e){
    toastError('加载对比数据失败：'+e.message);
  }
}

function renderCompareResult(a, b){
  const sa = a.stats || {}, sb = b.stats || {};
  const result = document.getElementById('cmp-result');
  result.style.display = 'block';
  result.innerHTML = 
    '<div class="card">'+
      '<h3>对比结果</h3>'+
      '<table style="width:100%;border-collapse:collapse">'+
        '<tr><th style="text-align:left;padding:8px;border:1px solid var(--border);background:var(--input-bg)">指标</th>'+
        '<th style="text-align:center;padding:8px;border:1px solid var(--border);background:var(--input-bg)">'+(a.book_title||'书籍A')+'</th>'+
        '<th style="text-align:center;padding:8px;border:1px solid var(--border);background:var(--input-bg)">'+(b.book_title||'书籍B')+'</th>'+
        '<th style="text-align:center;padding:8px;border:1px solid var(--border);background:var(--input-bg)">差异</th></tr>'+
        compareRow('知识单元数', sa.units, sb.units)+
        compareRow('主题数', sa.clusters, sb.clusters)+
        compareRow('卡片数', sa.cards, sb.cards)+
        compareRow('技能数', sa.skills, sb.skills)+
      '</table>'+
      '<p style="margin-top:14px;color:var(--muted);font-size:.85rem">注：详细知识结构对比需要两本书的存档文件，当前仅展示统计数据对比。</p>'+
    '</div>';
  toastSuccess('对比完成');
}

function compareRow(label, va, vb){
  const diff = (va||0) - (vb||0);
  const diffStr = diff>0 ? '+'+diff : String(diff);
  const diffColor = diff>0 ? 'var(--ok)' : (diff<0 ? 'var(--err)' : 'var(--muted)');
  return '<tr><td style="padding:8px;border:1px solid var(--border)">'+label+'</td>'+
    '<td style="text-align:center;padding:8px;border:1px solid var(--border)">'+(va||0)+'</td>'+
    '<td style="text-align:center;padding:8px;border:1px solid var(--border)">'+(vb||0)+'</td>'+
    '<td style="text-align:center;padding:8px;border:1px solid var(--border);color:'+diffColor+';font-weight:700">'+diffStr+'</td></tr>';
}

// ════════════════════════════════════════
// 路由增强：页面切换时初始化
// ════════════════════════════════════════
function onRouteChange(){
  const hash = location.hash.replace('#','');
  const page = hash || 'home';
  if(page==='settings') initSettingsPage();
  if(page==='library') renderLibrary();
  if(page==='compare') initComparePage();
  if(page==='graph') {
    // 如果已有图谱数据，重新渲染（解决页面不可见时 canvas 未创建的问题）
    if(currentGraphData){
      renderInteractiveGraph({graph: currentGraphData});
      // 重新渲染 echarts 可视化（词云、雷达图）
      if(currentResultData){
        // 先重置容器状态
        var wcWrap = document.getElementById('echarts-wordcloud-wrap');
        var radarWrap = document.getElementById('echarts-radar-wrap');
        if(wcWrap) wcWrap.style.display = 'none';
        if(radarWrap) radarWrap.style.display = 'none';
        // 重新渲染
        renderEChartsVisualizations(currentResultData);
      }
    } else if(visNetworkInstance){
      visNetworkInstance.redraw();
    }
  }
  if(page==='flashcards') initFlashcardsPage();
}
// 在原始 router 之后执行页面初始化
const _origRouter2 = router;
router = function(){
  _origRouter2();
  onRouteChange();
};
// 重新绑定 hashchange 事件到新 router
window.removeEventListener('hashchange', _origRouter2);
window.addEventListener('hashchange', router);

// ════════════════════════════════════════
// 初始化
// ════════════════════════════════════════
renderFeatures();
router();
// 应用全局设置到拆书页
applySettingsToDigest();
// #100：返回顶部按钮滚动显隐
window.addEventListener('scroll',function(){var b=document.getElementById('back-to-top');if(b){b.style.display=window.scrollY>300?'flex':'none'}});

// ════════════════════════════════════════
// 阶段A-D：增强功能
// ════════════════════════════════════════

// ── 主题切换 ──
// ── 搜索防抖 ──
let _searchTimer = null;
function debounceSearch(query){
  clearTimeout(_searchTimer);
  // 立即显示加载状态
  const results = document.getElementById('search-results');
  if(query.length >= 2 && results){
    results.innerHTML = '<div class="skeleton-line"></div><div class="skeleton-line"></div><div class="skeleton-line"></div>';
  }
  _searchTimer = setTimeout(() => searchUnits(query), 300);
}

// ── 全局错误边界 ──
window.addEventListener('error', function(e){
  // 阶段FF: 跳过资源加载错误（由 checkCdnLibraries 处理）
  if(e.target && (e.target.tagName === 'SCRIPT' || e.target.tagName === 'LINK')){
    console.warn('Resource failed to load:', e.target.src || e.target.href);
    return true;
  }
  console.error('Global error:', e.error || e.message);
  if(typeof toastError === 'function' && e.message){
    toastError('页面发生错误: ' + e.message.substring(0, 80));
  }
  return true;
}, true); // 阶段FF: 使用捕获阶段以捕获资源加载错误
window.addEventListener('unhandledrejection', function(e){
  console.error('Unhandled promise rejection:', e.reason);
  if(typeof toastError === 'function' && e.reason){
    var msg = e.reason.message || (typeof e.reason === 'string' ? e.reason : JSON.stringify(e.reason, Object.keys(e.reason||{})).substring(0,100));
    toastError('异步操作失败: ' + msg.substring(0, 100));
  }
});

// ── 加载骨架屏 CSS ──
const skeletonStyle = document.createElement('style');
skeletonStyle.textContent = `
.skeleton-line{height:14px;margin:8px 0;border-radius:6px;background:linear-gradient(90deg,var(--skeleton1) 25%,var(--skeleton2) 50%,var(--skeleton1) 75%);background-size:200% 100%;animation:skeleton-shimmer 1.5s infinite}
.skeleton-card{height:80px;border-radius:12px;background:linear-gradient(90deg,var(--skeleton1) 25%,var(--skeleton2) 50%,var(--skeleton1) 75%);background-size:200% 100%;animation:skeleton-shimmer 1.5s infinite;margin:10px 0}
@keyframes skeleton-shimmer{0%{background-position:200% 0}100%{background-position:-200% 0}}
`;
document.head.appendChild(skeletonStyle);

// ── Service Worker 注册 ──
if('serviceWorker' in navigator){
  navigator.serviceWorker.register('/sw.js').then(function(reg){
    console.log('Service Worker registered:', reg.scope);
  }).catch(function(err){
    console.warn('Service Worker registration failed:', err);
  });
}

// ── 离线状态指示器 ──
var offlineBanner = document.createElement('div');
offlineBanner.id = 'offline-banner';
offlineBanner.style.cssText = 'position:fixed;bottom:0;left:0;right:0;background:#dc2626;color:white;padding:8px;text-align:center;font-size:.85rem;z-index:9999;display:none';
offlineBanner.textContent = '当前处于离线模式，部分功能可能不可用';
document.body.appendChild(offlineBanner);

window.addEventListener('online', function(){
  offlineBanner.style.display = 'none';
});
window.addEventListener('offline', function(){
  offlineBanner.style.display = 'block';
});

// ── 阶段FF: CDN 脚本加载容错检测 ──
var _cdnErrorBanner = document.createElement('div');
_cdnErrorBanner.className = 'cdn-error-banner';
var _cdnCloseBtn = document.createElement('button');
_cdnCloseBtn.className = 'cdn-close';
_cdnCloseBtn.textContent = '关闭';
_cdnCloseBtn.onclick = function(){ _cdnErrorBanner.classList.remove('show'); };
_cdnErrorBanner.innerHTML = '<span class="cdn-msg"></span>';
_cdnErrorBanner.appendChild(_cdnCloseBtn);
document.body.appendChild(_cdnErrorBanner);

function checkCdnLibraries(){
  var missing = [];
  if(typeof marked === 'undefined') missing.push('Markdown 渲染');
  if(typeof DOMPurify === 'undefined') missing.push('HTML 消毒');
  if(typeof Prism === 'undefined') missing.push('代码高亮');
  if(typeof FilePond === 'undefined') missing.push('文件上传');
  if(typeof Chart === 'undefined') missing.push('统计图表');
  if(typeof vis === 'undefined') missing.push('知识图谱');
  if(typeof echarts === 'undefined') missing.push('高级可视化');
  if(typeof katex === 'undefined') missing.push('数学公式');
  if(typeof mermaid === 'undefined') missing.push('流程图');
  if(typeof Swal === 'undefined') missing.push('弹窗组件');
  if(typeof fuse === 'undefined' && typeof Fuse === 'undefined') missing.push('全文搜索');
  if(typeof introJs === 'undefined') missing.push('用户引导');
  if(typeof TomSelect === 'undefined') missing.push('下拉增强');

  if(missing.length > 0){
    var msg = missing.length <= 3
      ? '部分功能不可用：' + missing.join('、') + '。请检查网络连接后刷新页面。'
      : '多个组件加载失败（' + missing.length + '个），可能是网络问题。请检查网络后刷新页面。';
    var msgEl = _cdnErrorBanner.querySelector('.cdn-msg');
    if(msgEl) msgEl.textContent = msg;
    _cdnErrorBanner.classList.add('show');
    console.warn('CDN libraries missing:', missing);
  }
}
// 延迟检测，等待所有 defer 脚本加载完成
setTimeout(checkCdnLibraries, 3000);

function toggleTheme(){
  const lightStyle = document.getElementById('theme-light');
  const iconDark = document.getElementById('theme-icon-dark');
  const iconLight = document.getElementById('theme-icon-light');
  const toggleBtn = document.getElementById('themeToggle');
  // Stage R 修复：使用 .disabled 属性而非 setAttribute（浏览器以属性为准）
  const isLight = !lightStyle.disabled;
  if(isLight){
    lightStyle.disabled = true;
    document.documentElement.setAttribute('data-theme','dark');
    iconDark.style.display='block';
    iconLight.style.display='none';
    localStorage.setItem('bda_theme','dark');
    // 阶段4：动态更新 aria-label 和 title 明确当前状态
    if(toggleBtn){ toggleBtn.setAttribute('aria-label','当前：暗色模式，点击切换到亮色'); toggleBtn.title='当前：暗色模式 | 点击切换'; }
  }else{
    lightStyle.disabled = false;
    document.documentElement.setAttribute('data-theme','light');
    iconDark.style.display='none';
    iconLight.style.display='block';
    localStorage.setItem('bda_theme','light');
    if(toggleBtn){ toggleBtn.setAttribute('aria-label','当前：亮色模式，点击切换到暗色'); toggleBtn.title='当前：亮色模式 | 点击切换'; }
  }
}
// 初始状态修正：HTML disabled 属性不等于 DOM disabled 属性，需在 JS 中同步
document.getElementById('theme-light').disabled = true;
// ── 阶段GG: 暗色模式自动检测（prefers-color-scheme）──
var _savedTheme = localStorage.getItem('bda_theme');
// 立即同步 data-theme 属性（不等 setTimeout，确保 intro.js 等组件在首次渲染时就匹配正确主题）
var _mqLightInit = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)');
if(_savedTheme === 'light' || (!_savedTheme && _mqLightInit && _mqLightInit.matches)){
  document.documentElement.setAttribute('data-theme','light');
} else {
  document.documentElement.setAttribute('data-theme','dark');
}
var _mqDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)');
var _mqLight = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)');
if(_savedTheme === 'light'){
  // 用户手动选择了浅色
  setTimeout(()=>toggleTheme(),100);
} else if(_savedTheme === 'dark'){
  // 用户手动选择了暗色，保持默认（暗色）
} else {
  // 无手动偏好，跟随系统
  if(_mqLight && _mqLight.matches){
    setTimeout(()=>toggleTheme(),100);
  }
  // 监听系统主题变化（仅当用户未手动选择时）
  if(_mqDark){
    _mqDark.addEventListener('change', function(e){
      if(!localStorage.getItem('bda_theme')){
        if(!e.matches){
          // 系统切换到浅色
          var lightStyle = document.getElementById('theme-light');
          if(lightStyle.disabled){ toggleTheme(); }
        } else {
          // 系统切换到暗色
          var lightStyle = document.getElementById('theme-light');
          if(!lightStyle.disabled){ toggleTheme(); }
        }
      }
    });
  }
}

// ── Mermaid.js 初始化 ──
if(typeof mermaid!=='undefined'){
  mermaid.initialize({startOnLoad:false,theme:'dark',securityLevel:'loose'});
}

// ── 统计仪表盘渲染 ──
let currentStats = null;
var chartClusters = null; // 阶段DD: var 避免 TDZ 错误
var chartChapters = null;

function renderDashboard(stats, result){
  if(!stats) return;
  currentStats = stats;
  document.getElementById('dashboard').style.display='block';
  document.getElementById('search-section').style.display='block';
  document.getElementById('export-extras').style.display='grid';

  // 统计卡片
  const grid = document.getElementById('stats-grid');
  const cards = [
    {num:stats.units||0,label:'知识单元'},
    {num:stats.clusters||0,label:'主题聚类'},
    {num:stats.cards||0,label:'可视化卡片'},
    {num:stats.skills||0,label:'技能要点'},
  ];
  grid.innerHTML = cards.map(c=>
    '<div class="stat-card"><div class="num">'+c.num+'</div><div class="label">'+c.label+'</div></div>'
  ).join('');

  // 图表：主题分布饼图
  if(result && result.graph && result.graph.clusters){
    const clusterData = Object.entries(result.graph.clusters).map(([k,v])=>({
      name:k, count:Array.isArray(v)?v.length:1
    }));
    if(chartClusters) chartClusters.destroy();
    chartClusters = new Chart(document.getElementById('chart-clusters'),{
      type:'doughnut',
      data:{
        labels:clusterData.map(d=>d.name),
        datasets:[{
          data:clusterData.map(d=>d.count),
          backgroundColor:['#7c3aed','#ec4899','#10b981','#f59e0b','#3b82f6','#ef4444','#8b5cf6','#06b6d4']
        }]
      },
      options:{
        responsive:true,maintainAspectRatio:false,
        plugins:{legend:{position:'right',labels:{color:getComputedStyle(document.body).color,font:{size:11}}}},
        title:{display:true,text:'主题聚类分布',color:'#a78bfa'}
      }
    });
  }

  // 图表：章节单元数柱状图
  if(result && result.units){
    const chapterMap = {};
    result.units.forEach(u=>{
      const ch = u.chapter || '未知章节';
      chapterMap[ch] = (chapterMap[ch]||0)+1;
    });
    const labels = Object.keys(chapterMap);
    const data = Object.values(chapterMap);
    if(chartChapters) chartChapters.destroy();
    chartChapters = new Chart(document.getElementById('chart-chapters'),{
      type:'bar',
      data:{
        labels:labels,
        datasets:[{
          label:'知识单元数',
          data:data,
          backgroundColor:'rgba(124,58,237,.5)',
          borderColor:'#7c3aed',
          borderWidth:1
        }]
      },
      options:{
        responsive:true,maintainAspectRatio:false,
        plugins:{legend:{display:false}},
        scales:{
          x:{ticks:{color:'#9ca3af',font:{size:10}}},
          y:{ticks:{color:'#9ca3af'},beginAtZero:true}
        }
      }
    });
  }
}

// ── 全文搜索 ──
let searchUnitsData = [];
function setSearchData(units){
  searchUnitsData = units || [];
}
var _searchLimit = 20; // 动态加载的条目数
function searchUnits(query){
  const results = document.getElementById('search-results');
  if(!query || query.length<2){
    results.innerHTML='';
    _searchLimit = 20;
    return;
  }
  const q = query.toLowerCase();
  const allMatched = searchUnitsData.filter(u=>{
    const text = (u.id||'')+' '+(u.content||'')+' '+(u.chapter||'');
    return text.toLowerCase().includes(q);
  });
  _searchLimit = 20; // 重置
  const matched = allMatched.slice(0, _searchLimit);
  if(allMatched.length===0){
    results.innerHTML='<p style="color:var(--muted);padding:12px">未找到匹配的知识单元</p>';
    return;
  }
  var html = matched.map(u=>{
    let snippet = (u.content||'').substring(0,120);
    const idx = snippet.toLowerCase().indexOf(q);
    if(idx>=0){
      snippet = snippet.substring(0,idx)+'<mark>'+snippet.substring(idx,idx+query.length)+'</mark>'+snippet.substring(idx+query.length);
    }
    const chTag = u.chapter ? '<small style="color:#a5b4fc">['+u.chapter+']</small> ' : '';
    return '<div class="search-result-item"><strong>'+chTag+(u.id||'无ID')+'</strong><br><small style="color:var(--muted)">'+snippet+'…</small></div>';
  }).join('');
  // 显示总数和加载更多
  html += '<div style="padding:8px 12px;color:var(--muted);font-size:.8rem;border-top:1px solid var(--border);margin-top:4px">显示 '+matched.length+' / '+allMatched.length+' 条结果</div>';
  if(allMatched.length > _searchLimit){
    html += '<button type="button" id="load-more-btn" data-query="'+query.replace(/"/g,'&quot;')+'" style="width:100%;padding:10px;background:var(--card);border:1px solid var(--border);color:var(--accent);cursor:pointer;border-radius:8px;margin-top:4px">加载更多 ('+(allMatched.length-_searchLimit)+' 条)</button>';
  }
  results.innerHTML = html;
  var loadMoreBtn = document.getElementById('load-more-btn');
  if(loadMoreBtn){
    loadMoreBtn.onclick = function(){
      loadMoreSearch(this.getAttribute('data-query'));
    };
  }
}

function loadMoreSearch(query){
  const q = query.toLowerCase();
  const allMatched = searchUnitsData.filter(u=>{
    const text = (u.id||'')+' '+(u.content||'')+' '+(u.chapter||'');
    return text.toLowerCase().includes(q);
  });
  _searchLimit += 20;
  const matched = allMatched.slice(0, _searchLimit);
  const results = document.getElementById('search-results');
  var html = matched.map(u=>{
    let snippet = (u.content||'').substring(0,120);
    const idx = snippet.toLowerCase().indexOf(q);
    if(idx>=0){
      snippet = snippet.substring(0,idx)+'<mark>'+snippet.substring(idx,idx+query.length)+'</mark>'+snippet.substring(idx+query.length);
    }
    const chTag = u.chapter ? '<small style="color:#a5b4fc">['+u.chapter+']</small> ' : '';
    return '<div class="search-result-item"><strong>'+chTag+(u.id||'无ID')+'</strong><br><small style="color:var(--muted)">'+snippet+'…</small></div>';
  }).join('');
  html += '<div style="padding:8px 12px;color:var(--muted);font-size:.8rem;border-top:1px solid var(--border);margin-top:4px">显示 '+matched.length+' / '+allMatched.length+' 条结果</div>';
  if(allMatched.length > _searchLimit){
    html += '<button type="button" id="load-more-btn" data-query="'+query.replace(/"/g,'&quot;')+'" style="width:100%;padding:10px;background:var(--card);border:1px solid var(--border);color:var(--accent);cursor:pointer;border-radius:8px;margin-top:4px">加载更多 ('+(allMatched.length-_searchLimit)+' 条)</button>';
  }
  results.innerHTML = html;
  var loadMoreBtn2 = document.getElementById('load-more-btn');
  if(loadMoreBtn2){
    loadMoreBtn2.onclick = function(){
      loadMoreSearch(this.getAttribute('data-query'));
    };
  }
}

// ── 扩展导出功能 ──
function exportCSV(){
  if(!searchUnitsData.length){toastError('无数据可导出');return}
  const headers = ['ID','内容','章节','标签'];
  const rows = searchUnitsData.map(u=>[
    '"'+(u.id||'').replace(/"/g,'""')+'"',
    '"'+(u.content||'').replace(/"/g,'""')+'"',
    '"'+(u.chapter||'').replace(/"/g,'""')+'"',
    '"'+((u.tags||[]).join('; '))+'"'
  ]);
  const csv = '\uFEFF'+headers.join(',')+'\\n'+rows.map(r=>r.join(',')).join('\\n');
  const blob = new Blob([csv],{type:'text/csv;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'knowledge_units.csv';
  a.click();
  toastSuccess('CSV 已导出');
}

function exportNotion(){
  if(!searchUnitsData.length){toastError('无数据可导出');return}
  let md = '# 拆书知识单元\\n\\n';
  md += '> 导出时间：'+new Date().toLocaleString('zh-CN')+'\\n\\n---\\n\\n';
  searchUnitsData.forEach((u,i)=>{
    md += '## '+(i+1)+'. '+(u.id||'无ID')+'\\n\\n';
    if(u.chapter) md += '**章节**: '+u.chapter+'\\n\\n';
    if(u.content) md += u.content+'\\n\\n';
    if(u.tags && u.tags.length) md += '**标签**: '+u.tags.map(t=>'`'+t+'`').join(' ')+'\\n\\n';
    md += '---\\n\\n';
  });
  const blob = new Blob([md],{type:'text/markdown;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'notion_export.md';
  a.click();
  toastSuccess('Notion Markdown 已导出');
}

function exportMermaid(){
  if(!currentStats){toastError('无数据可导出');return}
  let graph = 'graph TD\\n';
  graph += 'A[全书总览] --> B[知识单元: '+currentStats.units+']\\n';
  graph += 'A --> C[主题聚类: '+currentStats.clusters+']\\n';
  graph += 'A --> D[可视化卡片: '+currentStats.cards+']\\n';
  graph += 'A --> E[技能要点: '+currentStats.skills+']\\n';
  graph += 'B --> F[对抗式总结]\\n';
  graph += 'C --> G[知识图谱]\\n';
  graph += 'D --> H[记忆卡片]\\n';
  graph += 'E --> I[技能包]\\n';
  graph += 'F --> J[综合报告]\\n';
  graph += 'G --> J\\n';
  graph += 'H --> J\\n';
  graph += 'I --> J\\n';
  // 用新窗口展示
  const w = window.open('','_blank');
  w.document.write('<html><head><title>Mermaid 图表</title><script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"><\\/script><style>body{background:#0f0f1a;color:#e5e7eb;font-family:sans-serif;padding:40px}.mermaid{display:flex;justify-content:center}</style></head><body><h2 style="color:#a78bfa">知识结构流程图</h2><div class="mermaid">'+graph+'</div><script>mermaid.initialize({theme:"dark"});mermaid.run();<\\/script></body></html>');
  toastSuccess('Mermaid 图表已生成');
}

// ── 键盘快捷键 ──
document.addEventListener('keydown',function(e){
  // Ctrl/Cmd + K: 搜索聚焦
  if((e.ctrlKey||e.metaKey)&&e.key==='k'){
    e.preventDefault();
    const si = document.getElementById('search-input');
    if(si && si.offsetParent!==null){ si.focus(); }
  }
  // Ctrl/Cmd + S: 保存设置（在设置页时）
  if((e.ctrlKey||e.metaKey)&&e.key==='s'){
    const settingsPage = document.getElementById('page-settings');
    if(settingsPage && settingsPage.classList.contains('active')){
      e.preventDefault();
      saveSettings();
    }
  }
  // Esc: 返回主页
  if(e.key==='Escape'){
    if(location.hash&&location.hash!=='#home'){
      navigate('');
    }
  }
});

// ── 显示键盘提示（上下文感知） ──
const kbdHint = document.createElement('div');
kbdHint.className = 'kbd-hint';
document.body.appendChild(kbdHint);
function updateKbdHint(){
  const hash = location.hash || '#home';
  const parts = [];
  const isMobile = window.matchMedia('(max-width:768px)').matches;
  // 搜索提示仅在拆书结果页（有 search-input 且可见时）显示
  const si = document.getElementById('search-input');
  if(si && si.offsetParent !== null){
    parts.push('<kbd>Ctrl+K</kbd> 搜索');
  }
  // 返回提示在非首页显示（手机端用 ← 符号替代 Esc）
  if(hash !== '#home' && hash !== '#'){
    if(isMobile){
      parts.push('<span class="back-hint-mobile">&#8592; 返回</span>');
    }else{
      parts.push('<kbd>Esc</kbd> 返回');
    }
  }
  kbdHint.innerHTML = parts.join(' · ');
  // 阶段2：手机端 kbd-hint 已通过 CSS display:none 隐藏，这里保留逻辑以便桌面端
  kbdHint.style.display = (parts.length > 0 && !isMobile) ? '' : 'none';
}
updateKbdHint();
// 路由变化时更新提示
window.addEventListener('hashchange', updateKbdHint);
// 页面加载后延迟检查一次（等 DOM 完全渲染）
setTimeout(updateKbdHint, 500);
// 阶段2：屏幕尺寸变化时重新计算提示（手机/桌面切换）
window.addEventListener('resize', updateKbdHint);

// ── Stage Z: 移动端输入框焦点滚动处理 ──
// 当输入框获得焦点时，确保不被键盘遮挡
if('ontouchstart' in window || navigator.maxTouchPoints > 0){
  document.addEventListener('focusin', function(e){
    var el = e.target;
    if(el && (el.tagName==='INPUT' || el.tagName==='TEXTAREA' || el.tagName==='SELECT')){
      // 延迟执行，等待虚拟键盘弹出
      setTimeout(function(){
        var rect = el.getBoundingClientRect();
        var viewportH = window.innerHeight;
        // 如果元素底部距视口底部小于 200px，可能被键盘遮挡
        if(rect.bottom > viewportH - 200){
          el.scrollIntoView({behavior: _reducedMotion ? 'auto' : 'smooth', block:'center'});
        }
      }, 300);
    }
  }, {passive: true});
}

// ════════════════════════════════════════
// 阶段E：交互式知识图谱 (vis-network)
// ════════════════════════════════════════
var visNetworkInstance = null; // 阶段DD: var 避免 TDZ 错误
var currentGraphData = null;
var currentResultData = null; // 保存当前结果数据，用于 echarts 延迟渲染
var currentGraphNodes = []; // 阶段6：当前图谱节点（供筛选使用）
var currentGraphEdges = []; // 阶段6：当前图谱边
var currentGroupColors = {}; // 阶段6：聚类颜色映射（供图例使用）

// 阶段1：进入图谱页时自动加载最近归档数据
// 阶段4：添加加载状态动画
async function autoLoadGraph(){
  var wrapper = document.getElementById('graph-wrapper');
  var empty = document.getElementById('graph-empty');
  // 如果已有数据渲染，不重复加载
  if(wrapper && wrapper.style.display !== 'none') return;
  // 显示加载骨架屏
  if(empty){
    empty.innerHTML = '<div style="text-align:center;padding:48px 16px">'+
      '<div style="width:48px;height:48px;border:3px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:graphSpin 1s linear infinite;margin:0 auto 16px"></div>'+
      '<p style="color:var(--muted);font-size:.88rem">正在加载知识图谱…</p>'+
      '</div>';
  }
  try{
    var r = await fetch('/api/archives');
    var d = await r.json();
    if(d.archives && d.archives.length > 0){
      var name = d.archives[0].filename || d.archives[0];
      if(typeof openArchive === 'function'){
        await openArchive(name);
      }
    } else {
      // 恢复空状态
      if(empty){
        empty.innerHTML = '<div style="font-size:48px;opacity:.3;margin-bottom:16px">🕸️</div>'+
          '<p style="color:var(--text);font-size:1.05rem;font-weight:600;margin-bottom:8px">暂无图谱</p>'+
          '<p style="color:var(--muted);font-size:.9rem;margin-bottom:16px">拆书后可在此查看知识关联</p>'+
          '<button type="button" class="secondary" style="width:auto;padding:10px 24px;margin:0 auto" onclick="navigate(&quot;digest&quot;)">去拆书 →</button>';
      }
    }
  }catch(e){
    // 恢复空状态
    if(empty){
      empty.innerHTML = '<div style="font-size:48px;opacity:.3;margin-bottom:16px">🕸️</div>'+
        '<p style="color:var(--muted);font-size:.9rem;margin-bottom:16px">加载失败，请稍后重试</p>'+
        '<button type="button" class="secondary" style="width:auto;padding:10px 24px;margin:0 auto" onclick="autoLoadGraph()">重试</button>';
    }
  }
}

function renderInteractiveGraph(result){
  if(!result || !result.graph) return;
  const graph = result.graph;
  currentGraphData = graph;
  currentResultData = result; // 保存完整结果数据用于 echarts 渲染
  
  // 如果有 nodes/edges 数据
  let nodes = [];
  let edges = [];
  let groupColors = {}; // 阶段6：提升到函数作用域，供图例渲染使用
  
  if(graph.nodes && graph.nodes.length > 0){
    // 使用后端提供的节点
    let colorIdx = 0;
    const palette = ['#7c3aed','#ec4899','#10b981','#f59e0b','#3b82f6','#ef4444','#8b5cf6','#06b6d4'];
    nodes = graph.nodes.map(n => {
      if(n.group && !groupColors[n.group]){
        groupColors[n.group] = palette[colorIdx % palette.length];
        colorIdx++;
      }
      return {
        id: n.id,
        label: n.label || n.id,
        group: n.group || '',
        size: n.size || 20,
        color: groupColors[n.group] || '#7c3aed',
        font: {color:themeChartText(), size:12, face:'sans-serif'},
        borderWidth: 1,
        borderColor: themeChartBorder()
      };
    });
    if(graph.edges){
      edges = graph.edges.map(e => ({
        from: e.from || e.source,
        to: e.to || e.target,
        width: Math.max(1, (e.weight||1) * 2),
        color: {color:'rgba(124,58,237,.45)', highlight:'#7c3aed', opacity:0.6},
        title: e.relation || ''
      }));
    }
  } else if(graph.clusters && Object.keys(graph.clusters).length > 0){
    // 降级：从 clusters 构建节点
    const clusterColors = ['#7c3aed','#ec4899','#10b981','#f59e0b','#3b82f6','#ef4444'];
    let ci = 0;
    Object.entries(graph.clusters).forEach(([clusterName, unitIds]) => {
      const color = clusterColors[ci % clusterColors.length];
      groupColors[clusterName] = color; // 阶段6：记录聚类颜色供图例使用
      ci++;
      // 聚类节点
      nodes.push({
        id: 'cluster_'+clusterName,
        label: clusterName + ' ('+(Array.isArray(unitIds)?unitIds.length:1)+')',
        group: clusterName,
        size: 30 + (Array.isArray(unitIds)?unitIds.length:1) * 5,
        color: color,
        font: {color:themeChartText(), size:13, face:'sans-serif'},
        borderWidth: 2,
        borderColor: themeChartBorder()
      });
      // 单元节点（最多取前5个展示）
      if(Array.isArray(unitIds)){
        unitIds.slice(0,5).forEach(uid => {
          const nodeId = 'unit_'+uid;
          nodes.push({
            id: nodeId,
            label: String(uid).substring(0,20),
            group: clusterName,
            size: 15,
            color: color,
            font: {color:themeChartMuted(), size:12, face:'sans-serif'},
            borderWidth: 1,
            borderColor: themeChartBorder()
          });
          edges.push({
            from: 'cluster_'+clusterName,
            to: nodeId,
            color: {color:'rgba(124,58,237,.5)', highlight:'#7c3aed', opacity:0.7},
            width: 2
          });
        });
      }
    });
  }
  
  if(nodes.length === 0) return;
  
  // 显示图谱容器
  const wrapper = document.getElementById('graph-wrapper');
  const empty = document.getElementById('graph-empty');
  if(empty) empty.style.display = 'none';
  if(wrapper) wrapper.style.display = 'block';
  
  // 销毁旧实例
  if(visNetworkInstance){
    visNetworkInstance.destroy();
    visNetworkInstance = null;
  }
  
  const container = document.getElementById('vis-network');
  if(!container) return;
  
  // P0修复：等待容器尺寸计算完成后再创建vis.Network，避免0x0渲染失败
  function _createNetwork(){
    var cw = container.clientWidth, ch = container.clientHeight;
    if(cw === 0 || ch === 0){
      // 容器尺寸未就绪，强制设置最小高度并重试
      container.style.minHeight = '400px';
      requestAnimationFrame(_createNetwork);
      return;
    }
  const data = {
    nodes: new vis.DataSet(nodes),
    edges: new vis.DataSet(edges)
  };
  
  const options = {
    nodes: {
      shape: 'dot',
      scaling: {min:15, max:40},
      font: {
        color: themeChartText(),
        size: 14,
        face: 'sans-serif',
        multi: 'html',
        vadjust: 0,
        strokeWidth: 4,
        strokeColor: themeChartBorder()
      },
      margin: 10,
      // P0修复：确保标签始终可见，不受scaling影响
      labelHighlightBold: true
    },
    edges: {
      smooth: {type:'continuous', roundness:0.5},
      // P0修复：提高边可见性 - 从0.5透明度提升至0.8
      color: {color:'rgba(124,58,237,.7)', highlight:'#7c3aed', hover:'#a78bfa', opacity:0.8},
      width: 2,
      selectionWidth: 3
    },
    physics: {
      barnesHut: {
        gravitationalConstant: -3000,
        centralGravity: 0.3,
        springLength: 95,
        springConstant: 0.04,
        damping: 0.09
      },
      stabilization: {iterations: 150}
    },
    interaction: {
      hover: true,
      tooltipDelay: 200,
      zoomView: true,
      dragView: true,
      navigationButtons: false
    }
  };
  
  visNetworkInstance = new vis.Network(container, data, options);
  
  // 阶段9：首次交互后隐藏手势提示
  var hintEl = document.getElementById('graph-gesture-hint');
  var hintHidden = false;
  function hideGestureHint(){
    if(hintHidden || !hintEl) return;
    hintHidden = true;
    hintEl.classList.add('hidden');
    setTimeout(function(){ if(hintEl) hintEl.style.display='none'; }, 600);
  }
  visNetworkInstance.on('click', hideGestureHint);
  visNetworkInstance.on('dragStart', hideGestureHint);
  visNetworkInstance.on('zoom', hideGestureHint);
  // 8秒后自动消失
  setTimeout(hideGestureHint, 8000);
  
  // 阶段6：存储节点与颜色映射，供图例与筛选使用
  currentGraphNodes = nodes;
  currentGraphEdges = edges;
  currentGroupColors = groupColors;
  // 渲染图例
  renderGraphLegend(groupColors);
  // 清空筛选框
  const filterInput = document.getElementById('graph-filter');
  if(filterInput) filterInput.value = '';
  
  // 点击节点显示详情
  visNetworkInstance.on('click', function(params){
    if(params.nodes.length > 0){
      const nodeId = params.nodes[0];
      const node = nodes.find(n => n.id === nodeId);
      const detail = document.getElementById('graph-detail');
      if(detail && node){
        detail.style.display = 'block';
        const linkedEdges = edges.filter(e => e.from === nodeId || e.to === nodeId);
        detail.innerHTML = '<strong style="color:#c4b5fd">'+node.label+'</strong>'+
          (node.group?'<br><small style="color:var(--muted)">聚类: '+node.group+'</small>':'')+
          '<br><small style="color:var(--muted)">连接数: '+linkedEdges.length+'</small>';
      }
    }
  });
  } // 关闭 _createNetwork
  // P0修复：启动网络创建（rAF确保容器尺寸就绪）
  requestAnimationFrame(_createNetwork);
}

// 阶段CC: 尊重 prefers-reduced-motion
var _reducedMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

// 辅助函数：获取当前主题的 CSS 变量值（用于图表配色）
function themeVar(name, fallback){
  var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}
function themeChartBorder(){ return themeVar('--border', '#3a3a55'); }
function themeChartText(){ return themeVar('--text', '#e5e7eb'); }
function themeChartMuted(){ return themeVar('--muted', '#a8b0bc'); }
function themeChartCardBg(){ return themeVar('--card', '#1e1e2e'); }
function graphZoomIn(){
  if(visNetworkInstance) visNetworkInstance.moveTo({scale: visNetworkInstance.getScale() * 1.3, animation: !_reducedMotion});
}
function graphZoomOut(){
  if(visNetworkInstance) visNetworkInstance.moveTo({scale: visNetworkInstance.getScale() / 1.3, animation: !_reducedMotion});
}
function graphFit(){
  if(visNetworkInstance) visNetworkInstance.fit({animation: !_reducedMotion});
}

// 阶段6：渲染图谱图例（聚类颜色对照）
function renderGraphLegend(groupColors){
  const legend = document.getElementById('graph-legend');
  if(!legend) return;
  const entries = Object.entries(groupColors || {});
  if(entries.length === 0){ legend.innerHTML = ''; return; }
  legend.innerHTML = entries.map(([name, color]) => {
    const displayName = String(name).length > 12 ? String(name).substring(0,12)+'…' : String(name);
    return '<span class="legend-item"><span class="legend-dot" style="background:'+color+'"></span>'+displayName+'</span>';
  }).join('');
  // P0修复：移动端图例默认折叠
  if(window.innerWidth <= 768 && entries.length > 3){
    legend.classList.add('collapsed');
    var toggle = document.getElementById('legend-toggle');
    if(toggle){
      toggle.setAttribute('aria-expanded', 'false');
      toggle.textContent = '聚类图例 ▸';
    }
  }
}

// 阶段9：切换图例展开/折叠（移动端）
function toggleGraphLegend(){
  var legend = document.getElementById('graph-legend');
  var toggle = document.getElementById('legend-toggle');
  if(!legend || !toggle) return;
  var collapsed = legend.classList.toggle('collapsed');
  toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  toggle.textContent = collapsed ? '聚类图例 ▸' : '聚类图例 ▾';
}

// 阶段6：按标签筛选图谱节点（高亮匹配项，淡化其余）
function filterGraphNodes(query){
  if(!visNetworkInstance || !currentGraphNodes) return;
  const q = (query || '').trim().toLowerCase();
  const updates = currentGraphNodes.map(n => {
    const match = !q || String(n.label || n.id || '').toLowerCase().indexOf(q) !== -1;
    return {
      id: n.id,
      opacity: match ? 1 : 0.12,
      font: {color: match ? (n.font && n.font.color ? n.font.color : themeChartText()) : 'rgba(156,163,175,.25)', size: n.font && n.font.size ? Math.max(12, n.font.size) : 12, face:'sans-serif'}
    };
  });
  try{
    visNetworkInstance.body.data.nodes.update(updates);
  }catch(e){}
}

// ════════════════════════════════════════
// 阶段F：SM-2 间隔重复闪卡系统
// ════════════════════════════════════════
const FC_KEY = 'bda_flashcards';

function loadFlashcards(){
  try{
    const raw = localStorage.getItem(FC_KEY);
    if(raw) return JSON.parse(raw);
  }catch(e){}
  return [];
}

function saveFlashcards(cards){
  try{ localStorage.setItem(FC_KEY, JSON.stringify(cards)); }catch(e){}
}

// 阶段7：清除闪卡内容中的 null 字符等控制字符，修复 "u0000" 显示 bug
function _sanitizeCardText(s){
  var nul = String.fromCharCode(0);
  return String(s == null ? '' : s).split(nul).join('').replace(/u0000/g, '').replace(new RegExp('['+String.fromCharCode(0)+'-\\x08\\x0b\\x0c\\x0e-\\x1f\\x7f]','g'),'');
}

function importFlashcardsFromResult(result){
  if(!result) return;
  const existing = loadFlashcards();
  const existingIds = new Set(existing.map(c => c.id));
  let added = 0;
  
  // 从知识单元创建闪卡
  if(result.units){
    result.units.forEach(u => {
      const fcId = 'unit_'+u.id;
      if(!existingIds.has(fcId)){
        // P0修复：闪卡正面为知识要点提示，背面为完整内容（而非章节标题）
        var unitContent = _sanitizeCardText(u.content || '');
        var unitLabel = _sanitizeCardText(u.id || '');
        var chapterTag = u.chapter ? _sanitizeCardText(u.chapter) : '';
        // 生成问题式正面：提取内容前30字作为提示
        var frontText = unitContent.length > 0
          ? '📖 ' + (chapterTag ? '[' + chapterTag + '] ' : '') + unitLabel + '\\n\\n💡 回忆这个知识要点的核心内容…'
          : '📖 ' + unitLabel;
        existing.push({
          id: fcId,
          front: frontText,
          back: unitContent.substring(0, 300) || unitLabel,
          tags: u.tags || [],
          // SM-2 参数
          ease: 2.5,
          interval: 0,
          reps: 0,
          due: Date.now(),
          createdAt: Date.now()
        });
        existingIds.add(fcId);
        added++;
      }
    });
  }
  
  // 从卡片创建闪卡
  if(result.cards && Array.isArray(result.cards)){
    result.cards.forEach((c, i) => {
      const fcId = 'card_'+i;
      if(!existingIds.has(fcId) && (c.question || c.q) && (c.answer || c.a)){
        existing.push({
          id: fcId,
          front: _sanitizeCardText(c.question || c.q),
          back: _sanitizeCardText(c.answer || c.a),
          tags: c.tags || [],
          ease: 2.5,
          interval: 0,
          reps: 0,
          due: Date.now(),
          createdAt: Date.now()
        });
        existingIds.add(fcId);
        added++;
      }
    });
  }
  
  saveFlashcards(existing);
  return added;
}

// SM-2 算法核心
function sm2Algorithm(card, quality){
  // P0修复：4级评分映射到SM-2的0-5分制 (生疏=0, 困难=3, 良好=4, 熟练=5)
  const q = quality === 1 ? 0 : quality === 2 ? 3 : quality === 3 ? 4 : quality === 4 ? 5 : 0;
  
  if(q < 3){
    // 答错了，重置
    card.reps = 0;
    card.interval = 0;
    card.ease = Math.max(1.3, card.ease - 0.2);
  } else {
    // 答对了
    if(card.reps === 0){
      card.interval = 1;
    } else if(card.reps === 1){
      card.interval = 6;
    } else {
      card.interval = Math.round(card.interval * card.ease);
    }
    card.reps++;
    card.ease = card.ease + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02));
    if(card.ease < 1.3) card.ease = 1.3;
  }
  
  // 计算下次复习时间（毫秒）
  const dayMs = 86400000;
  card.due = Date.now() + card.interval * dayMs;
  
  return card;
}

let fcQueue = [];
let fcCurrentIdx = 0;
let fcStats = {total: 0, reviewed: 0, correct: 0};

function initFlashcardsPage(){
  const cards = loadFlashcards();
  const dueCards = cards.filter(c => c.due <= Date.now());
  
  if(cards.length === 0){
    document.getElementById('fc-empty').style.display = 'block';
    document.getElementById('fc-review').style.display = 'none';
    return;
  }
  
  document.getElementById('fc-empty').style.display = 'none';
  document.getElementById('fc-review').style.display = 'block';
  // 阶段6：显示暂停按钮
  var pauseBtn = document.getElementById('fc-pause-btn');
  if(pauseBtn){pauseBtn.style.display = 'inline-block';pauseBtn.classList.add('active');}
  
  // 显示统计（阶段7：三态 总计 / 已学 / 待复习）
  const stats = document.getElementById('fc-stats');
  const learnedCount = cards.filter(c => c.reps && c.reps > 0).length;
  stats.innerHTML = 
    '<div class="review-stat"><div class="num">'+cards.length+'</div><div class="label">全部</div></div>'+
    '<div class="review-stat"><div class="num">'+learnedCount+'</div><div class="label">已完成</div></div>'+
    '<div class="review-stat"><div class="num">'+dueCards.length+'</div><div class="label">进行中</div></div>';
  
  // 准备复习队列
  fcQueue = dueCards.length > 0 ? dueCards : cards.slice(0, 10);
  fcCurrentIdx = 0;
  fcStats = {total: fcQueue.length, reviewed: 0, correct: 0};
  
  showNextCard();
  // 阶段7：初始化滑动手势（左滑下一张，上滑翻转）
  initFlashcardSwipe();
}

// 阶段7：跳到下一张（不评分，仅前进）
function skipCard(){
  if(fcCurrentIdx >= fcQueue.length) return;
  fcCurrentIdx++;
  showNextCard();
}

// 阶段7：闪卡滑动手势
function initFlashcardSwipe(){
  const card = document.getElementById('fc-card');
  if(!card || card.dataset.swipeBound === '1') return;
  card.dataset.swipeBound = '1';
  let startX=0, startY=0, moved=false;
  card.addEventListener('touchstart', function(e){
    if(!e.touches[0]) return;
    startX = e.touches[0].clientX;
    startY = e.touches[0].clientY;
    moved = false;
  }, {passive:true});
  card.addEventListener('touchmove', function(e){
    if(!e.touches[0]) return;
    const dx = e.touches[0].clientX - startX;
    const dy = e.touches[0].clientY - startY;
    if(Math.abs(dx) > 8 || Math.abs(dy) > 8) moved = true;
  }, {passive:true});
  card.addEventListener('touchend', function(e){
    const t = e.changedTouches[0];
    if(!t) return;
    const dx = t.clientX - startX;
    const dy = t.clientY - startY;
    const absX = Math.abs(dx), absY = Math.abs(dy);
    // 小位移视为点击，交给 flipCard 处理
    if(Math.max(absX, absY) < 40) return;
    if(absX > absY){
      if(dx < 0){ skipCard(); } // 左滑：下一张
    } else {
      if(dy < 0){ flipCard(); } // 上滑：翻转
    }
    // 标记已滑动，阻止后续 click 再次触发 flipCard
    if(moved){
      card.dataset.justSwiped = '1';
      e.preventDefault();
    }
  }, {passive:false});
  // 捕获阶段拦截滑动后的 click，避免重复翻转
  card.addEventListener('click', function(e){
    if(card.dataset.justSwiped === '1'){
      e.preventDefault();
      e.stopPropagation();
      card.dataset.justSwiped = '0';
    }
  }, true);
}

function showNextCard(){
  if(fcCurrentIdx >= fcQueue.length){
    // 复习完成
    const front = document.getElementById('fc-front');
    const back = document.getElementById('fc-back');
    const card = document.getElementById('fc-card');
    const controls = document.getElementById('fc-controls');
    front.innerHTML = '<h3 style="color:#34d399">复习完成！</h3>';
    back.innerHTML = '<p>本次复习 '+fcStats.reviewed+' 张卡片<br>正确率: '+(fcStats.reviewed>0?Math.round(fcStats.correct/fcStats.reviewed*100):0)+'%</p>';
    card.classList.remove('flipped');
    controls.style.display = 'none';
    controls.classList.remove('show');
    return;
  }
  
  const card = fcQueue[fcCurrentIdx];
  document.getElementById('fc-front').textContent = _sanitizeCardText(card.front);
  document.getElementById('fc-back').textContent = _sanitizeCardText(card.back);
  document.getElementById('fc-card').classList.remove('flipped');
  const controls = document.getElementById('fc-controls');
  controls.style.display = 'none';
  controls.classList.remove('show');
  
  // 更新进度
  const pct = fcStats.total > 0 ? (fcCurrentIdx / fcStats.total) * 100 : 0;
  document.getElementById('fc-bar').style.width = pct + '%';
  document.getElementById('fc-progress-text').textContent = '第 ' + (fcCurrentIdx + 1) + ' 张，共 ' + fcStats.total + ' 张';
}

// 阶段6：暂停复习（自动保存进度，下次进入继续）
function pauseReview(){
  // 闪卡进度已自动保存在 localStorage，暂停只需返回首页
  if(typeof Swal !== 'undefined'){
    Swal.fire({title:'已暂停',text:'复习进度已自动保存，下次可继续',icon:'success',timer:1500,showConfirmButton:false});
  } else {
    toastSuccess('已暂停，进度已保存');
  }
  var pauseBtn = document.getElementById('fc-pause-btn');
  if(pauseBtn){pauseBtn.style.display = 'none';pauseBtn.classList.remove('active');}
  navigate('home');
}

function flipCard(){
  const card = document.getElementById('fc-card');
  card.classList.toggle('flipped');
  const controls = document.getElementById('fc-controls');
  if(card.classList.contains('flipped')){
    // 阶段7：翻转后显示评分面板（桌面端 display:grid；移动端通过 .show 类触发底部弹出动画）
    controls.style.display = 'grid';
    controls.classList.add('show');
  } else {
    controls.classList.remove('show');
  }
}

function reviewCard(quality, btnEl){
  if(fcCurrentIdx >= fcQueue.length) return;
  // #033：评分按钮即时视觉反馈（短暂高亮闪烁）
  if(btnEl){btnEl.classList.add('rated');setTimeout(function(){btnEl.classList.remove('rated');},300);}
  const card = fcQueue[fcCurrentIdx];
  const allCards = loadFlashcards();
  const idx = allCards.findIndex(c => c.id === card.id);
  // 阶段9：保存撤销快照
  if(idx >= 0){
    fcUndoSnapshot = {card: JSON.parse(JSON.stringify(allCards[idx])), idx: fcCurrentIdx, quality: quality};
    allCards[idx] = sm2Algorithm(card, quality);
    saveFlashcards(allCards);
  }
  fcStats.reviewed++;
  if(quality >= 3) fcStats.correct++;
  fcCurrentIdx++;
  showNextCard();
  // 阶段9：显示撤销提示
  showUndoToast(quality);
}

// 阶段9：撤销上一次评分
var fcUndoSnapshot = null;
function showUndoToast(quality){
  var undoToastBox = document.getElementById('fc-undo-toast');
  var msg = document.getElementById('fc-undo-msg');
  if(!undoToastBox || !msg) return;
  var labels = {1:'生疏',2:'困难',3:'良好',4:'熟练'};
  msg.textContent = '已评分：' + (labels[quality] || '已评分');
  undoToastBox.style.display = 'flex';
  setTimeout(function(){ undoToastBox.style.display = 'none'; }, 3000);
}

function undoReviewCard(){
  if(!fcUndoSnapshot) return;
  var snap = fcUndoSnapshot;
  var allCards = loadFlashcards();
  if(snap.idx < allCards.length){
    // 恢复卡片数据
    var currentCard = fcQueue[snap.idx];
    if(currentCard){
      allCards[snap.idx] = snap.card;
      saveFlashcards(allCards);
    }
  }
  // 回退索引和统计
  if(fcCurrentIdx > 0) fcCurrentIdx--;
  if(fcStats.reviewed > 0) fcStats.reviewed--;
  if(snap.quality >= 3 && fcStats.correct > 0) fcStats.correct--;
  fcUndoSnapshot = null;
  // 隐藏toast并重新渲染
  var undoToastEl = document.getElementById('fc-undo-toast');
  if(undoToastEl) undoToastEl.style.display = 'none';
  showNextCard();
  toast('已撤销评分', 'info');
}

// ════════════════════════════════════════
// 阶段G：PDF 导出 (jsPDF + html2canvas)
// ════════════════════════════════════════
async function exportPDF(){
  if(!currentStats){toastError('请先完成拆书');return}
  toast('正在生成 PDF…', 'info');
  try{
    const {jsPDF} = window.jspdf || {};
    if(!jsPDF){toastError('PDF 库未加载');return}
    
    const doc = new jsPDF('p', 'mm', 'a4');
    const pageW = 210, pageH = 297, margin = 15;
    let y = margin;
    
    // 标题
    doc.setFontSize(22);
    doc.setTextColor(124, 58, 237);
    doc.text('Book Digest Report', pageW/2, y+10, {align:'center'});
    y += 20;
    
    doc.setFontSize(10);
    doc.setTextColor(100);
    doc.text(new Date().toLocaleString('zh-CN'), pageW/2, y, {align:'center'});
    y += 10;
    
    // 分隔线
    doc.setDrawColor(200);
    doc.line(margin, y, pageW - margin, y);
    y += 10;
    
    // 统计数据
    doc.setFontSize(14);
    doc.setTextColor(50);
    doc.text('Statistics', margin, y);
    y += 7;
    
    doc.setFontSize(11);
    const stats = [
      'Knowledge Units: ' + (currentStats.units || 0),
      'Topic Clusters: ' + (currentStats.clusters || 0),
      'Visual Cards: ' + (currentStats.cards || 0),
      'Skill Points: ' + (currentStats.skills || 0)
    ];
    stats.forEach(s => {
      if(y > pageH - margin){doc.addPage(); y = margin;}
      doc.text(s, margin + 5, y);
      y += 6;
    });
    y += 5;
    
    // 知识单元
    if(searchUnitsData && searchUnitsData.length > 0){
      if(y > pageH - 40){doc.addPage(); y = margin;}
      doc.setFontSize(14);
      doc.setTextColor(50);
      doc.text('Knowledge Units', margin, y);
      y += 7;
      
      doc.setFontSize(9);
      searchUnitsData.slice(0, 50).forEach((u, i) => {
        if(y > pageH - margin){doc.addPage(); y = margin;}
        doc.setTextColor(124, 58, 237);
        doc.text(String(i+1) + '. ' + (u.id||''), margin + 5, y);
        y += 5;
        doc.setTextColor(80);
        const content = (u.content||'').substring(0, 200);
        const lines = doc.splitTextToSize(content, pageW - margin * 2 - 10);
        lines.forEach(line => {
          if(y > pageH - margin){doc.addPage(); y = margin;}
          doc.text(line, margin + 5, y);
          y += 4;
        });
        if(u.chapter){y += 2; doc.setTextColor(100); doc.text('Chapter: ' + u.chapter, margin + 5, y); y += 5;}
        y += 3;
      });
    }
    
    // 页脚
    const pageCount = doc.internal.getNumberOfPages();
    for(let p = 1; p <= pageCount; p++){
      doc.setPage(p);
      doc.setFontSize(8);
      doc.setTextColor(150);
      doc.text('Generated by Book Digest Agent  |  Page ' + p + '/' + pageCount, pageW/2, pageH - 5, {align:'center'});
    }
    
    doc.save('book_digest_report.pdf');
    toastSuccess('PDF 已导出');
  }catch(e){
    toastError('PDF 导出失败：' + e.message);
  }
}

// ════════════════════════════════════════
// 阶段H：Fuse.js 模糊搜索 + intro.js 引导
// ════════════════════════════════════════
let fuseSearchInstance = null;

function initFuseSearch(units){
  if(typeof Fuse === 'undefined' || !units) return;
  fuseSearchInstance = new Fuse(units, {
    keys: ['id', 'content', 'chapter', 'tags'],
    includeScore: true,
    threshold: 0.4,
    ignoreLocation: true,
    minMatchCharLength: 2
  });
}

// 升级原有 searchUnits 使用 Fuse.js
const _origSearchUnits = searchUnits;
searchUnits = function(query){
  const results = document.getElementById('search-results');
  if(!query || query.length < 2){
    results.innerHTML = '';
    return;
  }
  let matched = [];
  if(fuseSearchInstance){
    matched = fuseSearchInstance.search(query).slice(0, 20).map(r => r.item);
  } else {
    // 降级到原有逻辑
    const q = query.toLowerCase();
    matched = searchUnitsData.filter(u => {
      const text = (u.id||'') + ' ' + (u.content||'') + ' ' + (u.chapter||'');
      return text.toLowerCase().includes(q);
    }).slice(0, 20);
  }
  if(matched.length === 0){
    results.innerHTML = '<p style="color:var(--muted);padding:12px">未找到匹配的知识单元</p>';
    return;
  }
  results.innerHTML = matched.map(u => {
    let snippet = (u.content||'').substring(0, 120);
    const q = query.toLowerCase();
    const idx = snippet.toLowerCase().indexOf(q);
    if(idx >= 0){
      snippet = snippet.substring(0, idx) + '<mark>' + snippet.substring(idx, idx + query.length) + '</mark>' + snippet.substring(idx + query.length);
    }
    const chTag = u.chapter ? '<small style="color:#a5b4fc">[' + u.chapter + ']</small> ' : '';
    return '<div class="search-result-item"><strong>' + chTag + (u.id||'无ID') + '</strong><br><small style="color:var(--muted)">' + snippet + '...</small></div>';
  }).join('');
};

// intro.js 用户引导
function startOnboarding(){
  if(typeof introJs === 'undefined') return;
  var _isMobile = window.matchMedia('(max-width:768px)').matches;
  var allSteps = [
    {
      title: '欢迎',
      intro: '欢迎使用拆书Agent！让我们快速了解核心功能。'
    },
    {
      element: '#featGrid',
      title: '功能导航',
      intro: '点击任意卡片进入对应功能页面。'
    },
    {
      element: '#page-digest .card',
      title: 'AI 拆书',
      intro: '上传电子书，AI 自动拆解为思维导图、知识图谱等多种形态。'
    },
    {
      element: '#themeToggle',
      title: '主题切换',
      intro: '点击切换明暗主题，偏好会被记住。'
    },
    {
      element: '.kbd-hint',
      title: '快捷键',
      intro: 'Ctrl+K 聚焦搜索，Esc 返回主页。'
    },
    {
      title: '探索更多',
      intro: '去"知识图谱"探索交互式图谱，<br>或在"闪卡复习"中科学记忆知识要点！'
    }
  ];
  var steps = _isMobile ? allSteps.filter(function(s){ return s.element !== '.kbd-hint'; }) : allSteps;
  var totalSteps = steps.length;
  introJs().setOptions({
    steps: steps,
    skipLabel: '跳过引导',
    nextLabel: '下一步 →',
    prevLabel: '← 上一步',
    doneLabel: '完成 ✓',
    showProgress: true,
    showBullets: true,
    exitOnOverlayClick: true,
    tooltipPosition: 'bottom',
    helperElementPadding: 8,
    scrollToElement: true,
    scrollPadding: 40,
    accessibility: true
  }).onbeforechange(function(targetEl){
    // 阶段1：动态更新步骤指示器（延迟到intro.js重渲染后执行）
    var self = this;
    setTimeout(function(){
      var intro = self._introItems[self._currentStep];
      if(intro && intro.title){
        var titleEl = document.querySelector('.introjs-tooltip-title');
        if(titleEl){
          titleEl.textContent = '步骤 ' + (self._currentStep + 1) + '/' + totalSteps + ' · ' + intro.title;
        }
      }
      var progressEl = document.querySelector('.introjs-progress');
      if(progressEl){
        var pct = Math.round(((self._currentStep + 1) / totalSteps) * 100);
        progressEl.setAttribute('aria-label', '进度 ' + pct + '%');
      }
    }, 50);
  }).start();
  // 添加"不再显示"选项
  setTimeout(function(){
    var tooltip = document.querySelector('.introjs-tooltip');
    if(tooltip){
      var existing = document.getElementById('intro-no-show');
      if(!existing){
        var noShow = document.createElement('label');
        noShow.id = 'intro-no-show';
        noShow.style.cssText = 'display:flex;align-items:center;gap:6px;padding:4px 16px 8px;font-size:.78rem;color:var(--muted);cursor:pointer';
        noShow.innerHTML = '<input type="checkbox" style="min-height:auto;width:16px;height:16px" onchange="if(this.checked){localStorage.setItem(\\'bda_onboarded\\',\\'1\\')}"> 不再自动显示引导';
        tooltip.appendChild(noShow);
      }
    }
  }, 100);
}

// 首次访问自动引导
if(!localStorage.getItem('bda_onboarded')){
  localStorage.setItem('bda_onboarded', '1');
  setTimeout(() => {
    if(typeof introJs !== 'undefined' && (location.hash === '' || location.hash === '#home' || location.hash === '#digest')) startOnboarding();
  }, 1500);
}

// 键盘遮挡处理：输入框获焦时自动滚动到可视区域
document.addEventListener('focusin', function(e){
  if(e.target.matches('input,textarea,select')){
    setTimeout(function(){
      e.target.scrollIntoView({behavior:'smooth',block:'center'});
    }, 300);
  }
});
// 使用 VisualViewport API 检测键盘弹出
if(window.visualViewport){
  window.visualViewport.addEventListener('resize', function(){
    var active = document.activeElement;
    if(active && active.matches('input,textarea,select')){
      var rect = active.getBoundingClientRect();
      if(rect.bottom > window.visualViewport.height){
        active.scrollIntoView({behavior:'smooth',block:'center'});
      }
    }
  });
}

// ════════════════════════════════════════
// 阶段I：KaTeX 数学公式 + ECharts 可视化
// ════════════════════════════════════════
function renderMath(container){
  if(typeof renderMathInElement === 'undefined' || !container) return;
  renderMathInElement(container, {
    delimiters: [
      {left: '$$', right: '$$', display: true},
      {left: '$', right: '$', display: false},
      {left: '\\\\(', right: '\\\\)', display: false},
      {left: '\\\\[', right: '\\\\]', display: true}
    ],
    throwOnError: false
  });
}

var echartsRadarInstance = null; // 阶段DD: var 避免 TDZ 错误
var echartsWordcloudInstance = null;

function renderEChartsVisualizations(result){
  if(typeof echarts === 'undefined') return;
  
  // 1. 雷达图：技能包评估
  if(result && result.skill_pack && result.skill_pack.skills){
    const skills = result.skill_pack.skills;
    if(skills.length > 0){
      const radarEl = document.getElementById('echarts-radar-dashboard');
      if(radarEl && radarEl.offsetWidth > 0){
        try {
          radarEl.style.display = 'block';
          if(echartsRadarInstance) echartsRadarInstance.dispose();
          echartsRadarInstance = echarts.init(radarEl);
          echartsRadarInstance.setOption({
            title: {text: '技能雷达图', left: 'center', textStyle: {color: '#a78bfa', fontSize: 14}},
            tooltip: {},
            radar: {
              indicator: skills.slice(0, 8).map(s => ({name: s.name || s.title || String(s).substring(0,10), max: 100})),
              shape: 'polygon',
              splitArea: {areaStyle: {color: ['rgba(124,58,237,.03)','rgba(124,58,237,.06)']}},
              axisLine: {lineStyle: {color: themeChartBorder()}},
              splitLine: {lineStyle: {color: themeChartBorder()}}
            },
            series: [{
              type: 'radar',
              data: [{
                value: skills.slice(0, 8).map(s => s.level || s.score || Math.floor(Math.random()*40)+60),
                name: '技能水平',
                areaStyle: {color: 'rgba(124,58,237,.2)'},
                lineStyle: {color: '#7c3aed'},
                itemStyle: {color: '#ec4899'}
              }]
            }]
          });
        } catch(e) {
          console.warn('ECharts radar init failed:', e);
          radarEl.style.display = 'none';
        }
      }
    }
  }
  
  // 2. 词语云：从知识单元提取高频词
  if(result && result.units && result.units.length > 0){
    const wordFreq = {};
    result.units.forEach(u => {
      const text = (u.content || '') + ' ' + (u.id || '');
      const words = text.match(/[\u4e00-\u9fa5]{2,4}|[a-zA-Z]{3,}/g) || [];
      words.forEach(w => { wordFreq[w] = (wordFreq[w]||0) + 1; });
    });
    const wordData = Object.entries(wordFreq)
      .sort((a,b) => b[1] - a[1])
      .slice(0, 50)
      .map(([name, value]) => ({name, value}));
    
    if(wordData.length > 0){
      const wcWrap = document.getElementById('echarts-wordcloud-wrap');
      if(wcWrap && wcWrap.offsetWidth > 0){
        try {
          wcWrap.style.display = 'block';
          if(echartsWordcloudInstance) echartsWordcloudInstance.dispose();
          echartsWordcloudInstance = echarts.init(wcWrap);
          // 使用 treemap 替代 wordcloud（不需要额外插件）
          echartsWordcloudInstance.setOption({
            title: {text: '知识关键词分布', left: 'center', textStyle: {color: '#a78bfa', fontSize: 14}},
            tooltip: {formatter: '{b}: {c}'},
            series: [{
              type: 'treemap',
              data: wordData.map(w => ({name: w.name, value: w.value, itemStyle: {color: '#7c3aed'}})),
              roam: false,
              label: {show: true, color: themeChartText(), fontSize: 11},
              itemStyle: {borderColor: themeChartCardBg(), borderWidth: 2}
            }]
          });
        } catch(e) {
          console.warn('ECharts wordcloud init failed:', e);
          wcWrap.style.display = 'none';
        }
      }
    }
  }
}
</script>
<!-- 阶段1：Notyf Toast + SweetAlert2 弹窗 -->
<script src="https://cdn.jsdelivr.net/npm/notyf@3.10.0/notyf.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/sweetalert2@11/dist/sweetalert2.all.min.js"></script>
<script>
// 全局 Toast 实例（暗色主题适配）
const notyf = new Notyf({
  duration: 3500,
  position: {x:'center',y:'top'},
  types: [
    {type:'success', background:'#34d399', icon:false, dismissible:true},
    {type:'error', background:'#f87171', icon:false, dismissible:true},
    {type:'warning', background:'#fbbf24', icon:false, dismissible:true},
    {type:'info', background:'#7c3aed', icon:false, dismissible:true}
  ]
});
// P0修复：Toast去重 - 防止重复通知堆叠
var _lastToastMsg = '';
var _lastToastTime = 0;
// 便捷方法
function toast(msg, type='info'){
  if(typeof notyf==='undefined') return;
  // 去重：相同消息3秒内不重复显示
  var now = Date.now();
  if(msg === _lastToastMsg && (now - _lastToastTime) < 3000) return;
  _lastToastMsg = msg;
  _lastToastTime = now;
  notyf.open({type:type, message:msg});
}
function toastError(msg){ toast(msg,'error'); }
function toastSuccess(msg){ toast(msg,'success'); }

// SweetAlert2 暗色主题适配
if(typeof Swal!=='undefined'){
  Swal.mixin({
    background:'var(--card)',
    color:'#e5e7eb',
    confirmButtonColor:'#7c3aed',
    cancelButtonColor:'#6b7280',
    customClass:{
      popup:'swal-dark',
      confirmButton:'swal2-confirm',
      cancelButton:'swal2-cancel'
    },
    showClass:{popup:'swal2-show'},
    hideClass:{popup:'swal2-hide'},
    allowOutsideClick:false,
    allowEscapeKey:true
  });
}
</script>
<!-- 阶段2：marked.js + DOMPurify 初始化 -->
<script>
// 配置 marked.js
if(typeof marked!=='undefined'){
  marked.setOptions({
    breaks: true,
    gfm: true
  });
}

/**
 * 安全渲染 Markdown → HTML（经 DOMPurify 消毒）
 * @param {string} md - Markdown 原文
 * @returns {string} 安全的 HTML 字符串
 */
function renderMarkdown(md){
  if(!md) return '';
  if(typeof marked==='undefined' || typeof DOMPurify==='undefined'){
    // 库未加载时的降级：HTML 转义后换行保留
    return String(md)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/\\n/g,'<br>');
  }
  const raw = marked.parse(md);
  return DOMPurify.sanitize(raw, {
    ALLOWED_TAGS: ['h1','h2','h3','h4','h5','h6','p','br','hr','ul','ol','li',
      'strong','em','del','code','pre','blockquote','a','table','thead','tbody',
      'tr','th','td','img','span','div','sup','sub'],
    ALLOWED_ATTR: ['href','src','alt','title','class','target','rel']
  });
}
</script>
<!-- 阶段3：Prism.js 代码高亮核心 + 自动加载器 -->
<script src="https://cdn.jsdelivr.net/npm/prismjs@1.29.0/components/prism-core.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/prismjs@1.29.0/plugins/autoloader/prism-autoloader.min.js"></script>
<script>
// 配置 Prism 自动加载器路径
if(typeof Prism!=='undefined' && Prism.plugins && Prism.plugins.autoloader){
  Prism.plugins.autoloader.languages_path = 'https://cdn.jsdelivr.net/npm/prismjs@1.29.0/components/';
}

/**
 * 对指定容器内的代码块执行 Prism 高亮
 * @param {HTMLElement} container - 包含代码块的容器
 */
function highlightCode(container){
  if(typeof Prism==='undefined') return;
  if(!container) return;
  container.querySelectorAll('pre code').forEach(block=>{
    try{ Prism.highlightElement(block); }catch(e){}
  });
}
</script>
<!-- 阶段4：FilePond 文件上传增强 -->
<script src="https://cdn.jsdelivr.net/npm/filepond@4/dist/filepond.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/filepond-plugin-file-validate-type/dist/filepond-plugin-file-validate-type.min.js"></script>
<script>
// 初始化 FilePond 文件上传组件
let pond = null;
function initFilePond(){
  if(typeof FilePond==='undefined') return;
  // 注册文件类型验证插件
  FilePond.registerPlugin(FilePondPluginFileValidateType);
  // 获取原文件输入元素
  const input = document.getElementById('file');
  if(!input) return;
  // 初始化 FilePond
  pond = FilePond.create(input, {
    name: 'file',
    allowMultiple: false,
    maxFiles: 1,
    maxFileSize: '50MB',
    acceptedFileTypes: [
      'application/pdf',
      'application/epub+zip',
      'text/plain',
      'text/markdown'
    ],
    labelIdle: '<div style="font-size:2.5rem;margin-bottom:8px;opacity:.5">📤</div><span class="fp-drop-text" style="line-height:1.6">拖放文件或 </span><span class="filepond--label-action">点击选择文件</span><div style="margin-top:6px"><small style="color:#6b7280;line-height:1.6">PDF / EPUB / MD / TXT，单文件 ≤ 50MB</small></div>',
    labelInvalidField: '文件类型不支持',
    labelFileTypeNotAllowed: '仅支持 PDF / EPUB / MD / TXT 格式',
    fileValidateTypeLabelExpectedTypes: '期望格式：PDF / EPUB / MD / TXT',
    labelFileWaitingForSize: '计算大小中…',
    labelFileSizeNotAvailable: '大小未知',
    labelFileLoading: '加载中…',
    labelFileLoadError: '加载失败',
    labelTapToCancel: '点击取消',
    labelTapToUndo: '点击撤销',
    stylePanelLayout: 'compact',
    stylePanelAspectRatio: 'auto',
    styleButtonRemoveItemPosition: 'right',
    imagePreviewHeight: 0,
    credits: false,
    // 阶段EE: 文件添加时显示大小信息 + 阶段1：文件名显示
    onaddfile: function(err, fileItem){
      if(err) return;
      var sizeBytes = fileItem.fileSize || 0;
      var sizeMB = (sizeBytes / 1024 / 1024).toFixed(2);
      var sizeKB = (sizeBytes / 1024).toFixed(1);
      var sizeStr = sizeBytes > 1024*1024 ? sizeMB + ' MB' : sizeKB + ' KB';
      var fileName = fileItem.filename || (fileItem.file ? fileItem.file.name : '未知文件');
      var info = document.getElementById('file-size-info');
      if(!info){
        info = document.createElement('div');
        info.id = 'file-size-info';
        info.className = 'file-size-info';
        var pondEl = document.querySelector('.filepond--root');
        if(pondEl && pondEl.parentNode) pondEl.parentNode.insertBefore(info, pondEl.nextSibling);
      }
      // 阶段1：显示文件名 + 大小 + 格式标签
      var ext = fileName.split('.').pop().toUpperCase();
      info.innerHTML = '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'+
        '<span style="font-weight:600;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:200px">'+fileName+'</span>'+
        '<span style="font-size:.75rem;color:var(--muted);background:var(--input-bg);padding:2px 8px;border-radius:4px">'+ext+'</span>'+
        '<span style="color:var(--muted)">'+sizeStr+'</span>'+
      '</div>';
      info.className = 'file-size-info' + (sizeBytes > 50*1024*1024 ? ' too-large' : '');
      if(sizeBytes > 50*1024*1024){
        info.innerHTML += '<div style="color:var(--err);font-size:.8rem;margin-top:4px">⚠ 超过 50MB 限制</div>';
      }
    },
    onremovefile: function(){
      var info = document.getElementById('file-size-info');
      if(info) info.textContent = '';
    }
  });
  // 暗色主题适配
  const pondRoot = document.querySelector('.filepond--root');
  if(pondRoot){
    pondRoot.style.setProperty('--filepond--color', '#9ca3af');
  }
}
// 页面加载后初始化 FilePond
initFilePond();
</script>
<!-- FilePond 暗色主题覆盖 -->
<style>
.filepond--root{font-family:inherit!important}
.filepond--root.filepond--root-drag-over{border:2px dashed var(--accent)!important;background:rgba(124,58,237,.05)!important;border-radius:12px!important}
.filepond--panel-root{background-color:var(--input-bg)!important;border:2px dashed var(--border-strong)!important;border-radius:12px!important}
.filepond--panel-root:hover{border-color:var(--accent)!important}
.filepond--drop-label{color:#9ca3af!important;text-align:center}
.filepond--drop-label label{color:var(--accent-light)!important;cursor:pointer;text-decoration:none}
.filepond--drop-label small{display:block;margin-top:4px}
.filepond--item-panel{background-color:var(--card)!important;border:1px solid var(--border)!important;border-radius:8px!important}
.filepond--file-info-main{color:var(--text)!important}
.filepond--file-info-sub{color:var(--muted)!important}
.filepond--file-action-button{background-color:rgba(124,58,237,.15)!important;color:var(--text)!important}
.filepond--file-action-button:hover{background-color:rgba(124,58,237,.3)!important}
.filepond--dripp{background:rgba(124,58,237,.1)!important}
.filepond--panel-top, .filepond--panel-bottom{display:none!important}
</style>
<!-- 阶段5：Lucide 图标 + Tom Select 下拉增强 -->
<script src="https://cdn.jsdelivr.net/npm/lucide@0.400.0/dist/umd/lucide.min.js" defer onload="if(typeof lucide!=='undefined'){lucide.createIcons()}"></script>
<script src="https://cdn.jsdelivr.net/npm/tom-select@2/dist/js/tom-select.complete.min.js"></script>
<script>
// 初始化 Tom Select 下拉选择器
function initTomSelect(){
  if(typeof TomSelect==='undefined') return;
  const sel = document.getElementById('book_type');
  if(!sel) return;
  new TomSelect(sel, {
    create: false,
    sortField: {field:'text'},
    maxItems: 1,
    placeholder: '选择书籍类型',
    allowEmptyOption: false
  });
}
// 渲染 Lucide 图标 + 初始化 Tom Select
if(typeof lucide!=='undefined'){ lucide.createIcons(); }
initTomSelect();
</script>
<!-- Lucide 图标 + Tom Select 暗色主题 -->
<style>
.feat-icon svg{width:28px;height:28px;stroke-width:1.8}
/* 阶段2：移动端卡片图标SVG适配 */
@media(max-width:768px){.feat-icon svg{width:22px;height:22px}}
.ts-wrapper{width:100%}
.ts-control{background:var(--input-bg)!important;border:1px solid var(--border)!important;border-radius:10px!important;color:var(--text)!important;min-height:44px;box-shadow:none!important}
.ts-control:focus{border-color:var(--accent)!important;box-shadow:0 0 0 2px rgba(124,58,237,.15)!important}
.ts-dropdown{background:var(--card)!important;border:1px solid var(--border)!important;border-radius:10px!important;color:var(--text)!important;box-shadow:0 8px 32px rgba(0,0,0,.4)!important}
.ts-dropdown .active{background:rgba(124,58,237,.2)!important;color:var(--text)!important}
.ts-dropdown .option:hover{background:rgba(124,58,237,.1)!important}
.ts-dropdown .create:hover{background:rgba(124,58,237,.1)!important}
.ts-caret{color:var(--muted)!important}
.ts-wrapper.plugin-dropdown_input .ts-dropdown{border-radius:10px!important}
</style>
<style>.swal-dark{border:1px solid var(--border)!important}</style>
</body>
</html>"""

"""媒体智能管理平台 - 主应用"""
import os
import secrets
import json
import threading
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel

# 加载 .env
_env = Path(__file__).parent / '.env'
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip())
from models import init_db, engine, Media
from sqlmodel import Session
from indexer import index_directories
from batch import create_batch, get_batch, rate_batch, rate_media, get_stats
from feishu import send_images, send_video_frame

NAS_ROOT = os.environ.get('NAS_ROOT', '/mnt/nas')
DB_PATH = os.environ.get('DB_PATH', os.path.join(os.path.dirname(__file__), 'media_manager.db'))

def local_path(p: str) -> str:
    """将 /mnt/nas 路径转换为本地 NAS_ROOT 路径"""
    if p.startswith('/mnt/nas'):
        return NAS_ROOT + p[8:]  # 替换 /mnt/nas 前缀
    return p


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    print(f"✅ 媒体管理平台启动 | DB: {DB_PATH} | NAS: {NAS_ROOT}")
    yield
    print("👋 媒体管理平台关闭")


BASIC_USER = os.environ.get('BASIC_USER')
BASIC_PASS = os.environ.get('BASIC_PASS')

# === 登录安全 ===
from collections import defaultdict
import time as _time
_failed_logins = defaultdict(list)  # ip -> [timestamps]
_lock = threading.Lock()
MAX_ATTEMPTS = 3
LOCKOUT_SECONDS = 900  # 15 分钟
CLEANUP_INTERVAL = 300

def check_bruteforce(key: str) -> bool:
    """检查 key 是否被锁定，返回 True=已锁定"""
    with _lock:
        attempts = _failed_logins[key]
        cutoff = _time.time() - LOCKOUT_SECONDS
        _failed_logins[key] = [t for t in attempts if t > cutoff]
        return len(_failed_logins[key]) >= MAX_ATTEMPTS

def record_failed_login(key: str):
    """记录一次失败登录"""
    with _lock:
        _failed_logins[key].append(_time.time())

def clear_failed_logins(key: str):
    """登录成功，清除记录"""
    with _lock:
        _failed_logins.pop(key, None)

def _extract_user(auth_header: str) -> str:
    """从 Basic Auth 头提取用户名"""
    try:
        import base64
        decoded = base64.b64decode(auth_header.split(' ', 1)[1]).decode()
        return decoded.split(':', 1)[0]
    except Exception:
        return 'unknown'

# === 可调配置 ===
CONFIG_PATH = Path(__file__).parent / 'config.json'
DEFAULT_CONFIG = {
    'batch_count': 10,
    'video_frame_time': 5.0,
    'thumb_max_size': 1200,
    'thumb_quality': 80,
}

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
            return {**DEFAULT_CONFIG, **cfg}
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)

def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))

def get_config(key: str = None):
    cfg = load_config()
    return cfg.get(key) if key else cfg


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """对非本地请求启用 HTTP Basic Auth + 暴力破解保护"""
    async def dispatch(self, request, call_next):
        client = request.client.host if request.client else ""
        # 本地访问不需认证
        if client in ('127.0.0.1', '::1', '192.168.3.155'):
            return await call_next(request)

        auth = request.headers.get('authorization', '')

        # 暴力破解检查（仅对有认证头的请求）
        if auth.startswith('Basic '):
            lock_key = _extract_user(auth)
            if check_bruteforce(lock_key):
                with _lock:
                    attempts = _failed_logins[lock_key]
                    elapsed = _time.time() - attempts[-1] if attempts else LOCKOUT_SECONDS
                    remaining = max(0, int(LOCKOUT_SECONDS - elapsed))
                return JSONResponse(
                    {'error': f'登录次数过多，请 {remaining} 秒后重试'},
                    status_code=429,
                    headers={'Retry-After': str(remaining)}
                )
        else:
            # 未携带认证信息，直接要求登录，不记录失败次数
            return JSONResponse(
                {'error': '需要登录认证'},
                status_code=401,
                headers={'WWW-Authenticate': 'Basic realm="Media Manager"'}
            )

        # 验证认证信息
        import base64
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            user, pwd = decoded.split(':', 1)
            if secrets.compare_digest(user, BASIC_USER) and secrets.compare_digest(pwd, BASIC_PASS):
                clear_failed_logins(lock_key)
                return await call_next(request)
        except Exception:
            pass

        record_failed_login(lock_key)
        with _lock:
            attempts_left = MAX_ATTEMPTS - len(_failed_logins[lock_key])
        return JSONResponse(
            {'error': f'认证失败，剩余尝试次数: {max(0, attempts_left)}'},
            status_code=401,
            headers={'WWW-Authenticate': 'Basic realm="Media Manager"'}
        )


app = FastAPI(title="媒体智能管理平台", version="2.0", lifespan=lifespan)
app.add_middleware(BasicAuthMiddleware)


# === 请求模型 ===

class IndexRequest(BaseModel):
    directories: list[str]


from typing import Optional
class RateBatchRequest(BaseModel):
    scores: list[Optional[int]]


class RateMediaRequest(BaseModel):
    media_id: int
    score: int


class SendRequest(BaseModel):
    media_ids: list[int]
    receive_id: str


class SendVideoRequest(BaseModel):
    media_id: int
    receive_id: str
    filepath: str


# === API ===

@app.get("/")
async def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), 'static', 'index.html'))

KNOWN_SCAN_DIRS = [
    {"name": "优秀网络素材", "path": "/Secret/优秀网络素材"},
    {"name": "media-ingest", "path": "/Secret/media-ingest"},
]

@app.get("/settings.html")
async def settings_page():
    return FileResponse(os.path.join(os.path.dirname(__file__), 'static', 'settings.html'))


@app.get("/api/index/dirs")
async def api_index_dirs():
    """列出可扫描目录及文件数"""
    dirs = []
    for d in KNOWN_SCAN_DIRS:
        full = os.path.join(NAS_ROOT, d["path"].lstrip('/'))
        count = 0
        if os.path.isdir(full):
            count = sum(1 for _ in os.scandir(full) if _.is_file())
        dirs.append({"name": d["name"], "path": d["path"], "count": count})
    return {"dirs": dirs}


@app.post("/api/index/directories")
async def api_index(req: IndexRequest):
    """扫描目录并索引媒体"""
    stats = index_directories(req.directories, nas_root=NAS_ROOT)
    return stats


@app.get("/api/index/stats")
async def api_stats():
    """获取统计信息"""
    return get_stats()


@app.get("/api/batch")
async def api_create_batch(
    count: int = Query(default=0, ge=0, le=50),
    type: str = Query(default='image', pattern='^(image|video)$')
):
    """创建评分批次"""
    if count <= 0:
        count = get_config('batch_count')
    batch = create_batch(count=count, media_type=type)
    if not batch:
        raise HTTPException(404, "没有更多未评分的媒体了，换个类型试试？")
    return batch


@app.get("/api/batch/{batch_id}")
async def api_get_batch(batch_id: int):
    """获取批次详情"""
    batch = get_batch(batch_id)
    if not batch:
        raise HTTPException(404, "批次不存在")
    return batch


@app.post("/api/batch/{batch_id}/rate")
async def api_rate_batch(batch_id: int, req: RateBatchRequest):
    """批量评分"""
    result = rate_batch(batch_id, req.scores)
    return result


@app.post("/api/batch/rate-media")
async def api_rate_media(req: RateMediaRequest):
    """单个评分"""
    result = rate_media(req.media_id, req.score)
    return result


@app.post("/api/feishu/send")
async def api_send_images(req: SendRequest):
    """发送图片到飞书"""
    from sqlmodel import Session, select
    from models import Media

    with Session(engine) as session:
        paths = []
        valid_ids = []
        for mid in req.media_ids:
            media = session.get(Media, mid)
            if media:
                paths.append(local_path(media.path))
                valid_ids.append(mid)

    result = await send_images(req.receive_id, valid_ids, paths)
    return result


@app.post("/api/feishu/send-video")
async def api_send_video(req: SendVideoRequest):
    """发送视频帧预览"""
    result = await send_video_frame(req.receive_id, req.media_id, req.filepath)
    return result


# === 静态文件 ===
static_dir = os.path.join(os.path.dirname(__file__), 'static')
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


# === 图片/缩略图 ===
@app.get("/api/media/{media_id}/image")
async def api_get_image(media_id: int, size: str = Query(default='original', pattern='^(original|thumb)$')):
    """获取图片（原图或缩略图）"""
    from sqlmodel import Session
    from models import Media
    with Session(engine) as session:
        media = session.get(Media, media_id)
        if not media:
            raise HTTPException(404, "媒体不存在")
        path = local_path(media.path)
    if not os.path.exists(path):
        raise HTTPException(404, "文件不存在")

    if size == 'thumb':
        from pathlib import Path
        from PIL import Image
        import io
        thumb_dir = Path(local_path('/mnt/nas/Secret/media-manager/cache/thumbs'))
        thumb_dir.mkdir(parents=True, exist_ok=True)
        thumb_path = thumb_dir / f'{media_id}.jpg'
        if thumb_path.exists():
            return FileResponse(str(thumb_path), media_type='image/jpeg',
                                headers={'Cache-Control': 'public, max-age=86400'})
        img = Image.open(path)
        max_size = get_config('thumb_max_size')
        img.thumbnail((max_size, max_size))
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')
        img.save(str(thumb_path), 'JPEG', quality=get_config('thumb_quality'))
        return FileResponse(str(thumb_path), media_type='image/jpeg',
                            headers={'Cache-Control': 'public, max-age=86400'})

    return FileResponse(path, media_type='image/jpeg')


@app.get("/api/media/{media_id}/stream")
async def api_stream_video(media_id: int):
    """视频流式播放"""
    from sqlmodel import Session
    from models import Media
    with Session(engine) as session:
        media = session.get(Media, media_id)
        if not media or media.media_type != 'video':
            raise HTTPException(404, "视频不存在")
        path = local_path(media.path)
    if not os.path.exists(path):
        raise HTTPException(404, "文件不存在")
    return FileResponse(path, media_type='video/mp4',
                        headers={'Accept-Ranges': 'bytes'})


@app.get("/api/media/{media_id}/frame")
async def api_get_frame(media_id: int, t: float = Query(default=0)):
    """从视频提取帧作为预览图"""
    if t <= 0:
        t = get_config('video_frame_time')
    from sqlmodel import Session
    from models import Media
    with Session(engine) as session:
        media = session.get(Media, media_id)
        if not media or media.media_type != 'video':
            raise HTTPException(404, "视频不存在")
        path = local_path(media.path)
    if not os.path.exists(path):
        raise HTTPException(404, "文件不存在")

    import subprocess
    from pathlib import Path
    t_sec = int(t)
    frame_dir = Path(local_path('/mnt/nas/Secret/media-manager/cache/frames'))
    frame_dir.mkdir(parents=True, exist_ok=True)
    frame_path = frame_dir / f'{media_id}_{t_sec}.jpg'
    if frame_path.exists():
        return FileResponse(str(frame_path), media_type='image/jpeg',
                            headers={'Cache-Control': 'public, max-age=86400'})
    cmd = ['ffmpeg', '-ss', str(t), '-i', path, '-frames:v', '1',
           '-q:v', '2', '-f', 'mjpeg', 'pipe:1', '-loglevel', 'error']
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=10)
        if result.returncode == 0 and result.stdout:
            frame_path.write_bytes(result.stdout)
            return FileResponse(str(frame_path), media_type='image/jpeg',
                                headers={'Cache-Control': 'public, max-age=86400'})
    except Exception:
        pass
    raise HTTPException(500, "帧提取失败")


# === 配置 API ===
@app.get("/api/config")
async def api_get_config():
    return get_config()

@app.post("/api/config")
async def api_set_config(request: Request):
    data = await request.json()
    cfg = load_config()
    for k in ('batch_count', 'video_frame_time', 'thumb_max_size', 'thumb_quality'):
        if k in data:
            val = data[k]
            try:
                if k == 'batch_count':
                    val = int(val)
                    if val < 1 or val > 50:
                        return JSONResponse({'error': f'批次数量必须在 1-50 之间，收到 {val}'}, status_code=400)
                    cfg[k] = val
                elif k == 'video_frame_time':
                    val = float(val)
                    if val < 0.5 or val > 300:
                        return JSONResponse({'error': f'帧时间点必须在 0.5-300 秒之间，收到 {val}'}, status_code=400)
                    cfg[k] = val
                elif k == 'thumb_max_size':
                    val = int(val)
                    if val < 200 or val > 4000:
                        return JSONResponse({'error': f'缩略图尺寸必须在 200-4000 之间，收到 {val}'}, status_code=400)
                    cfg[k] = val
                elif k == 'thumb_quality':
                    val = int(val)
                    if val < 30 or val > 100:
                        return JSONResponse({'error': f'图片质量必须在 30-100 之间，收到 {val}'}, status_code=400)
                    cfg[k] = val
            except (ValueError, TypeError):
                return JSONResponse({'error': f'{k} 的值无效: {data[k]}'}, status_code=400)
    save_config(cfg)
    return cfg


# === 评分统计 ===
@app.get("/api/stats/ratings")
async def api_rating_stats():
    """评分分布统计"""
    from sqlmodel import Session, select, func
    from models import Media, Rating
    stats = {}
    with Session(engine) as session:
        for media_type in ('image', 'video'):
            total = session.exec(select(func.count(Media.id)).where(
                Media.media_type == media_type)).one()
            rated = session.exec(
                select(func.count(Rating.id)).join(Media, Rating.media_id == Media.id).where(
                    Media.media_type == media_type)
            ).one()
            dist = {}
            for s in range(10):
                c = session.exec(
                    select(func.count(Rating.id)).join(Media, Rating.media_id == Media.id).where(
                        Media.media_type == media_type, Rating.score == s)
                ).one()
                if c > 0:
                    dist[str(s)] = c
            stats[media_type] = {'total': total, 'rated': rated, 'unrated': total - rated, 'distribution': dist}
    return stats


# === 已评记录 ===
@app.get("/api/rated")
async def api_rated_list(
    type: str = Query(default='image', pattern='^(image|video)$'),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    min_score: int = Query(default=0, ge=0, le=9),
    max_score: int = Query(default=9, ge=0, le=9),
):
    """获取已评记录"""
    from sqlmodel import Session, select
    from models import Media, Rating
    with Session(engine) as session:
        base = select(Media, Rating).join(Rating, Rating.media_id == Media.id).where(
            Media.media_type == type,
            Rating.score >= min_score,
            Rating.score <= max_score
        ).order_by(Rating.score.desc())
        rows = session.exec(base.offset((page - 1) * page_size).limit(page_size)).all()
        from sqlmodel import func
        count_q = select(func.count(Rating.id)).join(Media, Rating.media_id == Media.id).where(
            Media.media_type == type, Rating.score >= min_score, Rating.score <= max_score)
        total = session.exec(count_q).one()
        items = []
        for m, r in rows:
            items.append({'media_id': m.id, 'filename': m.filename, 'path': m.path,
                          'score': r.score, 'media_type': m.media_type,
                          'width': m.width, 'height': m.height, 'file_size': m.file_size})
        return {'items': items, 'total': total, 'page': page, 'page_size': page_size}


# === 修改评分 ===
@app.post("/api/media/{media_id}/rescore")
async def api_rescore(media_id: int, request: Request):
    """修改单个媒体评分"""
    data = await request.json()
    score = data.get('score')
    if score is not None:
        score = int(score)
        if score < 0 or score > 9:
            raise HTTPException(400, "评分必须在 0-9 之间")
    # 检查媒体是否存在
    with Session(engine) as session:
        media = session.get(Media, media_id)
        if not media:
            raise HTTPException(404, "媒体不存在")
    result = rate_media(media_id, score)
    return {'id': media_id, 'score': score}


# === 批量删除低分 ===
@app.post("/api/delete-low")
async def api_delete_low(request: Request):
    """删除评分低于阈值的媒体（移动到回收站）"""
    data = await request.json()
    max_score = data.get('max_score', 0)
    media_type = data.get('type', 'image')
    if max_score < 0 or max_score > 9:
        raise HTTPException(400, "阈值必须在 0-9 之间")

    import shutil
    from sqlmodel import Session, select
    from models import Media, Rating
    trash_dir = Path(local_path('/mnt/nas/Secret/media-manager/trash'))
    trash_dir.mkdir(parents=True, exist_ok=True)

    deleted = []
    with Session(engine) as session:
        rows = session.exec(
            select(Media, Rating).join(Rating, Rating.media_id == Media.id).where(
                Media.media_type == media_type, Rating.score <= max_score)
        ).all()
        for m, r in rows:
            src = Path(m.path)
            if src.exists():
                dst = trash_dir / src.name
                i = 1
                while dst.exists():
                    dst = trash_dir / f"{src.stem}_{i}{src.suffix}"
                    i += 1
                shutil.move(str(src), str(dst))
                deleted.append({'id': m.id, 'filename': m.filename, 'score': r.score})
                session.delete(r)
                session.delete(m)
        session.commit()
    return {'deleted': deleted, 'total': len(deleted)}


# === 导出评分 CSV ===
@app.get("/api/export/csv")
async def api_export_csv(type: str = Query(default='image')):
    """导出评分数据为 CSV"""
    from sqlmodel import Session, select
    from models import Media, Rating
    import csv, io
    with Session(engine) as session:
        rows = session.exec(
            select(Media, Rating).join(Rating, Rating.media_id == Media.id).where(
                Media.media_type == type).order_by(Rating.score.desc())
        ).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', '文件名', '路径', '评分', '类型', '宽', '高', '大小(MB)'])
    for m, r in rows:
        writer.writerow([m.id, m.filename, m.path, r.score, m.media_type,
                          m.width, m.height, round(m.file_size / 1048576, 2)])

    from fastapi.responses import StreamingResponse
    content = '\ufeff' + output.getvalue()  # BOM for Excel
    return StreamingResponse(
        io.BytesIO(content.encode('utf-8')),
        media_type='text/csv',
        headers={'Content-Disposition': f'attachment; filename=ratings_{type}.csv'}
    )


# === 浏览器扩展：一键保存 ===


# === yt-dlp 增强版 ingest API ===
from sqlmodel import select
from ingest_routes import register_ingest_routes
register_ingest_routes(app, engine, Session, select, NAS_ROOT)

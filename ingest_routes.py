"""旺仔 ingest API - 本地下载版（临时文件 + 自动清理）"""
import os, subprocess, hashlib, tempfile, shutil
from pathlib import Path
from datetime import datetime as dt
from typing import List
from fastapi import HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

YT_DLP = os.environ.get('YT_DLP_PATH', '/usr/local/bin/yt-dlp')
TEMP_DIR = Path(tempfile.gettempdir()) / 'wangzai-downloads'
TEMP_DIR.mkdir(parents=True, exist_ok=True)


class IngestUrlRequest(BaseModel):
    url: str
    media_type: str = "auto"


class IngestBatchRequest(BaseModel):
    urls: List[dict]


def register_ingest_routes(app, engine, Session, select, NAS_ROOT):
    from models import Media

    def _detect_type(url: str, hint: str = 'auto') -> str:
        if hint != 'auto':
            return hint
        lower = url.lower()
        video_domains = ['bilibili', 'youtube', 'youtu.be', 'douyin', 'tiktok', 'vimeo', 'xiaohongshu', 'weibo']
        video_exts = ['.mp4', '.webm', '.mov', '.mkv', '.flv']
        if any(d in lower for d in video_domains) or any(lower.endswith(e) for e in video_exts):
            return 'video'
        return 'image'

    @app.post("/api/ingest/url")
    async def api_ingest_url(req: IngestUrlRequest):
        """yt-dlp 下载到临时目录，不存 NAS"""
        media_type = _detect_type(req.url, req.media_type)

        if media_type == 'video':
            # 用 yt-dlp 下载到临时目录
            ts = dt.now().strftime('%Y%m%d_%H%M%S')
            out_template = str(TEMP_DIR / f'{ts}_%(title).50s.%(ext)s')
            cmd = [
                YT_DLP,
                '-f', 'bestvideo[height<=1080]+bestaudio/best[height<=1080]',
                '--merge-output-format', 'mp4',
                '-o', out_template,
                '--no-playlist',
                '--socket-timeout', '30',
                req.url
            ]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if result.returncode != 0:
                    raise HTTPException(500, f'yt-dlp 失败: {result.stderr[:300]}')

                # 找到下载的文件（最新的）
                files = sorted(TEMP_DIR.glob('*'), key=os.path.getmtime, reverse=True)
                downloaded = [f for f in files if not f.name.startswith('.') and f.stat().st_size > 0]
                if not downloaded:
                    raise HTTPException(500, '未找到下载文件')

                filepath = downloaded[0]
                return {
                    'status': 'ok',
                    'filename': filepath.name,
                    'temp_key': filepath.name,  # 用于后续下载
                    'size': filepath.stat().st_size
                }
            except subprocess.TimeoutExpired:
                raise HTTPException(500, '下载超时（300秒）')

        else:
            # 图片：返回原始 URL，浏览器直接下载
            return {'status': 'ok', 'url': req.url, 'media_type': 'image', 'direct': True}

    @app.get("/api/download/{filename}")
    async def api_download_file(filename: str):
        """下载临时文件，下载后自动删除"""
        filepath = TEMP_DIR / filename
        if not filepath.exists():
            raise HTTPException(404, '文件不存在或已过期')

        # 流式返回，下载完后删除
        return FileResponse(
            path=str(filepath),
            filename=filename,
            media_type='video/mp4',
            background=lambda: _cleanup_file(filepath)  # 后台清理
        )

    def _cleanup_file(filepath: Path):
        """下载完成后删除临时文件"""
        try:
            filepath.unlink()
        except Exception:
            pass

    @app.post("/api/ingest/batch")
    async def api_ingest_batch(req: IngestBatchRequest):
        """批量下载"""
        results = []
        for item in req.urls[:20]:
            url = item.get('url', '')
            if not url:
                continue
            media_type = _detect_type(url, item.get('media_type', 'auto'))

            if media_type == 'video':
                try:
                    ts = dt.now().strftime('%Y%m%d_%H%M%S_%f')
                    out = str(TEMP_DIR / f'{ts}.%(ext)s')
                    cmd = [YT_DLP, '-f', 'best[height<=720]', '-o', out, '--no-playlist', url]
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                    if r.returncode == 0:
                        files = sorted(TEMP_DIR.glob('*'), key=os.path.getmtime, reverse=True)
                        f = files[0] if files else None
                        if f and f.stat().st_size > 0:
                            results.append({'url': url, 'status': 'ok', 'filename': f.name, 'temp_key': f.name})
                        else:
                            results.append({'url': url, 'status': 'error', 'error': '空文件'})
                    else:
                        results.append({'url': url, 'status': 'error', 'error': r.stderr[:200]})
                except Exception as e:
                    results.append({'url': url, 'status': 'error', 'error': str(e)})
            else:
                results.append({'url': url, 'status': 'ok', 'url': url, 'direct': True})

        return {'status': 'ok', 'total': len(results),
                'success': sum(1 for r in results if r.get('status') == 'ok'),
                'results': results}

    # 定期清理超过1小时的临时文件
    import threading
    def _periodic_cleanup():
        import time
        while True:
            time.sleep(3600)
            for f in TEMP_DIR.glob('*'):
                if f.is_file() and time.time() - f.stat().st_mtime > 3600:
                    try: f.unlink()
                    except: pass
    threading.Thread(target=_periodic_cleanup, daemon=True).start()

"""飞书发送模块"""
import os
import json
import httpx
import subprocess
from pathlib import Path

FEISHU_APP_ID = os.environ.get('FEISHU_APP_ID', 'cli_a934d8a6d6385cb3')
FEISHU_APP_SECRET = os.environ.get('FEISHU_APP_SECRET', 'MEFqexzOxBXXFdA00KzZrhPUnNU0ohC8')

_token_cache = {'token': None, 'expires': 0}


async def get_tenant_token() -> str:
    """获取飞书 tenant_access_token"""
    import time
    now = time.time()
    if _token_cache['token'] and _token_cache['expires'] > now + 60:
        return _token_cache['token']

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
            json={'app_id': FEISHU_APP_ID, 'app_secret': FEISHU_APP_SECRET}
        )
        data = resp.json()
        token = data['tenant_access_token']
        _token_cache['token'] = token
        _token_cache['expires'] = now + data.get('expire', 7200)
        return token


async def upload_image(token: str, filepath: str) -> str | None:
    """上传图片到飞书，返回image_key"""
    async with httpx.AsyncClient(timeout=30) as client:
        with open(filepath, 'rb') as f:
            resp = await client.post(
                'https://open.feishu.cn/open-apis/im/v1/images',
                headers={'Authorization': f'Bearer {token}'},
                data={'image_type': 'message'},
                files={'image': (os.path.basename(filepath), f, 'image/jpeg')}
            )
            result = resp.json()
            if result.get('code') == 0:
                return result['data']['image_key']
    return None


async def upload_video(token: str, filepath: str) -> str | None:
    """上传视频到飞书，返回file_key"""
    file_size = os.path.getsize(filepath)
    async with httpx.AsyncClient(timeout=120) as client:
        with open(filepath, 'rb') as f:
            resp = await client.post(
                'https://open.feishu.cn/open-apis/im/v1/files',
                headers={'Authorization': f'Bearer {token}'},
                data={
                    'file_type': 'mp4',
                    'file_name': os.path.basename(filepath),
                    'file_size': str(file_size),
                },
                files={'file': (os.path.basename(filepath), f, 'video/mp4')}
            )
            result = resp.json()
            if result.get('code') == 0:
                return result['data']['file_key']
    return None


def extract_video_frame(filepath: str, timestamp: str = '00:00:01') -> str | None:
    """提取视频帧作为预览图"""
    out_path = filepath + '_preview.jpg'
    try:
        cmd = [
            'ffmpeg', '-y', '-i', filepath,
            '-ss', timestamp, '-vframes', '1',
            '-vf', 'scale=640:-1',
            out_path
        ]
        subprocess.run(cmd, capture_output=True, timeout=30)
        if os.path.exists(out_path):
            return out_path
    except Exception:
        pass
    return None


async def send_images(receive_id: str, media_ids: list[int], paths: list[str]) -> dict:
    """发送图片到飞书"""
    token = await get_tenant_token()
    sent = []

    for media_id, filepath in zip(media_ids, paths):
        if not os.path.exists(filepath):
            continue

        # 跳过PNG
        if filepath.lower().endswith('.png'):
            continue

        image_key = await upload_image(token, filepath)
        if image_key:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    'https://open.feishu.cn/open-apis/im/v1/messages',
                    params={'receive_id_type': 'open_id'},
                    headers={
                        'Authorization': f'Bearer {token}',
                        'Content-Type': 'application/json'
                    },
                    json={
                        'receive_id': receive_id,
                        'msg_type': 'image',
                        'content': json.dumps({'image_key': image_key})
                    }
                )
                if resp.status_code == 200:
                    sent.append(media_id)

    return {'sent': sent, 'total': len(sent)}


async def send_video_frame(receive_id: str, media_id: int, filepath: str) -> dict:
    """发送视频帧预览图"""
    if not os.path.exists(filepath):
        return {'error': 'file not found'}

    frame_path = extract_video_frame(filepath)
    if not frame_path:
        return {'error': 'frame extraction failed'}

    token = await get_tenant_token()
    image_key = await upload_image(token, frame_path)

    # 清理临时文件
    try:
        os.remove(frame_path)
    except Exception:
        pass

    if image_key:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                'https://open.feishu.cn/open-apis/im/v1/messages',
                params={'receive_id_type': 'open_id'},
                headers={
                    'Authorization': f'Bearer {token}',
                    'Content-Type': 'application/json'
                },
                json={
                    'receive_id': receive_id,
                    'msg_type': 'image',
                    'content': json.dumps({'image_key': image_key})
                }
            )
            return {'sent': resp.status_code == 200}

    return {'error': 'upload failed'}

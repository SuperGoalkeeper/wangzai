"""目录索引模块 - NFS优化版（无MD5，find批量扫描）"""
import os
import subprocess
from pathlib import Path
from models import Media, engine, init_db
from sqlmodel import Session, select

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tiff'}
VIDEO_EXTS = {'.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.ts', '.rmvb'}
ALL_EXTS = IMAGE_EXTS | VIDEO_EXTS
SKIP_PREFIXES = ('._', '.DS_Store', 'Thumbs.db')


def is_media_file(path: str) -> str | None:
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXTS: return 'image'
    if ext in VIDEO_EXTS: return 'video'
    return None


def get_image_info(filepath: str) -> tuple[int, int]:
    try:
        from PIL import Image
        with Image.open(filepath) as img:
            return img.size
    except Exception:
        return 0, 0


def get_video_info(filepath: str) -> tuple[int, int, float]:
    try:
        cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_format', '-show_streams', filepath
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        import json
        data = json.loads(result.stdout)
        w, h, dur = 0, 0, 0.0
        for s in data.get('streams', []):
            if s.get('codec_type') == 'video':
                w = int(s.get('width', 0))
                h = int(s.get('height', 0))
                break
        dur = float(data.get('format', {}).get('duration', 0))
        return w, h, dur
    except Exception:
        return 0, 0, 0.0


def find_media_files(directories: list[str], nas_root: str = '/mnt/nas') -> list[str]:
    """用 find 命令批量查找媒体文件，比 os.walk 快得多"""
    dirs_to_scan = []
    for d in directories:
        full = d if os.path.isabs(d) else os.path.join(nas_root, d)
        if os.path.isdir(full):
            dirs_to_scan.append(full)

    if not dirs_to_scan:
        return []

    # 构建 find 命令：-iname 匹配扩展名，-type f 普通文件
    ext_args = []
    for ext in ALL_EXTS:
        ext_args.extend(['-iname', f'*{ext}'])

    # 用 -or 组合所有扩展名
    find_parts = []
    for i, ext in enumerate(ALL_EXTS):
        if i > 0:
            find_parts.append('-o')
        find_parts.extend(['-iname', f'*{ext}'])

    cmd = ['find'] + dirs_to_scan + ['-type', 'f', '('] + find_parts + [')', '-not', '-name', '._*']

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        files = [f for f in result.stdout.strip().split('\n') if f]
        return files
    except Exception as e:
        print(f"find error: {e}")
        return []


def index_directories(directories: list[str], nas_root: str = '/mnt/nas') -> dict:
    """扫描目录并索引媒体文件"""
    init_db()
    stats = {'indexed': 0, 'skipped': 0, 'errors': 0, 'total': 0}

    print(f"🔍 扫描中...", flush=True)
    all_files = find_media_files(directories, nas_root)
    stats['total'] = len(all_files)
    print(f"📁 发现 {len(all_files)} 个媒体文件", flush=True)

    if not all_files:
        return stats

    with Session(engine) as session:
        # 批量查已有路径
        existing_paths = set()
        for media in session.exec(select(Media.path)).all():
            existing_paths.add(media)

        batch = []
        for filepath in all_files:
            if filepath in existing_paths:
                stats['skipped'] += 1
                continue

            # 跳过特殊前缀
            fname = os.path.basename(filepath)
            if any(fname.startswith(p) for p in SKIP_PREFIXES):
                stats['skipped'] += 1
                continue

            try:
                media_type = is_media_file(filepath)
                if not media_type:
                    stats['skipped'] += 1
                    continue

                file_size = os.path.getsize(filepath)
                width, height, duration = 0, 0, 0.0

                if media_type == 'image':
                    width, height = get_image_info(filepath)
                else:
                    width, height, duration = get_video_info(filepath)

                media = Media(
                    path=filepath,
                    filename=fname,
                    media_type=media_type,
                    file_size=file_size,
                    md5='',
                    width=width,
                    height=height,
                    duration=duration,
                )
                batch.append(media)
                stats['indexed'] += 1

                # 每100条提交一次
                if len(batch) >= 100:
                    session.add_all(batch)
                    session.commit()
                    print(f"  ✅ 已索引 {stats['indexed']}/{stats['total']}", flush=True)
                    batch = []

            except Exception as e:
                stats['errors'] += 1
                print(f"  ❌ {filepath}: {e}", flush=True)

        # 提交剩余
        if batch:
            session.add_all(batch)
            session.commit()

    print(f"\n✅ 完成！索引={stats['indexed']}, 跳过={stats['skipped']}, 错误={stats['errors']}")
    return stats

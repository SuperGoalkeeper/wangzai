"""批次管理模块"""
import os
import random
from datetime import datetime
from sqlmodel import Session, select, func
from models import Media, Rating, Batch, BatchMedia, engine, init_db


def create_batch(count: int = 5, media_type: str = 'image') -> dict | None:
    """创建评分批次"""
    init_db()
    with Session(engine) as session:
        # 找未评分的媒体
        rated_ids = select(Rating.media_id)
        unrated = session.exec(
            select(Media)
            .where(Media.media_type == media_type)
            .where(~Media.id.in_(rated_ids))
        ).all()

        if not unrated:
            return None

        # 随机选取
        chosen = random.sample(unrated, min(count, len(unrated)))

        # 创建批次
        batch = Batch(media_type=media_type, status='pending')
        session.add(batch)
        session.flush()

        for i, media in enumerate(chosen):
            bm = BatchMedia(batch_id=batch.id, media_id=media.id, position=i)
            session.add(bm)

        session.commit()
        return get_batch_detail(session, batch.id)


def get_batch(batch_id: int) -> dict | None:
    """获取批次详情"""
    init_db()
    with Session(engine) as session:
        return get_batch_detail(session, batch_id)


def get_batch_detail(session: Session, batch_id: int) -> dict | None:
    batch = session.get(Batch, batch_id)
    if not batch:
        return None

    items = session.exec(
        select(BatchMedia, Media)
        .join(Media, BatchMedia.media_id == Media.id)
        .where(BatchMedia.batch_id == batch_id)
        .order_by(BatchMedia.position)
    ).all()

    # 获取已有评分
    rated = {}
    for bm, media in items:
        r = session.exec(
            select(Rating).where(Rating.media_id == media.id)
        ).first()
        if r:
            rated[media.id] = r.score

    return {
        'id': batch.id,
        'media_type': batch.media_type,
        'status': batch.status,
        'items': [
            {
                'media_id': media.id,
                'filename': media.filename,
                'path': media.path,
                'media_type': media.media_type,
                'width': media.width,
                'height': media.height,
                'duration': media.duration,
                'file_size': media.file_size,
                'score': rated.get(media.id),
            }
            for bm, media in items
        ]
    }


def rate_batch(batch_id: int, scores: list) -> dict:
    """批量评分，None/null 跳过，0分自动删除文件"""
    init_db()
    with Session(engine) as session:
        batch = session.get(Batch, batch_id)
        if not batch:
            return {'error': 'batch not found'}

        items = session.exec(
            select(BatchMedia)
            .where(BatchMedia.batch_id == batch_id)
            .order_by(BatchMedia.position)
        ).all()

        deleted = []
        rated_count = 0
        for bm, score in zip(items, scores):
            if score is None:
                continue  # 跳过未评分的
            rated_count += 1
            # 更新或创建评分
            existing = session.exec(
                select(Rating).where(Rating.media_id == bm.media_id)
            ).first()

            if existing:
                existing.score = score
                existing.batch_id = batch_id
                existing.rated_at = datetime.now().isoformat()
            else:
                rating = Rating(
                    media_id=bm.media_id,
                    score=score,
                    batch_id=batch_id,
                )
                session.add(rating)

            # 0分删除NAS文件
            if score == 0:
                media = session.get(Media, bm.media_id)
                if media and os.path.exists(media.path):
                    try:
                        os.remove(media.path)
                        deleted.append({'media_id': media.id, 'filename': media.filename})
                    except Exception as e:
                        deleted.append({'media_id': media.id, 'error': str(e)})

        batch.status = 'done'
        session.commit()

        return {'batch_id': batch_id, 'rated': rated_count, 'total': len(items), 'deleted': deleted}


def rate_media(media_id: int, score: int) -> dict:
    """单个评分，0分自动删除文件"""
    init_db()
    with Session(engine) as session:
        existing = session.exec(
            select(Rating).where(Rating.media_id == media_id)
        ).first()

        if existing:
            existing.score = score
            existing.rated_at = datetime.now().isoformat()
        else:
            rating = Rating(media_id=media_id, score=score)
            session.add(rating)

        session.commit()

        # 0分删除NAS文件
        deleted = False
        if score == 0:
            media = session.get(Media, media_id)
            if media and os.path.exists(media.path):
                try:
                    os.remove(media.path)
                    deleted = True
                except Exception:
                    pass

        return {'media_id': media_id, 'score': score, 'deleted': deleted}


def get_stats() -> dict:
    """获取统计信息"""
    init_db()
    with Session(engine) as session:
        total_images = session.exec(
            select(func.count()).where(Media.media_type == 'image')
        ).one()
        total_videos = session.exec(
            select(func.count()).where(Media.media_type == 'video')
        ).one()
        total_rated = session.exec(
            select(func.count()).select_from(Rating)
        ).one()

        # 评分分布
        score_dist = {}
        for s in range(9):
            count = session.exec(
                select(func.count()).where(Rating.score == s)
            ).one()
            if count > 0:
                score_dist[str(s)] = count

        return {
            'total_images': total_images,
            'total_videos': total_videos,
            'total_rated': total_rated,
            'total_unrated': (total_images + total_videos) - total_rated,
            'score_distribution': score_dist,
        }

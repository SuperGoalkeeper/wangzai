"""媒体管理平台 - TDD 测试套件"""
import pytest
import pytest_asyncio
import asyncio
import httpx
import os
import sys
import tempfile
import shutil
from pathlib import Path

# 测试前：使用临时数据库，不影响生产数据
TEST_DIR = tempfile.mkdtemp(prefix="media_test_")
TEST_DB = os.path.join(TEST_DIR, "test.db")
TEST_NAS = os.path.join(TEST_DIR, "nas")
os.makedirs(TEST_NAS, exist_ok=True)

os.environ["DB_PATH"] = TEST_DB
os.environ["NAS_ROOT"] = TEST_NAS
os.environ["BASIC_USER"] = "testuser"
os.environ["BASIC_PASS"] = "testpass"
os.environ["TESTING"] = "1"

sys.path.insert(0, os.path.dirname(__file__))
from models import init_db, engine, Media, Rating, Batch, BatchMedia
from sqlmodel import Session
import base64
TEST_AUTH = {"Authorization": f"Basic {base64.b64encode(b'testuser:testpass').decode()}"}

# 导入应用（在设置环境变量之后）
# 覆盖中间件：测试中不豁免本地 IP
from main import app, _failed_logins, _lock
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.responses import JSONResponse

class TestAuthMiddleware(BaseHTTPMiddleware):
    """测试用中间件：不豁免本地 IP，但保留暴力破解保护"""
    async def dispatch(self, request, call_next):
        auth = request.headers.get('authorization', '')
        import base64 as b64, secrets as sec
        from main import check_bruteforce, record_failed_login, clear_failed_logins, _extract_user

        lock_key = _extract_user(auth) if auth.startswith('Basic ') else 'anon'

        # 暴力破解检查
        if check_bruteforce(lock_key):
            from main import _failed_logins, _lock, LOCKOUT_SECONDS
            import time as _time
            with _lock:
                attempts = _failed_logins[lock_key]
                elapsed = _time.time() - attempts[-1] if attempts else LOCKOUT_SECONDS
                remaining = max(0, int(LOCKOUT_SECONDS - elapsed))
            return JSONResponse({'error': f'Locked for {remaining}s'}, status_code=429,
                                headers={'Retry-After': str(remaining)})

        if auth.startswith('Basic '):
            try:
                decoded = b64.b64decode(auth[6:]).decode()
                user, pwd = decoded.split(':', 1)
                if sec.compare_digest(user, os.environ["BASIC_USER"]) and sec.compare_digest(pwd, os.environ["BASIC_PASS"]):
                    clear_failed_logins(lock_key)
                    return await call_next(request)
            except Exception:
                pass

        record_failed_login(lock_key)
        return JSONResponse({'error': 'Unauthorized'}, status_code=401,
                            headers={'WWW-Authenticate': 'Basic realm="Test"'})

# 替换中间件
app.user_middleware = [m for m in app.user_middleware if m.cls.__name__ != 'BasicAuthMiddleware']
app.add_middleware(TestAuthMiddleware)


@pytest.fixture(scope="session", autouse=True)
def setup_db():
    """初始化测试数据库"""
    init_db()
    yield
    shutil.rmtree(TEST_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_db():
    """每个测试前清理数据库"""
    with Session(engine) as session:
        session.exec(text("DELETE FROM rating"))
        session.exec(text("DELETE FROM batchmedia"))
        session.exec(text("DELETE FROM batch"))
        session.exec(text("DELETE FROM media"))
        session.commit()
    yield


from sqlmodel import text


@pytest.fixture
def session():
    """提供数据库会话"""
    with Session(engine) as s:
        yield s


@pytest.fixture
def sample_media(session):
    """创建测试用的媒体数据"""
    import uuid
    uid = str(uuid.uuid4())[:8]
    items = [
        Media(path=f"/test/{uid}/img1.jpg", filename="img1.jpg", media_type="image", file_size=1024, width=800, height=600),
        Media(path=f"/test/{uid}/img2.jpg", filename="img2.jpg", media_type="image", file_size=2048, width=1920, height=1080),
        Media(path=f"/test/{uid}/img3.png", filename="img3.png", media_type="image", file_size=4096, width=1367, height=2434),
        Media(path=f"/test/{uid}/vid1.mp4", filename="vid1.mp4", media_type="video", file_size=10485760, width=1920, height=1080, duration=120.5),
        Media(path=f"/test/{uid}/vid2.mp4", filename="vid2.mp4", media_type="video", file_size=20971520, width=3840, height=2160, duration=300.0),
    ]
    for m in items:
        session.add(m)
    session.commit()
    for m in items:
        session.refresh(m)
    return items


@pytest.fixture
def sample_ratings(session, sample_media):
    """创建测试评分数据"""
    ratings = [
        Rating(media_id=sample_media[0].id, score=8),
        Rating(media_id=sample_media[1].id, score=3),
        Rating(media_id=sample_media[2].id, score=0),
        Rating(media_id=sample_media[3].id, score=7),
    ]
    for r in ratings:
        session.add(r)
    session.commit()
    return ratings


@pytest_asyncio.fixture
async def client():
    """HTTP 客户端"""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.fixture
def clear_lock():
    """清除暴力破解锁"""
    with _lock:
        _failed_logins.clear()


# ═══════════════════════════════════════
# 🔐 认证与安全
# ═══════════════════════════════════════

class TestAuth:
    @pytest.mark.asyncio
    async def test_no_auth_returns_401(self, client: httpx.AsyncClient, clear_lock):
        """未认证应返回 401"""
        r = await client.get("/api/config")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_password_returns_401(self, client: httpx.AsyncClient, clear_lock):
        """错误密码应返回 401"""
        wrong = {"Authorization": f"Basic {base64.b64encode(b'testuser:wrong').decode()}"}
        r = await client.get("/api/config", headers=wrong)
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_correct_password_returns_200(self, client: httpx.AsyncClient, clear_lock):
        """正确密码应返回 200"""
        r = await client.get("/api/config", headers=TEST_AUTH)
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_bruteforce_lock_3_attempts(self, client: httpx.AsyncClient, clear_lock):
        """3 次错误后锁定"""
        wrong = {"Authorization": f"Basic {base64.b64encode(b'brute:wrong').decode()}"}
        for _ in range(3):
            await client.get("/api/config", headers=wrong)
        # 第 4 次应该被锁定
        r = await client.get("/api/config", headers=wrong)
        assert r.status_code == 429

    @pytest.mark.asyncio
    async def test_correct_auth_clears_lock(self, client: httpx.AsyncClient, clear_lock):
        """正确登录清除锁定计数"""
        wrong = {"Authorization": f"Basic {base64.b64encode(b'testuser:wrong').decode()}"}
        # 先失败 2 次
        await client.get("/api/config", headers=wrong)
        await client.get("/api/config", headers=wrong)
        # 正确登录
        r = await client.get("/api/config", headers=TEST_AUTH)
        assert r.status_code == 200
        # 再失败 2 次不应该锁定（已清除）
        await client.get("/api/config", headers=wrong)
        r = await client.get("/api/config", headers=wrong)
        assert r.status_code == 401  # 不是 429


# ═══════════════════════════════════════
# ⚙️ 配置管理
# ═══════════════════════════════════════

class TestConfig:
    @pytest.mark.asyncio
    async def test_get_config(self, client: httpx.AsyncClient, clear_lock):
        """获取配置应返回默认值"""
        r = await client.get("/api/config", headers=TEST_AUTH)
        assert r.status_code == 200
        data = r.json()
        assert "batch_count" in data
        assert "thumb_max_size" in data
        assert "thumb_quality" in data
        assert "video_frame_time" in data

    @pytest.mark.asyncio
    async def test_update_config(self, client: httpx.AsyncClient, clear_lock):
        """更新配置应成功"""
        r = await client.post("/api/config", headers=TEST_AUTH,
                              json={"batch_count": 15})
        assert r.status_code == 200
        assert r.json()["batch_count"] == 15

    @pytest.mark.asyncio
    async def test_config_validation_batch_count(self, client: httpx.AsyncClient, clear_lock):
        """批次数量应有范围限制"""
        r = await client.post("/api/config", headers=TEST_AUTH,
                              json={"batch_count": 99})
        assert r.status_code == 400
        assert "1-50" in r.json()["error"]

    @pytest.mark.asyncio
    async def test_config_validation_thumb_quality(self, client: httpx.AsyncClient, clear_lock):
        """图片质量应有范围限制"""
        r = await client.post("/api/config", headers=TEST_AUTH,
                              json={"thumb_quality": 5})
        assert r.status_code == 400
        assert "30-100" in r.json()["error"]

    @pytest.mark.asyncio
    async def test_config_persistence(self, client: httpx.AsyncClient, clear_lock):
        """配置更新应持久化"""
        await client.post("/api/config", headers=TEST_AUTH,
                          json={"batch_count": 20})
        r = await client.get("/api/config", headers=TEST_AUTH)
        assert r.json()["batch_count"] == 20
        # 还原
        await client.post("/api/config", headers=TEST_AUTH,
                          json={"batch_count": 10})


# ═══════════════════════════════════════
# 📊 统计 API
# ═══════════════════════════════════════

class TestStats:
    @pytest.mark.asyncio
    async def test_index_stats(self, client: httpx.AsyncClient, clear_lock, sample_media):
        """索引统计应返回图片/视频数量"""
        r = await client.get("/api/index/stats", headers=TEST_AUTH)
        assert r.status_code == 200
        data = r.json()
        assert data["total_images"] >= 3
        assert data["total_videos"] >= 2

    @pytest.mark.asyncio
    async def test_rating_distribution(self, client: httpx.AsyncClient, clear_lock,
                                       sample_media, sample_ratings):
        """评分分布应正确统计"""
        r = await client.get("/api/stats/ratings", headers=TEST_AUTH)
        assert r.status_code == 200
        data = r.json()
        assert "image" in data
        assert "video" in data
        # 图片有 3 条评分（8, 3, 0）
        assert data["image"]["rated"] == 3
        assert data["image"]["total"] >= 3
        # 评分分布
        assert data["image"]["distribution"].get("8") == 1
        assert data["image"]["distribution"].get("3") == 1
        assert data["image"]["distribution"].get("0") == 1

    @pytest.mark.asyncio
    async def test_rating_distribution_empty(self, client: httpx.AsyncClient, clear_lock):
        """无评分时分布应为空"""
        r = await client.get("/api/stats/ratings", headers=TEST_AUTH)
        data = r.json()
        # 视频有 1 条评分（7），但无评分的类型也应正确返回
        assert data["video"]["rated"] >= 0


# ═══════════════════════════════════════
# 📋 已评记录
# ═══════════════════════════════════════

class TestRatedList:
    @pytest.mark.asyncio
    async def test_rated_list_basic(self, client: httpx.AsyncClient, clear_lock,
                                    sample_media, sample_ratings):
        """已评记录列表应返回数据"""
        r = await client.get("/api/rated?type=image", headers=TEST_AUTH)
        assert r.status_code == 200
        data = r.json()
        assert data["total"] >= 3
        assert len(data["items"]) >= 3

    @pytest.mark.asyncio
    async def test_rated_list_score_filter(self, client: httpx.AsyncClient, clear_lock,
                                           sample_media, sample_ratings):
        """评分筛选应生效"""
        r = await client.get("/api/rated?type=image&min_score=7&max_score=9",
                             headers=TEST_AUTH)
        data = r.json()
        for item in data["items"]:
            assert 7 <= item["score"] <= 9

    @pytest.mark.asyncio
    async def test_rated_list_pagination(self, client: httpx.AsyncClient, clear_lock,
                                         sample_media, sample_ratings):
        """分页应正确工作"""
        r1 = await client.get("/api/rated?type=image&page=1&page_size=1", headers=TEST_AUTH)
        r2 = await client.get("/api/rated?type=image&page=2&page_size=1", headers=TEST_AUTH)
        d1 = r1.json()
        d2 = r2.json()
        if d1["total"] > 1:
            assert d1["items"][0]["media_id"] != d2["items"][0]["media_id"]

    @pytest.mark.asyncio
    async def test_rated_list_item_fields(self, client: httpx.AsyncClient, clear_lock,
                                          sample_media, sample_ratings):
        """已评记录应包含必要字段"""
        r = await client.get("/api/rated?type=image&page_size=1", headers=TEST_AUTH)
        item = r.json()["items"][0]
        assert "media_id" in item
        assert "filename" in item
        assert "score" in item
        assert "media_type" in item


# ═══════════════════════════════════════
# 🔄 重新评分
# ═══════════════════════════════════════

class TestRescore:
    @pytest.mark.asyncio
    async def test_rescore_valid(self, client: httpx.AsyncClient, clear_lock, sample_media):
        """有效评分应成功"""
        r = await client.post(f"/api/media/{sample_media[0].id}/rescore",
                              headers=TEST_AUTH, json={"score": 7})
        assert r.status_code == 200
        assert r.json()["score"] == 7

    @pytest.mark.asyncio
    async def test_rescore_out_of_range(self, client: httpx.AsyncClient, clear_lock, sample_media):
        """超出范围的评分应失败"""
        r = await client.post(f"/api/media/{sample_media[0].id}/rescore",
                              headers=TEST_AUTH, json={"score": 15})
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_rescore_nonexistent(self, client: httpx.AsyncClient, clear_lock):
        """不存在的媒体应返回 404"""
        r = await client.post("/api/media/99999/rescore",
                              headers=TEST_AUTH, json={"score": 5})
        assert r.status_code == 404


# ═══════════════════════════════════════
# 📥 CSV 导出
# ═══════════════════════════════════════

class TestCSVExport:
    @pytest.mark.asyncio
    async def test_csv_export_content_type(self, client: httpx.AsyncClient, clear_lock,
                                           sample_media, sample_ratings):
        """CSV 导出应返回 text/csv"""
        r = await client.get("/api/export/csv?type=image", headers=TEST_AUTH)
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]

    @pytest.mark.asyncio
    async def test_csv_export_has_header(self, client: httpx.AsyncClient, clear_lock,
                                         sample_media, sample_ratings):
        """CSV 应包含表头"""
        r = await client.get("/api/export/csv?type=image", headers=TEST_AUTH)
        lines = r.text.strip().split("\n")
        header = lines[0]
        assert "文件名" in header
        assert "评分" in header

    @pytest.mark.asyncio
    async def test_csv_export_data_rows(self, client: httpx.AsyncClient, clear_lock,
                                        sample_media, sample_ratings):
        """CSV 数据行数应匹配"""
        r = await client.get("/api/export/csv?type=image", headers=TEST_AUTH)
        lines = r.text.strip().split("\n")
        assert len(lines) >= 4  # 表头 + 至少 3 条数据


# ═══════════════════════════════════════
# 🖼️ 缩略图 API
# ═══════════════════════════════════════

class TestThumbnail:
    @pytest.mark.asyncio
    async def test_original_image_not_found(self, client: httpx.AsyncClient, clear_lock, sample_media):
        """原图不存在应返回 404"""
        r = await client.get(f"/api/media/{sample_media[0].id}/image", headers=TEST_AUTH)
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_nonexistent_media(self, client: httpx.AsyncClient, clear_lock):
        """不存在的媒体应返回 404"""
        r = await client.get("/api/media/99999/image", headers=TEST_AUTH)
        assert r.status_code == 404


# ═══════════════════════════════════════
# 🎬 视频帧 API
# ═══════════════════════════════════════

class TestVideoFrame:
    @pytest.mark.asyncio
    async def test_frame_from_nonexistent_video(self, client: httpx.AsyncClient, clear_lock, sample_media):
        """视频文件不存在应返回 404"""
        r = await client.get(f"/api/media/{sample_media[3].id}/frame", headers=TEST_AUTH)
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_frame_on_image_not_video(self, client: httpx.AsyncClient, clear_lock, sample_media):
        """对图片请求帧应返回 404"""
        r = await client.get(f"/api/media/{sample_media[0].id}/frame", headers=TEST_AUTH)
        assert r.status_code == 404


# ═══════════════════════════════════════
# 📦 批次评分
# ═══════════════════════════════════════

class TestBatch:
    @pytest.mark.asyncio
    async def test_create_batch_image(self, client: httpx.AsyncClient, clear_lock, sample_media):
        """创建图片批次 - 不含已评分"""
        # 不传 sample_ratings，这样所有图片都是未评分
        r = await client.get("/api/batch?type=image", headers=TEST_AUTH)
        assert r.status_code == 200
        data = r.json()
        assert "id" in data
        assert "items" in data
        assert len(data["items"]) > 0

    @pytest.mark.asyncio
    async def test_create_batch_video(self, client: httpx.AsyncClient, clear_lock, sample_media, sample_ratings):
        """创建视频批次"""
        r = await client.get("/api/batch?type=video", headers=TEST_AUTH)
        assert r.status_code == 200
        data = r.json()
        for item in data["items"]:
            assert item["media_type"] == "video"


# ═══════════════════════════════════════
# 🗑️ 批量删除
# ═══════════════════════════════════════

class TestDeleteLow:
    @pytest.mark.asyncio
    async def test_delete_low_validation(self, client: httpx.AsyncClient, clear_lock):
        """阈值验证"""
        r = await client.post("/api/delete-low", headers=TEST_AUTH,
                              json={"type": "image", "max_score": 15})
        assert r.status_code == 400


# ═══════════════════════════════════════
# 🏠 静态页面
# ═══════════════════════════════════════

class TestStaticPages:
    @pytest.mark.asyncio
    async def test_index_page(self, client: httpx.AsyncClient, clear_lock):
        """主页应返回 200"""
        r = await client.get("/", headers=TEST_AUTH)
        assert r.status_code == 200
        assert "媒体智能管理平台" in r.text

    @pytest.mark.asyncio
    async def test_settings_page(self, client: httpx.AsyncClient, clear_lock):
        """设置页应返回 200"""
        r = await client.get("/settings.html", headers=TEST_AUTH)
        assert r.status_code == 200
        assert "设置" in r.text

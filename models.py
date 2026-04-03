"""数据库模型"""
from sqlmodel import SQLModel, Field, create_engine, Session
from datetime import datetime
from typing import Optional
import os

DB_PATH = os.environ.get("DB_PATH", "media_manager.db")
DATABASE_URL = f"sqlite:///{DB_PATH}"
engine = create_engine(DATABASE_URL, echo=False)


class Media(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    path: str = Field(index=True, unique=True)
    filename: str
    media_type: str  # image / video
    file_size: int = 0
    md5: str = ""
    width: int = 0
    height: int = 0
    duration: float = 0.0  # video only
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    indexed_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class Rating(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    media_id: int = Field(index=True)
    score: int = 0  # 0-8
    batch_id: Optional[int] = Field(default=None, index=True)
    rated_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class Batch(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    media_type: str
    status: str = "pending"  # pending / done
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class BatchMedia(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    batch_id: int = Field(index=True)
    media_id: int = Field(index=True)
    position: int = 0


def init_db():
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session

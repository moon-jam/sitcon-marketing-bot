"""
資料庫操作模組 - 處理 reviews 和 reviewers 的 CRUD 操作
"""

import os
import aiosqlite
from datetime import datetime
from enum import Enum
from typing import Optional
from pathlib import Path

# 資料庫路徑可透過環境變數設定（Docker 使用）
DB_PATH = Path(os.getenv("DB_PATH", Path(__file__).parent / "reviews.db"))


class ReviewStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    NEED_FIX = "need_fix"


async def init_db():
    """初始化資料庫，建立必要的表"""
    async with aiosqlite.connect(DB_PATH) as db:
        # Reviews 表
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sponsor_name TEXT NOT NULL,
                link TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                submitter_id INTEGER,
                submitter_username TEXT,
                comment TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        )

        # 檢查並新增 comment 欄位（相容舊資料庫）
        try:
            await db.execute("SELECT comment FROM reviews LIMIT 1")
        except aiosqlite.OperationalError:
            await db.execute("ALTER TABLE reviews ADD COLUMN comment TEXT")

        # Reviewers 表
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS reviewers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        )

        await db.commit()


# ==================== Reviews 操作 ====================


async def add_review(
    sponsor_name: str, link: str, submitter_id: int, submitter_username: str
) -> int:
    """新增一筆 review 請求，回傳新增的 ID"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO reviews (sponsor_name, link, status, submitter_id, submitter_username)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                sponsor_name,
                link,
                ReviewStatus.PENDING.value,
                submitter_id,
                submitter_username,
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def get_review_by_name(sponsor_name: str) -> Optional[dict]:
    """根據贊助商名稱取得 review（取最新的一筆）"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM reviews 
            WHERE sponsor_name = ? 
            ORDER BY created_at DESC 
            LIMIT 1
            """,
            (sponsor_name,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def update_review_status(
    sponsor_name: str, status: ReviewStatus, comment: str = None
) -> bool:
    """更新 review 狀態（可選帶評語），回傳是否成功"""
    async with aiosqlite.connect(DB_PATH) as db:
        if comment is not None:
            cursor = await db.execute(
                """
                UPDATE reviews 
                SET status = ?, comment = ?, updated_at = ? 
                WHERE sponsor_name = ? AND status != ?
                """,
                (
                    status.value,
                    comment,
                    datetime.now(),
                    sponsor_name,
                    ReviewStatus.APPROVED.value,
                ),
            )
        else:
            cursor = await db.execute(
                """
                UPDATE reviews 
                SET status = ?, updated_at = ? 
                WHERE sponsor_name = ? AND status != ?
                """,
                (
                    status.value,
                    datetime.now(),
                    sponsor_name,
                    ReviewStatus.APPROVED.value,
                ),
            )
        await db.commit()
        return cursor.rowcount > 0


async def get_reviews_by_status(status: ReviewStatus) -> list[dict]:
    """取得特定狀態的所有 reviews"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM reviews WHERE status = ? ORDER BY created_at DESC",
            (status.value,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_pending_reviews() -> list[dict]:
    """取得所有待審核的 reviews"""
    return await get_reviews_by_status(ReviewStatus.PENDING)


async def get_need_fix_reviews() -> list[dict]:
    """取得所有需要修改的 reviews"""
    return await get_reviews_by_status(ReviewStatus.NEED_FIX)


async def get_all_active_reviews() -> list[dict]:
    """取得所有進行中的 reviews（pending + need_fix）"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM reviews 
            WHERE status IN (?, ?) 
            ORDER BY status, created_at DESC
            """,
            (ReviewStatus.PENDING.value, ReviewStatus.NEED_FIX.value),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


# ==================== Reviewers 操作 ====================


async def add_reviewer(username: str) -> bool:
    """新增 reviewer，回傳是否成功（已存在則失敗）"""
    # 移除 @ 符號
    username = username.lstrip("@")

    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("INSERT INTO reviewers (username) VALUES (?)", (username,))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def remove_reviewer(username: str) -> bool:
    """移除 reviewer，回傳是否成功"""
    username = username.lstrip("@")

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM reviewers WHERE username = ?", (username,)
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_all_reviewers() -> list[str]:
    """取得所有 reviewers 的 username 清單"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT username FROM reviewers") as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

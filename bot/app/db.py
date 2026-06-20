"""PostgreSQL 접근 계층 (asyncpg).

저장 모델은 명세 6번을 따른다: 사람당 핵심 두 값(last_run_date, current_streak)
+ 등록 플래그 + 통계(max_streak, total_runs). OCR 부가정보는 run_logs 로 분리.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import asyncpg

from .streak import recompute_from_dates

SCHEMA = """
CREATE TABLE IF NOT EXISTS runners (
    user_id        BIGINT PRIMARY KEY,
    registered     BOOLEAN     NOT NULL DEFAULT TRUE,
    last_run_date  DATE,
    current_streak INTEGER     NOT NULL DEFAULT 0,
    max_streak     INTEGER     NOT NULL DEFAULT 0,
    total_runs     INTEGER     NOT NULL DEFAULT 0,
    registered_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS run_logs (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    run_date    DATE   NOT NULL,
    distance_km NUMERIC,
    raw_text    TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS run_logs_user_date_idx ON run_logs (user_id, run_date);

-- OCR 부가정보 4필드(거리는 위에 이미 있음). 기존 테이블에도 멱등 추가.
ALTER TABLE run_logs ADD COLUMN IF NOT EXISTS duration_sec    INTEGER;
ALTER TABLE run_logs ADD COLUMN IF NOT EXISTS pace_sec_per_km INTEGER;
ALTER TABLE run_logs ADD COLUMN IF NOT EXISTS calories        INTEGER;

-- 하루 1회 불변식: 같은 사용자·같은 날 중복 기록 금지(동시 업로드 경합 방어).
CREATE UNIQUE INDEX IF NOT EXISTS run_logs_user_day_uq ON run_logs (user_id, run_date);
"""


@dataclass
class Runner:
    user_id: int
    registered: bool
    last_run_date: date | None
    current_streak: int
    max_streak: int
    total_runs: int


class Database:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
        async with self._pool.acquire() as conn:
            await conn.execute(SCHEMA)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database.connect() 가 먼저 호출되어야 합니다.")
        return self._pool

    # --- 등록제 (명세 4) ---------------------------------------------------

    async def register(self, user_id: int) -> None:
        """선수 등록(옵트인). 이미 있으면 registered=TRUE 로 되살린다(데이터 보존)."""
        await self.pool.execute(
            """
            INSERT INTO runners (user_id, registered)
            VALUES ($1, TRUE)
            ON CONFLICT (user_id) DO UPDATE SET registered = TRUE
            """,
            user_id,
        )

    async def unregister(self, user_id: int) -> bool:
        """등록 취소. registered=FALSE 로만 바꾸고 기록은 남긴다. 등록돼 있었으면 True."""
        row = await self.pool.fetchrow(
            """
            UPDATE runners SET registered = FALSE
            WHERE user_id = $1 AND registered = TRUE
            RETURNING user_id
            """,
            user_id,
        )
        return row is not None

    async def is_registered(self, user_id: int) -> bool:
        val = await self.pool.fetchval(
            "SELECT registered FROM runners WHERE user_id = $1", user_id
        )
        return bool(val)

    # --- 스트릭 데이터 ------------------------------------------------------

    async def load(self, user_id: int) -> Runner | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM runners WHERE user_id = $1", user_id
        )
        return _to_runner(row)

    async def record_run(
        self,
        user_id: int,
        run_date: date,
        current_streak: int,
        max_streak: int,
        *,
        distance_km: Decimal | None = None,
        duration_sec: int | None = None,
        pace_sec_per_km: int | None = None,
        calories: int | None = None,
        raw_text: str | None = None,
    ) -> bool:
        """실제 뛴 날 1건 반영 (명세 7.1·7.3).

        runners 갱신과 run_logs 원장 기록을 한 트랜잭션으로 묶어 항상 일관되게 한다.
        run_logs 는 '뛴 날 1행' 원장이라 취소/되돌리기 재계산의 근거가 된다.
        OCR 부가정보(거리·시간·페이스·칼로리)는 있으면 채우고 없으면 NULL.

        Returns:
            True  = 새로 기록됨.
            False = 같은 날 이미 기록이 있어 무시(동시 업로드 경합 등). 호출자는 응답을 보내지 않는다.
        """
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    # 원장 INSERT 를 먼저 시도 → 같은 날 유니크 충돌이면 즉시 롤백.
                    await conn.execute(
                        """
                        INSERT INTO run_logs
                            (user_id, run_date, distance_km, duration_sec, pace_sec_per_km, calories, raw_text)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        """,
                        user_id,
                        run_date,
                        distance_km,
                        duration_sec,
                        pace_sec_per_km,
                        calories,
                        raw_text,
                    )
                    await conn.execute(
                        """
                        UPDATE runners
                        SET last_run_date = $2,
                            current_streak = $3,
                            max_streak = $4,
                            total_runs = total_runs + 1
                        WHERE user_id = $1
                        """,
                        user_id,
                        run_date,
                        current_streak,
                        max_streak,
                    )
            return True
        except asyncpg.UniqueViolationError:
            return False

    async def undo_last_run(self, user_id: int) -> tuple[date, int, int] | None:
        """가장 최근 뛴 기록 1건을 취소하고 원장에서 스트릭을 재계산한다.

        Returns:
            (삭제된_run_date, 재계산된_current_streak, 재계산된_total_runs) 또는
            취소할 기록이 없으면 None.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    DELETE FROM run_logs
                    WHERE id = (
                        SELECT id FROM run_logs
                        WHERE user_id = $1
                        ORDER BY run_date DESC, id DESC
                        LIMIT 1
                    )
                    RETURNING run_date
                    """,
                    user_id,
                )
                if row is None:
                    return None
                deleted: date = row["run_date"]

                rows = await conn.fetch(
                    "SELECT run_date FROM run_logs WHERE user_id = $1", user_id
                )
                dates = [r["run_date"] for r in rows]
                cur, mx, last, total = recompute_from_dates(dates)
                await conn.execute(
                    """
                    UPDATE runners
                    SET current_streak = $2, max_streak = $3,
                        last_run_date = $4, total_runs = $5
                    WHERE user_id = $1
                    """,
                    user_id,
                    cur,
                    mx,
                    last,
                    total,
                )
                return deleted, cur, total

    async def list_registered(self) -> list[Runner]:
        rows = await self.pool.fetch(
            "SELECT * FROM runners WHERE registered = TRUE"
        )
        return [_to_runner(r) for r in rows]  # type: ignore[misc]

    async def stats(self, user_id: int) -> dict:
        """run_logs 기반 누적 집계 (명세 7.3 / OCR 부가정보). 값 없는 항목은 NULL 무시."""
        row = await self.pool.fetchrow(
            """
            SELECT
                count(*)                  AS runs,
                sum(distance_km)          AS dist_sum,
                count(distance_km)        AS dist_n,
                sum(duration_sec)         AS dur_sum,
                count(duration_sec)       AS dur_n,
                avg(pace_sec_per_km)      AS pace_avg,
                count(pace_sec_per_km)    AS pace_n,
                sum(calories)             AS cal_sum,
                count(calories)           AS cal_n
            FROM run_logs
            WHERE user_id = $1
            """,
            user_id,
        )
        return dict(row) if row else {}

    async def distance_totals(self) -> dict[int, Decimal]:
        """사용자별 누적 거리(km) 합계. 거리 기록이 없는 사람은 제외. (리더보드용, 1쿼리)"""
        rows = await self.pool.fetch(
            "SELECT user_id, sum(distance_km) AS dist FROM run_logs GROUP BY user_id"
        )
        return {r["user_id"]: r["dist"] for r in rows if r["dist"] is not None}

    async def count_runs_in_month(self, user_id: int, year: int, month: int) -> int:
        val = await self.pool.fetchval(
            """
            SELECT COUNT(*) FROM run_logs
            WHERE user_id = $1
              AND EXTRACT(YEAR FROM run_date) = $2
              AND EXTRACT(MONTH FROM run_date) = $3
            """,
            user_id,
            year,
            month,
        )
        return int(val or 0)


def _to_runner(row: asyncpg.Record | None) -> Runner | None:
    if row is None:
        return None
    return Runner(
        user_id=row["user_id"],
        registered=row["registered"],
        last_run_date=row["last_run_date"],
        current_streak=row["current_streak"],
        max_streak=row["max_streak"],
        total_runs=row["total_runs"],
    )

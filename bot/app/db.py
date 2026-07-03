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
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS run_logs_user_date_idx ON run_logs (user_id, run_date);

-- OCR 부가정보 4필드(거리는 위에 이미 있음). 기존 테이블에도 멱등 추가.
ALTER TABLE run_logs ADD COLUMN IF NOT EXISTS duration_sec    INTEGER;
ALTER TABLE run_logs ADD COLUMN IF NOT EXISTS pace_sec_per_km INTEGER;
ALTER TABLE run_logs ADD COLUMN IF NOT EXISTS calories        INTEGER;

-- 데이터 최소화: raw_text(OCR 원문 전문)는 어떤 조회에도 쓰이지 않으면서 타인 식별정보가 섞일 수
-- 있어 보관 위험만 있다 → 컬럼째 제거(기존 행의 원문도 함께 사라짐). 집계 4필드는 별도 컬럼이라 무영향.
ALTER TABLE run_logs DROP COLUMN IF EXISTS raw_text;

-- 하루 1회 불변식 도입 전, 기존 같은 (user_id, run_date) 중복이 있으면 최신 id만 남기고 제거
-- (그렇지 않으면 아래 UNIQUE INDEX 생성이 실패해 connect() 가 죽는다 — 진짜 멱등 보장).
DELETE FROM run_logs a
USING run_logs b
WHERE a.user_id = b.user_id AND a.run_date = b.run_date AND a.id < b.id;

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
        # command_timeout: DB 가 응답 불능일 때 코루틴이 무한 대기 → 풀 고갈되는 것을 방지.
        self._pool = await asyncpg.create_pool(
            self._dsn, min_size=1, max_size=5, command_timeout=15
        )
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

    async def purge_user(self, user_id: int) -> tuple[int, bool]:
        """본인 데이터 **전체 삭제**(개인정보 삭제권). run_logs 전부 + runners 행 제거.

        record_run/undo 와 동일하게 runners 행을 `FOR UPDATE` 로 직렬화한 뒤 삭제(동시 기록 경합 방지).
        Returns: (삭제된 run_logs 건수, runners 행 삭제 여부).
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT 1 FROM runners WHERE user_id = $1 FOR UPDATE", user_id
                )
                deleted = await conn.fetch(
                    "DELETE FROM run_logs WHERE user_id = $1 RETURNING id", user_id
                )
                row = await conn.fetchrow(
                    "DELETE FROM runners WHERE user_id = $1 RETURNING user_id", user_id
                )
                return len(deleted), row is not None

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
        *,
        distance_km: Decimal | None = None,
        duration_sec: int | None = None,
        pace_sec_per_km: int | None = None,
        calories: int | None = None,
    ) -> tuple[bool, int]:
        """실제 뛴 날 1건 반영 (명세 7.1·7.3). runners 를 run_logs 원장으로부터 **재계산**한다.

        - 같은 사용자의 record_run 을 runners 행 `FOR UPDATE` 로 **직렬화** → load→계산→저장
          사이의 경합(TOCTOU) 제거.
        - 증분(+1) 대신 원장 전체 재계산이라 **재등록 후 유령 부활·UPDATE-0행 문제도 함께 사라짐**.
        - OCR 부가정보는 있으면 채우고 없으면 NULL.

        Returns:
            (recorded, current_streak)
            recorded=False = 같은 날 이미 기록(유니크 충돌, 동시 업로드 등) → 호출자는 응답 생략.
        """
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    # 같은 사용자의 동시 기록을 직렬화(등록 사용자라 행이 존재).
                    await conn.execute(
                        "SELECT 1 FROM runners WHERE user_id = $1 FOR UPDATE", user_id
                    )
                    await conn.execute(
                        """
                        INSERT INTO run_logs
                            (user_id, run_date, distance_km, duration_sec, pace_sec_per_km, calories)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        """,
                        user_id,
                        run_date,
                        distance_km,
                        duration_sec,
                        pace_sec_per_km,
                        calories,
                    )
                    rows = await conn.fetch(
                        "SELECT run_date FROM run_logs WHERE user_id = $1", user_id
                    )
                    cur, mx, last, total = recompute_from_dates(
                        [r["run_date"] for r in rows]
                    )
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
            return True, cur
        except asyncpg.UniqueViolationError:
            return False, 0

    async def undo_last_run(self, user_id: int) -> tuple[date, int, int] | None:
        """가장 최근 뛴 기록 1건을 취소하고 원장에서 스트릭을 재계산한다.

        Returns:
            (삭제된_run_date, 재계산된_current_streak, 재계산된_total_runs) 또는
            취소할 기록이 없으면 None.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # record_run 과 동일 행 잠금으로 직렬화(동시 기록/취소 경합 방지).
                await conn.execute(
                    "SELECT 1 FROM runners WHERE user_id = $1 FOR UPDATE", user_id
                )
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
                count(calories)           AS cal_n,
                -- 가중 평균 페이스(총시간/총거리)용: 거리·시간이 '둘 다' 인식된 행만 짝지어 합산.
                -- (각 sum 은 NULL 을 독립적으로 건너뛰므로, 그냥 dur_sum/dist_sum 은 서로 다른
                --  행 집합을 나누게 되어 페이스가 왜곡될 수 있다.)
                sum(distance_km)  FILTER (WHERE distance_km IS NOT NULL AND duration_sec IS NOT NULL) AS paired_dist,
                sum(duration_sec) FILTER (WHERE distance_km IS NOT NULL AND duration_sec IS NOT NULL) AS paired_dur
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

    async def recent_runs(self, user_id: int, limit: int = 12) -> list[dict]:
        """최근 러닝 N건(오래된→최신 순). 성장 추세(스파크라인)용."""
        rows = await self.pool.fetch(
            """
            SELECT run_date, distance_km, duration_sec, pace_sec_per_km, calories
            FROM run_logs WHERE user_id = $1
            ORDER BY run_date DESC, id DESC LIMIT $2
            """,
            user_id,
            limit,
        )
        return [dict(r) for r in reversed(rows)]

    async def month_run_metrics(self, user_id: int, year: int, month: int) -> list[dict]:
        """해당 월의 러닝 기록(달력·월합계용)."""
        # 범위 조건으로 (user_id, run_date) 인덱스를 활용(EXTRACT 는 인덱스 못 탐).
        rows = await self.pool.fetch(
            """
            SELECT run_date, distance_km, duration_sec, calories
            FROM run_logs
            WHERE user_id = $1
              AND run_date >= make_date($2, $3, 1)
              AND run_date < (make_date($2, $3, 1) + INTERVAL '1 month')
            ORDER BY run_date
            """,
            user_id,
            year,
            month,
        )
        return [dict(r) for r in rows]

    async def period_summary(self, user_id: int, start_date: date, end_date: date) -> dict:
        """기간 합계(주간 등). start_date~end_date 포함."""
        row = await self.pool.fetchrow(
            """
            SELECT count(*) AS cnt, sum(distance_km) AS dist,
                   sum(duration_sec) AS dur, sum(calories) AS cal
            FROM run_logs
            WHERE user_id = $1 AND run_date >= $2 AND run_date <= $3
            """,
            user_id,
            start_date,
            end_date,
        )
        return dict(row) if row else {}

    async def count_runs_in_month(self, user_id: int, year: int, month: int) -> int:
        val = await self.pool.fetchval(
            """
            SELECT COUNT(*) FROM run_logs
            WHERE user_id = $1
              AND run_date >= make_date($2, $3, 1)
              AND run_date < (make_date($2, $3, 1) + INTERVAL '1 month')
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

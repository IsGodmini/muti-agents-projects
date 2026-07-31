"""Async Postgres integration with graceful fallback to file-based PlanStore.

When DATABASE_URL is configured, uses asyncpg for persistence.
Otherwise falls back to the existing file-based plan_store.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

_pool = None


async def get_pool():
    """Lazily create and return the asyncpg connection pool."""
    global _pool
    if _pool is not None:
        return _pool

    settings = get_settings()
    if not settings.database_url:
        return None

    try:
        import asyncpg

        dsn = settings.database_url.replace("+asyncpg", "")
        _pool = await asyncpg.create_pool(
            dsn,
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
        logger.info("Postgres pool created (%s)", settings.database_url.split("@")[-1])
        return _pool
    except Exception:
        logger.warning("Failed to create Postgres pool; falling back to file store", exc_info=True)
        return None


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("Postgres pool closed")


async def save_plan_version(plan_id: str, version: int, snapshot: dict, created_by: str = "system") -> bool:
    """Save a plan version to Postgres. Returns True on success."""
    pool = await get_pool()
    if pool is None:
        return False

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO plan_versions (id, plan_id, version, snapshot, created_by, created_at)
                VALUES (gen_random_uuid(), $1, $2, $3, $4, NOW())
                ON CONFLICT (plan_id, version) DO UPDATE SET snapshot = $3
                """,
                plan_id,
                version,
                json.dumps(snapshot, ensure_ascii=False, default=str),
                created_by,
            )
        return True
    except Exception:
        logger.warning("Postgres save_plan_version failed for %s", plan_id, exc_info=True)
        return False


async def log_agent_run(
    plan_id: str,
    thread_id: str,
    agent_name: str,
    model_name: str,
    status: str,
    latency_ms: int | None = None,
    input_summary: dict | None = None,
    output_summary: dict | None = None,
) -> str | None:
    """Log an agent run to Postgres. Returns the run ID or None."""
    pool = await get_pool()
    if pool is None:
        return None

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO agent_runs (id, plan_id, thread_id, agent_name, model_name, status, latency_ms, input_summary, output_summary, created_at)
                VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6, $7, $8, NOW())
                RETURNING id
                """,
                plan_id,
                thread_id,
                agent_name,
                model_name,
                status,
                latency_ms,
                json.dumps(input_summary, ensure_ascii=False, default=str) if input_summary else None,
                json.dumps(output_summary, ensure_ascii=False, default=str) if output_summary else None,
            )
        return str(row["id"]) if row else None
    except Exception:
        logger.warning("Postgres log_agent_run failed", exc_info=True)
        return None


async def log_tool_invocation(
    agent_run_id: str | None,
    tool_name: str,
    risk_level: str,
    input_payload: dict,
    output_payload: dict | None = None,
    status: str = "success",
    latency_ms: int | None = None,
) -> bool:
    """Log a tool invocation to Postgres."""
    pool = await get_pool()
    if pool is None:
        return False

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO tool_invocations (id, agent_run_id, tool_name, risk_level, input_payload, output_payload, status, latency_ms, created_at)
                VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6, $7, NOW())
                """,
                agent_run_id,
                tool_name,
                risk_level,
                json.dumps(input_payload, ensure_ascii=False, default=str),
                json.dumps(output_payload, ensure_ascii=False, default=str) if output_payload else None,
                status,
                latency_ms,
            )
        return True
    except Exception:
        logger.warning("Postgres log_tool_invocation failed", exc_info=True)
        return False


async def save_plan_record(plan_id: str, tenant_id: str, title: str, status: str, request: dict, current_stage: str) -> bool:
    """Upsert a plan record."""
    pool = await get_pool()
    if pool is None:
        return False

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO plans (id, tenant_id, title, status, request, current_stage, created_at, updated_at)
                VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, NOW(), NOW())
                ON CONFLICT (id) DO UPDATE SET status = $4, current_stage = $6, updated_at = NOW()
                """,
                plan_id,
                tenant_id,
                title,
                status,
                json.dumps(request, ensure_ascii=False, default=str),
                current_stage,
            )
        return True
    except Exception:
        logger.warning("Postgres save_plan_record failed for %s", plan_id, exc_info=True)
        return False


async def search_resources_by_embedding(embedding: list[float], limit: int = 10, tenant_id: str | None = None) -> list[dict]:
    """Vector similarity search on travel_resources using pgvector."""
    pool = await get_pool()
    if pool is None:
        return []

    try:
        async with pool.acquire() as conn:
            embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
            query = """
                SELECT id, name, category, attributes, evidence_uri,
                       1 - (embedding <=> $1::vector) AS similarity
                FROM travel_resources
            """
            params: list[Any] = [embedding_str]
            if tenant_id:
                query += " WHERE tenant_id = $2::uuid"
                params.append(tenant_id)
            query += " ORDER BY embedding <=> $1::vector LIMIT $3" if not tenant_id else " ORDER BY embedding <=> $1::vector LIMIT $2"
            params.append(limit)

            rows = await conn.fetch(query, *params)
            return [dict(row) for row in rows]
    except Exception:
        logger.warning("Postgres vector search failed", exc_info=True)
        return []

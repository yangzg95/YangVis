"""Qdrant 向量存储访问。

每个用户一个 collection（``kb_u{owner_id}``）。这不是风格选择而是硬约束：
collection 的向量维度在创建时就固定了，每个用户配置自己的 embedding 模型，
两个用户如果用了不同的模型，维度根本对不上。共享一个 collection 的话，
任何人切换 provider 都得重建一次索引。
"""
from __future__ import annotations

import logging
import uuid
from typing import Iterable, List, Optional, Sequence

from qdrant_client import AsyncQdrantClient, models

from app.config import get_settings

logger = logging.getLogger("yangvis.vectorstore")

settings = get_settings()


class VectorStoreError(RuntimeError):
    """Qdrant 连不上，或者拒绝了这次操作。"""


def collection_name(owner_id: int) -> str:
    return f"kb_u{owner_id}"


def new_point_id() -> str:
    return uuid.uuid4().hex


def _client() -> AsyncQdrantClient:
    # 每次调用都新建，不共用：Gunicorn 会 fork worker，在前 fork 阶段创建
    # 的 async 客户端会带着一条在子进程里已经不存在的 event loop。
    return AsyncQdrantClient(
        host=settings.QDRANT_HOST,
        port=settings.QDRANT_PORT,
        timeout=settings.QDRANT_TIMEOUT,
    )


async def ensure_collection(name: str, vector_size: int, *, recreate: bool = False) -> None:
    """collection 不存在就创建，也可以先删掉再建。"""
    client = _client()
    try:
        exists = await client.collection_exists(name)
        if exists and recreate:
            await client.delete_collection(name)
            exists = False
        if not exists:
            await client.create_collection(
                collection_name=name,
                vectors_config=models.VectorParams(
                    size=vector_size,
                    distance=models.Distance.COSINE,
                ),
            )
            logger.info("created qdrant collection %s (size=%s)", name, vector_size)
    except Exception as exc:  # noqa: BLE001 - qdrant 会抛出好几种异常类型
        raise VectorStoreError(f"向量库操作失败：{exc}") from exc
    finally:
        await client.close()


async def drop_collection(name: str) -> None:
    client = _client()
    try:
        if await client.collection_exists(name):
            await client.delete_collection(name)
            logger.info("dropped qdrant collection %s", name)
    except Exception as exc:  # noqa: BLE001
        raise VectorStoreError(f"删除向量集合失败：{exc}") from exc
    finally:
        await client.close()


async def upsert_points(
    name: str,
    *,
    point_ids: Sequence[str],
    vectors: Sequence[Sequence[float]],
    payloads: Sequence[dict],
) -> None:
    """写入 chunk 向量。传 ids 进来是为了让重建索引时可以原地覆盖。"""
    if not point_ids:
        return

    client = _client()
    try:
        await client.upsert(
            collection_name=name,
            points=models.Batch(
                ids=list(point_ids),
                vectors=[list(vector) for vector in vectors],
                payloads=list(payloads),
            ),
            wait=True,
        )
    except Exception as exc:  # noqa: BLE001
        raise VectorStoreError(f"写入向量失败：{exc}") from exc
    finally:
        await client.close()


async def delete_by_doc(name: str, owner_id: int, doc_id: int) -> None:
    """删掉某一篇文档名下的所有 point。"""
    client = _client()
    try:
        if not await client.collection_exists(name):
            return
        await client.delete(
            collection_name=name,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="owner_id", match=models.MatchValue(value=owner_id)
                        ),
                        models.FieldCondition(
                            key="doc_id", match=models.MatchValue(value=doc_id)
                        ),
                    ]
                )
            ),
            wait=True,
        )
    except Exception as exc:  # noqa: BLE001
        raise VectorStoreError(f"删除向量失败：{exc}") from exc
    finally:
        await client.close()


async def search(
    name: str,
    *,
    vector: Sequence[float],
    owner_id: int,
    top_k: int = 8,
    type_ids: Optional[Sequence[int]] = None,
    score_threshold: Optional[float] = None,
) -> List[models.ScoredPoint]:
    """查询 collection。

    ``owner_id`` 照样要过滤，哪怕这个 collection 本来就只属于一个用户。
    它今天是冗余的，而这正是重点：万一以后改成共享 collection，过滤条件
    早就在那儿了，而不是变成某个人漏掉的那一行。

    项目维度的限定，传到这里时是项目下的 ``type_ids``，而不是它自己的
    ``project_id``。在项目这个概念出现之前建好的 point，payload 里根本没有
    ``project_id``，按它过滤会让所有旧 chunk 在全量重建索引之前一声不响地
    全被漏掉；而 ``type_id`` 从一开始就在 payload 里。
    """
    conditions: List[models.Condition] = [
        models.FieldCondition(key="owner_id", match=models.MatchValue(value=owner_id))
    ]
    if type_ids is not None:
        conditions.append(
            models.FieldCondition(
                key="type_id", match=models.MatchAny(any=list(type_ids))
            )
        )

    client = _client()
    try:
        if not await client.collection_exists(name):
            return []
        return await client.search(
            collection_name=name,
            query_vector=list(vector),
            query_filter=models.Filter(must=conditions),
            limit=top_k,
            score_threshold=score_threshold,
            with_payload=True,
        )
    except Exception as exc:  # noqa: BLE001
        raise VectorStoreError(f"向量检索失败：{exc}") from exc
    finally:
        await client.close()


async def count_points(name: str) -> int:
    client = _client()
    try:
        if not await client.collection_exists(name):
            return 0
        result = await client.count(collection_name=name, exact=True)
        return int(result.count)
    except Exception as exc:  # noqa: BLE001
        raise VectorStoreError(f"统计向量数量失败：{exc}") from exc
    finally:
        await client.close()


async def health() -> bool:
    """Qdrant 是否连得上，供就绪探测接口使用。"""
    client = _client()
    try:
        await client.get_collections()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("qdrant unreachable: %s", exc)
        return False
    finally:
        await client.close()

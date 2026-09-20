"""模型 provider 客户端与连通性探测。

不假定任何厂商：唯一的约定就是一套 OpenAI 兼容的 HTTP API，因此一份配置
无非是 ``base_url`` + ``model`` + ``api_key``。凡是与某一家 provider 强相关
的东西（向量维度、``model`` 到底指模型名还是部署 id），一律靠探测得出，
而不是写死在代码里。

客户端每次请求现建而不缓存：``ModelConfig`` 是按用户存的、随时可改，
长期存活的客户端会心安理得地继续用着用户早已改掉的凭证。
"""
from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from app.config import get_settings

from app.config import get_settings
from app.crypto import decrypt
from app.models.entities import ModelConfig

logger = logging.getLogger("yangvis.providers")
settings = get_settings()

# 发给 /embeddings，纯粹是为了观察返回结果的维度。
PROBE_TEXT = "yangvis connectivity probe"


class ProviderError(RuntimeError):
    """上游模型 provider 拒绝了请求，或者根本连不上。"""


def _api_key(config: ModelConfig) -> str:
    # 挂在网关后面的 OpenAI 兼容服务往往不需要 key，但客户端要求这里
    # 必须是个非空字符串。
    return decrypt(config.api_key_enc) or "not-needed"


def build_chat_model(
    config: ModelConfig,
    *,
    temperature: float = 0.3,
    streaming: bool = False,
    timeout: Optional[float] = None,
) -> ChatOpenAI:
    """根据已保存的配置创建一个对话客户端。

    ``timeout`` 缺省用全局 ``MODEL_HTTP_TIMEOUT``（30s，适合对话与连通性测试）；
    简历分析这类一次性长输出的后台任务应显式传更长的值——非流式调用要等整个
    completion 落地才返回，30s 几乎必然超时。
    """
    return ChatOpenAI(
        model=config.model_name,
        base_url=config.base_url,
        api_key=_api_key(config),
        temperature=temperature,
        streaming=streaming,
        timeout=timeout if timeout is not None else settings.MODEL_HTTP_TIMEOUT,
        max_retries=1,
    )


def build_embeddings(config: ModelConfig) -> OpenAIEmbeddings:
    """根据已保存的配置创建一个 embeddings 客户端。"""
    return OpenAIEmbeddings(
        model=config.model_name,
        base_url=config.base_url,
        api_key=_api_key(config),
        timeout=settings.MODEL_HTTP_TIMEOUT,
        max_retries=1,
        # 部分 OpenAI 兼容服务不接受该客户端默认使用的 base64
        # encoding_format，而且它用来预切分文本的 tokeniser 本来也
        # 对不上非 OpenAI 的模型。
        check_embedding_ctx_length=False,
    )


def _friendly_error(exc: Exception) -> str:
    """把 provider 抛出的异常转成用户能看懂的提示。"""
    text = str(exc)
    lowered = text.lower()
    if "401" in text or "invalid_api_key" in lowered or "unauthorized" in lowered:
        return "认证失败：api_key 无效或没有该模型的权限"
    if "404" in text or "not found" in lowered or "does not exist" in lowered:
        return (
            "模型不存在：请检查 model 名称与 base_url。"
            "部分服务商此处要求填写部署 / 接入点 ID 而非模型名。"
        )
    if "timeout" in lowered or "timed out" in lowered:
        return "连接超时：base_url 不可达或响应过慢"
    if "connection" in lowered or "getaddrinfo" in lowered or "name or service" in lowered:
        return "无法连接：请检查 base_url 是否正确"
    if "429" in text or "rate limit" in lowered:
        return "触发限流：请稍后重试"
    return f"调用失败：{text[:200]}"


async def test_chat_model(config: ModelConfig) -> Tuple[bool, str]:
    """发一条最短的 prompt，验证 base_url + api_key + model 在一起能用。"""
    try:
        client = build_chat_model(config, temperature=0.0)
        result = await client.ainvoke(
            [
                SystemMessage(content="Reply with the single word: ok"),
                HumanMessage(content="ping"),
            ]
        )
    except Exception as exc:  # noqa: BLE001 - provider SDK 抛出的异常类型五花八门
        logger.warning("chat model test failed for config %s: %s", config.id, exc)
        return False, _friendly_error(exc)

    text = (result.content or "").strip() if hasattr(result, "content") else ""
    return True, f"连接成功，模型返回：{text[:64] or '(空响应)'}"


async def probe_embedding_dimension(config: ModelConfig) -> Tuple[bool, str, int | None]:
    """验证 embedding 接口可用，并实测它输出的向量维度。

    维度是实测出来的而不是假定的：Qdrant collection 的向量维度在创建时
    就固定了，这里猜错的话，不做一次全量重建索引就没法挽回。
    """
    try:
        client = build_embeddings(config)
        vector: List[float] = await client.aembed_query(PROBE_TEXT)
    except Exception as exc:  # noqa: BLE001 - provider SDK 抛出的异常类型五花八门
        logger.warning("embedding test failed for config %s: %s", config.id, exc)
        return False, _friendly_error(exc), None

    if not vector:
        return False, "向量模型返回了空结果", None

    return True, f"连接成功，向量维度 {len(vector)}", len(vector)

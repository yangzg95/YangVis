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

from langchain_core.messages import AIMessageChunk, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGenerationChunk
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from app.config import get_settings
from app.crypto import decrypt
from app.models.entities import ModelConfig

logger = logging.getLogger("yangvis.providers")
settings = get_settings()

# 发给 /embeddings，纯粹是为了观察返回结果的维度。
PROBE_TEXT = "yangvis connectivity probe"


class ProviderError(RuntimeError):
    """上游模型 provider 拒绝了请求，或者根本连不上。"""


class ReasoningChatOpenAI(ChatOpenAI):
    """把 provider 私有的 ``reasoning_content`` 增量搬进 ``additional_kwargs``。

    ``ChatOpenAI`` 只按官方 OpenAI 规范解析响应，DeepSeek / 通义这类思考流用的
    ``delta.reasoning_content`` 会被直接丢掉（其文档明确写了这一点）。思考过程要
    能展示，只能在 chunk 转换这一层自己接。上游没发这个字段时行为与普通
    ``ChatOpenAI`` 完全一致，所以对所有 provider 都是无害的。
    """

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: Optional[dict],
    ) -> Optional[ChatGenerationChunk]:
        generation_chunk = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if generation_chunk is None:
            return None

        choices = chunk.get("choices") or []
        first = choices[0] if choices else None
        delta = first.get("delta") if isinstance(first, dict) else None
        reasoning = delta.get("reasoning_content") if isinstance(delta, dict) else None
        if isinstance(reasoning, str) and reasoning:
            message = generation_chunk.message
            if isinstance(message, AIMessageChunk):
                message.additional_kwargs["reasoning_content"] = reasoning
        return generation_chunk


def _api_key(config: ModelConfig) -> str:
    # 挂在网关后面的 OpenAI 兼容服务往往不需要 key，但客户端要求这里
    # 必须是个非空字符串。
    return decrypt(config.api_key_enc) or "not-needed"


def resolve_temperature(
    config: ModelConfig, agent_temperature: Optional[int]
) -> Optional[float]:
    """智能体覆盖，模型配置兜底：任一为 0-100 整数则 /100，都没有则返回 None。

    返回 ``None`` 时 langchain 会把 temperature 从请求体里剔除，交给服务商用自己的
    默认——这是给「只接受特定温度 / 压根不接受该参数」的模型（Kimi、OpenAI o 系列）
    留的口子。
    """
    if agent_temperature is not None:
        return agent_temperature / 100
    if config.temperature is not None:
        return config.temperature / 100
    return None


def build_chat_model(
    config: ModelConfig,
    *,
    temperature: Optional[float] = 0.3,
    streaming: bool = False,
    timeout: Optional[float] = None,
) -> ChatOpenAI:
    """根据已保存的配置创建一个对话客户端。

    ``temperature`` 传 ``None`` 时该字段不会进请求体（langchain 会剔除空值），
    交给服务商用自己的默认——这是给「只接受特定温度 / 压根不接受该参数」的模型
    （Kimi、OpenAI o 系列）留的口子。

    ``timeout`` 缺省用全局 ``MODEL_HTTP_TIMEOUT``（30s，适合对话与连通性测试）；
    简历分析这类一次性长输出的后台任务应显式传更长的值——非流式调用要等整个
    completion 落地才返回，30s 几乎必然超时。

    ``reasoning_mode`` 决定思维链怎么处理：``off`` 直接用原生 ``ChatOpenAI``，
    ``reasoning_content`` 在解析层就没了；``auto`` 装上下面的子类，上游发了才展示；
    ``on`` 除了展示，还把 ``request_extra`` 原样并进请求体——各家「开启思考」的参数名
    互不相同，所以由配置的人填，代码不猜厂商。
    """
    mode = config.reasoning_mode or "auto"
    extra_body = dict(config.request_extra) if mode == "on" and config.request_extra else None
    client_cls = ChatOpenAI if mode == "off" else ReasoningChatOpenAI
    return client_cls(
        model=config.model_name,
        base_url=config.base_url,
        api_key=_api_key(config),
        temperature=temperature,
        streaming=streaming,
        timeout=timeout if timeout is not None else settings.MODEL_HTTP_TIMEOUT,
        max_retries=1,
        extra_body=extra_body,
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
        # 探测只验「地址 + 密钥 + 模型名」这三件事，不该顺手规定采样温度：
        # Kimi 的某些模型只接受 temperature=1，OpenAI o 系列干脆不接受该参数，
        # 传 0.0 会让它们当场 400，把「模型其实能用」误判成连不通。传 None 让
        # langchain 把 temperature 整个从请求体里省掉，由服务商套自己的默认值。
        client = build_chat_model(config, temperature=None)
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

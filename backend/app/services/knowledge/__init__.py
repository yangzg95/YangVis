"""知识库业务包：文档、切分、embedding 与检索。

对外只有这一张脸：routers 和其它 service 都从这里取名字，不需要知道包里有
几个模块。``service`` 是主入口，``vectorstore`` 单独成一个模块是因为它管的
只有 Qdrant 的 collection、point 与打分：换成别的向量库只重写它一个，动不到
切分和检索范围那套规则。

``chunking`` 和 ``uploads`` 没有收进来：简历的 router 两个都在用，service 层也
直接引用 ``chunking``，它们是跨域共用的工具。挪进知识库包里，等于让别的域隔着
知识库去拿通用能力。
"""
from app.services.knowledge import vectorstore  # noqa: F401
from app.services.knowledge.service import (  # noqa: F401
    IndexStatus,
    KnowledgeService,
    index_document_task,
    model_signature,
    reset_stale_indexing,
)

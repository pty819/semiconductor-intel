.. _api-source:

源码 API 参考
=============

autodoc 从源码 docstring 生成。领域纯函数（validation / identity /
timeline）与任务队列语义是最有阅读价值的部分。

配置
----

.. automodule:: intel.settings

契约（DTO）
-----------

.. automodule:: intel.contracts.models

领域规则（纯函数）
------------------

.. automodule:: intel.domain.validation
.. automodule:: intel.domain.identity
.. automodule:: intel.domain.time
.. automodule:: intel.domain.urlnorm

数据库
------

.. automodule:: intel.db.rls
.. automodule:: intel.db.models.pool
.. automodule:: intel.db.models.knowledge
.. automodule:: intel.db.models.jobs
.. automodule:: intel.db.models.conversation
.. automodule:: intel.db.models.retrieval

服务（用例层）
--------------

.. automodule:: intel.services.jobs
.. automodule:: intel.services.knowledge
.. automodule:: intel.services.timeline
.. automodule:: intel.services.evolution
.. automodule:: intel.services.research
.. automodule:: intel.services.reviews
.. automodule:: intel.services.reports
.. automodule:: intel.services.errors

检索
----

.. automodule:: intel.retrieval.chunker
.. automodule:: intel.retrieval.indexer
.. automodule:: intel.retrieval.bm25
.. automodule:: intel.retrieval.vector
.. automodule:: intel.retrieval.recall

NOOA 适配
---------

.. automodule:: intel.nooa_adapter.agents
.. automodule:: intel.nooa_adapter.factory
.. automodule:: intel.nooa_adapter.middleware
.. automodule:: intel.nooa_adapter.gateway
.. automodule:: intel.nooa_adapter.tracing

工作流（后台任务处理器）
------------------------

.. automodule:: intel.workflows.ingest
.. automodule:: intel.workflows.route
.. automodule:: intel.workflows.extract
.. automodule:: intel.workflows.event_build
.. automodule:: intel.workflows.answer
.. automodule:: intel.workflows.investigate
.. automodule:: intel.workflows.report
.. automodule:: intel.workflows.review

采集与解析
----------

.. automodule:: intel.sources.adapters.feed
.. automodule:: intel.sources.fetcher
.. automodule:: intel.sources.browser
.. automodule:: intel.sources.ssrf
.. automodule:: intel.sources.politeness
.. automodule:: intel.parsing.parser
.. automodule:: intel.parsing.quality
.. automodule:: intel.parsing.blocksdiff

API 层
------

.. automodule:: intel.api.app
.. automodule:: intel.api.deps
.. automodule:: intel.api.errors
.. automodule:: intel.api.pagination
.. automodule:: intel.api.idempotency
.. automodule:: intel.api.sse

Workers（组合根与租约）
-----------------------

.. automodule:: intel.workers.composition
.. automodule:: intel.workers.runner
.. automodule:: intel.workers.leases
.. automodule:: intel.workers.scheduler
.. automodule:: intel.workers.stores

仓储（scope 强制与 SQL 适配）
--------------------------------------------

.. automodule:: intel.repositories.base
.. automodule:: intel.repositories.knowledge
.. automodule:: intel.repositories.conversations
.. automodule:: intel.repositories.reviews
.. automodule:: intel.repositories.jobs
.. automodule:: intel.repositories.idempotency

-- yangvis schema
--
-- Reviewable copy of the DDL that app/bootstrap.py create_tables() applies via
-- SQLAlchemy. Keep both in sync; this file is what to read when debugging a
-- live database.

CREATE TABLE IF NOT EXISTS sys_user (
  id            BIGINT PRIMARY KEY AUTO_INCREMENT,
  username      VARCHAR(64)  NOT NULL COMMENT '登录名',
  password_hash VARCHAR(255) NOT NULL COMMENT 'pbkdf2_sha256$iterations$salt$hash',
  nickname      VARCHAR(64)  NULL,
  email         VARCHAR(128) NULL,
  status        TINYINT(1)   NOT NULL DEFAULT 1 COMMENT '1 启用 / 0 禁用',
  is_admin      TINYINT(1)   NOT NULL DEFAULT 0,
  ops_write     TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '运维写通行证；闸门判定 is_admin OR ops_write',
  last_login_at DATETIME     NULL,
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='本地用户账号';

CREATE TABLE IF NOT EXISTS model_config (
  id              BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id        BIGINT        NOT NULL COMMENT 'sys_user.id',
  purpose         VARCHAR(16)   NOT NULL COMMENT 'chat | embedding',
  title           VARCHAR(128)  NOT NULL,
  model_name      VARCHAR(128)  NOT NULL,
  base_url        VARCHAR(512)  NOT NULL,
  api_key_enc     VARBINARY(1024) NULL COMMENT 'Fernet 密文，绝不明文返回',
  remark          VARCHAR(512)  NULL,
  is_default      TINYINT(1)    NOT NULL DEFAULT 0 COMMENT '同 owner + purpose 下唯一',
  vector_size     INT           NULL COMMENT '仅 embedding，保存时探针实测',
  last_tested_at  DATETIME      NULL,
  last_test_ok    TINYINT(1)    NOT NULL DEFAULT 0,
  last_test_error VARCHAR(512)  NULL,
  created_at      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户的模型接入配置';

CREATE TABLE IF NOT EXISTS agent (
  id            BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id      BIGINT        NOT NULL DEFAULT 0 COMMENT '0 = 内置，全体可见',
  slug          VARCHAR(64)   NOT NULL,
  name          VARCHAR(128)  NOT NULL,
  description   VARCHAR(512)  NULL,
  system_prompt TEXT          NOT NULL,
  use_knowledge TINYINT(1)    NOT NULL DEFAULT 1 COMMENT '是否检索知识库',
  use_ops       TINYINT(1)    NOT NULL DEFAULT 0 COMMENT '是否挂运维只读工具（服务器命令/数据库查询）',
  use_memory    TINYINT(1)    NOT NULL DEFAULT 0 COMMENT '是否注入长期记忆并参与记忆提取',
  chat_visible  TINYINT(1)    NOT NULL DEFAULT 1 COMMENT '是否出现在主对话的智能体选择器',
  temperature   INT           NOT NULL DEFAULT 30 COMMENT '0-100，除以 100 后传给模型',
  is_builtin    TINYINT(1)    NOT NULL DEFAULT 0,
  enabled       TINYINT(1)    NOT NULL DEFAULT 1,
  sort_order    INT           NOT NULL DEFAULT 0,
  created_at    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_owner_slug (owner_id, slug),
  KEY idx_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='可选择的智能体';

CREATE TABLE IF NOT EXISTS kb_project (
  id            BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id      BIGINT       NOT NULL COMMENT 'sys_user.id',
  name          VARCHAR(128) NOT NULL,
  description   VARCHAR(512) NULL,
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_project_owner_name (owner_id, name),
  KEY idx_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='知识库项目，检索的最外层边界';

CREATE TABLE IF NOT EXISTS knowledge_type (
  id            BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id      BIGINT       NOT NULL COMMENT 'sys_user.id',
  project_id    BIGINT       NOT NULL DEFAULT 0 COMMENT 'kb_project.id；0 仅在迁移回填前出现',
  name          VARCHAR(128) NOT NULL,
  description   VARCHAR(512) NULL,
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_owner (owner_id),
  KEY idx_type_owner_project (owner_id, project_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='知识库文件夹分类，隶属于一个项目';

CREATE TABLE IF NOT EXISTS kb_document (
  id            BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id      BIGINT       NOT NULL COMMENT 'sys_user.id',
  scope         VARCHAR(16)  NOT NULL DEFAULT 'private' COMMENT 'private | project，为组织级共享预留',
  type_id       BIGINT       NULL,
  filename      VARCHAR(255) NOT NULL,
  mime          VARCHAR(128) NULL,
  size          BIGINT       NOT NULL DEFAULT 0,
  content       MEDIUMTEXT   NOT NULL COMMENT '提取后的纯文本，重建索引的数据源',
  status        VARCHAR(16)  NOT NULL DEFAULT 'pending' COMMENT 'pending | indexing | ready | error',
  error_msg     VARCHAR(512) NULL,
  chunk_count   INT          NOT NULL DEFAULT 0,
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_owner (owner_id),
  KEY idx_doc_owner_type (owner_id, type_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='上传的知识库文档';

CREATE TABLE IF NOT EXISTS kb_chunk (
  id            BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id      BIGINT       NOT NULL,
  doc_id        BIGINT       NOT NULL,
  seq           INT          NOT NULL,
  content       TEXT         NOT NULL,
  char_count    INT          NOT NULL DEFAULT 0,
  point_id      VARCHAR(64)  NULL COMMENT 'Qdrant point id',
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_owner (owner_id),
  KEY idx_chunk_owner_doc (owner_id, doc_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='文档分片，与 Qdrant 一一对应';

CREATE TABLE IF NOT EXISTS kb_index_state (
  owner_id        BIGINT       NOT NULL PRIMARY KEY COMMENT 'sys_user.id',
  collection_name VARCHAR(64)  NOT NULL,
  embed_model_id  BIGINT       NULL COMMENT '指向 model_config.id',
  embed_model_sig VARCHAR(256) NOT NULL DEFAULT '' COMMENT 'base_url + model_name 指纹',
  vector_size     INT          NOT NULL DEFAULT 0,
  doc_count       INT          NOT NULL DEFAULT 0,
  chunk_count     INT          NOT NULL DEFAULT 0,
  status          VARCHAR(16)  NOT NULL DEFAULT 'uninitialized' COMMENT 'uninitialized | indexing | ready | rebuilding | error',
  error_msg       VARCHAR(512) NULL,
  updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='每用户的 Qdrant 集合元信息';

CREATE TABLE IF NOT EXISTS chat_conversation (
  id            BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id      BIGINT       NOT NULL,
  title         VARCHAR(255) NOT NULL DEFAULT '新会话',
  agent_id      BIGINT       NULL,
  project_id    BIGINT       NULL COMMENT 'kb_project.id，检索边界',
  type_ids      JSON         NULL COMMENT '[knowledge_type.id]，NULL 表示项目下全部知识类型',
  model_config_id BIGINT     NULL COMMENT 'model_config.id，本会话选用的对话模型；NULL 表示跟随默认配置',
  ops_target_type VARCHAR(16) NULL COMMENT 'server | database；NULL 表示普通对话',
  ops_target_id   BIGINT      NULL COMMENT 'ops_server.id | ops_database.id，绑定运维目标的问答会话',
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='对话会话';

CREATE TABLE IF NOT EXISTS chat_message (
  id              BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id        BIGINT       NOT NULL,
  conversation_id BIGINT       NOT NULL,
  role            VARCHAR(16)  NOT NULL COMMENT 'user | assistant',
  content         MEDIUMTEXT   NOT NULL,
  citations       JSON         NULL COMMENT '[{"index","doc_id","chunk_id","filename","score","excerpt"}]',
  created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_conv (conversation_id, id),
  KEY idx_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='对话消息';

CREATE TABLE IF NOT EXISTS chat_summary (
  conversation_id BIGINT PRIMARY KEY COMMENT 'chat_conversation.id，一个会话至多一行',
  owner_id        BIGINT       NOT NULL,
  summary         TEXT         NOT NULL COMMENT '回放窗口之外历史的滚动摘要（全量重算）',
  updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='会话摘要';

CREATE TABLE IF NOT EXISTS user_memory (
  id                     BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id               BIGINT       NOT NULL,
  content                VARCHAR(512) NOT NULL COMMENT '一条记忆一句话',
  source_conversation_id BIGINT       NULL COMMENT '提取出这条记忆的会话，仅溯源用',
  created_at             DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at             DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户长期记忆';


-- ---------------------------------------------------------------------------
-- 智能运维
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ops_server (
  id               BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id         BIGINT       NOT NULL,
  name             VARCHAR(128) NOT NULL,
  host             VARCHAR(255) NOT NULL,
  port             INT          NOT NULL DEFAULT 22,
  username         VARCHAR(64)  NOT NULL,
  auth_type        VARCHAR(16)  NOT NULL DEFAULT 'password' COMMENT 'password | key',
  password_enc     VARBINARY(2048) NULL COMMENT 'Fernet 密文',
  private_key_enc  VARBINARY(8192) NULL COMMENT 'Fernet 密文',
  passphrase_enc   VARBINARY(1024) NULL COMMENT 'Fernet 密文',
  host_key         VARCHAR(512) NULL COMMENT '首连记录的主机公钥（TOFU），之后逐次比对',
  remark           VARCHAR(512) NULL,
  last_checked_at  DATETIME     NULL,
  last_check_ok    TINYINT(1)   NOT NULL DEFAULT 0,
  last_check_error VARCHAR(512) NULL,
  created_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_server_owner_name (owner_id, name),
  KEY idx_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='运维服务器';

CREATE TABLE IF NOT EXISTS ops_database (
  id               BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id         BIGINT       NOT NULL,
  name             VARCHAR(128) NOT NULL,
  db_type          VARCHAR(16)  NOT NULL DEFAULT 'mysql' COMMENT 'mysql（含 MariaDB）| redis',
  host             VARCHAR(255) NOT NULL,
  port             INT          NOT NULL DEFAULT 3306,
  username         VARCHAR(64)  NULL,
  password_enc     VARBINARY(2048) NULL COMMENT 'Fernet 密文',
  db_name          VARCHAR(128) NULL COMMENT 'MySQL 为库名，Redis 为库编号',
  remark           VARCHAR(512) NULL,
  last_checked_at  DATETIME     NULL,
  last_check_ok    TINYINT(1)   NOT NULL DEFAULT 0,
  last_check_error VARCHAR(512) NULL,
  created_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_database_owner_name (owner_id, name),
  KEY idx_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='运维数据库';

CREATE TABLE IF NOT EXISTS ops_audit_log (
  id          BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id    BIGINT       NOT NULL,
  target_type VARCHAR(16)  NOT NULL COMMENT 'server | database',
  target_id   BIGINT       NOT NULL,
  target_name VARCHAR(128) NULL,
  actor       VARCHAR(16)  NOT NULL DEFAULT 'user' COMMENT 'user | ai',
  command     MEDIUMTEXT   NOT NULL,
  verdict     VARCHAR(16)  NOT NULL DEFAULT 'readonly'
              COMMENT 'readonly | confirmed | rejected | forbidden | manual',
  success     TINYINT(1)   NOT NULL DEFAULT 0,
  error       VARCHAR(512) NULL,
  pending_action_id BIGINT NULL COMMENT 'ops_pending_action.id；手动/只读执行为 NULL',
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_audit_owner_time (owner_id, created_at),
  KEY idx_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='运维操作审计';

CREATE TABLE IF NOT EXISTS ops_pending_action (
  id              BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id        BIGINT       NOT NULL,
  conversation_id BIGINT       NOT NULL COMMENT 'chat_conversation.id',
  message_id      BIGINT       NULL COMMENT '触发该提议的 assistant 消息，回答落库后回填',
  target_type     VARCHAR(16)  NOT NULL COMMENT 'server | database（本期仅 server 写命令走确认）',
  target_id       BIGINT       NOT NULL,
  target_name     VARCHAR(128) NULL,
  command         MEDIUMTEXT   NOT NULL,
  reason          VARCHAR(512) NULL COMMENT 'AI 给用户看的执行理由',
  status          VARCHAR(16)  NOT NULL DEFAULT 'pending'
                  COMMENT 'pending | approved | rejected | expired | executed | failed',
  result          MEDIUMTEXT   NULL COMMENT '执行输出（截断），供续答上下文与历史展示',
  exit_status     INT          NULL,
  created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  resolved_at     DATETIME     NULL,
  KEY idx_action_conv (conversation_id, id),
  KEY idx_action_owner_status (owner_id, status),
  KEY idx_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='运维写命令待确认项（两阶段落库确认）';

CREATE TABLE IF NOT EXISTS ops_sql_favorite (
  id          BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id    BIGINT       NOT NULL,
  database_id BIGINT       NULL COMMENT 'ops_database.id；NULL 表示所有连接通用',
  title       VARCHAR(128) NOT NULL,
  content     TEXT         NOT NULL,
  remark      VARCHAR(512) NULL,
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_favorite_owner_db (owner_id, database_id),
  KEY idx_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='SQL 收藏';

CREATE TABLE IF NOT EXISTS interview_record (
  id             BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id       BIGINT       NOT NULL,
  company        VARCHAR(128) NOT NULL,
  position       VARCHAR(128) NOT NULL,
  interview_date DATE         NULL,
  round          VARCHAR(32)  NULL COMMENT '一面/二面/HR面 等，自由文本',
  result         VARCHAR(16)  NOT NULL DEFAULT 'pending' COMMENT 'pending | passed | failed | offer',
  notes          MEDIUMTEXT   NULL COMMENT '整场面试的复盘备注',
  questions      JSON         NOT NULL COMMENT '[{qid, question, my_answer, note, ref_answer, ref_status, ref_error}]',
  created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='面试场次记录（含问题清单与 AI 参考答案）';


-- ---------------------------------------------------------------------------
-- AI 网关：对外一把统一密钥，后面挂多个 OpenAI 兼容的上游厂商
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ai_channel (
  id              BIGINT PRIMARY KEY AUTO_INCREMENT,
  name            VARCHAR(128)  NOT NULL COMMENT '通道名，全局唯一',
  base_url        VARCHAR(512)  NOT NULL COMMENT 'OpenAI 兼容根地址，含 /v1',
  api_key_enc     VARBINARY(1024) NULL COMMENT '上游 key 的 Fernet 密文，绝不明文返回',
  protocol        VARCHAR(16)   NOT NULL DEFAULT 'openai',
  models          JSON          NULL COMMENT '该通道能提供的上游模型名，仅作配置候选提示',
  enabled         TINYINT(1)    NOT NULL DEFAULT 1,
  remark          VARCHAR(512)  NULL,
  last_tested_at  DATETIME      NULL,
  last_test_ok    TINYINT(1)    NOT NULL DEFAULT 0,
  last_test_error VARCHAR(512)  NULL,
  created_at      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_channel_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI 网关上游通道';

CREATE TABLE IF NOT EXISTS ai_model_route (
  id             BIGINT PRIMARY KEY AUTO_INCREMENT,
  model_name     VARCHAR(128)  NOT NULL COMMENT '调用方请求里写的对外模型名',
  channel_id     BIGINT        NOT NULL COMMENT 'ai_channel.id',
  upstream_model VARCHAR(128)  NULL COMMENT '实际发给上游的模型名，NULL 表示同名',
  priority       INT           NOT NULL DEFAULT 100 COMMENT '同名多通道时按升序尝试，失败转移',
  enabled        TINYINT(1)    NOT NULL DEFAULT 1,
  remark         VARCHAR(512)  NULL,
  created_at     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_route_model_channel (model_name, channel_id),
  KEY idx_route_model (model_name, priority)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='对外模型名到上游通道的映射';

CREATE TABLE IF NOT EXISTS ai_api_key (
  id           BIGINT PRIMARY KEY AUTO_INCREMENT,
  name         VARCHAR(128) NOT NULL,
  key_hash     VARCHAR(64)  NOT NULL COMMENT '明文的 SHA-256，明文不落库',
  key_prefix   VARCHAR(32)  NOT NULL DEFAULT '' COMMENT '明文前若干位，仅用于列表里辨认',
  enabled      TINYINT(1)   NOT NULL DEFAULT 1,
  remark       VARCHAR(512) NULL,
  call_count   BIGINT       NOT NULL DEFAULT 0,
  last_used_at DATETIME     NULL,
  created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_key_hash (key_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='对外签发的统一网关密钥';

CREATE TABLE IF NOT EXISTS ai_call_log (
  id                BIGINT PRIMARY KEY AUTO_INCREMENT,
  request_id        VARCHAR(64)  NOT NULL COMMENT '一次客户端调用一个 id，换通道重试不变',
  key_id            BIGINT       NULL COMMENT 'ai_api_key.id',
  key_name          VARCHAR(128) NULL COMMENT '写入时快照，密钥删了历史仍可读',
  endpoint          VARCHAR(32)  NOT NULL COMMENT 'chat/completions | embeddings | models',
  model             VARCHAR(128) NULL COMMENT '对外模型名',
  stream            TINYINT(1)   NOT NULL DEFAULT 0,
  channel_id        BIGINT       NULL COMMENT '最终应答的上游通道',
  channel_name      VARCHAR(128) NULL,
  upstream_model    VARCHAR(128) NULL,
  attempts          JSON         NULL COMMENT '[{channel_id, channel_name, status_code, error, latency_ms}] 转移轨迹',
  status_code       INT          NOT NULL DEFAULT 0,
  success           TINYINT(1)   NOT NULL DEFAULT 0,
  error             VARCHAR(512) NULL,
  prompt_tokens     INT          NOT NULL DEFAULT 0,
  completion_tokens INT          NOT NULL DEFAULT 0,
  total_tokens      INT          NOT NULL DEFAULT 0,
  latency_ms        INT          NOT NULL DEFAULT 0,
  first_token_ms    INT          NULL COMMENT '流式首字延迟；非流式为 NULL',
  client_ip         VARCHAR(64)  NULL,
  request_body      MEDIUMTEXT   NULL COMMENT '按 AI_GATEWAY_LOG_MAX_CHARS 截断',
  response_body     MEDIUMTEXT   NULL COMMENT '流式是拼接后的完整回答',
  created_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_call_created (created_at),
  KEY idx_call_key_created (key_id, created_at),
  KEY idx_call_channel_created (channel_id, created_at),
  KEY idx_call_request (request_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI 网关调用监控与审计';


-- ---------------------------------------------------------------------------
-- 升级已有库：新增「项目」维度
--
-- app/bootstrap.py 的 migrate_schema() 在启动时会自动执行等价操作（含回填），
-- 这里保留一份可人工审阅、可手动执行的版本。重复执行会因列已存在而报错。
-- ---------------------------------------------------------------------------

-- ALTER TABLE knowledge_type
--   ADD COLUMN project_id BIGINT NOT NULL DEFAULT 0 COMMENT 'kb_project.id' AFTER owner_id,
--   ADD KEY idx_type_owner_project (owner_id, project_id);
--
-- INSERT INTO kb_project (owner_id, name, description)
--   SELECT DISTINCT owner_id, '默认项目', '迁移时自动创建，用于存放已有的知识类型'
--     FROM knowledge_type WHERE project_id = 0;
--
-- UPDATE knowledge_type t
--   JOIN kb_project p ON p.owner_id = t.owner_id AND p.name = '默认项目'
--    SET t.project_id = p.id
--  WHERE t.project_id = 0;
--
-- ALTER TABLE chat_conversation
--   ADD COLUMN project_id BIGINT NULL COMMENT 'kb_project.id，检索边界' AFTER agent_id,
--   ADD COLUMN type_ids   JSON   NULL COMMENT '[knowledge_type.id]，NULL 表示全部' AFTER project_id,
--   ADD COLUMN model_config_id BIGINT NULL COMMENT 'model_config.id，NULL 表示跟随默认配置' AFTER type_ids;
--
-- 运维问答并入主对话（两阶段落库确认）：
-- ALTER TABLE chat_conversation
--   ADD COLUMN ops_target_type VARCHAR(16) NULL COMMENT 'server | database；NULL 表示普通对话',
--   ADD COLUMN ops_target_id   BIGINT      NULL COMMENT 'ops_server.id | ops_database.id';
--
-- ALTER TABLE ops_audit_log
--   ADD COLUMN pending_action_id BIGINT NULL COMMENT 'ops_pending_action.id';
--
-- 长期记忆与会话摘要：
-- ALTER TABLE agent
--   ADD COLUMN use_memory TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否注入长期记忆并参与记忆提取';
-- （chat_summary / user_memory 两张新表由 create_all 自动创建，DDL 见上文。）

-- 进程重启遗留的待确认项物化为 expired（启动时自动执行）：
-- UPDATE ops_pending_action
--    SET status = 'expired', resolved_at = NOW()
--  WHERE status = 'pending'
--    AND created_at < NOW() - INTERVAL 180 SECOND;

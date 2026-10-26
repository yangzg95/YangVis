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
  reasoning_mode  VARCHAR(16)   NOT NULL DEFAULT 'auto' COMMENT '思维链：auto 上游发了才展示 | on 带 request_extra 并要求展示 | off 丢弃',
  request_extra   JSON          NULL COMMENT '仅 reasoning_mode=on：原样并入请求体的私有参数（enable_thinking / thinking.type 等），代码不猜厂商',
  temperature     INT           NULL COMMENT '0-100，除以100传给模型；模型级硬约束，设了就盖过智能体；NULL=不设限，由智能体决定',
  supports_vision TINYINT(1)    NOT NULL DEFAULT 0 COMMENT '仅 chat：是否接受图片输入（由配置的人按服务商文档声明）',
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
  temperature   INT           NULL COMMENT '0-100智能体偏好；模型配置设了温度时以模型为准；NULL=跟随模型配置',
  is_builtin    TINYINT(1)    NOT NULL DEFAULT 0,
  enabled       TINYINT(1)    NOT NULL DEFAULT 1,
  sort_order    INT           NOT NULL DEFAULT 0,
  created_at    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_owner_slug (owner_id, slug),
  KEY idx_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='可选择的智能体';

-- 智能体配置的不可变版本快照：新建即 v1，人设字段每次保存追加新版本，
-- 内置智能体随代码升级在启动 seed 时对账追加。工作流步骤开跑时绑定当时版本号，
-- 历史运行永远对得上当时的 system_prompt。智能体删除后版本行保留。
CREATE TABLE IF NOT EXISTS agent_version (
  id         BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id   BIGINT NOT NULL DEFAULT 0 COMMENT '冗余 owner：内置为 0',
  agent_id   BIGINT NOT NULL COMMENT 'agent.id；智能体删除后行保留',
  version    INT    NOT NULL COMMENT '该智能体名下单调递增，从 1 开始',
  snapshot   JSON   NOT NULL COMMENT '当时的人设字段：name/description/system_prompt/temperature/use_knowledge/use_ops/use_memory/chat_visible',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_agent_version_no (agent_id, version),
  KEY idx_agent_version_owner (owner_id),
  KEY idx_agent_version_agent (agent_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='智能体配置版本快照';

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
  local_tool_keys JSON       NULL COMMENT '会话级勾选的本地工具 key（services/tools 注册），NULL/空数组表示未选',
  mcp_server_ids  JSON       NULL COMMENT '会话级挂载的 mcp_server.id 列表，与 workflow.mcp_server_ids 同义',
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
  timeline        JSON         NULL COMMENT '本轮按真实顺序落库的事件序列，历史回放据此还原步骤与思考过程',
  created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_conv (conversation_id, id),
  KEY idx_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='对话消息';

CREATE TABLE IF NOT EXISTS chat_attachment (
  id              BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id        BIGINT       NOT NULL,
  conversation_id BIGINT       NULL COMMENT '发送后回填；未发送的附件为 NULL',
  message_id      BIGINT       NULL COMMENT '绑定的 user 消息 id；NULL 表示尚未发送',
  kind            VARCHAR(8)   NOT NULL COMMENT 'image | file',
  filename        VARCHAR(255) NOT NULL COMMENT '仅展示用，不参与路径拼接',
  mime            VARCHAR(128) NULL,
  size            INT          NOT NULL DEFAULT 0 COMMENT '字节',
  rel_path        VARCHAR(512) NULL COMMENT '图片在媒体目录下的相对路径；文件附件为 NULL',
  text            MEDIUMTEXT   NULL COMMENT '文档抽取出的正文；图片为 NULL',
  created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_attachment_owner (owner_id),
  KEY idx_attachment_conversation (conversation_id),
  KEY idx_attachment_message (message_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='对话附件：文档存抽取文本，图片存原文件';

CREATE TABLE IF NOT EXISTS chat_summary (
  conversation_id BIGINT PRIMARY KEY COMMENT 'chat_conversation.id，一个会话至多一行',
  owner_id        BIGINT       NOT NULL,
  summary         TEXT         NOT NULL COMMENT '回放窗口之外历史的滚动摘要（全量重算）',
  cut_message_id  BIGINT       NULL COMMENT '手动压缩锚点：该 id 及之前的消息不再回放；NULL 表示纯自动摘要',
  instruction     VARCHAR(500) NULL COMMENT '用户手动压缩时给的额外摘要要求，自动重算沿用',
  updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='会话摘要与手动压缩锚点';

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

CREATE TABLE IF NOT EXISTS resume_doc (
  id         BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id   BIGINT       NOT NULL,
  title      VARCHAR(128) NOT NULL,
  doc        JSON         NOT NULL COMMENT '在线简历结构：{template, module_order, profile, companies[], projects[], educations[], skills_html}，模块顺序与条目顺序都在里面',
  created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='智能应用 · 在线编辑的简历（与上传式 resume 表并存，富文本为白名单消毒后的 HTML 片段）';

CREATE TABLE IF NOT EXISTS interview_record (
  id             BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id       BIGINT       NOT NULL,
  company        VARCHAR(128) NOT NULL,
  position       VARCHAR(128) NOT NULL,
  interview_date DATE         NULL,
  round          VARCHAR(32)  NULL COMMENT '一面/二面/HR面 等，自由文本',
  result         VARCHAR(16)  NOT NULL DEFAULT 'pending' COMMENT 'pending | passed | failed | offer',
  notes          MEDIUMTEXT   NULL COMMENT '整场面试的复盘备注',
  questions      JSON         NOT NULL COMMENT '[{qid, question, my_answer, note, ref_answer}]，参考答案的进度/失败原因在 workflow_run（subject_kind=interview-question, subject_ref=qid）',
  created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='面试场次记录（含问题清单与 AI 参考答案）';

CREATE TABLE IF NOT EXISTS slide_deck (
  id          BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id    BIGINT       NOT NULL,
  title       VARCHAR(128) NOT NULL COMMENT '演示主题',
  description VARCHAR(512) NULL     COMMENT '对这份幻灯片的补充说明，进 prompt',
  requirement VARCHAR(512) NULL     COMMENT '用户当次提出的要求（受众/页数/风格），重新生成时可覆盖',
  source_text MEDIUMTEXT   NOT NULL COMMENT '创建时的材料快照：粘贴文案 + 上传文档解析出的文本',
  assets      JSON         NOT NULL COMMENT '[{id, name, mime, size, rel_path, url}]，图片文件在服务器本地磁盘',
  theme       JSON         NOT NULL COMMENT '{preset, ratio, accent}，合法值由 services/slides/html.py 白名单把关',
  slides      JSON         NOT NULL COMMENT '[{sid, layout, title, subtitle, bullets, asset_id, notes}]，唯一事实源',
  html        MEDIUMTEXT   NULL     COMMENT '派生物：每次写 slides/theme/assets 时由服务端重渲染，预览/放映/导出共用',
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='智能应用 · 幻灯片（AI 生成的结构化页面 + 渲染出的 HTML）；生成进度在 workflow_run（subject_kind=slide-deck）';


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
  auto_eligible  TINYINT(1)    NOT NULL DEFAULT 0 COMMENT '是否进入 model=auto 的候选池',
  auto_tags      JSON          NULL COMMENT '对模型的定义：能力 chat/embedding/vision/tools + 任务 reasoning/code/writing/translation/general',
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
  auto_route        JSON         NULL COMMENT 'Auto 的决策快照 {required, task, source, resolved_model}；点名模型时为 NULL',
  status_code       INT          NOT NULL DEFAULT 0,
  success           TINYINT(1)   NOT NULL DEFAULT 0,
  error             VARCHAR(512) NULL,
  prompt_tokens     INT          NOT NULL DEFAULT 0,
  completion_tokens INT          NOT NULL DEFAULT 0,
  total_tokens      INT          NOT NULL DEFAULT 0,
  cached_tokens     INT          NULL COMMENT '命中缓存的 prompt token；NULL=上游没报，0=报了但没命中',
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
-- 工作流：把「功能里硬编码的一次模型调用」搬进配置
--
-- 定义（workflow / workflow_step）与现场（workflow_run / workflow_run_step）
-- 分开：定义可以被改动甚至删除，历史运行要一直读得懂。组间顺序、组内并行，
-- 每个步骤就是一次智能体调用（见 services/workflows.py）。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS mcp_server (
  id           BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id     BIGINT       NOT NULL,
  name         VARCHAR(128) NOT NULL,
  transport    VARCHAR(16)  NOT NULL COMMENT 'stdio | sse | streamable_http',
  url          VARCHAR(1024) NULL COMMENT 'sse/streamable_http 的服务地址',
  command      VARCHAR(1024) NULL COMMENT 'stdio 的启动命令',
  args         JSON         NULL COMMENT 'stdio 启动参数 list[str]',
  env_enc      VARBINARY(8192) NULL COMMENT 'Fernet 密文 JSON：stdio 子进程环境变量',
  headers_enc  VARBINARY(8192) NULL COMMENT 'Fernet 密文 JSON：远程请求头',
  enabled      TINYINT(1)   NOT NULL DEFAULT 1,
  remark       VARCHAR(512) NULL,
  last_test_at DATETIME     NULL,
  last_test_ok TINYINT(1)   NULL,
  last_test_msg VARCHAR(1024) NULL,
  tools_snapshot JSON       NULL COMMENT '最近一次连通测试发现的工具快照 [{name,description,input_schema}]',
  created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_mcp_owner_name (owner_id, name),
  KEY idx_mcp_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='MCP 服务配置（个人资产，无内置项）';

CREATE TABLE IF NOT EXISTS workflow (
  id           BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id     BIGINT        NOT NULL DEFAULT 0 COMMENT '0 = 内置，全体可见',
  slug         VARCHAR(64)   NOT NULL,
  name         VARCHAR(128)  NOT NULL,
  description  VARCHAR(512)  NULL,
  input_schema JSON          NULL COMMENT '[{name, label, required}]，声明接受哪些变量',
  mcp_server_ids JSON        NULL COMMENT '挂载的 mcp_server.id 列表，运行时对该流程所有步骤生效',
  is_builtin   TINYINT(1)    NOT NULL DEFAULT 0,
  enabled      TINYINT(1)    NOT NULL DEFAULT 1 COMMENT '只影响界面上的可选性；内置流程被功能自己调用时不看这一位',
  sort_order   INT           NOT NULL DEFAULT 0,
  created_at   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_workflow_owner_slug (owner_id, slug),
  KEY idx_workflow_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='一条可配置的智能体流水线';

CREATE TABLE IF NOT EXISTS workflow_step (
  id              BIGINT PRIMARY KEY AUTO_INCREMENT,
  workflow_id     BIGINT       NOT NULL COMMENT 'workflow.id',
  `key`           VARCHAR(64)  NOT NULL COMMENT '步骤标识：运行记录、单步重跑与日志都靠它定位',
  group_key       VARCHAR(64)  NOT NULL COMMENT '同组并行执行，组间按 sort_order 顺序',
  sort_order      INT          NOT NULL DEFAULT 0,
  agent_slug      VARCHAR(64)  NOT NULL COMMENT '人设来自 agent.slug，不在模板里复制一份',
  prompt_tpl      TEXT         NOT NULL COMMENT '只做 {{name}} 替换，不含条件判断',
  output_kind     VARCHAR(16)  NOT NULL DEFAULT 'markdown' COMMENT 'markdown | json',
  output_keys     JSON         NULL COMMENT 'output_kind=json 时要求的顶层字段',
  output_var      VARCHAR(64)  NULL COMMENT '有值则本步输出以此名进上下文，供后续步骤引用',
  model_config_id BIGINT       NULL COMMENT '按步骤覆盖模型配置；NULL 用调用者的默认对话模型',
  mcp_server_ids  JSON         NULL COMMENT '节点级挂载的 mcp_server.id 列表，仅本步生效，与 workflow.mcp_server_ids 取并集',
  optional        TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '可选步骤失败不拖垮整条流程',
  created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_workflow_step_key (workflow_id, `key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='工作流的一个步骤（一次智能体调用）';

CREATE TABLE IF NOT EXISTS workflow_run (
  id            BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id      BIGINT       NOT NULL COMMENT 'sys_user.id',
  workflow_id   BIGINT       NOT NULL COMMENT 'workflow.id',
  workflow_slug VARCHAR(64)  NOT NULL COMMENT '定义可能被改名或删除，历史列表要能看懂当时跑的是哪条',
  inputs        JSON         NOT NULL COMMENT '发起时的变量快照，来源数据删了现场仍可回看',
  subject_kind  VARCHAR(32)  NULL COMMENT '认领的业务对象类型：resume | resume-comparison | resume-toolkit | slide-deck | interview-question',
  subject_ref   VARCHAR(64)  NULL COMMENT '该对象的主键，整数 id 转字符串存；面试题用它的 qid',
  subject_label VARCHAR(128) NULL COMMENT '发起时抄下的对象名字（简历标题 / 「公司 · 岗位 · 题干」），对象删了也看得懂这次是给谁跑的',
  status        VARCHAR(16)  NOT NULL DEFAULT 'pending' COMMENT 'pending | running | succeeded | failed | cancelled',
  error_msg     VARCHAR(512) NULL,
  elapsed_ms    BIGINT       NULL,
  started_at    DATETIME     NULL,
  finished_at   DATETIME     NULL,
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_run_owner (owner_id),
  KEY idx_run_workflow (workflow_id),
  KEY idx_run_subject (owner_id, subject_kind, subject_ref)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='一次工作流执行；subject_* 让它兼任业务对象的进度事实源';

CREATE TABLE IF NOT EXISTS workflow_run_step (
  id              BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id        BIGINT       NOT NULL COMMENT '冗余 owner：读快照时按用户收敛，不必先 join run',
  run_id          BIGINT       NOT NULL COMMENT 'workflow_run.id',
  `key`           VARCHAR(64)  NOT NULL,
  group_key       VARCHAR(64)  NOT NULL,
  agent_slug      VARCHAR(64)  NOT NULL,
  sort_order      INT          NOT NULL DEFAULT 0,
  status          VARCHAR(16)  NOT NULL DEFAULT 'pending' COMMENT 'pending | running | succeeded | failed | skipped',
  prompt          MEDIUMTEXT   NULL COMMENT '渲染后的 prompt——「当时模型到底收到了什么」问得出来',
  output          MEDIUMTEXT   NULL COMMENT '模型的原始输出',
  parsed          JSON         NULL COMMENT 'json 步骤解析出的对象；markdown 步骤为 NULL',
  trace           JSON         NULL COMMENT '步骤内部统一执行轨迹 [{kind:llm|tool_start|tool_end,...}]；老运行为 NULL',
  error_msg       VARCHAR(512) NULL,
  model_config_id BIGINT       NULL COMMENT '这次实际用的模型配置',
  agent_id        BIGINT       NULL COMMENT '开跑时绑定的人设 agent.id；上线前老运行为 NULL',
  agent_version   INT          NULL COMMENT '开跑时人设的版本号，对 agent_version 表取当时快照；老运行为 NULL',
  elapsed_ms      BIGINT       NULL,
  started_at      DATETIME     NULL,
  finished_at     DATETIME     NULL,
  created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_run_step_owner (owner_id),
  KEY idx_run_step_run (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='一次运行中单个步骤的现场';

-- 运行对比评测：评分细则是可复用的尺子（2–6 条标准），评测保存时把标准冻结成
-- 快照；被评测引用的运行在留存清理里硬豁免，分数永远有现场可复核（见
-- services/workflows/evals.py）。
CREATE TABLE IF NOT EXISTS workflow_eval_rubric (
  id          BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id    BIGINT       NOT NULL COMMENT 'sys_user.id',
  name        VARCHAR(128) NOT NULL,
  description VARCHAR(512) NULL,
  criteria    JSON         NOT NULL COMMENT '[{name, description}]，2–6 条，顺序即打分表顺序',
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_eval_rubric_owner_name (owner_id, name),
  KEY idx_eval_rubric_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='运行对比评分细则（个人资产）';

CREATE TABLE IF NOT EXISTS workflow_evaluation (
  id              BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id        BIGINT      NOT NULL COMMENT 'sys_user.id',
  workflow_id     BIGINT      NOT NULL COMMENT '两次运行必须同属一条工作流',
  rubric_id       BIGINT      NOT NULL COMMENT 'workflow_eval_rubric.id；细则删除后行仍保留',
  rubric_snapshot JSON        NOT NULL COMMENT '保存时冻结的标准 [{name, description}]',
  run_a_id        BIGINT      NOT NULL COMMENT '基准运行 workflow_run.id',
  run_b_id        BIGINT      NOT NULL COMMENT '候选运行 workflow_run.id；反选是另一次评测',
  scores          JSON        NOT NULL COMMENT '[{index, score_a, score_b}]，index 对齐快照，分数 1–5',
  avg_a           FLOAT       NOT NULL COMMENT 'A 的非加权均分',
  avg_b           FLOAT       NOT NULL COMMENT 'B 的非加权均分',
  note            TEXT        NULL COMMENT '评测者写的总体结论，不由分数替人决定',
  created_at      DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_eval_pair_rubric (owner_id, run_a_id, run_b_id, rubric_id),
  KEY idx_eval_owner (owner_id),
  KEY idx_eval_workflow (workflow_id),
  KEY idx_eval_run_a (run_a_id),
  KEY idx_eval_run_b (run_b_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='一次 A/B 双运行对比评测';


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
-- 对话可选工具（本地工具 + MCP 服务，会话级记住；都由 migrate_schema() 自动补）：
-- ALTER TABLE chat_conversation
--   ADD COLUMN local_tool_keys JSON NULL COMMENT '勾选的本地工具 key',
--   ADD COLUMN mcp_server_ids  JSON NULL COMMENT '挂载的 mcp_server.id 列表';
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

-- 运行记录兼任进度事实源（subject_* 三列由 migrate_schema() 自动补，索引同上）：
-- ALTER TABLE workflow_run
--   ADD COLUMN subject_kind  VARCHAR(32)  NULL COMMENT '认领的业务对象类型',
--   ADD COLUMN subject_ref   VARCHAR(64)  NULL COMMENT '该对象主键的字符串形式，面试题用 qid',
--   ADD COLUMN subject_label VARCHAR(128) NULL COMMENT '发起时抄下的对象名字',
--   ADD KEY idx_run_subject (owner_id, subject_kind, subject_ref);
--
-- 业务表上的进度列退役：status / error_msg（以及面试问题 JSON 里的 ref_status /
-- ref_error）改由最近一次认领的运行翻译出来。这些列在库里是 NOT NULL 且没有 DB
-- 默认值（ORM 的 default= 只写在 Python 侧），插入不再提及它们就会报错，所以
-- migrate_schema() 会在启动时删掉——先查列存在，重复执行是空操作。删列不可逆：
-- 历史行「当时卡在哪个状态」随之消失，产物与运行现场都不受影响。
-- ALTER TABLE resume               DROP COLUMN status, DROP COLUMN error_msg;
-- ALTER TABLE resume_comparison    DROP COLUMN status, DROP COLUMN error_msg;
-- ALTER TABLE resume_toolkit_task  DROP COLUMN status, DROP COLUMN error_msg;
-- ALTER TABLE slide_deck           DROP COLUMN status, DROP COLUMN error_msg;
--
-- AI 运维助手的执行过程回放与思维链开关（都由 migrate_schema() 自动补）：
-- ALTER TABLE chat_message
--   ADD COLUMN timeline JSON NULL COMMENT '本轮按真实顺序落库的事件序列';
-- ALTER TABLE model_config
--   ADD COLUMN reasoning_mode VARCHAR(16) NOT NULL DEFAULT 'auto' COMMENT 'auto | on | off',
--   ADD COLUMN request_extra  JSON NULL COMMENT '仅 on：原样并入请求体的私有参数';
--
-- 对话附件与图片理解（chat_attachment 由 create_all 自动创建，DDL 见上文）：
-- ALTER TABLE model_config
--   ADD COLUMN supports_vision TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否接受图片输入';
--
-- MCP 连通测试的工具快照（migrate_schema() 自动补）：
-- ALTER TABLE mcp_server
--   ADD COLUMN tools_snapshot JSON NULL COMMENT '最近一次测试发现的工具 [{name,description,input_schema}]';

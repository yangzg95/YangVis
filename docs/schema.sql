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
  purpose         VARCHAR(16)   NOT NULL COMMENT 'chat | embedding | image | video | tts',
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
  request_extra   JSON          NULL COMMENT '原样并入请求体的厂商私有参数（chat 的 enable_thinking / thinking.type，tts 的资源 ID 等），代码不猜厂商',
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
  filename      VARCHAR(512) NOT NULL COMMENT '上传为文件名；仓库文件为相对路径，故加长到 512',
  mime          VARCHAR(128) NULL,
  size          BIGINT       NOT NULL DEFAULT 0,
  content       MEDIUMTEXT   NOT NULL COMMENT '提取后的纯文本，重建索引的数据源',
  status        VARCHAR(16)  NOT NULL DEFAULT 'pending' COMMENT 'pending | indexing | ready | error',
  error_msg     VARCHAR(512) NULL,
  chunk_count   INT          NOT NULL DEFAULT 0,
  source        VARCHAR(16)  NOT NULL DEFAULT 'upload' COMMENT 'upload | repo',
  repo_id       BIGINT       NULL COMMENT 'kb_repo.id；source=repo 时非空',
  blob_hash     VARCHAR(64)  NULL COMMENT 'git blob SHA，仓库对账依据（不变则零嵌入调用）',
  language      VARCHAR(32)  NULL COMMENT '代码分片器识别的语言族：python | brace | declarative',
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_owner (owner_id),
  KEY idx_doc_owner_type (owner_id, type_id),
  KEY idx_doc_owner_repo (owner_id, repo_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='上传的知识库文档（含仓库同步物化出的代码文件）';

CREATE TABLE IF NOT EXISTS kb_chunk (
  id            BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id      BIGINT       NOT NULL,
  doc_id        BIGINT       NOT NULL,
  seq           INT          NOT NULL,
  content       TEXT         NOT NULL,
  char_count    INT          NOT NULL DEFAULT 0,
  point_id      VARCHAR(64)  NULL COMMENT 'Qdrant point id',
  line_start    INT          NULL COMMENT 'source=repo 时相对文件头的起始行（1 起），上传文档为 NULL',
  line_end      INT          NULL COMMENT '结束行（含）',
  symbol        VARCHAR(255) NULL COMMENT '所属函数/类/方法名，规则分片器给出',
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_owner (owner_id),
  KEY idx_chunk_owner_doc (owner_id, doc_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='文档分片，与 Qdrant 一一对应';

CREATE TABLE IF NOT EXISTS kb_repo (
  id               BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id         BIGINT       NOT NULL COMMENT 'sys_user.id',
  type_id          BIGINT       NOT NULL COMMENT 'knowledge_type.id，仓库↔知识类型 1:1',
  name             VARCHAR(128) NOT NULL,
  auth_type        VARCHAR(16)  NOT NULL DEFAULT 'ssh_key' COMMENT 'ssh_key | none（匿名 http(s)）| http_token（HTTPS PAT）',
  remote_url       VARCHAR(512) NOT NULL COMMENT '不允许内嵌凭据；展示可能脱敏',
  branch           VARCHAR(255) NULL COMMENT 'NULL = 跟随远端默认分支',
  private_key_enc  VARBINARY(8192) NULL COMMENT 'deploy key 私钥的 Fernet 密文，任何接口都不回传明文',
  http_token_enc   VARBINARY(2048) NULL COMMENT 'HTTPS 个人访问令牌的 Fernet 密文，任何接口都不回传明文',
  http_username    VARCHAR(128) NULL COMMENT '令牌配套用户名；NULL = 缺省 x-access-token',
  public_key       TEXT         NULL COMMENT '生成的 ed25519 公钥行，供用户登记到 Git 平台',
  known_hosts      TEXT         NULL COMMENT 'TOFU 记录的 known_hosts 片段，之后逐次比对',
  host_fingerprint VARCHAR(255) NULL COMMENT '主机公钥 SHA256 指纹，仅展示',
  exclude_globs    TEXT         NULL COMMENT '用户追加的排除 glob，每行一个',
  status           VARCHAR(16)  NOT NULL DEFAULT 'pending' COMMENT 'pending | syncing | ready | error',
  error_msg        VARCHAR(512) NULL,
  head_commit      VARCHAR(40)  NULL COMMENT '最近同步到的 commit SHA',
  last_sync_at     DATETIME     NULL,
  last_sync_result JSON         NULL COMMENT '{added,changed,removed,skipped,indexed_chunks,duration_ms,commit}',
  created_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_repo_type (type_id),
  KEY idx_repo_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='代码仓库：同步时把某次提交的跟踪文件物化进 kb_document';

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

CREATE TABLE IF NOT EXISTS job_posting (
  id            BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id      BIGINT        NOT NULL,
  source        VARCHAR(16)   NOT NULL DEFAULT 'boss',
  external_id   VARCHAR(128)  NOT NULL,
  title         VARCHAR(200)  NOT NULL,
  company       VARCHAR(200)  NOT NULL,
  city          VARCHAR(32)   NOT NULL,
  district      VARCHAR(64)   NULL,
  salary_text   VARCHAR(64)   NOT NULL,
  salary_min    DOUBLE        NULL COMMENT '最低月薪 K；面议/日薪/时薪为 NULL',
  salary_max    DOUBLE        NULL COMMENT '最高月薪 K；50K以上等开放区间为 NULL',
  experience    VARCHAR(32)   NULL,
  education     VARCHAR(32)   NULL,
  skills        JSON          NOT NULL COMMENT '最多 8 个技能/岗位标签',
  recruiter     VARCHAR(200)  NULL,
  published_at  DATETIME      NULL COMMENT '仅来源明确提供的岗位发布时间，不用活跃时间冒充',
  apply_url     VARCHAR(512)  NOT NULL COMMENT '由岗位 ID 生成的 BOSS 官方 HTTPS 地址',
  first_seen_at DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen_at  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_job_posting_owner_source_external (owner_id, source, external_id),
  KEY idx_job_posting_owner_published (owner_id, published_at),
  KEY idx_job_posting_owner_seen (owner_id, last_seen_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='智能应用 · 当前用户缓存的外部招聘岗位';

CREATE TABLE IF NOT EXISTS job_note (
  id         BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id   BIGINT       NOT NULL,
  title      VARCHAR(200) NOT NULL,
  content    MEDIUMTEXT   NULL COMMENT 'markdown 纯文本，渲染在前端（markdown-it，html:false）',
  created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='智能应用 · 求职笔记（简历页的独立记录本，与简历/面试无外键关联）';

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
  expires_at     DATETIME      NULL COMMENT '有效期截止时刻，NULL 表示长期有效；到期后该路由不参与转发',
  token_quota    BIGINT        NULL COMMENT 'token 总额度（total_tokens 累计上限），NULL 表示不限制',
  token_used     BIGINT        NOT NULL DEFAULT 0 COMMENT '已累计消耗的 total_tokens，每次调用后原子累加',
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


-- 通知渠道、移动 App 收件箱与 webhook 发送留痕。AppMessage 是 App 的事实源，
-- 不随渠道数量重复，也不因没有配置 webhook 而缺失。
CREATE TABLE IF NOT EXISTS notify_channel (
  id              BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id        BIGINT       NOT NULL COMMENT 'sys_user.id',
  name            VARCHAR(64)  NOT NULL,
  kind            VARCHAR(16)  NOT NULL COMMENT 'feishu | wecom | dingtalk | generic',
  webhook_url_enc VARBINARY(2048) NOT NULL COMMENT 'Webhook URL 的 Fernet 密文',
  secret_enc      VARBINARY(1024) NULL COMMENT '加签密钥的 Fernet 密文',
  events          JSON         NOT NULL COMMENT '订阅的事件 key 列表',
  enabled         TINYINT(1)   NOT NULL DEFAULT 1,
  last_test_at    DATETIME     NULL,
  last_test_ok    TINYINT(1)   NULL,
  last_test_error VARCHAR(256) NULL,
  created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_notify_owner_name (owner_id, name),
  KEY idx_notify_channel_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Webhook 机器人通知渠道';

CREATE TABLE IF NOT EXISTS notify_log (
  id           BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id     BIGINT       NOT NULL,
  channel_id   BIGINT       NOT NULL COMMENT 'notify_channel.id 的逻辑外键，渠道删除后行仍保留',
  channel_name VARCHAR(64)  NOT NULL COMMENT '渠道名快照，删渠道后历史仍可读',
  event        VARCHAR(32)  NOT NULL COMMENT '事件 key',
  title        VARCHAR(128) NOT NULL,
  body         VARCHAR(1024) NULL COMMENT '通知正文快照，供 webhook 发送审计回放',
  status       VARCHAR(8)   NOT NULL COMMENT 'sent | failed',
  error        VARCHAR(512) NULL,
  elapsed_ms   BIGINT       NULL,
  created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_notify_log_owner_time (owner_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='一次通知发送的留痕';

CREATE TABLE IF NOT EXISTS app_message (
  id         BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id   BIGINT       NOT NULL COMMENT 'sys_user.id；专用通知令牌必须绑定一个 owner',
  event      VARCHAR(32)  NOT NULL COMMENT '业务事件 key',
  title      VARCHAR(128) NOT NULL,
  body       VARCHAR(1024) NULL,
  created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_app_message_owner_id (owner_id, id),
  KEY idx_app_message_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='移动 App 通知收件箱；默认保留 30 天，SSE 按 id 增量补拉';

-- 已部署库里的旧 HMS 渠道、配置表与 notify_log 只归档保留，不做 DROP 或数据改写。
-- 新库不再创建 notify_huawei_config。


-- ---------------------------------------------------------------------------
-- 智能应用 · 短视频（想法 → 脚本/分镜 → 首帧图 → 视频片段，火山方舟 Ark）
--
-- 三张表按「谁记状态」切开：项目行与镜头行只存产物，进度一律不抄一份；
-- 脚本阶段的状态在 workflow_run（subject_kind=video-project），渲染阶段的
-- 状态与产物路径只在 media_job。理由见 app/services/media/__init__.py。
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- 智能应用 · 素材库（跨项目复用的图片/视频素材）
--
-- 与 video_project.assets 那个已经退役的 JSON 数组的区别只有一句话：库属于账号，
-- 不属于某个项目。所以删一条短视频不会带走人物形象图，而「谁在引用这条素材」是
-- 跨行反查（video_shot.material_ids），只能靠一张表来支撑。
-- 文件在服务器本地磁盘的 materials/<owner_id>/ 下，库里只存 rel_path。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS material_asset (
  id         BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id   BIGINT       NOT NULL,
  kind       VARCHAR(8)   NOT NULL COMMENT 'image | video，由文件头魔数判定，不信客户端报的 Content-Type',
  name       VARCHAR(128) NOT NULL COMMENT '界面上看得出的名字，取自上传文件名；不参与任何路径拼接',
  tag        VARCHAR(128) NULL     COMMENT '打标分组：从短视频存进来的首帧自动打上项目名，用户可改；NULL = 未打标',
  mime       VARCHAR(64)  NOT NULL,
  size       BIGINT       NOT NULL DEFAULT 0 COMMENT '字节数，页面用它报全库占用',
  rel_path   VARCHAR(512) NOT NULL COMMENT '相对 UPLOAD_MEDIA_DIR 的路径，URL 由它现拼（不入库，避免两份事实）',
  created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_material_owner_kind (owner_id, kind)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='素材库条目；视频素材这一期只记录引用，参考视频进模型要公网可达的地址';

-- ---------------------------------------------------------------------------
-- 智能应用 · 人物库（跨项目复用的角色档案与形象图）
--
-- 与 material_asset 的分工：素材是无结构的图/视频文件，人物是带文字档案的角色。
-- 档案（人设/衣着/特色）在脚本生成时拼进提示词，形象图在渲染时作为参考图注入。
-- 文件在服务器本地磁盘的 characters/<owner_id>/ 下，库里只存 rel_path；
-- 删除是硬删除，video_project.character_ids 里的残留 id 由视频侧按「已不存在」跳过。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS video_character (
  id         BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id   BIGINT       NOT NULL,
  name       VARCHAR(64)  NOT NULL COMMENT '角色名，脚本提示词与界面展示都用它',
  persona    VARCHAR(512) NOT NULL DEFAULT '' COMMENT '人设：身份、性格、背景、说话方式',
  outfit     VARCHAR(512) NOT NULL DEFAULT '' COMMENT '衣着：固定穿搭，渲染一致性主要靠它和形象图',
  features   VARCHAR(512) NOT NULL DEFAULT '' COMMENT '外形特色：发型、体型、标志物等视觉特征',
  mime       VARCHAR(64)  NOT NULL COMMENT '形象图的 MIME，只收图片，视频在写入前就被拒',
  size       BIGINT       NOT NULL DEFAULT 0,
  rel_path   VARCHAR(512) NOT NULL COMMENT '相对 UPLOAD_MEDIA_DIR 的 characters/<owner_id>/<随机 id>.<扩展名>',
  created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_character_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='人物库条目：角色档案 + 专属形象图，被 video_project.character_ids 认领';

CREATE TABLE IF NOT EXISTS video_project (
  id               BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id         BIGINT       NOT NULL,
  title            VARCHAR(128) NOT NULL COMMENT '工作标题，不是发布标题（后者在 publish 里）',
  platform         VARCHAR(16)  NOT NULL DEFAULT 'douyin' COMMENT 'douyin | kuaishou | other，只影响人设里的节奏与时长偏好',
  idea             MEDIUMTEXT   NOT NULL COMMENT '用户给的那句话（想法/素材/卖点），脚本流程的输入',
  requirement      VARCHAR(512) NULL     COMMENT '补充要求：重新生成脚本时还要按它跑，所以存下来',
  brief            JSON         NOT NULL COMMENT '创作卡：{hook, audience, structure, goal, duration_s}',
  script           MEDIUMTEXT   NOT NULL COMMENT '完整口播/剧情纯文本，供人通读与手改；分镜是它的切分',
  style            JSON         NOT NULL COMMENT '{visual_style, persona, palette}，逐镜提示词都要贴它',
  params           JSON         NOT NULL COMMENT '渲染参数快照：{ratio, resolution, watermark, generate_audio, image_size, tts_enabled, tts_voice, chain_clips}，发起渲染/合成时抄下，改模型配置不影响已入队的作业；本镜时长取 video_shot.duration_s',
  publish          JSON         NOT NULL COMMENT '{title_candidates, tags, cover_prompt}',
  character_ids    JSON         NULL     COMMENT '项目角色组：video_character.id 的字符串列表，上限 CHARACTER_MAX_PER_PROJECT（3）；旧库补列前为 NULL，服务层按空角色组处理',
  created_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='智能应用 · 短视频项目；脚本进度在 workflow_run（subject_kind=video-project），渲染进度在 media_job';

CREATE TABLE IF NOT EXISTS video_shot (
  id             BIGINT PRIMARY KEY AUTO_INCREMENT,
  project_id     BIGINT       NOT NULL,
  owner_id       BIGINT       NOT NULL COMMENT '冗余一份归属：入队与列表都要按 owner 拦一道，不必处处先 join 项目',
  sort_order     INT          NOT NULL DEFAULT 0 COMMENT '镜头顺序，工作台可手改；不靠数组下标，重排与并发增删会漂移',
  start_s        INT          NOT NULL DEFAULT 0 COMMENT '在成片时间线上的起点（秒），字幕时间轴按它算',
  duration_s     INT          NOT NULL DEFAULT 5 COMMENT '本镜时长，受所选视频型号的 duration 区间约束',
  scene          VARCHAR(512) NULL     COMMENT '画面：给人看的那一句「这是什么场景」',
  scene_key      VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '相邻同场景标识：与前一镜一字不差相同才做尾帧衔接，空串=不衔接（链式判定唯一依据，旧库补列默认空）',
  camera         VARCHAR(256) NULL     COMMENT '运镜',
  action         VARCHAR(256) NULL     COMMENT '主体动作',
  dialogue       VARCHAR(512) NULL     COMMENT '台词/口播（本镜那一句，不是全文）',
  on_screen_text VARCHAR(256) NULL     COMMENT '屏幕字（花字/关键词），预览叠显并作为 SRT 的来源',
  image_prompt   VARCHAR(1024) NULL    COMMENT '喂文生图的首帧提示词：决定「长什么样」',
  video_prompt   VARCHAR(1024) NULL    COMMENT '喂视频模型的运动提示词：只描述「怎么动」，两者分开是关键',
  gen_mode       VARCHAR(24)  NOT NULL DEFAULT 'first_frame' COMMENT 't2v | first_frame | first_last_frame | reference（词表见 services/media）',
  material_ids   JSON         NOT NULL COMMENT '本镜引用的 material_asset.id 列表（字符串形式，最多 4 条）；只有图片类素材进得了这一期的请求体',
  image_job_id   BIGINT       NULL     COMMENT 'media_job.id，最近一次首帧作业；状态与产物路径都从那一行读',
  video_job_id   BIGINT       NULL     COMMENT 'media_job.id，最近一次视频作业',
  created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_shot_project (project_id, sort_order),
  KEY idx_shot_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='一个镜头：画面/运动两套提示词 + 指向最近一次图与视频作业的指针（本行无状态列、无产物列）';

CREATE TABLE IF NOT EXISTS media_job (
  id              BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id        BIGINT       NOT NULL,
  provider        VARCHAR(32)  NOT NULL DEFAULT 'ark',
  kind            VARCHAR(16)  NOT NULL COMMENT 'video | image',
  biz_type        VARCHAR(32)  NOT NULL COMMENT 'video-shot | video-project-cover，形状与 workflow_run.subject_kind 一致',
  biz_id          VARCHAR(64)  NOT NULL COMMENT '业务对象 id 转字符串存，与 subject_ref 同一约定',
  model_config_id BIGINT       NULL     COMMENT '用的哪份配置（历史留痕，删配置不回填空）',
  model_name      VARCHAR(128) NOT NULL COMMENT '当时的 Model ID 快照',
  remote_task_id  VARCHAR(64)  NULL     COMMENT '上游任务 id；图片接口没有任务 id，那一类永远是 NULL',
  status          VARCHAR(16)  NOT NULL DEFAULT 'queued' COMMENT 'queued|running|downloading|succeeded|failed|cancelled|expired，前六个与方舟 status 同名同义',
  error_code      VARCHAR(64)  NULL     COMMENT '上游错误码或 Timeout/NoModelConfig 这类本地码',
  error_msg       VARCHAR(512) NULL,
  request         JSON         NOT NULL COMMENT '要发什么的归一形状（提示词+参数+参考素材 rel_path，图片与视频共用 images 键）；base64 不进库',
  response        JSON         NULL     COMMENT '最后一次查询到的原始响应（按字段截断），出错时唯一能看出上游说了什么',
  rel_path        VARCHAR(512) NULL     COMMENT '转存后的本地产物相对路径；上游 URL 24 小时失效，所以这一列才是产物',
  mime            VARCHAR(64)  NULL,
  file_size       BIGINT       NULL,
  last_frame_rel_path VARCHAR(512) NULL COMMENT '视频成功时上游随片返回的尾帧转存路径；链式渲染里下一镜拿它当首帧，图片作业恒为 NULL',
  usage           JSON         NULL     COMMENT '上游用量（token/张数），用量统计与成本核算按它',
  attempts        INT          NOT NULL DEFAULT 0 COMMENT '只数可重试的失败（限流/5xx/网络/下载超时）',
  submitted_at    DATETIME     NULL     COMMENT '是不是空 = 有没有提交给上游；这个区分不能用 remote_task_id 做（图片没有任务 id）',
  last_polled_at  DATETIME     NULL,
  finished_at     DATETIME     NULL,
  elapsed_ms      BIGINT       NULL,
  created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_media_remote_task (remote_task_id) COMMENT 'NULL 可重复，所以未提交的行不受影响',
  KEY idx_media_claim (status, last_polled_at),
  KEY idx_media_biz (biz_type, biz_id),
  KEY idx_media_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='一次媒体生成作业：上游异步任务的本地镜像，渲染进度的唯一事实源，由 lifespan 里的常驻循环推进';


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
--
-- 知识库接入代码仓库（kb_repo 为新表由 create_all 自动创建，DDL 见上文）：
-- HTTPS 个人访问令牌认证（migrate_schema() 自动补）：
-- ALTER TABLE kb_repo
--   ADD COLUMN http_token_enc BLOB          NULL COMMENT 'HTTPS PAT 的 Fernet 密文',
--   ADD COLUMN http_username  VARCHAR(128)  NULL COMMENT '令牌配套用户名';
--
-- ALTER TABLE kb_document
--   MODIFY COLUMN filename VARCHAR(512) NOT NULL,
--   ADD COLUMN source    VARCHAR(16) NOT NULL DEFAULT 'upload' COMMENT 'upload | repo',
--   ADD COLUMN repo_id   BIGINT      NULL COMMENT 'kb_repo.id',
--   ADD COLUMN blob_hash VARCHAR(64) NULL COMMENT 'git blob SHA，对账依据',
--   ADD COLUMN language  VARCHAR(32) NULL COMMENT 'python | brace | declarative',
--   ADD KEY idx_doc_owner_repo (owner_id, repo_id);
--
-- ALTER TABLE kb_chunk
--   ADD COLUMN line_start INT          NULL COMMENT '代码文件起始行（1 起）',
--   ADD COLUMN line_end   INT          NULL COMMENT '结束行（含）',
--   ADD COLUMN symbol     VARCHAR(255) NULL COMMENT '函数/类/方法名';

-- 通知留痕补正文快照（migrate_schema() 启动时自动补，手工重复执行会因列已存在而报错）：
-- ALTER TABLE notify_log
--   ADD COLUMN body VARCHAR(1024) NULL COMMENT '通知正文快照，供 webhook 发送审计回放' AFTER title;

-- ---------------------------------------------------------------------------
-- 智能应用 · 短视频发布（成片合成 + 手动发布包，由 create_all 自动建表）
--
-- media_job 对接火山方舟、由常驻循环推进；publish_job 是本机 ffmpeg 的活，
-- 跑在请求级后台任务里。request.sources 是发起时的片段 rel_path 顺序快照，
-- request.tts（可选）是配音开关、音色与逐镜台词/时间轴快照，成片落在
-- videos/<project_id>/ 下，删项目时跟着目录树一起清。
-- channel 目前只有 manual；open_api / browser 是预留通道，不需要迁移。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS publish_job (
  id          BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id    BIGINT       NOT NULL,
  project_id  BIGINT       NOT NULL,
  platform    VARCHAR(16)  NOT NULL DEFAULT 'douyin' COMMENT 'douyin | kuaishou | other，发起时从项目抄过来',
  channel     VARCHAR(16)  NOT NULL DEFAULT 'manual' COMMENT 'manual | open_api | browser，后两个预留',
  status      VARCHAR(16)  NOT NULL DEFAULT 'queued' COMMENT 'queued | running | succeeded | failed',
  error_msg   VARCHAR(512) NULL,
  request     JSON         NOT NULL COMMENT 'sources 片段 rel_path 顺序快照 + 标题/标签/封面快照',
  rel_path    VARCHAR(512) NULL COMMENT '合成成片的相对路径，URL 由 store 现拼，与 media_job 同一约定',
  mime        VARCHAR(64)  NULL,
  file_size   BIGINT       NULL,
  elapsed_ms  BIGINT       NULL COMMENT '从入队执行到产物落盘的墙上时间',
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  finished_at DATETIME     NULL,
  KEY idx_publish_project (project_id, id),
  KEY idx_publish_owner_status (owner_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='短视频发布作业：成片合成与发布包';

-- ---------------------------------------------------------------------------
-- 智能应用 · 创作（写小说 / 写文章，由 create_all 自动建表）
--
-- writing_work 一行是一部小说或一篇文章（kind 区分），outline JSON 是大纲树的
-- 唯一事实源：小说为「卷 → 章」两级（也允许章直接挂顶层），文章为扁平段落；
-- 叶子 cid 与 writing_part 一行 1:1 对齐。行上不存生成状态——M2 起进度在
-- workflow_run（subject_kind = writing-work / writing-part），同 slide_deck。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS writing_work (
  id          BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id    BIGINT       NOT NULL,
  kind        VARCHAR(8)   NOT NULL COMMENT 'novel | article，服务端白名单',
  title       VARCHAR(128) NOT NULL,
  brief       MEDIUMTEXT   NOT NULL COMMENT '灵感/主题原文，AI 生成的输入快照',
  requirement VARCHAR(512) NULL COMMENT '补充要求，重新生成时仍生效',
  params      JSON         NOT NULL COMMENT '按 kind 白名单清洗过的创作参数',
  outline     JSON         NOT NULL COMMENT '大纲树 [{cid,kind,title,synopsis,target_words,children?}]',
  meta        JSON         NOT NULL COMMENT '副产物：标题候选、标签等',
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_writing_work_owner (owner_id),
  KEY idx_writing_work_owner_kind (owner_id, kind)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='创作作品：小说/文章';

CREATE TABLE IF NOT EXISTS writing_part (
  id         BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id   BIGINT       NOT NULL,
  work_id    BIGINT       NOT NULL,
  cid        VARCHAR(32)  NOT NULL COMMENT 'p + 16 位 hex，与幻灯片 sid 同形，数组下标永不参与寻址',
  sort_order INT          NOT NULL DEFAULT 0 COMMENT '大纲叶子展平后的序号',
  title      VARCHAR(128) NOT NULL DEFAULT '',
  content    MEDIUMTEXT   NOT NULL COMMENT 'markdown 正文',
  word_count INT          NOT NULL DEFAULT 0 COMMENT '服务端统计：CJK 字 + 拉丁词',
  created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_writing_part_cid (work_id, cid),
  KEY idx_writing_part_owner (owner_id),
  KEY idx_writing_part_work (owner_id, work_id, sort_order)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='创作正文块：小说章节 / 文章段落';
-- 修复连接器:注册令牌 + 连接器身份
-- 应用启动会自动建表(checkfirst);手工部署 MySQL 时用这份 DDL 对齐结构。

CREATE TABLE IF NOT EXISTS repair_enrollment_tokens (
  token_id   VARCHAR(32) NOT NULL PRIMARY KEY,
  target_id  BIGINT      NOT NULL,
  token_hash VARCHAR(64) NOT NULL,
  expires_at DATETIME    NOT NULL,
  used_at    DATETIME    NULL,
  revoked_at DATETIME    NULL,
  created_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_repair_token_target (target_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS repair_connectors (
  id             VARCHAR(32) NOT NULL PRIMARY KEY,
  target_id      BIGINT      NOT NULL,
  name           VARCHAR(128) NOT NULL,
  status         VARCHAR(16)  NOT NULL DEFAULT 'online',
  secret_hash    VARCHAR(64)  NOT NULL,
  capabilities   JSON         NULL,
  host_info      JSON         NULL,
  last_seen_at   DATETIME     NULL,
  created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  revoked_at     DATETIME     NULL,
  KEY idx_repair_connector_target (target_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS repair_plans (
  id            VARCHAR(32)  NOT NULL PRIMARY KEY,
  target_id     BIGINT       NOT NULL,
  connector_id  VARCHAR(32)  NOT NULL,
  conversation_id BIGINT     NULL,
  diagnosis_id  VARCHAR(16)  NULL,
  action        VARCHAR(32)  NOT NULL,
  service       VARCHAR(128) NOT NULL,
  params        JSON         NULL,
  reason        VARCHAR(500) NOT NULL DEFAULT '',
  risk          VARCHAR(16)  NOT NULL DEFAULT '中',
  plan_hash     VARCHAR(64)  NOT NULL,
  status        VARCHAR(32)  NOT NULL DEFAULT 'awaiting_confirmation',
  created_by    VARCHAR(64)  NOT NULL DEFAULT 'admin',
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at    DATETIME     NOT NULL,
  KEY idx_repair_plan_target (target_id),
  KEY idx_repair_plan_connector (connector_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS repair_approvals (
  id          VARCHAR(32) NOT NULL PRIMARY KEY,
  plan_id     VARCHAR(32) NOT NULL,
  plan_hash   VARCHAR(64) NOT NULL,
  approved_by VARCHAR(64) NOT NULL,
  approved_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at  DATETIME    NOT NULL,
  status      VARCHAR(16) NOT NULL DEFAULT 'active',
  KEY idx_repair_approval_plan (plan_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS repair_executions (
  id            VARCHAR(32) NOT NULL PRIMARY KEY,
  plan_id       VARCHAR(32) NOT NULL,
  approval_id   VARCHAR(32) NOT NULL,
  request_id    VARCHAR(64) NULL,
  connector_id  VARCHAR(32) NOT NULL,
  status        VARCHAR(24) NOT NULL DEFAULT 'running',
  started_at    DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at   DATETIME    NULL,
  result        JSON        NULL,
  verification  JSON        NULL,
  error         VARCHAR(500) NULL,
  KEY idx_repair_execution_plan (plan_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS repair_audit_logs (
  id           BIGINT      NOT NULL AUTO_INCREMENT PRIMARY KEY,
  target_id    BIGINT      NULL,
  connector_id VARCHAR(32) NULL,
  plan_id      VARCHAR(32) NULL,
  execution_id VARCHAR(32) NULL,
  actor        VARCHAR(64) NOT NULL DEFAULT 'admin',
  event_type   VARCHAR(48) NOT NULL,
  payload      JSON        NULL,
  created_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_repair_audit_target (target_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 诊断表新增列(应用启动会自动 ALTER,手工部署时可执行一次)
-- ALTER TABLE diagnosis_runs ADD COLUMN platform VARCHAR(128) NULL;
-- ALTER TABLE diagnosis_runs ADD COLUMN confirmation VARCHAR(16) DEFAULT 'pending';
-- ALTER TABLE diagnosis_runs ADD COLUMN confirmed_at DATETIME NULL;
-- ALTER TABLE diagnosis_runs ADD COLUMN confirmed_by VARCHAR(64) NULL;

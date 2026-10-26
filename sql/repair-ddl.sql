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

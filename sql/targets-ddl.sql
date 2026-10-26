-- =============================================================
-- 靶子接入 · 建表 DDL
-- 新建:targets(外接数据源注册表:被测服务 + 若干观测数据源)
-- 数据源是通用结构:sources JSON 数组,元素 {"type":"prometheus|jaeger","url":"..."},
-- 同类型可配多路;接新类型系统时在 app/api/targets.py 的 PROBES 注册探活器即可。
-- 说明:本地 SQLite 不用跑这份,应用启动时 ensure_targets_table() 会自建;
--       这份给 MySQL 部署按惯例手工执行。
-- =============================================================

SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS targets (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '靶子主键',
  name        VARCHAR(128)    NOT NULL                COMMENT '靶子名称,如 otel-demo',
  cluster     VARCHAR(64) NOT NULL DEFAULT '' COMMENT '所属集群',
  sources     JSON            NOT NULL                COMMENT '观测数据源数组,元素 {"type","url"}',
  enabled     INT UNSIGNED    NOT NULL DEFAULT 1      COMMENT '1=接入 0=保留配置暂不接入(为 Agent 侧接线预留)',
  created_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '接入时间',
  updated_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最近修改',
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='检测靶子:外接数据源注册表';

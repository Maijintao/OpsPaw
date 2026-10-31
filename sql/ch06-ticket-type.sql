-- 运维化迁移:将旧客服工单类型改为 on-call 工单类型。

-- 确保中文 ENUM 定义值按 utf8mb4 解析
-- (否则 latin1 默认的 mysql client 会把中文 double-encode,四个枚举值全存成乱码)
SET NAMES utf8mb4;

-- Preserve old rows by mapping customer-service-only categories to the generic
-- on-call consultation type before replacing the enum values.
UPDATE tickets SET ticket_type='咨询' WHERE ticket_type IN ('售后','退款');

ALTER TABLE tickets
  MODIFY COLUMN ticket_type ENUM('故障','变更','咨询','投诉') NOT NULL;

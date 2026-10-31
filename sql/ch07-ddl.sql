-- =============================================================
-- ch07 · 会话上下文管理 · 建表 DDL
-- 本章不新建表,给 ch02 的 conversations 表加两列存滚动摘要
-- 双层结构:最近几轮留原文(滑窗),更早的轮次异步压成摘要接在上下文前头
-- 摘要落 conversations 表,不单开摘要表
-- =============================================================

-- 确保中文 COMMENT 按 utf8mb4 解析(latin1 默认的 mysql client 会把中文 double-encode)
SET NAMES utf8mb4;

ALTER TABLE conversations
  ADD COLUMN summary             TEXT            NULL COMMENT '早期轮次滚动摘要,接在上下文前头' AFTER status,
  ADD COLUMN summary_upto_msg_id BIGINT UNSIGNED NULL COMMENT '摘要已覆盖到哪条消息,滑窗从其后接原文' AFTER summary;

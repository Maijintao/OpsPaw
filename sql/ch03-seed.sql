-- =============================================================
-- ch03 · 合成历史值班对话(喂挖知识 job)。三段式 + SET NAMES utf8mb4 + 幂等。
-- =============================================================
SET NAMES utf8mb4;

-- 段1 查询:执行前 seed 会话数
SELECT COUNT(*) AS before_conv FROM conversations WHERE user_id LIKE 'seed-%';

-- 段2 写入:先幂等清理 seed-% 会话及其消息,再插入
DELETE FROM messages WHERE conversation_id IN (SELECT id FROM conversations WHERE user_id LIKE 'seed-%');
DELETE FROM conversations WHERE user_id LIKE 'seed-%';

INSERT INTO conversations (user_id, status) VALUES ('seed-u1', '已结束'), ('seed-u2', '已结束');
SET @c1 = (SELECT id FROM conversations WHERE user_id = 'seed-u1' ORDER BY id DESC LIMIT 1);
SET @c2 = (SELECT id FROM conversations WHERE user_id = 'seed-u2' ORDER BY id DESC LIMIT 1);

INSERT INTO messages (conversation_id, role, content) VALUES
  (@c1, 'user',      '收到告警后第一步要干嘛啊'),
  (@c1, 'assistant', '先认领再排查:在值班群回复接手,认领后 5 分钟内开始排查,先看告警级别定响应优先级,不要一上来就重启。'),
  (@c2, 'user',      '变更窗口是几点'),
  (@c2, 'assistant', '常规变更窗口为每日 02:00-06:00(业务低峰),窗口外变更默认需二线值班确认,紧急止血不受窗口限制。');

-- 段3 验证:seed 会话应为 2,消息应为 4
SELECT COUNT(*) AS after_conv FROM conversations WHERE user_id LIKE 'seed-%';
SELECT COUNT(*) AS after_msg FROM messages WHERE conversation_id IN (SELECT id FROM conversations WHERE user_id LIKE 'seed-%');

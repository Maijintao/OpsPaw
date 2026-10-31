-- 存量库升级:messages 加 meta 列——前端历史回载重建动作卡(诊断卡/服务卡/按钮)用。
-- 动机:诊断卡片刷新即丢,suggested_actions 此前不落库,回载靠文案精确匹配兜不住。
-- 新装库直接用 ch02-ddl.sql(已含 meta),不用跑本脚本。
ALTER TABLE messages
  ADD COLUMN meta JSON NULL COMMENT '前端回放数据(如 suggested_actions),不进 LLM 上下文';

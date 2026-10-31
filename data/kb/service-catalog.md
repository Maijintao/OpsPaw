# 服务目录与归属(CMDB)

## 服务清单总表

平台现有 8 个核心服务,编号与服务名一一对应:1001 order-service(订单服务)、2002 payment-service(支付服务)、3003 data-sync-service(数据同步服务)、4004 api-gateway-service(API 网关服务)、5005 user-service(用户服务)、6006 notification-service(通知服务)、7007 search-service(搜索服务)、8008 cart-service(购物车服务)。服务编号是 4 位数字,查服务状态、发起变更都用编号。

## 各服务负责团队

交易平台组负责:order-service(1001)、cart-service(8008)。支付组负责:payment-service(2002)。数据组负责:data-sync-service(3003)、search-service(7007)。基础架构组负责:api-gateway-service(4004)、user-service(5005)、notification-service(6006)。找服务负责人先按团队找,再按值班表确认当班人。

## 核心服务依赖关系

调用链自上而下:api-gateway-service(4004) 是统一入口,下游调用 order-service(1001)、user-service(5005)、search-service(7007);order-service(1001) 依赖 payment-service(2002) 完成支付、依赖 cart-service(8008) 读购物车;data-sync-service(3003) 消费上游业务库做数据同步,是异步链路;notification-service(6006) 被订单和支付调用发送通知。排障时按依赖方向查:网关报错先看下游哪个服务,下游服务报错再看它的依赖。

## 服务等级与告警策略

支付链路服务(payment-service 2002、order-service 1001)为 P0 等级,告警立即响应;网关与用户服务(4004、5005)为 P1;其余为 P2。所有服务 CPU 告警阈值 80%、内存 70%、错误率 50% 触发紧急告警。

## 数据同步服务(data-sync-service)说明

data-sync-service(3003) 是异步数据同步服务,负责把业务库数据同步到数仓与搜索索引,归数据组。它的日志分两个主题:topic-001 应用日志、topic-002 错误日志(查询用 search_topic_by_service_name 传服务名定位)。该服务报错通常表现为同步任务失败、数据延迟,不影响线上交易主链路,但会导致报表与搜索数据滞后。

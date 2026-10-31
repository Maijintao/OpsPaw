# 归因板块交接文档

写给下一个接手的人。只讲怎么用、卡在哪、坑在哪，不讲愿景。

## 一、两台机器

**靶机（被诊断的电商演示）**

```
42.193.188.243    ubuntu / sztu@2024
2 核 / 3GB / 30GB，22 个容器
演示项目在 /home/ubuntu/opentelemetry-demo
```

**跑批与服务机**

```
54.252.67.182    ssh -i ~/Desktop/opspaw.pem ubuntu@54.252.67.182
OpsPaw 在 ~/OpsPaw，服务端口 8000，systemd 托管
```

本地改完代码用 `./scripts/sync_server.sh` 同步到远端，加 `--restart` 会顺带重启服务并做健康检查。

## 二、跑批怎么看

**前置自检**（每次跑之前先跑，它会拦住不该跑的场合）

```bash
cd ~/OpsPaw && set -a && source .env && set +a
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
export PYTHONUTF8=1 PYTHONPATH=.
.venv/bin/python scripts/bench_preflight.py
```

九项检查：服务端、三条隧道、并发、归因依赖、模型通道、历史深度、靶子开关。历史深度不够会直接拦下。

**手动开跑**

```bash
sudo systemd-run --unit=bench-manual --collect --quiet --uid=ubuntu --gid=ubuntu \
  --working-directory=/home/ubuntu/OpsPaw /home/ubuntu/OpsPaw/scripts/nightly_bench.sh
```

11 组，settle 300 秒，一轮约 2 小时 40 分。定时任务每天 06:30 自动跑一轮。

**看结果**

```bash
L=$(ls -t ~/OpsPaw/log/bench_night_*.log | head -1)
grep -E "^detection|^localization|^mitigation|其中|剔除" "$L"
grep -E "^  [✓✗] localization" "$L"
```

日志里还有两行值得看：`注入效果`（故障到底造出来没有）、`其中 N 题…`（哪些题的结果不可信）。

**看靶机状态**

```bash
sshpass -p 'sztu@2024' ssh ubuntu@42.193.188.243 'cat /proc/loadavg; free -m | tail -2'
```

负载长期 6 以上说明内存不够在拿磁盘顶，指标会抖。压测器配置在 `/home/ubuntu/opentelemetry-demo/.env` 与 `compose.yaml`，改完要 `docker compose -f compose.yaml -f compose.observability.yaml -f compose.loki.yaml up -d --no-deps load-generator`。

## 三、当前的难点

**1. 分数不稳，单次结果不可信**

同一套代码跑出过 5、5、4、3、1。改一个判据跑一题看结果，等于在看噪声。要判断改动好坏，至少同题重复跑三轮。

**2. 流量一变，某个服务就被顶成锚点**

流量从 5 个用户提到 20 个之后，先出现「cart 霸榜」（10 题答 6 次），撤掉那条改动后又出现「payment 霸榜」（10 题答 8 次）。共同点：某个服务的某个维度相对它自己的历史暴涨，被判成锚点，压过真正出问题的服务。

payment 那次的具体证据：

```
粗筛锚点:payment(调用量暴涨:当前 0.4333,超出自身正常上界 0.02083
                    历史 p50 0.004167 / p99 0.004167)
```

它历史上一整个基线期几乎没人调用，流量提上来之后下单流程才跑起来。

**3. 只有一档时间尺度**

当前窗和历史窗都是 300 秒。突发型故障能抓，缓慢劣化（内存泄漏、连接池耗尽）看不见——每五分钟涨一点，每次都在波动范围里。

**4. 三个 agent 里前两个贡献很小**

异常哨兵、故障诊断、根因侦探三个角色串行跑。前两个的结论现在会传给根因侦探（`opsagent_adapter.run_opsagent` 里拼进它的 user_prompt），但最终判定基本只看根因侦探。改之前它们连结论都不通。

**5. 开关变更通道只覆盖一个服务**

靶机上只有 cart 上报 flagd 求值遥测，其他服务一个都没有。而且指标族里只有 `feature_flag_evaluation_success_total` 带 `feature_flag_result_variant` 标签，另外两个指标族报不出取值。

**6. 源头没有信号时，纯遥测推不出来**

`recommendationCacheFailure` 这题，recommendation 错误率 0.017 次/秒，调用方 frontend-web 是 1.05 次/秒。缓存坏在它身上，但它自己的指标没动。这题无论怎么改排序都答不对。

## 四、归因板块的代码地图

链路：粗筛 → 四维偏离 → 候选构造与排序 → 三个 agent → 守卫 → 结论。

| 文件 | 职责 |
|---|---|
| `app/diagnosis/detect.py` | 粗筛。历史分布建基线，锚点选择，因果剪枝 |
| `app/diagnosis/stats.py` | 分布判据本体。`deviation()` 判异常，`band()` 算边界 |
| `app/diagnosis/deviation.py` | 四维偏离。错误率、时延、CPU、内存 |
| `app/diagnosis/localize.py` | 候选构造、层级、排序键 `rank_key` |
| `app/diagnosis/edgeview.py` | 边级错误归属，谁的失败调用指向谁 |
| `app/diagnosis/flagchange.py` | 开关变更探测 |
| `app/diagnosis/faulttype.py` | 故障形态判定 |
| `app/diagnosis/opsagent_adapter.py` | 三 agent 编排、结论解析、三道守卫 |
| `app/diagnosis/opsagent/prompts/root_detective_prompt.py` | 根因侦探的提示词 |

**判据形式**：历史窗口排成分布，边界取 99 分位加四倍四分位距。错误率单独用泊松波动当噪声底，因为它大半窗口是零，四分位距会退化成零。四倍这个系数是经验值，没有论文依据，换环境要重标。

**排序键**：层级 → 有没有因果实锤 → 粗筛排位 → 发病先后 → 调用距离 → 层内偏离 → 服务名。层级从硬到软：别人调它它不回应、自己报错、慢在自己身上、慢在等下游、资源高、没人调它了。

## 五、已经踩过的坑

**别把开关变更探测提前到检测阶段**

试过，它会把跑批自己的开关翻转当成证据。跑批每二十分钟翻一次开关，探测窗口十五分钟，于是每道题都能查到一次「新鲜变更」，落点全是 cart，直接把 cart 抬成 tier 1。已回滚。

**别用 `sudo rm -rf 路径/*` 清数据**

通配符是普通用户展开的，没权限就静默跳过还返回成功。要用 `sudo sh -c 'rm -rf 路径/*'`。

**改流量之后必须重建基线**

历史分布是跟服务自己的过去比。流量从 5 个用户提到 20 个，所有服务相对历史都是暴涨，判据全面误报。2026-10-26 清过一次 Prometheus 数据，清完要等 8.3 小时（100 个 300 秒窗）才能跑。

**`HISTORY_HOURS` 与数据保留期不一致**

常量写 168 小时（7 天），Prometheus 实际只留 48 小时。已加 `detect._retention_hours()` 读数据源的真实保留期做收口，实测从 168 收到 48。

**开关变更曾经静默失败**

查到了数据、解析不出来、返回空列表加空说明，从外面看就是这功能不存在。现在会如实说原因。

## 六、改代码的规矩

- 不用 git 回滚，要恢复就用编辑工具手动改回去
- 不用 heredoc、sed、python 脚本改代码，一律用编辑工具
- 不写死阈值，边界从数据算
- 注释用中文，标识符保持英文
- 改完要跑，跑完要看结果，不能实现完就交

## 七、下一步该做什么

按性价比排：

1. **先把「某个维度相对历史暴涨就当锚点」这条堵住**。两个磁铁问题都是它引起的，这是当前分数最大的单一原因。
2. **同题重复跑三轮**，先把噪声范围摸清楚，否则后面所有改动都无法验证。
3. **加长窗交叉验证**。同一个服务同时看 5 分钟和 1 小时，两个窗都异常才算持续性故障，只有短窗异常算瞬时抖动。这条能补上慢性劣化的盲区。
4. 开关变更通道要覆盖更多服务，前提是让靶机的其他服务也上报 flagd 遥测。

第 1 条不做，后面几条都白搭——磁铁问题会把所有改动的影响盖掉。

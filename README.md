# NewsRadar

一个跑在 ARM64 小机器上的 7×24 财经宏观新闻雷达。

它持续采集财经新闻，在本地做规范化、去重和聚类；分数只决定处理顺序，不作为分析门槛。非重复、未过期的内容都会交给大模型分析，再把「事实摘要 + 影响链条 + 置信度 + 原文链接」推送到微信。

它不预测市场，也不替用户做决定；它做的是把每天几百条新闻压成几条值得看的内容，把判断留给人。

## 功能

- **多源采集**：天行数据、RSS、NewsAPI、GNews、Finnhub
- **本地处理**：HTML 清洗、URL 规范化、精确去重、SimHash 近重复聚类、启发式候选评分
- **批量分析**：兼容 OpenAI Chat Completions 接口，默认 DeepSeek，也可换 OpenAI、Kimi、GLM、通义、OpenRouter、硅基流动、Ollama
- **即时推送**：分析完成即推，不设定时摘要；模型判定与A股宏观相关、且置信度达到阈值才发送
- **额度提醒**：当日剩余推送次数降到 5 条时，额外发一条提醒
- **Web 管理**：仪表盘、新闻池、分析历史、采集源、模型、推送渠道、费用与 token 预算
- **费用控制**：按人民币记账，支持每日 token 预算和每日费用上限，达到上限自动停止
- **低资源运行**：FastAPI + SQLite + APScheduler，无 Redis、无 Celery、无 Node 构建
- **可运维**：systemd 双服务、健康检查、失败重试、日志脱敏、SQLite 备份

## 架构

```text
天行 / RSS / NewsAPI / GNews / Finnhub
                  │
                  ▼
         采集 collector（worker）
                  │
                  ▼
     规范化 → 去重 → 聚类 → 候选评分
                  │
                  ▼
           SQLite（WAL）
                  │
        ┌─────────┴─────────┐
        ▼                   ▼
  FastAPI Web          analyzer（worker）
  管理后台                   │
                        DeepSeek / 其他 LLM
                             │
                             ▼
                      push（WxPusher 等）
```

- `web` 只负责配置、展示和手动触发，不跑定时任务
- `worker` 负责采集、分析、推送和清理
- 两者共享同一个 SQLite 数据库；所有任务可重入，重启不会重复推送

## 技术栈

| 层 | 选型 |
|---|---|
| 语言 | Python 3.11+ |
| Web | FastAPI + Uvicorn + Jinja2 |
| 数据库 | SQLite（WAL）+ SQLAlchemy 2.0 + Alembic |
| 调度 | APScheduler |
| HTTP | httpx + feedparser + BeautifulSoup |
| 加密 | Fernet（配置加密）+ Argon2id（管理员密码） |
| 部署 | systemd + Caddy（可选） |
| 目标平台 | ARM64（Orange Pi 3B / RK3566 已验证） |

## 目录结构

```text
app/
  main.py                 # FastAPI 应用
  worker.py               # 采集/分析/推送 worker
  models.py               # SQLAlchemy 模型
  settings_service.py     # 分组配置、加密、缓存
  migrations/             # Alembic 迁移
  ingest/                 # 天行、RSS、海外新闻源适配
  analysis/               # 提示词、JSON 校验、计费、批量分析
  push/                   # 推送渠道与消息组装
  templates/              # Jinja2 页面
  static/                 # 本地 CSS/JS
deploy/
  install.sh
  systemd/
  Caddyfile
tests/
docs/
  providers.md            # 新闻源 / LLM / 推送渠道
  hardware-selection.md   # 低成本硬件与 BOM
```

## 快速开始

### 1. 准备

需要：

- Linux（ARM64 或 x86_64 均可）
- Python 3.11+
- SQLite 3
- 可访问新闻源和 LLM API 的网络

```bash
git clone https://github.com/peyton-yan/newsradar.git
cd newsradar
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

也可以从 Gitee 克隆：

```bash
git clone https://gitee.com/pimpyan/newsradar.git
```

### 2. 配置环境变量

复制示例文件：

```bash
sudo mkdir -p /etc/newsradar /var/lib/newsradar /var/backups/newsradar
sudo cp deploy/newsradar.env.example /etc/newsradar/newsradar.env
sudo chmod 600 /etc/newsradar/newsradar.env
```

最小配置：

```ini
NR_ENV=production
NR_DB_PATH=/var/lib/newsradar/newsradar.db
NR_MASTER_KEY_FILE=/etc/newsradar/master.key
NR_HOST=127.0.0.1
NR_PORT=8080
NR_LOG_LEVEL=INFO
```

首次启动会自动生成 `NR_MASTER_KEY_FILE`，权限 600。

### 3. 初始化数据库

```bash
alembic upgrade head
```

### 4. 启动

开发/单机直接启动：

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8080
python -m app.worker
```

浏览器打开 `http://127.0.0.1:8080`，按首次向导完成：

1. 设置管理员密码
2. 填写新闻源 Key
3. 填写 LLM Key
4. 配置推送渠道
5. 发送测试推送

> 管理后台默认只监听 `127.0.0.1`。局域网访问请用 Caddy/Nginx 反代，不要把 8080 直接暴露到公网。

### 5. systemd（推荐）

仓库自带两个 unit：

```bash
sudo cp deploy/systemd/newsradar-web.service /etc/systemd/system/
sudo cp deploy/systemd/newsradar-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now newsradar-web newsradar-worker
```

查看状态：

```bash
systemctl status newsradar-web newsradar-worker
journalctl -u newsradar-worker -f
curl -s http://127.0.0.1:8080/healthz
```

### 6. 局域网访问（可选）

`deploy/Caddyfile` 提供反向代理示例：

```caddyfile
news.lan {
    reverse_proxy 127.0.0.1:8080
}
```

手机和电脑在同一局域网时访问 `http://news.lan` 或服务器 IP。需要 HTTPS 时可在 Caddy 中配置内部证书。

## 配置说明

后台路径：

| 页面 | 作用 |
|---|---|
| `/` | 仪表盘：采集、分析、推送、心跳、费用 |
| `/news` | 新闻池与详情 |
| `/analyses` | 分析历史与推送状态 |
| `/sources` | 天行、RSS、海外新闻源 |
| `/settings/analysis` | 模型、频率、批量、候选分门槛、token 预算、费用上限 |
| `/settings/push` | 推送渠道、相关性分数门槛、紧急阈值、每日上限、测试 |
| `/settings/security` | 修改密码 |

### 分析频率

- 默认每 120 分钟检查一次，不设每日分析次数上限
- “极速 5 分钟”预设：每 5 分钟检查一次，批次不限
- 真正的硬上限是每日费用和输入/输出 token 预算
- 只要有 **1 条候选**就分析，不再等凑够 3 条
- **候选分门槛**在 `/settings/analysis` 修改，默认 **0**：0 表示不拦，所有非重复、未过期新闻都进入分析；大于 0 时才按本地分数过滤
- 达到 token 或费用上限自动停止，次日 00:00 恢复

### 提示词自定义

`/settings/analysis` 提供“提示词自定义”：

- 固定提示词模板只读展示，包含评分区间、JSON 字段、sectors 列表和输出格式
- 用户最多填写 2000 字补充要求，插入到模板的唯一可修改区
- 补充要求只能影响关注重点和表达方式，不能修改固定格式；冲突时以固定规则为准
- 留空表示使用默认提示词

### 推送

- 分析完成即推，不设定时摘要
- 推送前先看 **财经/A股相关度评分**：模型给 0–100 分，低于门槛的不推
- **相关性分数门槛**在 `/settings/push` 修改，默认 **50**；达到该分数才推送，重要性/推送分只用于排序
- 0 表示不按相关性过滤，不是自动把天文、体育、娱乐变成相关
- 每条消息默认最多 5 条新闻

### 相关性分、推送分、候选分

它们是不同的东西：

| 名称 | 谁产生 | 用在哪 |
|---|---|---|
| `relevance` 相关性分 | 模型 | **推送门槛**；0–100，低于后台设置的门槛不推 |
| `confidence` 置信度 | 模型 | 只表示事实/传导链的确定性；**不决定推不推** |
| `push_score` 推送分 | 服务端按 `0.55 × 重要性 + 0.25 × 时效 + 0.20 × 置信度` 计算 | **决定排序和紧急标记**；不决定是否推送 |
| `heuristic_score` 候选分 | 本地按关键词、来源、时效、簇大小计算 | **只决定分析顺序**；默认门槛 0，不拦新闻 |

推送资格：

```text
relevance >= push.min_confidence
```

`relevance` 是模型对“财经/股票/宏观相关度”的打分，不是置信度，也不是关键词命中。公司回购、并购、行业政策等即使没有固定关键词，只要模型判断有财经/A股传导，就能得到较高分；天文、体育、娱乐会得到低分。

模型允许做多跳联想，但必须写出具体链条。例如“未来 6 个月少雨 → 水库来水减少 → 水力发电量下降 → 局部电力供应偏紧 → 火电/电煤需求预期上升 → 电力、煤炭板块”，可以给 65–79 分；只写“天气可能影响经济”这种空话则不给高分。
- 每日推送上限默认 **0（不限）**；大于 0 时才按设置限额
- 限额大于 0 且剩余 5 条时，发送一次额度提醒
- 支持 WxPusher、PushPlus、Server酱、企业微信机器人

### LLM 与新闻源

服务商、模型名、Base URL、单价都可在后台修改。默认：

- LLM：DeepSeek `deepseek-flash`
- 中文财经：天行数据
- 海外补充：RSS / NewsAPI / GNews / Finnhub

各服务商的免费额度、资费和限制见 [docs/providers.md](docs/providers.md)。

## 费用

DeepSeek 按官方人民币价记账，单位为元/百万 tokens：

| 项目 | 空闲时段 | 高峰时段 |
|---|---:|---:|
| 输入缓存命中 | ¥0.02 | ¥0.04 |
| 输入缓存未命中 | ¥1 | ¥2 |
| 输出 | ¥4 | ¥8 |

高峰时段：北京时间周一至周五 09:00–12:00、14:00–18:00；其余为空闲。节假日未单独识别，按高峰保守估算。

后台按当日实际 `usage` 记账，只显示人民币。每日费用上限默认 ¥2.00，0 表示不限。

## 运维

### 健康检查

```bash
curl -s http://127.0.0.1:8080/healthz
```

返回数据库、worker 心跳、磁盘剩余和版本。

### 备份

```bash
sqlite3 /var/lib/newsradar/newsradar.db \
  ".backup '/var/backups/newsradar/newsradar-$(date +%F).db'"
```

建议：

- 每日备份数据库，保留 14 份日备份 + 4 份周备份
- `master.key` 单独备份；丢失后已加密的 API Key 无法恢复
- 恢复后先跑 `alembic upgrade head`，再启动服务

### 日志

```bash
journalctl -u newsradar-web -f
journalctl -u newsradar-worker -f
```

日志中不会记录 API Key、Authorization、完整请求 URL 或新闻全文。

## 安全

- 管理后台只监听本机，由反向代理对外
- 管理员密码使用 Argon2id
- API Key、推送 token 使用 Fernet 加密后存 SQLite
- 所有状态变更 POST 校验 CSRF
- 新闻内容按不可信数据处理，模板自动转义
- RSS/Webhook 默认拒绝内网和回环地址，防止 SSRF
- 不要把 `master.key`、`newsradar.env`、数据库或备份提交到仓库

## 硬件

已在 Orange Pi 3B（RK3566 / 2GB / Armbian Debian 13）验证。

低成本选型建议：

- 验证板：Orange Pi Zero 3 1GB，约 120–150 元
- 产品化：Radxa Zero 3E 2GB，约 200–260 元

详细对比、BOM 和风险见 [docs/hardware-selection.md](docs/hardware-selection.md)。

## 开发

```bash
# 测试
pytest -q

# 静态检查
ruff check app tests

# 新建迁移
alembic revision --autogenerate -m "describe change"
alembic upgrade head
```

测试使用临时 SQLite，不会调用真实付费 API。

## 许可证

MIT。详见 [LICENSE](LICENSE)。

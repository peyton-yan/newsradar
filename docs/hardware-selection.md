# 低成本硬件选型（2026-10）

目标：24/7 运行 NewsRadar 的 Python 服务栈（FastAPI + Uvicorn 单 worker、SQLite WAL、APScheduler、httpx、feedparser、BeautifulSoup）。模型推理在云端，板子只做网络 I/O、轻量 SQLite 和定时调度。

## 结论

| 用途 | 推荐 | 参考价（CNY） | 理由 |
|---|---|---:|---|
| 绝对最低成本验证 | Orange Pi Zero 2W 1GB | 110–140 | 最便宜、能跑；只有 WiFi，没有网口 |
| 最低成本且“合理” | **Orange Pi Zero 3 1GB** | 120–150 | 多约 15 元换千兆网口，24/7 网络服务更合适 |
| 产品化优先 | **Radxa Zero 3E 2GB** | 200–260 | Armbian Standard、千兆网口、可选 eMMC，和现有 3B 同属 RK3566 |
| 不推荐 | Raspberry Pi Zero 2 W | 160–200 | 只有 512MB，内存不够 |

价格是 2026-10 渠道参考，受内存行情影响 ±25%，下单前以官方店为准。

## 运行内存评估

1GB 可用，512MB 不够：

| 组件 | 常驻内存 |
|---|---:|
| OS + systemd + journald | 70–110 MB |
| Caddy | 20–30 MB |
| web（FastAPI + SQLAlchemy） | 130–190 MB |
| worker（APScheduler + HTTP + 解析） | 100–150 MB |
| SQLite cache | 16 MB |
| 峰值 | 约 340–500 MB |

建议 1GB 起步；产品化用 2GB，给备份、VACUUM 和新闻突发留余量。启用 256MB zram、限制 journald 到 200MB、避免频繁全量 VACUUM。

## BOM（可售整机，CNY）

| 项目 | Zero 2W 1GB | Zero 3 1GB | Radxa 3E 2GB |
|---|---:|---:|---:|
| 主板 | 125 | 135 | 230 |
| 32GB 高耐久 SD | 35 | 35 | 35 |
| 5V/3A 电源 | 18 | 18 | 20 |
| 外壳 | 10 | 10 | 15 |
| 散热片 | 4 | 4 | 6 |
| USB-C 线 | 6 | 6 | 6 |
| 螺丝/包装/标签 | 8 | 8 | 10 |
| 物料合计 | 206 | 216 | 322 |
| 组装/烧录/测试 | 20 | 20 | 25 |
| 返修 3% | 6 | 6 | 10 |
| **落地成本** | **约 232** | **约 242** | **约 357** |

## 产品化风险

- **SD 卡损坏是头号现场故障**：用高耐久/pSLC 卡、只读 root + overlayfs、log2ram、noatime、SQLite WAL + 每日 `.backup`；或者用 eMMC/USB SSD。
- **电源**：5V/3A、CCC 认证；劣质充电器会导致欠压、重启和 SD 损坏。
- **散热**：本工作负载以网络等待为主，密封壳内约 45–55°C；加 4 元散热片，避免完全密封的金属盒。
- **WiFi 天线**：金属外壳会屏蔽板载天线；产品优先有线。
- **批次差异**：Radxa Zero 3W 有 2026 年批次 WiFi 模块/DTB 变化导致官方镜像启动失败的记录；Zero 3E 没有 WiFi 模块，变量更少。每批验证并固定 u-boot/DTB。
- **命名陷阱**：Radxa Zero 3W = RK3566；Orange Pi Zero 3W = A733（带 NPU）。本项目的推理在云端，不需要 NPU。

## 来源

- [Orange Pi Zero 2W](http://www.orangepi.cn/html/hardWare/computerAndMicrocontrollers/details/Orange-Pi-Zero-2W.html)
- [Orange Pi Zero 3](http://www.orangepi.cn/html/hardWare/computerAndMicrocontrollers/details/Orange-Pi-Zero-3.html)
- [Armbian Zero 2W](https://armbian.com/boards/orangepizero2w)
- [Armbian Zero 3](https://armbian.com/boards/orangepizero3)
- [Armbian Radxa Zero 3](https://armbian.com/boards/radxa-zero3)
- [Radxa Zero 3E](https://radxa.com/products/zeros/zero3e/)
- [CNX: 2024 vs 2026 SBC prices](https://www.cnx-software.com/2026/04/28/what-a-difference-two-years-make-comparing-sbc-prices-in-2024-and-2026/)

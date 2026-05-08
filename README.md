# 京汉通勤价格监控

每周通勤北京-武汉的私人价格监控系统。每天定时抓取去哪儿机票 + 12306 高铁余票，触发规则后推送到微信和邮件。

## 系统架构

```
┌──────────────────────┐    ┌─────────────┐    ┌──────────────┐
│  GitHub Actions      │───▶│  抓取脚本   │───▶│  SQLite      │
│  (cron 每日3次)      │    │  (Python)   │    │  历史快照库  │
└──────────────────────┘    └─────────────┘    └──────────────┘
                                   │                    │
                                   ▼                    ▼
                            ┌─────────────┐    ┌──────────────┐
                            │  规则引擎   │    │  导出 JSON   │
                            └─────────────┘    └──────────────┘
                                   │                    │
                       ┌───────────┴────┐               │
                       ▼                ▼               ▼
                 ┌──────────┐    ┌──────────┐    ┌──────────────┐
                 │ 微信推送 │    │ 邮件推送 │    │ GitHub Pages │
                 └──────────┘    └──────────┘    │ 网页面板     │
                                                 └──────────────┘
```

## 快速部署（10 分钟）

### 第 1 步：创建 GitHub 仓库

1. 在 GitHub 新建一个仓库（建议私有仓库 private）
2. 把整个 `jh-commute-monitor` 目录上传上去：

```bash
cd jh-commute-monitor
git init
git add .
git commit -m "init"
git remote add origin git@github.com:YOUR_USERNAME/jh-commute-monitor.git
git push -u origin main
```

### 第 2 步：申请微信推送 Server酱

1. 访问 [sct.ftqq.com](https://sct.ftqq.com)，用 GitHub 账号登录
2. 扫码绑定微信
3. 复制控制台显示的 SendKey（形如 `SCTxxxxxxxx`）

### 第 3 步：配置邮箱（可选）

如果要邮件推送，准备一个 SMTP 邮箱（QQ/163/Gmail 都行）。
QQ 邮箱举例：
- HOST: `smtp.qq.com`
- PORT: `465`
- USER: 你的邮箱
- PASSWORD: **授权码**（不是登录密码，需要在邮箱设置里生成）

### 第 4 步：填写 GitHub Secrets

在仓库页面：`Settings` → `Secrets and variables` → `Actions` → `New repository secret`

依次添加（没用到的渠道可以不加）：

| Secret 名称 | 值 |
|---|---|
| `SERVERCHAN_KEY` | 第 2 步拿到的 SendKey |
| `EMAIL_HOST` | `smtp.qq.com` |
| `EMAIL_PORT` | `465` |
| `EMAIL_USER` | 发件邮箱 |
| `EMAIL_PASSWORD` | 邮箱授权码 |
| `EMAIL_TO` | 接收邮件的邮箱 |

### 第 5 步：开启 GitHub Pages

`Settings` → `Pages` → `Build and deployment`：
- Source 选 `GitHub Actions`

之后第一次抓取完成后，访问 `https://YOUR_USERNAME.github.io/jh-commute-monitor/` 就能看到面板。

### 第 6 步：手动触发一次

`Actions` 标签页 → `京汉通勤价格抓取` → 点 `Run workflow`

第一次跑完后看日志，确认抓到数据。之后每天自动跑 3 次（北京时间 8/13/20 点）。

## 个人定制

### 修改监控参数

编辑 `config.yaml`，常用调整：

```yaml
# 加大监控范围
monitor_weeks_ahead: 6   # 改成监控未来 6 周

# 调整提醒阈值
alert_rules:
  - name: "周五机票特价"
    threshold: 700        # 比如降到 700 才提醒
```

改完 push 一下，下次运行就生效。

### 关闭某个数据源

如果哪个源经常被风控，先在 `config.yaml` 里关掉：

```yaml
sources:
  qunar: false   # 暂时停用去哪儿
  railway: true
```

## 本地调试

```bash
# 装依赖
pip install -r requirements.txt

# 设置环境变量（仅推送需要）
export SERVERCHAN_KEY=SCTxxxxxxxx

# 跑一次
python -m src.main

# 检查输出
sqlite3 data/prices.db "SELECT count(*) FROM flight_snapshots;"
cat docs/data.json | head -50
```

## 抓取失败时怎么办

抓取脚本有三层防御，但任何源头都可能挂掉。**预期**：

- **去哪儿**：接口偶尔会变字段名，或者返回风控页。优先看 `Actions` 日志中的「响应结构」字样，对照实际返回 JSON 改 `scraper_qunar.py` 的 `_parse_response()` 字段映射
- **12306**：海外 IP（GitHub Actions）有时会被拒。如果 7 天内连续失败，迁移到国内云服务器或加代理
- **整体降级**：把 `sources.qunar` 改 false，只用 12306；高铁价格固定，光看余票也能撑一段时间

## 演进路线

按"先用着，慢慢补"的节奏：

1. **第 1 周**：先按本指南跑起来，看真实抓取效果
2. **第 2-3 周**：根据真实返回数据，微调 `_parse_response()` 字段映射
3. **第 1 个月**：积累出 30 天历史数据，前端走势图开始有意义
4. **长期**：如果抓取不稳定，申请 Skyscanner/Amadeus 免费 API 替换 `scraper_qunar.py`，其他模块完全不动

## 项目结构

```
jh-commute-monitor/
├── config.yaml                      ← 你的个人配置
├── requirements.txt
├── README.md
├── src/
│   ├── main.py                      ← 主入口
│   ├── scraper_qunar.py             ← 去哪儿机票
│   ├── scraper_railway.py           ← 12306 高铁
│   ├── storage.py                   ← SQLite 持久化
│   ├── alert_engine.py              ← 规则引擎
│   └── notifier.py                  ← 推送（微信/邮件）
├── data/
│   └── prices.db                    ← 自动生成，价格历史
├── docs/
│   ├── index.html                   ← 前端面板
│   └── data.json                    ← 自动生成，前端用
└── .github/workflows/
    ├── scrape.yml                   ← 定时抓取
    └── pages.yml                    ← 自动发布前端
```

## 法律 & 道德提示

- 抓取频率已设为每天 3 次，对源站负担极低，是合理范围
- 仅用于个人通勤价格监控，不要二次分发数据
- 如果某个站点明确通过 robots.txt 或服务条款禁止，请尊重并停用对应 source

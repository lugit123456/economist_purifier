---
name: economist-purifier
description: |
  《经济学人》EPUB 周刊双语智库:从本地 .epub 解包 → NCX 提取真实章节 → LLM 编译信达雅中文研报 →
  静态前端展示(红白经典风格 + 暗黑模式 + 全文搜索)。

  触发场景:用户投放新一期的 .epub 想要自动入库;用户想验证某期杂志文章总数是否符合预期;
  用户想调整编译策略(并发/超时/降级);用户想自定义板块黑名单或前端主题。
---

# Economist Purifier Skill

把《经济学人》.epub 一键变成离线双语研报库:解析 → 并发编译 → 落盘 → 静态前端展示。

## 何时使用

- 想把某期或某几期《经济学人》周刊自动入库为本地双语知识库
- 想验证 .epub 实际含多少篇文章,确认解析覆盖率
- Politics/Business 板块不想展开深度研报,只想翻译标题+保留原文
- 想让 Cartoon / Indicators 板块的图自动下载并可在前端灯箱预览
- 想 PC + 手机都能看,带暗黑模式,支持全文搜索
- 想后台常驻轮询,新一期投放即自动处理

**不要用本 skill 做的事**:
- 抓取不在 EPUB 里的内容(本 skill 只解析本地文件,不联网抓)
- 商业分发编译产物 — 含付费原文,仅供个人学习
- 把 `database.js` 推到公网仓库

## 快速调用

```bash
# 1. 安装
cd economist_purifier
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env:OPENAI_API_KEY + OPENAI_BASE_URL

# 2. 干跑验证(不调 API)
python -m backend.kb_agent --dry-run

# 3. 跑全量(单次)
python -m backend.kb_agent --once

# 4. 常驻轮询(服务器)
python -m backend.kb_agent

# 5. 打开前端(本地调试)
python3 -m http.server 8000
# 访问 http://localhost:8000

# 6. 构建 Netlify 部署包
python3 scripts/build_site.py
# 输出在 ./site/ 目录,纯静态文件,可直接拖到 Netlify Drop
```

完整文档见 [README.md](./README.md)。
## 输入 / 输出

| | |
|---|---|
| **输入** | `.env`(LLM 凭据 + 路径 + 并发 + 轮询周期);`raw/imports/*.epub`(用户投放) |
| **输出** | `frontend/database.js`(自动覆写,前端直接读);`output/{issue_date}/{标题}_{art_id}.md`(每篇研报);`raw/images/{issue_date}/`(封面 + 漫画 + 指标图);`raw/imports/archived/*.epub`(处理完归档) |
| **运行时长** | 单期 50-75 篇 × 8 路并发 ≈ 5-15 分钟(含 LLM 调用) |

## 三种编译策略

| 类别 | 板块 | 行为 |
|------|------|------|
| `analysis` | Leaders / Briefing / Asia / China / Europe 等 | 4 段式深度研报(一句话主旨 / 观点拆解 / 争议挑战 / 未来趋势) |
| `news` | Politics / Business | 忠实中文翻译全文(降级:仅翻译标题) |
| `cartoon` | Cartoon 板块 | 抽取漫画图,前端 banner 展示 |
| `indicators` | Economic & financial indicators | 抽取所有图表 + caption,前端画廊 + 灯箱 |

## 三级降级保护

LLM 调用失败时自动降级,绝不丢数据:

1. **瞬时错误**(timeout/限流) → 重试 3 次
2. **永久错误**(422 内容审核 / JSON 解析失败) → 跳过重试
3. **news 类降级** → 仅翻译标题 + 友好 summary + 原文备份
4. **最终兜底** → 英文标题 + 原文占位

## 关键配置速查(.env)

```bash
OPENAI_API_KEY=sk-...          # 必填
OPENAI_BASE_URL=https://...     # 兼容 OpenAI 协议即可
OPENAI_MODEL=gpt-4o-mini        # 推荐快便宜
OPENAI_USE_JSON_FORMAT=true      # 端点不支持时改 false
LLM_CONCURRENCY=8               # 并发上限
WATCH_DIR=./raw/imports         # 投放目录
OUTPUT_DIR=./output             # .md 落盘根目录
POLL_INTERVAL=10                # 轮询周期 (秒)
```

## CLI 速查

| 命令 | 行为 |
|------|------|
| `--dry-run` | 解析 + 统计,不调 LLM,不归档 |
| `--once` | 处理完所有 .epub 后退出 |
| (无参数) | 常驻轮询,每 `POLL_INTERVAL` 秒一次 |
| (无参数) + `AUTO_PUBLISH=1` | **投放即上线**: 每期编完自动 `git push` → Netlify 部署 |

### 一键发布 (手动模式)

```bash
python3 scripts/publish.py    # compile + build + commit + push 一条龙
python3 scripts/publish.py --no-compile   # 跳过编译 (kb_agent 已跑过)
python3 scripts/publish.py --no-push      # 只 build + commit (本地调试)
```

## 前端特性

- 📱 响应式:PC 网格 + 平板 + 手机,≤1024px 自动抽屉化目录
- 🌓 暗黑模式:报头 ☀️/🌙 切换,localStorage 记忆
- 🔍 全文搜索:5 字段(标题/板块/研报/正文),带命中位置徽章
- 🖼 漫画 banner + 指标画廊 + 灯箱 ←/→ 翻页
- ✨ 经典经济学人红白配色 + Playfair Display 衬线字体
- ⚡ 流畅动画:view 切换 + 卡片 stagger + hover 上浮

## 局限

- 仅解析用户提供的本地 .epub,不联网抓取
- LLM 输出偶有失败,422 内容审核政治文章常见(已自动降级)
- 部分板块名翻译可能与 The Economist 官方中文版不一致,以原文为准

## 相关项目

- `economist_weekly_archiver_skill` — 联网抓取版(本 skill 的姊妹项目)
- `frontend/database.js` — 由本 skill 自动维护,前端可直接 `python -m http.server` 查看

完整文档:[README.md](./README.md)
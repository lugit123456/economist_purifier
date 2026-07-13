# economist_purifier

> 基于原刊 EPUB 解包与 LLM 深度编译的私有双语智库系统

将《经济学人》周刊 EPUB 解包 → NCX 提取真实章节 → 白名单清洗 → 大模型深度编译为信达雅的中英双语研报 → 经济学人经典风格静态前端展示。支持暗黑模式、PC + 手机响应式、全文搜索、漫画与图表画廊。

---

## ✨ 核心特性

| 特性 | 说明 |
|------|------|
| 📚 **NCX 真源解析** | 直接读 EPUB 的 NCX/toc.xhtml,真实板块名 (`Leaders` / `Politics` / `Finance & economics` / `Asia` ...) |
| ⚡ **asyncio 并发编译** | 默认 8 路并发,单期 ~50-75 篇 ~5-15 分钟编译完 |
| 🛡 **三级降级策略** | 422 内容审核 / 超时 → 自动降级到标题翻译,不丢数据 |
| 🎨 **漫画自动提取** | 识别 Cartoon 板块,自动下载图并在 HTML 中展示 |
| 📊 **图表画廊** | Economic & financial indicators 板块,自动提取所有图,带灯箱预览 |
| 🌓 **暗黑模式** | 跟随系统偏好,localStorage 记忆,一键切换 |
| 📱 **手机端适配** | 抽屉式目录 FAB + 响应式 grid + 触摸优化 |
| 🔍 **全文搜索** | 标题/板块/研报/正文 5 字段搜索,带命中位置徽章 |
| 🧪 **三种运行模式** | `--dry-run` 数数量 / `--once` 单次跑 / 常驻轮询 |
| 📂 **本地落盘** | 每篇研报输出为 .md 文件,带 metadata + 原文 |

---

## 🗂 项目结构

```
economist_purifier/
├── .env.example                  # 环境变量模板
├── .gitignore
├── requirements.txt
├── README.md
├── SKILL.md                      # Skill 入口(本文档的精简版)
├── backend/
│   ├── __init__.py
│   ├── parser.py                 # EPUB 解包 + NCX 章节提取 + 漫画/图表识别 ⭐
│   ├── compiler.py               # asyncio + AsyncOpenAI 并发编译引擎 ⭐
│   └── kb_agent.py               # 常驻 Daemon + .env 加载 + 调度 ⭐
├── raw/
│   ├── imports/                  # 📥 投入 .epub,处理后自动归档到 archived/
│   │   └── archived/
│   └── images/                   # 📸 封面图 + 漫画图 + 指标图表
├── output/                       # 📝 .md 研报落盘根目录 (OUTPUT_DIR)
│   └── 2026-07-11/
│       └── 标题_art_2026-07-11_001.md
├── frontend/
│   ├── index.html                # 报头 + 封面墙 + 二级下钻 ⭐
│   ├── database.js               # kb_agent 自动生成
│   └── assets/
│       ├── style.css             # 1313 行,暗黑模式 + 响应式
│       └── app.js                # 路由 + 渲染 + 搜索 + 抽屉
└── _regression_test*.py          # 端到端回归测试
```

---

## 🚀 快速启动

### 1. 装依赖

```bash
cd economist_purifier
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配 .env

```bash
cp .env.example .env
# 编辑 .env,填入 OPENAI_API_KEY 与 OPENAI_BASE_URL
```

### 3. 验证数量(干跑)

```bash
python -m backend.kb_agent --dry-run
```

输出形如:
```
📊 TheEconomist.2026.07.11.epub: 75 篇
   板块分布: [('Leaders', 8), ('Politics', 6), ('Finance & economics', 7), ...]
   分类: {'analysis': 54, 'news': 39}
```

数量对得上再跑全量。

### 4. 跑全量

```bash
# 单次:处理完当前所有 .epub 后退出
python -m backend.kb_agent --once

# 常驻:监听 raw/imports/,新文件自动处理
python -m backend.kb_agent
```

### 5. 打开前端

```bash
cd frontend && python -m http.server 8000
# 访问 http://localhost:8000
```

---

## ⚙️ 配置项(.env 完整清单)

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `OPENAI_API_KEY` | API 密钥 (必填) | — |
| `OPENAI_BASE_URL` | 兼容 OpenAI 协议的自定义端点 | `https://api.openai.com/v1` |
| `OPENAI_MODEL` | 模型名 | `gpt-4o-mini` |
| `OPENAI_USE_JSON_FORMAT` | 端点不支持 `response_format=json_object` 时设为 `false` | `true` |
| `LLM_CONCURRENCY` | 单期文章并发上限 | `8` |
| `WATCH_DIR` | 监听 .epub 投放目录 | `./raw/imports` |
| `OUTPUT_DIR` | .md 研报落盘根目录 | `./output` |
| `DB_FILE` | 前端 database.js 路径 | `./frontend/database.js` |
| `IMAGE_DIR` | 封面/漫画/图表图持久化目录 | `./raw/images` |
| `POLL_INTERVAL` | 守护进程轮询周期 (秒) | `10` |

---

## 🧠 架构与流水线

```
┌──────────────────┐
│  raw/imports/    │
│  *.epub (用户投放) │
└────────┬─────────┘
         │ (1) parser.extract_and_parse_epub
         ↓
┌──────────────────────────────────────────────────┐
│ parser.py                                         │
│  • zipfile 内存解包                                │
│  • NCX/toc.xhtml → {file_path: section_name} 映射   │
│  • 跳过:Leaders 索引页/The world this week/标题     │
│         == 板块名/短文 < 300 字等                  │
│  • 识别:Cartoon → 提取漫画图                       │
│  • 识别:Indicators → 提取所有图表图                │
│  • 分类:Politics/Business → news(快讯)             │
│          其他板块 → analysis(深度研报)             │
└────────┬─────────────────────────────────────────┘
         │ 标准化 issue_data
         ↓
┌──────────────────────────────────────────────────┐
│ compiler.py                                       │
│  • asyncio.Semaphore(8) 并发                      │
│  • news 类 (Politics/Business):                    │
│      → NEWS_TRANSLATION_PROMPT 忠实中文翻译         │
│      → 失败自动降级到 _translate_title_only          │
│  • analysis 类 (其他板块):                         │
│      → SYSTEM_PROMPT 4 段式深度研报                │
│  • 三级错误处理:                                    │
│      1) 瞬时错误(timeout/限流) → 重试 3 次          │
│      2) 永久错误(422/JSON) → 跳过重试直接降级        │
│      3) 兜底:英文原文 + 失败提示                    │
└────────┬─────────────────────────────────────────┘
         │ 编译后的 articles
         ↓
   ┌─────┴──────┐
   ↓            ↓
   .md 落盘    database.js 回写
   OUTPUT_DIR  DB_FILE
```

---

## 📐 数据模型 (Schema)

`frontend/database.js` 的 `window.economist_db` 严格遵循:

```ts
type Issue = {
  issue_date: string;              // "YYYY-MM-DD"
  issue_id: string;                // "issue_2026-07-11"
  issue_cover: string;             // 项目根相对路径
  articles: Article[];
};

type Article = {
  id: string;                      // "art_2026-07-11_001"
  section: string;                 // NCX 提取的真实板块
  category: "news" | "analysis";   // 编译策略
  title: string;                   // 英文原标题
  title_zh: string;                // LLM 翻译的中文标题
  url: string;                     // 官方原文链接
  summary_md: string;              // Markdown 研报 / 板块快讯 / 忠实译文
  content_raw: string;             // 清洗后的英文原文 HTML

  // 可选字段
  cartoon_images?: string[];       // 仅 Cartoon 板块,图片项目根相对路径
  indicator_images?: Array<{       // 仅 Indicators 板块
    path: string;
    caption: string;
  }>;
  compile_status?: string;         // ok / news_fallback / failed
};
```

---

## 📝 三种编译产出

### analysis 类(深度研报) — 4 段式

```markdown
### 🌟 一句话核心主旨
(1-2 句话精准提炼,≥ 300 字)

### 🔍 核心观点与论据拆解
(分点 3-5 个,每点 50-80 字,含数据/人物/年份)

### 🤨 争议与潜在挑战
(若原文未涉及,写"原文未涉及明显争议")

### 🔮 未来趋势预判
(基于事实延伸,不可编造新数据)
```

### news 类(Politics/Business 忠实翻译)

```markdown
### 🌍 Politics · 忠实中文翻译
> 下方为英文原文的中文翻译 (按英文语义直译, 不做解读)。

---
[LLM 翻译的中文全文]
```

### news 类降级后(LLM 全文翻译失败时)

```markdown
### 🌍 Politics · 板块快讯
> ⚠️ LLM 全文翻译暂不可用 (内容审核拦截或超时), 仅完成标题翻译。

---
[英文原文备份]
```

---

## 🎨 前端特性

| 特性 | 实现 |
|------|------|
| 配色 | The Economist 红 `#e3120b` + 暖白 `#fbf8f3`,暗黑模式 `#0f0f0f` |
| 字体 | Playfair Display(标题)· Source Serif 4(正文)· Inter(UI) |
| 封面墙 | `auto-fill` 响应式 grid,卡片悬浮上浮,日期搜索 |
| 二级下钻 | fade + translateY 切换动画,左右双栏 Markdown / HTML 对照 |
| 漫画专栏 | 顶部红色横幅展示图 |
| 指标图表 | 顶部 banner + grid gallery,点击进灯箱全屏预览,←/→ 翻页 |
| 搜索 | 封面墙按日期搜,目录搜标题+研报+正文,带 `✦ 研报+正文` 徽章 |
| 响应式 | ≤1024px:目录变抽屉 FAB;≤640px:单列布局,字体缩 25%;≤400px:封面墙强制 1 列 |
| 暗黑模式 | 报头 ☀️/🌙 按钮,localStorage 记忆,系统偏好跟随 |
| 动画 | view 480ms cubic-bezier,卡片 stagger fade-in,hover 6px 上浮 |
| 打印 | `@media print` 隐藏导航/灯箱,可打印正文 |

---

## 🚢 部署方案

### 方式 1: 后台跑(macOS launchd — 推荐)

`~/Library/LaunchAgents/com.economist.kb_agent.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.economist.kb_agent</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/luzhe/.pyenv/versions/3.9.18/bin/python3</string>
        <string>-m</string>
        <string>backend.kb_agent</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/luzhe/Desktop/code/agent_skills/economist_purifier</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/tmp/kb_agent.out.log</string>
    <key>StandardErrorPath</key><string>/tmp/kb_agent.err.log</string>
</dict>
</plist>
```

```bash
launchctl load -w ~/Library/LaunchAgents/com.economist.kb_agent.plist
launchctl list | grep kb_agent   # 查状态
launchctl unload ~/Library/LaunchAgents/com.economist.kb_agent.plist  # 停止
```

### 方式 2: nohup + & (最简单)

```bash
nohup python3 -m backend.kb_agent > kb_agent.log 2>&1 &
echo $! > kb_agent.pid
tail -f kb_agent.log
kill $(cat kb_agent.pid)   # 停止
```

### 方式 3: tmux (开发期)

```bash
tmux new -s kb_agent
python3 -m backend.kb_agent
# Ctrl+B 然后 D 脱离
tmux attach -t kb_agent   # 重连
```

### 前端部署

```bash
cd frontend && python3 -m http.server 8000
# 或部署到 Nginx / GitHub Pages / Netlify (database.js 自动随 backend 更新)
```

---

## 🧪 三种运行模式

| 命令 | 行为 | 适用场景 |
|------|------|----------|
| `--dry-run` | 解析 + 统计,不调 LLM,不归档 | 数量验证,API 配额保护 |
| `--once` | 处理完所有 .epub 后退出 | CI / 一次性补抓 |
| (无参数) | 常驻轮询,每 `POLL_INTERVAL` 秒扫一次 | 服务器/开发监听 |

---

## 🔧 进阶定制

| 想改什么 | 在哪里改 |
|---------|---------|
| LLM 并发数 | `.env` 的 `LLM_CONCURRENCY` |
| 轮询周期 | `.env` 的 `POLL_INTERVAL` |
| 模型 | `.env` 的 `OPENAI_MODEL` |
| 端点 | `.env` 的 `OPENAI_BASE_URL` |
| 4 段式 prompt | `backend/compiler.py` 的 `SYSTEM_PROMPT` |
| 快讯 prompt | `backend/compiler.py` 的 `NEWS_TRANSLATION_PROMPT` |
| 落盘路径 | `.env` 的 `OUTPUT_DIR` |
| 跳过规则(板块黑名单) | `backend/parser.py` 的 `_SKIP_TITLE_PATTERNS` / `_SKIP_SECTION_NAMES` |
| 快讯板块分类 | `backend/parser.py` 的 `_NEWS_CATEGORIES` |
| 漫画识别 | `backend/parser.py` 的 `_CARTOON_KEYWORDS` |
| 指标识别 | `backend/parser.py` 的 `_INDICATOR_SECTION_NAMES` |
| 前端主题色 | `frontend/assets/style.css` 的 CSS 变量 |

---

## 🧪 回归测试

```bash
python3 _regression_test5.py   # 跳过规则 + dry-run + 快讯渲染
python3 _regression_test6.py   # Asia/China/Europe 不被误判为 news
python3 _regression_test7.py   # 422/超时降级路径
python3 _regression_test8.py   # indicators 板块识别 + 图片提取
```

总计覆盖 ~30 个测试用例,包括:
- ✅ NCX 真源解析,目录列示数 == 解析数
- ✅ Leaders/The world this week/Contents 等索引页被跳过
- ✅ Asia/China/Europe 走 analysis(4 段式),Politics/Business 走 news(忠实翻译)
- ✅ Cartoon 板块自动提取图片,Indicators 板块提取所有图表 + caption
- ✅ 422 内容审核 / 超时 / JSON 错误自动降级,不丢数据
- ✅ 短文(< 300 字符)被跳过

---

## 🐛 常见问题

**Q: 数量对不上,目录列示 75 但只解析 50?**

A: 检查 `_SKIP_TITLE_PATTERNS` 和 `_SKIP_SECTION_NAMES`,看是否误杀了你想保留的板块。Dry-run 日志会打印跳过的原因和样例。

**Q: LLM 返回非 JSON / 422 内容审核?**

A: 已在 compiler.py 实现三级降级:瞬时错误重试、永久错误不重试直接降级、news 文章降级到标题翻译。Politics/Business 全文翻译失败时仍能保留标题和原文。

**Q: OPENAI_USE_JSON_FORMAT 应该设什么?**

A: 若端点支持 `response_format={"type":"json_object"}` 设为 `true`(默认)。不支持的(如部分中转/Azure)设为 `false`,prompt 会强约束输出 JSON,防御性解析器仍能提取。

**Q: 怎么完全重跑某一期?**

A: 把归档的 .epub 移回 `raw/imports/`,运行 `--once`。`bake_into_local_database` 会按 `issue_id` 去重覆盖。

---

## 📜 许可与免责

- 仅供个人学习使用,抓取的内容版权归《经济学人》所有
- 禁止商业分发或公网公开 `database.js`(含付费原文)
- 生成的 .md 研报由 LLM 编译,可能存在错误,引用前请核对原文

---

**作者**: luzhe | **最后更新**: 2026-07-13
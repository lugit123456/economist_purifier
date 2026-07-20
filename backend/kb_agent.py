"""
economist_purifier 守护进程主入口

职责:
1. 加载 .env 配置
2. 监听 raw/imports/ 目录,捕获新 .epub 文件
3. 调度 parser 拆解 → compiler 并发编译 → md 落盘 → database.js 回写
4. 支持两种运行模式:
   - 常驻守护进程 (默认): time.sleep 轮询
   - 一次性处理: --once 参数,处理完即退出
"""

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# 支持直接运行 (python -m backend.kb_agent) 和包导入
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from backend.parser import extract_and_parse_epub
    from backend.compiler import EconomistCompiler
    from backend.state_db import StateDB, compute_sha256, infer_issue_id_from_filename
else:
    from .parser import extract_and_parse_epub
    from .compiler import EconomistCompiler
    from .state_db import StateDB, compute_sha256, infer_issue_id_from_filename


# ---------- 配置加载 ----------

import os


class Config:
    """从环境变量统一加载配置"""

    def __init__(self):
        load_dotenv()

        # LLM
        self.openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
        self.openai_base_url: str = os.getenv(
            "OPENAI_BASE_URL", "https://api.openai.com/v1"
        )
        self.openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.llm_concurrency: int = int(os.getenv("LLM_CONCURRENCY", "8"))
        self.use_json_format: bool = os.getenv("OPENAI_USE_JSON_FORMAT", "true").lower() in (
            "true", "1", "yes"
        )

        # 图表图片视觉解析开关
        # auto  (推荐): 多模态优先,API 拒绝 image_url 时自动降级纯文本
        # true        : 强制多模态,失败即报错
        # false       : 关闭视觉,只用 caption + alt + 上下文
        self.vision_enabled: str = os.getenv("LLM_VISION_ENABLED", "auto").strip().lower()
        # 多模态调用专用模型,空则复用 OPENAI_MODEL
        self.vision_model: str = os.getenv("LLM_VISION_MODEL", "").strip()
        # 图片在 base64 前的最长边 (像素),控制 token
        try:
            self.image_max_edge: int = int(os.getenv("LLM_IMAGE_MAX_EDGE", "1024"))
        except ValueError:
            self.image_max_edge = 1024

        # 路径
        self.base = Path(__file__).resolve().parent.parent
        self.watch_dir = Path(os.getenv("WATCH_DIR", "./raw/imports")).resolve()
        # ★ 兼容旧版: archived/ 不再自动写入,但保留路径供迁移使用
        self.archive_dir = self.watch_dir / "archived"
        self.image_dir = Path(os.getenv("IMAGE_DIR", "./raw/images")).resolve()
        # ★ 图片目录的项目根相对路径,用于写入 database.js 时保持 IMAGE_DIR 一致
        #   IMAGE_DIR=./frontend/images  → "frontend/images"
        #   IMAGE_DIR=./raw/images       → "raw/images" (向后兼容)
        try:
            self.image_dir_rel = self.image_dir.relative_to(self.base).as_posix()
        except ValueError:
            # IMAGE_DIR 不在项目根下(罕见),退化为绝对路径字符串
            self.image_dir_rel = str(self.image_dir)
        self.output_dir = Path(os.getenv("OUTPUT_DIR", "./output")).resolve()
        self.db_file = Path(os.getenv("DB_FILE", "./frontend/database.js")).resolve()
        # ★ 处理记录库 (SQLite),记录已处理 EPUB 的 sha256 + 元数据
        #   默认与 database.js 同目录,Netlify 部署时一并带走
        self.state_db_path = Path(
            os.getenv("STATE_DB_FILE", "./frontend/state.db")
        ).resolve()

        # 调度
        self.poll_interval: int = int(os.getenv("POLL_INTERVAL", "10"))

        # 校验
        self._validate()

        # 确保所有目录存在
        for d in (self.watch_dir, self.image_dir, self.output_dir, self.db_file.parent):
            d.mkdir(parents=True, exist_ok=True)
        # archived/ 仅作历史兼容,不再自动写入;不强制创建
        self.state_db = StateDB(self.state_db_path)

    def _validate(self):
        if not self.openai_api_key:
            print("⚠️  OPENAI_API_KEY 未配置,LLM 编译将无法启动")
        if self.llm_concurrency < 1:
            raise ValueError("LLM_CONCURRENCY 必须 >= 1")

    def summary(self) -> str:
        return (
            f"📋 配置:\n"
            f"  - 端点: {self.openai_base_url}\n"
            f"  - 模型: {self.openai_model}\n"
            f"  - 并发: {self.llm_concurrency}\n"
            f"  - 监听: {self.watch_dir}\n"
            f"  - 图片目录 (绝对): {self.image_dir}\n"
            f"  - 图片目录 (相对): {self.image_dir_rel}\n"
            f"  - 输出: {self.output_dir}\n"
            f"  - 数据库: {self.db_file}\n"
            f"  - 处理记录: {self.state_db_path} (已记录 {self.state_db.count()} 个)"
        )


# ---------- database.js 回写 ----------

# 块级 HTML 标签 + 图表占位符,用于把 content_raw 拆成段落
# [[CHART_N]] 由 parser._extract_content_with_images 注入, 保留图片原顺序
_BLOCK_TAG_RE = re.compile(
    r'(<(?:p|h[1-6])(?:\s[^>]*)?>[\s\S]*?</(?:p|h[1-6])>)|(\[\[CHART_\d+\]\])',
    re.IGNORECASE,
)


def extract_paragraphs_from_html(content_raw: str, article_id: str,
                                   chart_images: Optional[list] = None) -> list:
    """从 content_raw HTML 拆出段落数组(双语对照结构)

    返回:
        普通段:  {"para_id": "art_X_p3", "en_html": "<p>...</p>", "zh_text": "", "is_chart": False}
        图表段:  {"para_id": "art_X_p5", "en_html": "<figure class=\"chart-figure\">...</figure>",
                  "zh_text": "", "is_chart": True, "chart_id": "[[CHART_1]]"}

    切段规则:
    - 普通段: <p>/<h1-6> 块 (与旧版一致)
    - 图表段: content_raw 中的 [[CHART_N]] 占位符(由 parser._extract_content_with_images 注入),
              按原文顺序混合切分, 保证图片出现在原位置
    - chart_images: [{placeholder_id, path, caption, alt}, ...], 提供占位符对应的图片元数据
    """
    if not content_raw:
        return []

    # 构建 placeholder_id -> 图片信息 映射
    placeholder_map: dict = {}
    if chart_images:
        for ci in chart_images:
            pid = ci.get("placeholder_id") if isinstance(ci, dict) else None
            if pid:
                placeholder_map[pid] = ci

    matches = _BLOCK_TAG_RE.findall(content_raw)
    paragraphs = []
    idx = 0
    for block_match, chart_match in matches:
        if chart_match:
            # 图表占位符段 (独立出现的 [[CHART_N]],不在任何 <p> 内)
            paragraphs.append(_build_chart_paragraph(
                article_id, idx, chart_match, placeholder_map, html_escape,
            ))
        elif block_match and _CHART_PLACEHOLDER_RE.search(block_match):
            # <p>...[[CHART_N]]...</p> 中嵌入了 placeholder, 切分为多段
            for split_para in _split_block_by_chart(
                article_id, idx, block_match, placeholder_map, html_escape,
            ):
                paragraphs.append(split_para)
                idx += 1
            continue  # idx 已在 split_block_by_chart 内递增
        elif block_match:
            paragraphs.append({
                "para_id": f"{article_id}_p{idx + 1}",
                "en_html": block_match.strip(),
                "zh_text": "",
                "is_chart": False,
            })
        idx += 1
    return paragraphs


# 匹配 [[CHART_N]] 占位符, 用于在 <p>...</p> 内拆分
_CHART_PLACEHOLDER_RE = re.compile(r"\[\[CHART_\d+\]\]")


def _build_chart_paragraph(
    article_id: str, idx: int, placeholder_id: str,
    placeholder_map: dict, html_escape_fn,
):
    """根据 chart_images 元数据构造一个 chart 段 dict"""
    info = placeholder_map.get(placeholder_id, {}) or {}
    img_path = (info.get("path") or "").strip()
    caption = (info.get("caption") or "").strip()
    alt = (info.get("alt") or "").strip()
    safe_alt = html_escape_fn(alt, quote=True)
    safe_caption = html_escape_fn(caption, quote=True)

    figure_parts = [
        f'<figure class="chart-figure" data-chart-id="{placeholder_id}">',
        f'<img src="{html_escape_fn(img_path, quote=True)}" alt="{safe_alt}" loading="lazy" decoding="async">',
    ]
    if safe_caption:
        figure_parts.append(f'<figcaption>{safe_caption}</figcaption>')
    figure_parts.append('</figure>')

    return {
        "para_id": f"{article_id}_p{idx + 1}",
        "en_html": "".join(figure_parts),
        "zh_text": "",
        "is_chart": True,
        "chart_id": placeholder_id,
    }


def _split_block_by_chart(
    article_id: str, idx: int, block_html: str,
    placeholder_map: dict, html_escape_fn,
) -> list:
    """把 <p>...[[CHART_1]]...</p> 切成: 文本段 / chart段 / 文本段 / ...

    - 文本部分: 把占位符位置替换为空格, 简化成 <p>escaped(text)</p>
    - chart 部分: 用 _build_chart_paragraph 构造
    返回: 新的段列表 (idx 已内部递增, 外部不要再 idx+1)
    """
    # 提取块级标签与 inner 内容
    m = re.match(r'<(p|h[1-6])(\s[^>]*)?>([\s\S]*?)</\1>', block_html.strip(), re.IGNORECASE)
    if not m:
        # 兜底: 当作普通段处理
        return [{
            "para_id": f"{article_id}_p{idx + 1}",
            "en_html": block_html.strip(),
            "zh_text": "",
            "is_chart": False,
        }]
    tag_name = m.group(1).lower()
    inner = m.group(3)

    # 找所有占位符位置
    ph_matches = list(_CHART_PLACEHOLDER_RE.finditer(inner))
    if not ph_matches:
        return [{
            "para_id": f"{article_id}_p{idx + 1}",
            "en_html": block_html.strip(),
            "zh_text": "",
            "is_chart": False,
        }]

    cursor = 0
    out = []
    local_idx = idx
    for ph in ph_matches:
        # 占位符前的 inner 文本
        text_before = inner[cursor:ph.start()].strip()
        if text_before:
            safe = html_escape_fn(text_before, quote=False)
            out.append({
                "para_id": f"{article_id}_p{local_idx + 1}",
                "en_html": f"<{tag_name}>{safe}</{tag_name}>",
                "zh_text": "",
                "is_chart": False,
            })
            local_idx += 1
        # 占位符段
        out.append(_build_chart_paragraph(
            article_id, local_idx, ph.group(0), placeholder_map, html_escape_fn,
        ))
        local_idx += 1
        cursor = ph.end()
    text_after = inner[cursor:].strip()
    if text_after:
        safe = html_escape_fn(text_after, quote=False)
        out.append({
            "para_id": f"{article_id}_p{local_idx + 1}",
            "en_html": f"<{tag_name}>{safe}</{tag_name}>",
            "zh_text": "",
            "is_chart": False,
        })
        local_idx += 1
    return out


def html_escape(s: str, quote: bool = True) -> str:
    """HTML 字符转义, 用于 caption / alt / path 等可能含特殊字符的字段"""
    if not s:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;" if quote else '"')
        .replace("'", "&#39;" if quote else "'")
    )


def ensure_paragraphs(article: dict) -> dict:
    """确保 article 拥有 paragraphs 字段(中英双栏对照结构)

    规则:
    - 已有非空 paragraphs: 保留(包括可能的 zh_text 翻译),不重新生成
    - 没有 paragraphs 但有 content_raw: 从 content_raw 拆分生成
      同时传入 chart_images, 让 [[CHART_N]] 占位符也切成独立段落
    - 都没有: 不动
    """
    if not isinstance(article, dict):
        return article
    existing = article.get("paragraphs")
    if existing:
        return article
    content_raw = article.get("content_raw", "")
    chart_images = article.get("chart_images") or []
    if content_raw:
        article["paragraphs"] = extract_paragraphs_from_html(
            content_raw, article.get("id", "art_unknown"),
            chart_images=chart_images,
        )
    return article


def bake_into_local_database(db_file: Path, new_issue_data: dict,
                              ensure_paragraphs_flag: bool = True) -> None:
    """回流写入技术: 无缝覆写本地 database.js,确保前端无感感知

    ensure_paragraphs_flag=False 时跳过段落补全 (kb_agent 已在 compile 前补全过)
    """
    existing_data: list = []

    if db_file.exists() and db_file.stat().st_size > 0:
        try:
            content = db_file.read_text(encoding="utf-8").strip()
            # 剥离 window.economist_db = 前缀以获得标准 JSON
            json_str = re.sub(r"^window\.economist_db\s*=\s*", "", content)
            json_str = re.sub(r";?\s*$", "", json_str)
            existing_data = json.loads(json_str)
        except Exception as e:
            print(f"  ⚠️  现有数据库解析异常,将初始化新库: {e}")
            existing_data = []

    # 中英双栏对照结构补全: 每篇文章保证有 paragraphs 字段(已有则保留翻译)
    if ensure_paragraphs_flag:
        for article in new_issue_data.get("articles", []):
            ensure_paragraphs(article)

    # 合并防重
    existing_data = [
        d for d in existing_data
        if d.get("issue_id") != new_issue_data["issue_id"]
    ]
    existing_data.insert(0, new_issue_data)  # 最新期放最前

    db_file.write_text(
        f"window.economist_db = {json.dumps(existing_data, ensure_ascii=False, indent=2)};\n",
        encoding="utf-8",
    )
    print(f"  💾 数据已回流写入 {db_file.name}")


# ---------- 流水线 ----------

async def process_single_epub(epub_path: Path, cfg: Config,
                              compiler: EconomistCompiler,
                              dry_run: bool = False,
                              force: bool = False) -> bool:
    """处理单份 EPUB: 去重检查 → 拆解 → 编译 → 落盘 → 回写 → 入库

    dry_run=True: 仅解析, 打印统计, 不调 LLM, 不落盘, 不入库
    force=True:  忽略 state.db 去重 (用于 --reprocess 或 --force)
    """
    try:
        # Step 0: 内容指纹去重 (用 sha256,改一字节也算新文件)
        sha = compute_sha256(epub_path)
        if not force and cfg.state_db.is_processed(sha):
            rec = cfg.state_db.get_by_sha(sha)
            print(f"  ⏭️  {epub_path.name} 已处理过 (issue={rec.get('issue_id')}, "
                  f"at={rec.get('processed_at'):.0f}), 跳过 [use --force 强制重跑]")
            return True  # 算成功 (没失败),让主循环继续

        # Step 1: EPUB 极速解包 (dry_run 也需要这一步)
        raw_issue_data = extract_and_parse_epub(
            epub_path, image_dir=cfg.image_dir,
            image_dir_rel=cfg.image_dir_rel,
        )

        if dry_run:
            # 只统计不调用 LLM
            from collections import Counter
            cat_counts = Counter(a.get("category", "?") for a in raw_issue_data["articles"])
            print(f"  🧪 [DRY-RUN] {epub_path.name} → {len(raw_issue_data['articles'])} 篇")
            print(f"     category 分布: {dict(cat_counts)}")
            # dry-run 不入库,留待正式跑
            return True

        # Step 1.5: 拆出 paragraphs 块 (供 compiler 逐段翻译, 必须在编译前准备好)
        for article in raw_issue_data.get("articles", []):
            ensure_paragraphs(article)

        # Step 2: LLM 并发编译 (含主编译 + 逐段翻译)
        compiled_issue_data = await compiler.compile_issue(raw_issue_data)

        # Step 3: .md 研报本地落盘
        saved = compiler.save_issue_markdowns(compiled_issue_data)
        if saved:
            print(f"  📝 已落盘 {len(saved)} 篇 .md 研报到 {cfg.output_dir}")

        # Step 4: 回流 database.js (compile_issue 已翻译过 zh_text, 此处不重复 ensure)
        bake_into_local_database(cfg.db_file, compiled_issue_data,
                                  ensure_paragraphs_flag=False)

        # Step 5: 入处理记录库 (成功后入库;失败不污染 DB,下次重试)
        cfg.state_db.mark_processed(
            sha256=sha,
            filename=epub_path.name,
            size=epub_path.stat().st_size,
            issue_id=compiled_issue_data.get("issue_id", "unknown"),
        )
        print(f"  ✅ {epub_path.name} 处理闭环, 已记录到 state.db")
        return True

    except Exception as e:
        print(f"  ❌ {epub_path.name} 流程发生致命中断: {e}")
        import traceback
        traceback.print_exc()
        return False


def check_and_process_jobs(cfg: Config, dry_run: bool = False,
                           force: bool = False) -> int:
    """
    守护进程单次轮询入口

    ⚠️ 关键: 每次调用都 **新建** EconomistCompiler 实例。
    因为 compiler 内部的 Semaphore 和 httpx 连接池会绑定到
    创建时的 event loop,而本函数每次用 asyncio.run() 创建新 loop。
    跨 loop 复用会导致 "attached to a different loop" 错误。

    force=True: 忽略 state.db 去重, 强制重新处理每个 EPUB
    """
    epub_files = sorted(cfg.watch_dir.glob("*.epub"))
    if not epub_files:
        return 0

    if dry_run:
        print(f"  🧪 [DRY-RUN] 仅统计数量, 不调 LLM, 不入库")
        for epub in epub_files:
            try:
                issue = extract_and_parse_epub(epub, image_dir=cfg.image_dir,
                                               image_dir_rel=cfg.image_dir_rel)
                from collections import Counter
                cat_counts = Counter(a.get("category", "?") for a in issue["articles"])
                print(f"  📊 {epub.name}: {len(issue['articles'])} 篇")
                print(f"     板块分布: {Counter(a['section'] for a in issue['articles']).most_common(5)}")
                print(f"     分类: {dict(cat_counts)}")
            except Exception as e:
                print(f"  ❌ {epub.name} 解析失败: {e}")
        return len(epub_files)

    if force:
        print(f"  🔔 守护进程捕获到 {len(epub_files)} 份 EPUB, --force 模式: 忽略去重, 强制处理…")
    else:
        print(f"  🔔 守护进程捕获到 {len(epub_files)} 份 EPUB, 启动流水线…")

    async def process_all():
        compiler = EconomistCompiler(
            api_key=cfg.openai_api_key,
            base_url=cfg.openai_base_url,
            model=cfg.openai_model,
            concurrency=cfg.llm_concurrency,
            output_dir=cfg.output_dir,
            use_json_response_format=cfg.use_json_format,
            vision_enabled=cfg.vision_enabled,
            vision_model=cfg.vision_model or cfg.openai_model,
            image_max_edge=cfg.image_max_edge,
        )
        try:
            success = 0
            for epub in epub_files:
                if await process_single_epub(epub, cfg, compiler, force=force):
                    success += 1
            return success
        finally:
            await compiler.aclose()

    return asyncio.run(process_all())


# ---------- 主入口 ----------

def _auto_migrate(cfg: Config) -> None:
    """首次启动时,从 archived/*.epub 自动迁移历史记录到 state.db

    - 仅在 state.db 为空时执行 (避免重复导入)
    - 用文件 mtime 作为 processed_at,审计更接近历史
    """
    if cfg.state_db.count() > 0:
        return
    if not cfg.archive_dir.exists():
        return
    archived_epubs = list(cfg.archive_dir.glob("*.epub"))
    if not archived_epubs:
        return
    print(f"🔄 检测到 archived/ 里有 {len(archived_epubs)} 份历史 EPUB, 自动迁移到 state.db…")
    n = cfg.state_db.import_from_archived(cfg.archive_dir)
    print(f"   迁移完成: 新增 {n} 条 (已存在的 sha256 已跳过)")


def cmd_status(cfg: Config) -> None:
    """打印 state.db 全部记录 + 当前 WATCH_DIR 待处理文件"""
    print("=" * 72)
    print(f"📊 处理记录库: {cfg.state_db_path}")
    print(f"   共 {cfg.state_db.count()} 条记录")
    print("=" * 72)
    for rec in cfg.state_db.list_all():
        import datetime as _dt
        ts = _dt.datetime.fromtimestamp(rec["processed_at"]).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  ✅ {rec['issue_id']:<22} {rec['filename']:<48} {ts}")
    print("-" * 72)
    pending = sorted(cfg.watch_dir.glob("*.epub"))
    if pending:
        print(f"⏳ 待处理 (WATCH_DIR,共 {len(pending)} 份):")
        for epub in pending:
            # 查 sha256 是否已处理 (避免无谓的 IO)
            sha = compute_sha256(epub)
            if cfg.state_db.is_processed(sha):
                print(f"  ⏭️  {epub.name}  (sha256 已入库, 会被跳过)")
            else:
                print(f"  📥 {epub.name}  (新文件, 会触发编译)")
    else:
        print("⏳ 待处理: 无")
    print("=" * 72)


def cmd_reset_db(cfg: Config) -> None:
    """清空 state.db (强制重新处理所有 EPUB)"""
    n = cfg.state_db.count()
    cfg.state_db.reset()
    print(f"🗑️  state.db 已清空 (删除了 {n} 条记录)")


def cmd_reprocess(cfg: Config, issue_id: str) -> None:
    """按 issue_id 删除记录, 让下一次轮询重新处理该期"""
    n = cfg.state_db.remove_by_issue(issue_id)
    if n:
        print(f"♻️  已删除 issue_id={issue_id} 的 {n} 条记录, 下次轮询会重新处理")
    else:
        print(f"⚠️  state.db 中没有 issue_id={issue_id} 的记录")


def main():
    parser = argparse.ArgumentParser(
        description="economist_purifier 守护进程",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="一次性处理模式: 处理完当前所有 .epub 后退出",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="干跑模式: 只解析不编译, 打印文章数量/板块/分类, 不调 LLM, 不入库",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="强制模式: 忽略 state.db 去重, 重跑所有 EPUB (用于 schema 升级后批量重处理)",
    )
    # 互斥的运维子命令 (只能选一个, 与 --once/--dry-run/--force 互斥)
    ops = parser.add_mutually_exclusive_group()
    ops.add_argument(
        "--status", action="store_true",
        help="查看 state.db 全部记录 + WATCH_DIR 待处理文件",
    )
    ops.add_argument(
        "--reset-db", action="store_true",
        help="清空 state.db (后续轮询会重新处理所有 EPUB)",
    )
    ops.add_argument(
        "--reprocess", metavar="ISSUE_ID",
        help="按 issue_id 删除记录, 例: --reprocess issue_2026-07-11",
    )
    args = parser.parse_args()

    print("🚀 Economist 智库后端编译 Daemon 引擎启动…")
    cfg = Config()
    print(cfg.summary())

    # ---------- 运维子命令 (不需要 API key, 不进入编译流程) ----------
    if args.status:
        cmd_status(cfg)
        return
    if args.reset_db:
        cmd_reset_db(cfg)
        return
    if args.reprocess:
        cmd_reprocess(cfg, args.reprocess)
        return

    # ---------- 编译流程 (需要 API key,除非 --dry-run) ----------
    if not cfg.openai_api_key and not args.dry_run:
        print("❌ 未配置 OPENAI_API_KEY,无法启动 LLM 编译")
        sys.exit(1)

    # 首次启动自动迁移 archived/* → state.db
    _auto_migrate(cfg)

    if args.dry_run:
        print("🧪 干跑模式 (dry-run): 仅统计, 不消耗 API")
        n = check_and_process_jobs(cfg, dry_run=True)
        print(f"🏁 干跑完成: {n} 份刊物已统计, 请确认数量后去掉 --dry-run 跑全量")
        return

    if args.once:
        print("📦 一次性模式启动")
        n = check_and_process_jobs(cfg, force=args.force)
        print(f"🏁 处理完成: {n} 份刊物")
        # AUTO_PUBLISH=1 时, 编完自动调 publish.py → build_site + git push → Netlify
        if n > 0 and os.getenv("AUTO_PUBLISH") == "1":
            print("\n🚀 AUTO_PUBLISH=1, 自动触发 publish.py ...")
            subprocess.run(
                ["python3", "scripts/publish.py", "--no-compile"],
                cwd=str(Path(__file__).resolve().parent.parent),
            )
        return

    print(f"🔄 常驻模式: 监听 {cfg.watch_dir},轮询周期 {cfg.poll_interval}s")
    while True:
        try:
            n = check_and_process_jobs(cfg, force=args.force)
            # AUTO_PUBLISH=1 且本轮有新内容 → 自动 push (投放即上线)
            if n > 0 and os.getenv("AUTO_PUBLISH") == "1":
                print("\n🚀 AUTO_PUBLISH=1, 自动触发 publish.py ...")
                subprocess.run(
                    ["python3", "scripts/publish.py", "--no-compile"],
                    cwd=str(Path(__file__).resolve().parent.parent),
                )
        except KeyboardInterrupt:
            print("\n👋 守护进程被用户中断,优雅退出")
            break
        except Exception as e:
            print(f"  💥 轮询异常: {e}")
        time.sleep(cfg.poll_interval)


if __name__ == "__main__":
    main()
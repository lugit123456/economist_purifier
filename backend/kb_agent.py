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

from dotenv import load_dotenv

# 支持直接运行 (python -m backend.kb_agent) 和包导入
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from backend.parser import extract_and_parse_epub
    from backend.compiler import EconomistCompiler
else:
    from .parser import extract_and_parse_epub
    from .compiler import EconomistCompiler


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

        # 路径
        base = Path(__file__).resolve().parent.parent
        self.watch_dir = Path(os.getenv("WATCH_DIR", "./raw/imports")).resolve()
        self.archive_dir = self.watch_dir / "archived"
        self.image_dir = Path(os.getenv("IMAGE_DIR", "./raw/images")).resolve()
        self.output_dir = Path(os.getenv("OUTPUT_DIR", "./output")).resolve()
        self.db_file = Path(os.getenv("DB_FILE", "./frontend/database.js")).resolve()

        # 调度
        self.poll_interval: int = int(os.getenv("POLL_INTERVAL", "10"))

        # 校验
        self._validate()

        # 确保所有目录存在
        for d in (self.watch_dir, self.archive_dir,
                  self.image_dir, self.output_dir, self.db_file.parent):
            d.mkdir(parents=True, exist_ok=True)

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
            f"  - 输出: {self.output_dir}\n"
            f"  - 数据库: {self.db_file}"
        )


# ---------- database.js 回写 ----------

def bake_into_local_database(db_file: Path, new_issue_data: dict) -> None:
    """回流写入技术: 无缝覆写本地 database.js,确保前端无感感知"""
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
                              dry_run: bool = False) -> bool:
    """处理单份 EPUB: 拆解 → 编译 → 落盘 → 回写

    dry_run=True: 仅解析, 打印统计, 不调 LLM, 不落盘, 不归档
    """
    try:
        # Step 1: EPUB 极速解包 (dry_run 也需要这一步)
        raw_issue_data = extract_and_parse_epub(
            epub_path, image_dir=cfg.image_dir
        )

        if dry_run:
            # 只统计不调用 LLM
            from collections import Counter
            cat_counts = Counter(a.get("category", "?") for a in raw_issue_data["articles"])
            print(f"  🧪 [DRY-RUN] {epub_path.name} → {len(raw_issue_data['articles'])} 篇")
            print(f"     category 分布: {dict(cat_counts)}")
            # 不归档, 留待正式跑
            return True

        # Step 2: LLM 并发编译
        compiled_issue_data = await compiler.compile_issue(raw_issue_data)

        # Step 3: .md 研报本地落盘
        saved = compiler.save_issue_markdowns(compiled_issue_data)
        if saved:
            print(f"  📝 已落盘 {len(saved)} 篇 .md 研报到 {cfg.output_dir}")

        # Step 4: 回流 database.js
        bake_into_local_database(cfg.db_file, compiled_issue_data)

        # Step 5: 归档原始 EPUB
        archive_path = cfg.archive_dir / epub_path.name
        if archive_path.exists():
            archive_path.unlink()
        epub_path.rename(archive_path)
        print(f"  ✅ {epub_path.name} 完整智库编译流程成功闭环")
        return True

    except Exception as e:
        print(f"  ❌ {epub_path.name} 流程发生致命中断: {e}")
        import traceback
        traceback.print_exc()
        return False


def check_and_process_jobs(cfg: Config, dry_run: bool = False) -> int:
    """
    守护进程单次轮询入口

    ⚠️ 关键: 每次调用都 **新建** EconomistCompiler 实例。
    因为 compiler 内部的 Semaphore 和 httpx 连接池会绑定到
    创建时的 event loop,而本函数每次用 asyncio.run() 创建新 loop。
    跨 loop 复用会导致 "attached to a different loop" 错误。
    """
    epub_files = sorted(cfg.watch_dir.glob("*.epub"))
    if not epub_files:
        return 0

    if dry_run:
        print(f"  🧪 [DRY-RUN] 仅统计数量, 不调 LLM, 不归档")
        for epub in epub_files:
            try:
                issue = extract_and_parse_epub(epub, image_dir=cfg.image_dir)
                from collections import Counter
                cat_counts = Counter(a.get("category", "?") for a in issue["articles"])
                print(f"  📊 {epub.name}: {len(issue['articles'])} 篇")
                print(f"     板块分布: {Counter(a['section'] for a in issue['articles']).most_common(5)}")
                print(f"     分类: {dict(cat_counts)}")
            except Exception as e:
                print(f"  ❌ {epub.name} 解析失败: {e}")
        return len(epub_files)

    print(f"  🔔 守护进程捕获到 {len(epub_files)} 份新刊物,启动流水线…")

    async def process_all():
        compiler = EconomistCompiler(
            api_key=cfg.openai_api_key,
            base_url=cfg.openai_base_url,
            model=cfg.openai_model,
            concurrency=cfg.llm_concurrency,
            output_dir=cfg.output_dir,
            use_json_response_format=cfg.use_json_format,
        )
        try:
            success = 0
            for epub in epub_files:
                if await process_single_epub(epub, cfg, compiler):
                    success += 1
            return success
        finally:
            await compiler.aclose()

    return asyncio.run(process_all())


# ---------- 主入口 ----------

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
        help="干跑模式: 只解析不编译, 打印文章数量/板块/分类, 不调 LLM, 不归档",
    )
    args = parser.parse_args()

    print("🚀 Economist 智库后端编译 Daemon 引擎启动…")
    cfg = Config()
    print(cfg.summary())

    if not cfg.openai_api_key and not args.dry_run:
        print("❌ 未配置 OPENAI_API_KEY,无法启动 LLM 编译")
        sys.exit(1)

    if args.dry_run:
        print("🧪 干跑模式 (dry-run): 仅统计, 不消耗 API")
        n = check_and_process_jobs(cfg, dry_run=True)
        print(f"🏁 干跑完成: {n} 份刊物已统计, 请确认数量后去掉 --dry-run 跑全量")
        return

    if args.once:
        print("📦 一次性模式启动")
        n = check_and_process_jobs(cfg)
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
            check_and_process_jobs(cfg)
        except KeyboardInterrupt:
            print("\n👋 守护进程被用户中断,优雅退出")
            break
        except Exception as e:
            print(f"  💥 轮询异常: {e}")
        time.sleep(cfg.poll_interval)


if __name__ == "__main__":
    main()
"""
异步并发 LLM 编译引擎

职责:
1. 通过 OpenAI 兼容协议 (custom base_url) 调用大模型
2. asyncio.Semaphore 控制并发上限 (默认 8 路)
3. 重试 / 限流 / JSON 结构化输出
4. 每篇研报落盘到 OUTPUT_DIR/{issue_date}/{标题}_{art_id}.md
"""

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Optional

from openai import AsyncOpenAI, APIError, APITimeoutError, RateLimitError
from pydantic import BaseModel, Field

try:
    from bs4 import BeautifulSoup
    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False


# ---------- Prompt 设计 ----------

SYSTEM_PROMPT = """\
你是一位专业的国际政经与科技评论员。请阅读《经济学人》文章的英文原文,深度总结为中文。

硬性要求:
1. 总结总字数严禁少于 300 字(中文字符计)。
2. 严禁遗漏任何核心论点和数据(数字、人物、机构名、年份)。
3. 严禁引入原文外的信息。
4. 若原文涉及争议,保留双方观点,标注来源。

严格按以下 Markdown 格式输出(不可增删章节):

### 🌟 一句话核心主旨
(1-2 句话,精准点出文章最核心的论点)

### 🔍 核心观点与论据拆解
(分点列出,3-5 个,每点 50-80 字)

### 🤨 争议与潜在挑战
(若文章未涉及,写"原文未涉及明显争议")

### 🔮 未来趋势预判
(基于文章事实延伸,2-3 句话,不可编造新数据)

附: 严禁在 JSON 字符串值内部使用英文双引号 (")。
如需引用术语,使用「」或『』中文引号,或直接省略引号。否则会破坏 JSON 结构。"""


USER_PROMPT_TEMPLATE = """原刊期次: {issue_date}
所属板块: {section}
英文原标题: {title}

英文原文:
\"\"\"
{content}
\"\"\"

请按规范输出 JSON 格式编译结果 (包含 title_zh 与 summary_md 两个字段)。"""


# 快讯模式 prompt: 仅适用 Politics/Business 板块
# 要求: 按英文语义忠实翻译为中文, 不做解读/归纳/评论
# 输出: title_zh (中文标题) + summary_md (忠实中文译文全文)
NEWS_TRANSLATION_PROMPT = """你是专业英中翻译。请将《经济学人》Politics / Business 板块的英文原文逐段翻译为忠实中文。

硬性要求:
1. 严格忠于原文语义, 不增删不解读, 不写导语不写评论
2. 保留《经济学人》辛辣克制笔法
3. 专业术语精准 (stagflation → 滞胀, balance sheet recession → 资产负债表衰退)
4. 人名/地名/机构名使用约定俗成中文译名
5. 保留原文章节结构与段落分隔
6. 严禁在 JSON value 内使用英文双引号 "
7. 严禁输出除 JSON 以外的任何字符 (无前言无 markdown fence)

按以下 JSON 输出:
{{"title_zh": "中文主标题 (20-25字)", "summary_md": "忠实中文译文全文 (保留段落)"}}

英文原标题: {title}

英文原文:
\"\"\"
{content}
\"\"\""""


# 逐段翻译 prompt: 把 N 段英文段落逐段翻译为中文
# 输入: paragraphs 数组 (每项含 para_id + en_html)
# 输出: translations 数组 (与输入一一对应, 顺序一致, 每项是中文翻译的纯文本)
PARAGRAPH_TRANSLATION_PROMPT = """你是专业英中翻译。请将以下《经济学人》文章的英文段落数组 **逐段** 翻译为忠实中文。

硬性要求:
1. 严格忠于原文语义, 不增删不解读, 不写导语不写评论
2. 保留《经济学人》辛辣克制笔法
3. 专业术语精准 (stagflation → 滞胀, balance sheet recession → 资产负债表衰退)
4. 人名/地名/机构名使用约定俗成中文译名
5. 段落数量与输入完全一致, 顺序一一对应 (不增不减不调换)
6. 每段翻译是纯中文文本, 不要包裹 <p> 等 HTML 标签
7. 严禁在 JSON 字符串值内部使用英文双引号 "
8. 严禁输出除 JSON 以外的任何字符 (无前言无 markdown fence)

按以下 JSON 输出:
{{"translations": ["<第1段中文译文>", "<第2段中文译文>", ...]}}

待翻译段落:
\"\"\"
{paragraphs}
\"\"\""""


# ---------- 数据模型 ----------

class CompiledArticle(BaseModel):
    """LLM 返回的编译结果"""
    title_zh: str = Field(..., description="信达雅的中文主标题")
    summary_md: str = Field(..., description="严格三段式 Markdown 研报")


# ---------- 文件名安全化 ----------

_FILENAME_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_title_for_filename(title: str, max_len: int = 60) -> str:
    """将文章标题清洗为合法文件名片段"""
    cleaned = title.strip()
    # 替换文件系统非法字符
    cleaned = _FILENAME_ILLEGAL.sub("_", cleaned)
    # 合并连续空白 → 单个下划线 (中英文标题都更紧凑)
    cleaned = re.sub(r"\s+", "_", cleaned)
    # 合并连续下划线
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    # 截断长度
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip("_")
    return cleaned or "untitled"


# ---------- 防御性 JSON 解析 ----------

# 推理模型常见的 thinking 块包裹 (DeepSeek-R1 / Qwen-QwQ / MiniMax-M3 等)
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
# Markdown 代码围栏 ```json ... ``` 或 ``` ... ```
_CODE_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*([\s\S]*?)```")
# 抓取响应中第一个完整的 JSON 对象 {...}
_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")


def _parse_llm_json(raw: str) -> dict:
    """
    防御性解析 LLM 返回的 JSON。

    兼容以下异常情况:
    1. 推理模型在正文前输出 <think>...</think> 块
    2. 响应被 ```json ... ``` 代码围栏包裹
    3. LLM 在 JSON 前后追加说明性文字
    4. LLM 在字符串值内部使用未转义的英文双引号 (常见于中文场景)
    5. response_format=json_object 端点不支持 / 被忽略

    解析策略 (按顺序尝试,直到成功):
    a. 标准 JSON 解析
    b. 字段级兜底提取:用 quote-aware 状态机分别抓 title_zh / summary_md

    全部失败时抛出 ValueError,包含原始响应的前 200 字符便于排错
    """
    if not raw or not raw.strip():
        raise ValueError("LLM 返回为空")

    text = raw.strip()

    # 1. 剥离 <think>...</think> 块
    text = _THINK_BLOCK_RE.sub("", text).strip()

    # 2. 提取 ```json ... ``` 中的内容
    fence_match = _CODE_FENCE_RE.search(text)
    if fence_match:
        text = fence_match.group(1).strip()
    else:
        # 3. 尝试定位第一个 { ... } JSON 对象
        obj_match = _JSON_OBJECT_RE.search(text)
        if obj_match:
            text = obj_match.group(0).strip()

    # 4. 标准 JSON 解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 5. 字段级兜底: 中文模型常在 value 里写未转义双引号
    #    用 quote-aware 状态机分别抓取 title_zh / summary_md
    repaired = _smart_field_extraction(text)
    if repaired:
        return repaired

    # 6. 实在不行, 抛错带原始内容
    raise ValueError(
        f"LLM 返回非 JSON (剥离包装后): {text[:200]}…"
    )


def _smart_field_extraction(text: str) -> dict:
    """
    字段级兜底提取: 在 JSON 结构损坏时,逐个提取目标字段。

    容忍 value 内部出现未转义的英文双引号 (例如: "特朗普时代")
    """
    result = {}
    for key in ("title_zh", "summary_md"):
        value = _extract_string_value(text, key)
        if value is not None:
            result[key] = value

    # 至少要解析出一个字段才算成功
    return result if result else {}


def _extract_string_value(text: str, key: str) -> Optional[str]:
    """
    提取 `"key": "..."` 对应的字符串值,处理未转义双引号。

    算法:
    1. 定位 `"key":` 后的开引号
    2. 向后扫描, 遇到 `\\` 跳过下一字符
    3. 遇到 `"` 时, 看后续非空白字符:
       - 是 `,` `}` `]` → 这是 value 结束
       - 否则 → 这是 value 内部的引号, 跳过
    """
    pattern = re.compile(rf'"{re.escape(key)}"\s*:\s*"', re.DOTALL)
    match = pattern.search(text)
    if not match:
        return None

    start = match.end()
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        # 处理转义: \\" \\n \\t 等都跳过
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        # 遇到双引号
        if c == '"':
            # 判断是结束引号还是 value 内的杂散引号
            j = i + 1
            while j < n and text[j] in " \t\n\r":
                j += 1
            if j >= n or text[j] in ",}]":
                # 这是 value 真正的结束引号
                return text[start:i]
            # 否则是 value 内部的杂散引号, 跳过
        i += 1

    # 没找到结束引号, 兜底返回从 start 到末尾
    return text[start:] if n > start else None


# ---------- 主编译引擎 ----------

class EconomistCompiler:
    """asyncio + AsyncOpenAI 并发编译引擎"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        concurrency: int = 8,
        output_dir: Path = Path("./output"),
        max_retries: int = 3,
        timeout: float = 90.0,
        use_json_response_format: bool = True,
    ):
        if not api_key:
            raise ValueError("OPENAI_API_KEY 未配置,无法启动编译引擎")

        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
        )
        self.model = model
        self.concurrency = concurrency
        # ⚠️ 关键: 必须在运行中的 event loop 内创建 Semaphore,
        # 否则 Python 3.9 会绑定到隐式 default loop,与 asyncio.run() 新 loop 冲突。
        # 改为懒初始化,首次 async 调用时再创建。
        self._semaphore: Optional[asyncio.Semaphore] = None
        self.output_dir = Path(output_dir)
        self.max_retries = max_retries
        self.timeout = timeout
        # 部分 OpenAI 兼容端点 (推理模型 / 自部署) 不支持 response_format=json_object
        # 此时 prompt 强约束 + 防御性解析已足够
        self.use_json_response_format = use_json_response_format

        # 确保输出根目录存在
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _get_semaphore(self) -> asyncio.Semaphore:
        """懒初始化 semaphore — 必须在运行中的 event loop 内首次访问"""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.concurrency)
        return self._semaphore

    async def aclose(self) -> None:
        """关闭底层 httpx 连接池,释放资源。常驻模式下建议在每次轮询结束调用"""
        try:
            await self.client.close()
        except Exception:
            pass

    # -------- 单篇编译 (带重试) --------

    async def _call_llm_once(self, article: dict, issue_date: str) -> CompiledArticle:
        """单次 LLM 调用,失败由调用方重试

        根据 article["category"] 分支:
        - "news" (快讯: Politics/Business/Europe 等): 只译标题, summary_md 设占位
        - "analysis" (默认): 全量 4 段式中文解读
        """
        is_news = article.get("category") == "news"
        title_eng = article.get("title", "Untitled")
        section = article.get("section", "Standard Section")
        content = article.get("content_raw", "")
        # news 类上限 4000 字符 (避免超时/敏感拦截), 其他类上限 12000
        max_chars = 4000 if is_news else 12000
        if len(content) > max_chars:
            content = content[:max_chars] + "\n\n[…文章过长已截断…]"

        if is_news:
            # 快讯 (仅 Politics/Business): 忠实中文翻译, 不做解读
            # summary_md = 翻译后的中文全文 (与右侧 content_raw 英文原文成对照)
            user_prompt = NEWS_TRANSLATION_PROMPT.format(title=title_eng, content=content)
            system_prompt = "你是专业英中翻译,信达雅即可,仅输出 JSON。"
        else:
            # 中文解读模式: 4 段式分析
            user_prompt = USER_PROMPT_TEMPLATE.format(
                issue_date=issue_date,
                section=section,
                title=title_eng,
                content=content,
            )
            system_prompt = SYSTEM_PROMPT

        # 构造请求参数;不支持 json_object 的端点会忽略或报错,降级为纯 prompt 约束
        api_kwargs = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3 if is_news else 0.4,
        }
        if self.use_json_response_format:
            api_kwargs["response_format"] = {"type": "json_object"}

        try:
            response = await self.client.chat.completions.create(**api_kwargs)
        except (TypeError, ValueError) as e:
            if "response_format" in str(e) and "json_object" in api_kwargs:
                api_kwargs.pop("response_format", None)
                response = await self.client.chat.completions.create(**api_kwargs)
            else:
                raise

        raw = response.choices[0].message.content or "{}"
        data = _parse_llm_json(raw)

        title_zh = data.get("title_zh", title_eng)

        if is_news:
            # summary_md = 忠实中文译文 (供左侧"中文解读"面板展示)
            # 失败兜底: 用英文原文 (LLM 拒答时仍能展示原文, 不会断)
            translation = data.get("summary_md", "")
            if not translation or "编译失败" in translation:
                # 兜底: 直接把英文原文放进 summary_md, 至少不丢内容
                summary_md = (
                    f"## 🌍 {section} · 板块快讯\n\n"
                    f"> 忠实中文翻译暂不可用, 以下为原文备份:\n\n"
                    f"{article.get('content_raw', '')}"
                )
            else:
                # 正常: 翻译结果作为左侧面板
                summary_md = (
                    f"## 🌍 {section} · 忠实中文翻译\n\n"
                    f"> 下方为英文原文的中文翻译 (按英文语义直译, 不做解读)。\n\n"
                    f"---\n\n"
                    f"{translation}"
                )
        else:
            summary_md = data.get("summary_md", "### 一句话核心主旨\n编译失败,请检查日志。")

        return CompiledArticle(title_zh=title_zh, summary_md=summary_md)

    async def _translate_title_only(self, title_eng: str) -> str:
        """降级方案: 仅翻译标题 (短输入, 几乎不会被内容审核拦截)"""
        prompt = (
            f"你是《经济学人》中文标题翻译专家。仅翻译以下标题, 严格保留辛辣克制笔法, 控制在 25 字以内。\n\n"
            f"英文标题: {title_eng}\n\n"
            f"按 JSON 输出: {{\"title_zh\": \"你的译文\"}}"
        )
        api_kwargs = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
        }
        if self.use_json_response_format:
            api_kwargs["response_format"] = {"type": "json_object"}
        try:
            response = await self.client.chat.completions.create(**api_kwargs)
        except (TypeError, ValueError) as e:
            if "response_format" in str(e):
                api_kwargs.pop("response_format", None)
                response = await self.client.chat.completions.create(**api_kwargs)
            else:
                raise
        raw = response.choices[0].message.content or "{}"
        data = _parse_llm_json(raw)
        return data.get("title_zh", title_eng)

    def _news_fallback_summary(self, article: dict) -> str:
        """news 文章降级后的 summary_md: 友好说明 + 英文原文"""
        section = article.get("section", "Politics/Business")
        return (
            f"## 🌍 {section} · 板块快讯\n\n"
            f"> ⚠️ LLM 全文翻译暂不可用 (内容审核拦截或超时), 仅完成标题翻译。\n\n"
            f"> 下方为英文原文备份:\n\n"
            f"---\n\n"
            f"{article.get('content_raw', '')}"
        )

    async def compile_one(self, article: dict, issue_date: str) -> None:
        """并发安全的单篇编译入口。原地修改 article 字典

        三级降级策略:
        1. 全文翻译 (news) / 4 段式分析 (analysis)
        2. 瞬时错误 (超时/限流) 重试 max_retries 次
        3. 永久错误 (422 内容审核 / JSON 解析) → 不重试, 走降级
        4. news 文章降级: 标题翻译 + 友好 summary
        5. 最后兜底: 英文标题 + 原文
        """
        async with self._get_semaphore():
            art_id = article.get("id", "unknown")
            is_news = article.get("category") == "news"
            last_error = None
            permanent_failure = False

            # === 第一阶段: 主流程 ===
            for attempt in range(1, self.max_retries + 1):
                try:
                    result = await self._call_llm_once(article, issue_date)
                    article["title_zh"] = result.title_zh
                    article["summary_md"] = result.summary_md
                    print(f"  ✅ {art_id} 编译完成 ({attempt}/{self.max_retries})")
                    return
                except (RateLimitError, APITimeoutError) as e:
                    # 瞬时错误: 重试
                    last_error = e
                    wait = min(2 ** attempt, 30)
                    print(f"  ⚠️  {art_id} 第 {attempt} 次失败 (瞬时): {type(e).__name__}, {wait}s 后重试…")
                    await asyncio.sleep(wait)
                except APIError as e:
                    # API 错误: 区分 422 内容审核 vs 其他
                    status = getattr(e, "status_code", None)
                    err_str = str(e).lower()
                    if status == 422 or "sensitive" in err_str or "unprocessable" in err_str:
                        # 永久错误 (内容审核), 不重试
                        last_error = e
                        permanent_failure = True
                        print(f"  🛡  {art_id} 内容审核拦截 (422), 跳过重试")
                        break
                    else:
                        # 其他 API 错误: 重试
                        last_error = e
                        wait = min(2 ** attempt, 30)
                        print(f"  ⚠️  {art_id} 第 {attempt} 次失败 (API): {type(e).__name__}, {wait}s 后重试…")
                        await asyncio.sleep(wait)
                except Exception as e:
                    # JSON 解析失败等: 永久错误, 不重试
                    last_error = e
                    permanent_failure = True
                    print(f"  ❌ {art_id} 解析失败 (不可重试): {type(e).__name__}: {str(e)[:100]}")
                    break

            # === 第二阶段: news 文章降级到标题翻译 ===
            if is_news:
                print(f"  🔄 {art_id} 降级到标题翻译模式")
                try:
                    title_zh = await self._translate_title_only(article.get("title", ""))
                    article["title_zh"] = title_zh
                    article["summary_md"] = self._news_fallback_summary(article)
                    article["compile_status"] = "news_fallback"
                    print(f"  ✅ {art_id} 降级完成 (标题已翻译)")
                    return
                except Exception as e:
                    last_error = e
                    print(f"  ⚠️  {art_id} 标题翻译也失败: {e}")

            # === 第三阶段: 最终兜底 ===
            article["title_zh"] = article.get("title", "")
            err_name = type(last_error).__name__ if last_error else "UnknownError"
            err_msg = str(last_error)[:200] if last_error else "unknown"
            article["summary_md"] = (
                f"### ⚠️ 编译失败\n\n"
                f"`{err_name}: {err_msg}`\n\n"
                f"请检查 API 配置或稍后重试。"
            )
            article["compile_status"] = "failed"

    # -------- 逐段翻译 (双语对照阅读器用) --------

    # 块大小: 每个 LLM 请求最多翻译这么多段 (避免超长 prompt)
    _PARA_CHUNK_SIZE = 12

    @staticmethod
    def _html_to_text(en_html: str) -> str:
        """把 en_html (块级 HTML) 抽取为纯文本, 节省 LLM token"""
        if not en_html:
            return ""
        if not _HAS_BS4:
            # 退化: 简单剥标签
            return re.sub(r"<[^>]+>", "", en_html).strip()
        soup = BeautifulSoup(en_html, "lxml")
        return soup.get_text(separator=" ", strip=True)

    async def _call_llm_translate_paragraphs(self, plain_paragraphs: list[str]) -> list[str]:
        """单次 LLM 调用, 把一组英文段落翻译为中文

        输入: 纯文本段落数组
        输出: 中文翻译数组 (顺序与输入一致, 长度相同)
        """
        # 把段落用编号拼接, 帮助 LLM 保持顺序
        joined = "\n\n".join(
            f"[P{i + 1}] {p}" for i, p in enumerate(plain_paragraphs)
        )
        user_prompt = PARAGRAPH_TRANSLATION_PROMPT.format(paragraphs=joined)
        system_prompt = "你是专业英中翻译,信达雅即可,仅输出 JSON。"

        api_kwargs = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
        }
        if self.use_json_response_format:
            api_kwargs["response_format"] = {"type": "json_object"}

        try:
            response = await self.client.chat.completions.create(**api_kwargs)
        except (TypeError, ValueError) as e:
            if "response_format" in str(e) and "response_format" in api_kwargs:
                api_kwargs.pop("response_format", None)
                response = await self.client.chat.completions.create(**api_kwargs)
            else:
                raise

        raw = response.choices[0].message.content or "{}"
        data = _parse_llm_json(raw)
        translations = data.get("translations", [])
        # 防御: 长度不匹配时, 多余段留空, 缺失段留空
        if not isinstance(translations, list):
            return ["" for _ in plain_paragraphs]
        result = ["" for _ in plain_paragraphs]
        for i, t in enumerate(translations[:len(plain_paragraphs)]):
            if isinstance(t, str):
                result[i] = t.strip()
        return result

    async def _translate_paragraphs_with_retry(self, plain_paragraphs: list[str],
                                                art_id: str) -> list[str]:
        """带重试 + 降级的逐段翻译

        策略:
        1. 全文一次性 LLM 调用
        2. 瞬时错误 (timeout/限流) 重试 max_retries 次
        3. 永久错误 (422/JSON 解析) → 不重试, 走降级
        4. 降级: 分段(每段单独)再试一次 (避免长 prompt 触发审核)
        5. 最终兜底: 全部留空
        """
        if not plain_paragraphs:
            return []

        # === 第一阶段: 全文一次性 ===
        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                translations = await self._call_llm_translate_paragraphs(plain_paragraphs)
                # 校验: 至少翻译出一半以上的非空段落才算成功
                non_empty = sum(1 for t in translations if t)
                if non_empty >= max(1, len(plain_paragraphs) // 2):
                    print(f"  ✅ {art_id} 逐段翻译完成 ({attempt}/{self.max_retries}, {non_empty}/{len(plain_paragraphs)} 非空)")
                    return translations
                last_error = ValueError(f"逐段翻译返回过少有效译文 ({non_empty}/{len(plain_paragraphs)})")
                print(f"  ⚠️  {art_id} 逐段翻译有效数不足, 重试 ({attempt}/{self.max_retries})")
                await asyncio.sleep(2)
            except (RateLimitError, APITimeoutError) as e:
                last_error = e
                wait = min(2 ** attempt, 30)
                print(f"  ⚠️  {art_id} 逐段翻译瞬时错误: {type(e).__name__}, {wait}s 后重试…")
                await asyncio.sleep(wait)
            except APIError as e:
                status = getattr(e, "status_code", None)
                err_str = str(e).lower()
                if status == 422 or "sensitive" in err_str or "unprocessable" in err_str:
                    last_error = e
                    print(f"  🛡  {art_id} 逐段翻译内容审核拦截 (422), 进入降级")
                    break
                last_error = e
                wait = min(2 ** attempt, 30)
                print(f"  ⚠️  {art_id} 逐段翻译 API 错误: {type(e).__name__}, {wait}s 后重试…")
                await asyncio.sleep(wait)
            except Exception as e:
                last_error = e
                print(f"  ❌ {art_id} 逐段翻译解析失败 (不可重试): {type(e).__name__}: {str(e)[:100]}")
                break

        # === 第二阶段: 降级 — 逐段单独翻译 ===
        print(f"  🔄 {art_id} 逐段翻译降级: 单段逐次翻译")
        result = ["" for _ in plain_paragraphs]
        for i, p in enumerate(plain_paragraphs):
            if not p:
                continue
            for attempt in range(1, self.max_retries + 1):
                try:
                    single = await self._call_llm_translate_paragraphs([p])
                    if single and single[0]:
                        result[i] = single[0]
                        break
                except Exception as e:
                    if attempt < self.max_retries:
                        await asyncio.sleep(2)
                    else:
                        print(f"  ⚠️  {art_id} 第 {i+1} 段降级翻译失败: {type(e).__name__}: {str(e)[:80]}")
        non_empty = sum(1 for t in result if t)
        print(f"  📝 {art_id} 逐段翻译降级完成: {non_empty}/{len(result)} 非空")
        return result

    async def compile_paragraphs(self, article: dict) -> None:
        """把 article.paragraphs 的 zh_text 填上中文翻译 (原地修改)

        跳过条件:
        - 已有非空 zh_text (避免覆盖已翻译内容)
        - 没有 paragraphs 字段
        - 编译状态为 failed (没必要翻译失败的文章)
        """
        if not isinstance(article, dict):
            return
        paragraphs = article.get("paragraphs")
        if not paragraphs or not isinstance(paragraphs, list):
            return
        if article.get("compile_status") == "failed":
            return
        # 已全部翻译过 → 跳过
        if all((isinstance(p, dict) and (p.get("zh_text") or "").strip())
               for p in paragraphs):
            return

        art_id = article.get("id", "unknown")
        # 抽取每段纯文本 (en_html → text)
        plain = [self._html_to_text(p.get("en_html", "")) for p in paragraphs]
        # 跳过空段, 后面只翻译非空段
        non_empty_idx = [i for i, t in enumerate(plain) if t]
        non_empty_plain = [plain[i] for i in non_empty_idx]
        if not non_empty_plain:
            return
        # 分块: 每块最多 _PARA_CHUNK_SIZE 段
        translations: list[str] = []
        for chunk_start in range(0, len(non_empty_plain), self._PARA_CHUNK_SIZE):
            chunk = non_empty_plain[chunk_start:chunk_start + self._PARA_CHUNK_SIZE]
            chunk_translations = await self._translate_paragraphs_with_retry(
                chunk, f"{art_id}[{chunk_start + 1}-{chunk_start + len(chunk)}]"
            )
            translations.extend(chunk_translations)

        # 回填: 按原 index 写入 paragraphs[i].zh_text
        for k, idx in enumerate(non_empty_idx):
            if k < len(translations) and translations[k]:
                paragraphs[idx]["zh_text"] = translations[k]

    # -------- 整期并发编译 --------

    async def compile_issue(self, issue_data: dict) -> dict:
        """整期并发编译入口"""
        articles = issue_data.get("articles", [])
        issue_date = issue_data.get("issue_date", "unknown")

        if not articles:
            print(f"  ⚠️  {issue_date} 期无文章,跳过编译")
            return issue_data

        print(f"  🚀 启动 {self.concurrency} 路并发,"
              f"编译 {len(articles)} 篇中文解读…")

        start = time.time()
        # 每篇文章: 先做主编译 (title_zh + summary_md), 再做逐段翻译 (paragraphs[].zh_text)
        async def _one_full(art: dict):
            await self.compile_one(art, issue_date)
            if art.get("paragraphs"):
                await self.compile_paragraphs(art)

        tasks = [_one_full(art) for art in articles]
        await asyncio.gather(*tasks)

        elapsed = time.time() - start
        success = sum(1 for a in articles if not a["title_zh"].startswith("【编译失败】"))
        print(f"  🎯 {issue_date} 期编译闭环: 成功 {success}/{len(articles)},"
              f"耗时 {elapsed:.1f}s")
        return issue_data

    # -------- 单篇研报落盘 --------

    def save_md_artifact(self, issue_date: str, article: dict) -> Path:
        """
        将单篇研报落盘到 OUTPUT_DIR/{issue_date}/{标题}_{art_id}.md

        文件包含: 元信息 + summary_md + 原文 content_raw
        """
        issue_dir = self.output_dir / issue_date
        issue_dir.mkdir(parents=True, exist_ok=True)

        safe_title = sanitize_title_for_filename(article.get("title_zh", "untitled"))
        art_id = article.get("id", "unknown")
        filename = f"{safe_title}_{art_id}.md"
        filepath = issue_dir / filename

        # 处理重名 (极小概率,但需防护)
        counter = 1
        while filepath.exists():
            filepath = issue_dir / f"{safe_title}_{art_id}_{counter}.md"
            counter += 1

        content = self._render_md(article, issue_date)
        filepath.write_text(content, encoding="utf-8")
        return filepath

    def save_issue_markdowns(self, issue_data: dict) -> list[Path]:
        """批量落盘本期所有文章"""
        saved = []
        issue_date = issue_data.get("issue_date", "unknown")
        for art in issue_data.get("articles", []):
            try:
                path = self.save_md_artifact(issue_date, art)
                saved.append(path)
            except Exception as e:
                print(f"  ⚠️  {art.get('id')} 落盘失败: {e}")
        return saved

    @staticmethod
    def _render_md(article: dict, issue_date: str) -> str:
        """渲染单篇 .md 研报全文"""
        title_zh = article.get("title_zh", "")
        title_en = article.get("title", "")
        section = article.get("section", "Standard Section")
        art_id = article.get("id", "")
        url = article.get("url", "")
        summary = article.get("summary_md", "")
        content_raw = article.get("content_raw", "")
        cartoon_images = article.get("cartoon_images", [])

        # 漫画专栏特殊头部
        is_cartoon = section.lower() == "cartoon" or "cartoon" in title_en.lower()
        cartoon_block = ""
        if is_cartoon and cartoon_images:
            imgs_md = "\n".join(
                f"![漫画 {i+1}](../{path})" for i, path in enumerate(cartoon_images)
            )
            cartoon_block = f"\n---\n\n## 🎨 本期漫画\n\n{imgs_md}\n"

        return f"""# {title_zh}

> **英文原标题**: {title_en}

| 字段 | 值 |
|------|-----|
| **文章 ID** | `{art_id}` |
| **所属板块** | {section} |
| **原刊期次** | {issue_date} |
| **原文链接** | {url} |

---

## 📊 中文解读

{summary}
{cartoon_block}
---

## 📜 原文 (English Source)

{content_raw}

---

<sub>由 economist_purifier 智库引擎自动编译 · {time.strftime("%Y-%m-%d %H:%M:%S")}</sub>
"""
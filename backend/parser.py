"""
EPUB 极速解包与白名单清洗内核

职责:
1. zipfile 内存物理拆解
2. content.opf 标准目录流解析
3. 高清封面图精准拦截
4. BeautifulSoup 白名单标签清洗
"""

import os
import re
import warnings
import zipfile
from pathlib import Path
from typing import Optional

# EPUB .xhtml / .opf 严格符合 XML 规范,使用 lxml 的 xml 模式解析,
# 抑制 bs4 的 XMLParsedAsHTMLWarning 噪音
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)


def clean_html_content(html_bytes: bytes) -> str:
    """
    白名单清洗:彻底剔除内联样式与冗余标签,极致压缩 Token 消耗。
    仅保留 p / h1-h4 / 强调类标签,清空所有属性。
    """
    soup = BeautifulSoup(html_bytes, "lxml")
    # 移除无用节点
    for badge in soup(["script", "style", "link", "iframe", "noscript"]):
        badge.decompose()

    valid_tags = {"h1", "h2", "h3", "h4", "p", "b", "strong", "i", "em", "ul", "ol", "li"}
    cleaned_paragraphs = []

    for element in soup.find_all(True):
        if element.name in valid_tags:
            element.attrs = {}
            text = element.get_text(strip=True)
            if text:
                cleaned_paragraphs.append(str(element))

    return "\n".join(cleaned_paragraphs)


# ---------- 内容过滤 (极简版: 只剔除确认无效页) ----------

# 目录页特征: 大量短行 / 大量锚链 / 数字编号
_TOC_SIGNATURES = [
    re.compile(r"^\s*<p[^>]*>\s*(?:p\.?\s*)?\d+\s*</p>\s*$", re.MULTILINE),
    re.compile(r"^\s*(?:p\.?\s*)?\d+\s*$", re.MULTILINE),
]

# 板块索引/标头页标题黑名单 (命中整页跳过)
_SKIP_TITLE_PATTERNS = [
    re.compile(r"^contents?\s*$", re.I),
    re.compile(r"^table\s+of\s+contents\s*$", re.I),
    re.compile(r"^the\s+world\s+this\s+week\s*$", re.I),
    re.compile(r"^world\s+this\s+week\s*$", re.I),
    re.compile(r"^leaders?\s*$", re.I),
    re.compile(r"^briefing\s*$", re.I),
    re.compile(r"^briefly\s*$", re.I),
    re.compile(r"^letters?\s*$", re.I),
    re.compile(r"^letters?\s+to\s+the\s+editor\s*$", re.I),
    re.compile(r"^kicker\s*$", re.I),
    re.compile(r"^by\s+the\s+numbers\s*$", re.I),
    re.compile(r"^recommended\s+apps?\s*$", re.I),
    re.compile(r"^app\s+recommendations?\s*$", re.I),
    re.compile(r"^this\s+weeks?\s+cover\s*$", re.I),
    re.compile(r"^from\s+the\s+archive\s*$", re.I),
]

# Section 黑名单: 板块标头/索引页 (配合短文阈值使用)
_SKIP_SECTION_NAMES = {
    "this week", "the world this week", "contents", "letters",
    "letters to the editor", "from the archive",
}

# 快讯分类: 只有 Politics / Business 走 "忠实中文翻译" 分支 (不做解读)
# summary_md = content_raw 的中文翻译
# 其余板块 (Asia/China/Europe/Finance/Briefing 等) 仍走 4 段式中文解读
_NEWS_CATEGORIES = {
    "politics", "business",
}


def _is_definitely_junk(cleaned_text: str, raw_html: bytes,
                        title: str = "", section: str = "") -> tuple:
    """
    极简过滤: 剔除确认无效的页面 (版权页 / 纯目录 / 封面 / 板块索引)

    返回 (是否跳过, 原因)
    """
    if not cleaned_text or len(cleaned_text) < 30:
        return True, f"过短 ({len(cleaned_text)} 字符)"

    if b"cover" in raw_html and b"heading" not in raw_html and b"<h" not in raw_html:
        return True, "纯封面页"

    # 标题黑名单 (板块索引/标头页特征)
    for pat in _SKIP_TITLE_PATTERNS:
        if pat.match(title or ""):
            return True, f"标题命中索引页: {title[:30]}"

    # Section 黑名单 + 短文 -> 板块标头页
    sec_lower = (section or "").strip().lower()
    if sec_lower in _SKIP_SECTION_NAMES and len(cleaned_text) < 600:
        return True, f"板块标头/索引页 ({section})"

    # 纯目录页: 短行占比 > 90% + 页码模式
    lines = [l for l in cleaned_text.split("\n") if l.strip()]
    if len(lines) > 5:
        short_lines = sum(1 for l in lines if len(l) < 50)
        if short_lines / len(lines) > 0.9:
            for sig in _TOC_SIGNATURES:
                if sig.search(cleaned_text):
                    return True, "纯目录页"

    return False, ""


# ---------- 板块与 Cartoon 识别 ----------

# Cartoon 检测关键词 (标题或 section 命中任一即视为 cartoon)
_CARTOON_KEYWORDS = {"cartoon", "graphic", "每日漫画"}


def _is_cartoon_article(title: str, section: str = "") -> bool:
    """检测是否为漫画专栏"""
    t = title.lower().strip()
    if t.startswith("cartoon:") or t.startswith("cartoon "):
        return True
    if section and section.lower() in _CARTOON_KEYWORDS:
        return True
    return False


# ---------- NCX / nav.xhtml 板块解析 ----------

def _find_section_for_file(z: zipfile.ZipFile, opf_path: str,
                            opf_soup, file_path: str) -> str:
    """
    从 EPUB 的 NCX (EPUB2) 或 nav.xhtml (EPUB3) 解析每个文件所属板块。

    返回板块名 (例如 "Politics" / "Leaders"), 找不到则返回 "Standard Section"
    """
    # 在 manifest 中找 NCX / nav 项
    ncx_href = None
    nav_href = None

    for item in opf_soup.find_all("item"):
        media_type = item.get("media-type", "").lower()
        href = item.get("href")
        if not href:
            continue
        # EPUB3 nav: properties 属性含 "nav"
        props = item.get("properties", "").lower().split()
        if "nav" in props and ("xhtml" in media_type or "html" in media_type):
            nav_href = href
        # EPUB2 NCX: media-type 包含 dtbncx
        elif "dtbncx" in media_type or media_type.endswith("ncx"):
            ncx_href = href
        # 兜底: 文件名后缀
        elif href.lower().endswith(".ncx"):
            ncx_href = href

    # 优先解析 NCX
    if ncx_href:
        section = _parse_ncx_section(z, opf_path, ncx_href, file_path)
        if section:
            return section

    if nav_href:
        section = _parse_nav_section(z, opf_path, nav_href, file_path)
        if section:
            return section

    return "Standard Section"


def _parse_ncx_section(z: zipfile.ZipFile, opf_path: str,
                       ncx_href: str, target_file: str) -> str:
    """解析 NCX 文件, 找出 target_file 所属顶层板块

    lxml 解析器自动剥离命名空间, 用 plain 标签名 (navPoint) 即可匹配
    """
    try:
        opf_parent = str(Path(opf_path).parent)
        ncx_path = os.path.normpath(os.path.join(opf_parent, ncx_href)).replace("\\", "/")
        if ncx_path not in z.namelist():
            return ""

        content = z.read(ncx_path).decode("utf-8")
        soup = BeautifulSoup(content, "xml")

        target_tf = target_file.split("#")[0]
        opf_dir = str(Path(ncx_path).parent)

        # navMap 下的直接子节点 navPoint 才是板块 (顶层)
        nav_map = soup.find("navMap")
        if nav_map:
            top_level = [c for c in nav_map.find_all("navPoint", recursive=False)]
        else:
            top_level = []

        for nav_point in top_level:
            section_name = _extract_ncx_label(nav_point)
            if not section_name:
                continue

            # 该板块下所有 content src (含嵌套的二级 navPoint)
            for src in nav_point.find_all("content"):
                if _ncref_matches(src, opf_dir, target_tf):
                    return section_name
            # 处理二级嵌套 navPoint (同名板块下的子文章)
            for child in nav_point.find_all("navPoint"):
                for src in child.find_all("content"):
                    if _ncref_matches(src, opf_dir, target_tf):
                        return section_name
        return ""
    except Exception as e:
        import warnings
        warnings.warn(f"NCX parse failed: {e}", stacklevel=2)
        return ""


def _extract_ncx_label(nav_point) -> str:
    """从 navPoint 中提取板块名"""
    for label_tag in ("navLabel", "navlabel"):
        label = nav_point.find(label_tag)
        if label:
            text_el = label.find("text")
            if text_el:
                return text_el.get_text(strip=True)
    return ""


def _ncref_matches(src, opf_dir: str, target_tf: str) -> bool:
    """判断 NCX content 引用是否指向目标文件"""
    src_attr = src.get("src", "").split("#")[0]
    if not src_attr:
        return False
    ref_path = os.path.normpath(os.path.join(opf_dir, src_attr)).replace("\\", "/")
    return (ref_path == target_tf
            or ref_path.endswith("/" + target_tf)
            or ref_path.endswith(target_tf))


def _find_ncx_href(opf_soup) -> Optional[str]:
    """从 OPF manifest 找出 NCX 文件路径"""
    for item in opf_soup.find_all("item"):
        media_type = item.get("media-type", "").lower()
        href = item.get("href")
        if not href:
            continue
        if "dtbncx" in media_type or media_type.endswith("ncx") or href.lower().endswith(".ncx"):
            return href
    return None


def _parse_ncx_article_list(z: zipfile.ZipFile, opf_path: str,
                              ncx_href: str) -> dict:
    """
    解析 NCX, 返回 {file_path: section_name} 映射表。

    这是 **目录总篇数** 的唯一真源 — 用户确认 NCX 即总篇数。
    顶层 navPoint = 板块, 内嵌 navPoint = 该板块下文章。
    """
    result: dict = {}
    try:
        opf_parent = str(Path(opf_path).parent)
        ncx_path = os.path.normpath(os.path.join(opf_parent, ncx_href)).replace("\\", "/")
        if ncx_path not in z.namelist():
            return result
        content = z.read(ncx_path).decode("utf-8")
        soup = BeautifulSoup(content, "xml")
        nav_map = soup.find("navMap")
        if not nav_map:
            return result

        opf_dir = str(Path(ncx_path).parent)
        # 顶层 navPoint = 板块
        for section_nav in nav_map.find_all("navPoint", recursive=False):
            section_name = _extract_ncx_label(section_nav)
            if not section_name:
                continue

            # 第一层 content (板块标头本身, 如有)
            for src in section_nav.find_all("content", recursive=False):
                src_attr = src.get("src", "").split("#")[0]
                if not src_attr:
                    continue
                ref_path = os.path.normpath(os.path.join(opf_dir, src_attr)).replace("\\", "/")
                # 顶层 content 可能是板块索引页, 也可能是该板块第一篇
                # 不主动标记为文章, 由内嵌 navPoint 决定
                if ref_path and ref_path not in result:
                    result[ref_path] = section_name

            # 内嵌 navPoint 才是文章
            for article_nav in section_nav.find_all("navPoint"):
                for src in article_nav.find_all("content"):
                    src_attr = src.get("src", "").split("#")[0]
                    if not src_attr:
                        continue
                    ref_path = os.path.normpath(os.path.join(opf_dir, src_attr)).replace("\\", "/")
                    if ref_path and ref_path not in result:
                        result[ref_path] = section_name
        return result
    except Exception as e:
        import warnings
        warnings.warn(f"NCX parse failed: {e}", stacklevel=2)
        return result


def _parse_nav_article_list(z: zipfile.ZipFile, opf_path: str,
                              nav_href: str) -> dict:
    """
    解析 EPUB3 nav.xhtml, 返回 {file_path: section_name} 映射表。
    """
    result: dict = {}
    try:
        opf_parent = str(Path(opf_path).parent)
        nav_path = os.path.normpath(os.path.join(opf_parent, nav_href)).replace("\\", "/")
        if nav_path not in z.namelist():
            return result
        content = z.read(nav_path).decode("utf-8")
        soup = BeautifulSoup(content, "lxml")
        nav = soup.find("nav", attrs={"epub:type": "toc"}) or soup.find("nav")
        if not nav:
            return result
        opf_dir = str(Path(nav_path).parent)
        for li in nav.find_all("li", recursive=False):
            section_anchor = li.find("a", recursive=False)
            if not section_anchor:
                continue
            section_name = section_anchor.get_text(strip=True)
            # 内嵌 <ol><li><a> = 文章
            for sub_a in li.find_all("a"):
                href = sub_a.get("href", "").split("#")[0]
                if not href:
                    continue
                ref_path = os.path.normpath(os.path.join(opf_dir, href)).replace("\\", "/")
                if ref_path and ref_path not in result:
                    result[ref_path] = section_name
        return result
    except Exception as e:
        import warnings
        warnings.warn(f"nav.xhtml parse failed: {e}", stacklevel=2)
        return result


def _find_toc_articles(z: zipfile.ZipFile, opf_path: str, opf_soup) -> dict:
    """
    整合 NCX + nav.xhtml 的目录, 返回 {file_path: section_name}。

    优先级: NCX > nav.xhtml。
    这是目录的总篇数真源, 用户确认目录 = 总篇数。
    """
    ncx_href = _find_ncx_href(opf_soup)
    if ncx_href:
        ncx_articles = _parse_ncx_article_list(z, opf_path, ncx_href)
        if ncx_articles:
            return ncx_articles

    # EPUB3 nav.xhtml 兜底
    nav_href = None
    for item in opf_soup.find_all("item"):
        props = item.get("properties", "").lower().split()
        media_type = item.get("media-type", "").lower()
        if "nav" in props and ("xhtml" in media_type or "html" in media_type):
            nav_href = item.get("href")
            break

    if nav_href:
        return _parse_nav_article_list(z, opf_path, nav_href)

    return {}


def _parse_nav_section(z: zipfile.ZipFile, opf_path: str,
                       nav_href: str, target_file: str) -> str:
    """解析 EPUB3 nav.xhtml, 找出 target_file 所属板块 (best-effort)"""
    try:
        opf_parent = str(Path(opf_path).parent)
        nav_path = os.path.normpath(os.path.join(opf_parent, nav_href)).replace("\\", "/")
        if nav_path not in z.namelist():
            return ""
        content = z.read(nav_path).decode("utf-8")
        soup = BeautifulSoup(content, "lxml")
        ol = soup.find("nav", attrs={"epub:type": "toc"}) or soup.find("nav")
        if not ol:
            return ""
        # 简化: 把所有顶层 <ol><li><a> 作为板块, 其下内嵌作为文章
        for li in ol.find_all("li", recursive=False):
            section_anchor = li.find("a", recursive=False)
            if not section_anchor:
                continue
            section_name = section_anchor.get_text(strip=True)
            # 如果 target_file 在内嵌链接里
            for sub_a in li.find_all("a"):
                href = sub_a.get("href", "")
                href_path = href.split("#")[0]
                ref_parent = str(Path(nav_path).parent)
                ref_full = os.path.normpath(os.path.join(ref_parent, href_path)).replace("\\", "/")
                if ref_full == target_file:
                    return section_name
        return ""
    except Exception:
        return ""


# ---------- Cartoon 图片提取 ----------

_IMG_SRC_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)


def _extract_cartoon_images(z: zipfile.ZipFile, opf_path: str,
                             raw_html: bytes, issue_date: str,
                             art_id: str, image_dir: Path) -> list[str]:
    """
    从漫画文章的 XHTML 中提取 <img> 引用的图片, 持久化到 images/<issue_date>/ 目录

    返回保存的图片相对路径列表 (项目根相对)
    """
    saved = []
    opf_parent = str(Path(opf_path).parent)
    issue_img_dir = image_dir / issue_date
    issue_img_dir.mkdir(parents=True, exist_ok=True)

    counter = 1
    for src in _IMG_SRC_RE.findall(raw_html.decode("utf-8", errors="ignore")):
        # 解析相对路径
        img_path = os.path.normpath(os.path.join(opf_parent, src)).replace("\\", "/")
        # 解 fragment
        img_path = img_path.split("#")[0]
        if img_path not in z.namelist():
            continue
        # 保存
        ext = Path(img_path).suffix.lower() or ".jpg"
        out_filename = f"{art_id}_cartoon_{counter}{ext}"
        out_path = issue_img_dir / out_filename
        out_path.write_bytes(z.read(img_path))
        saved.append(f"raw/images/{issue_date}/{out_filename}")
        counter += 1

    return saved


# ---------- Economic & financial indicators 识别 ----------

_INDICATOR_SECTION_NAMES = {
    "economic & financial indicators",
    "economic indicators",
    "financial indicators",
    "indicators",
    "经济与金融指标",
}


def _is_indicators_section(title: str, section: str = "") -> bool:
    """检测是否为 Economic & financial indicators 板块 (图表密集页)"""
    sec_lower = (section or "").strip().lower()
    title_lower = (title or "").strip().lower()

    if sec_lower in _INDICATOR_SECTION_NAMES:
        return True

    # 标题包含 "indicator" 关键词但不是真文章 (典型 charts 页)
    if any(kw in title_lower for kw in ("indicator", "indicators")):
        return True

    # 容错: section 含 "economic & financial" 但不是 regular article
    if "economic & financial" in sec_lower and len(title_lower) < 80:
        return True

    return False


def _extract_indicator_images(z: zipfile.ZipFile, opf_path: str,
                                raw_html: bytes, issue_date: str,
                                art_id: str, image_dir: Path) -> list[dict]:
    """
    从 indicators 板块的 XHTML 中提取所有图表图片。

    返回 list of {"path": "...", "caption": "..."} 字典。
    尽力从相邻的 <figcaption> / <p> 抽取 caption, 否则 caption 为空。
    """
    saved = []
    opf_parent = str(Path(opf_path).parent)
    issue_img_dir = image_dir / issue_date
    issue_img_dir.mkdir(parents=True, exist_ok=True)

    html_text = raw_html.decode("utf-8", errors="ignore")
    soup = BeautifulSoup(html_text, "lxml")

    # 收集所有 <img>, 同时找附近的 <figcaption> / <p> 作为 caption
    for idx, img_tag in enumerate(soup.find_all("img"), start=1):
        src = img_tag.get("src", "")
        if not src:
            continue
        img_path = os.path.normpath(os.path.join(opf_parent, src)).replace("\\", "/").split("#")[0]
        if img_path not in z.namelist():
            continue

        # 提取 caption: 优先 figcaption, 其次父 <p>, 最后 alt
        caption = ""
        parent = img_tag.parent
        if parent and parent.name == "figure":
            fc = parent.find("figcaption")
            if fc:
                caption = fc.get_text(strip=True)[:200]
        if not caption:
            alt = img_tag.get("alt", "").strip()
            if alt:
                caption = alt[:200]

        ext = Path(img_path).suffix.lower() or ".jpg"
        out_filename = f"{art_id}_indicator_{idx}{ext}"
        out_path = issue_img_dir / out_filename
        out_path.write_bytes(z.read(img_path))
        saved.append({
            "path": f"raw/images/{issue_date}/{out_filename}",
            "caption": caption,
        })

    return saved


def _extract_cover(z: zipfile.ZipFile, opf_path: str, opf_soup: BeautifulSoup,
                   issue_date: str, image_dir: Path) -> str:
    """精准拦截本期封面,其他冗余插图一律丢弃以节省 98%+ 存储

    返回项目根相对路径 (符合 Schema 规范),前端可直接拼接 `../` 使用
    """
    cover_item = opf_soup.find("item", id=re.compile(r"cover(-image)?", re.I))
    if not cover_item or not cover_item.get("href"):
        return ""

    opf_parent = str(Path(opf_path).parent)
    full_cover_path = os.path.normpath(
        os.path.join(opf_parent, cover_item["href"])
    ).replace("\\", "/")

    if full_cover_path not in z.namelist():
        return ""

    cover_path = image_dir / f"cover_{issue_date}.jpg"
    with open(cover_path, "wb") as f:
        f.write(z.read(full_cover_path))
    print(f"  📸 成功提取本期高清封面: cover_{issue_date}.jpg")

    # 返回项目根相对路径,前端拼接 `../` 即可访问
    return f"raw/images/cover_{issue_date}.jpg"


def extract_and_parse_epub(epub_path: Path, image_dir: Path,
                           issue_date: str = "") -> dict:
    """
    EPUB 高性能解包入口。

    Args:
        epub_path: 原始 .epub 文件路径
        image_dir: 封面图持久化目录
        issue_date: 出版日期 (YYYY-MM-DD),为空时从文件名推断

    Returns:
        标准化 issue_data dict
    """
    print(f"  📦 正在对 {epub_path.name} 进行极速内存物理拆解...")

    # 从文件名推断日期 (支持 2026-07-11 或 2026.07.11 两种分隔符)
    if not issue_date:
        match = re.search(r"(\d{4})[-.](\d{2})[-.](\d{2})", epub_path.stem)
        if match:
            issue_date = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        else:
            issue_date = "1970-01-01"

    articles = []
    cover_saved_path = ""

    with zipfile.ZipFile(epub_path, "r") as z:
        # 1. 扫描寻找标准化 OPF 索引
        opf_path = next((f for f in z.namelist() if f.endswith(".opf")), None)
        if not opf_path:
            raise ValueError("非标准 EPUB 刊物:未找到 content.opf 索引文件")

        opf_content = z.read(opf_path).decode("utf-8")
        opf_soup = BeautifulSoup(opf_content, "xml")

        # 2. 精准拦截封面
        cover_saved_path = _extract_cover(z, opf_path, opf_soup, issue_date, image_dir)

        # 3. ★ 核心改动: 以 NCX/toc 为单一真源,不再以 spine 遍历
        toc_articles = _find_toc_articles(z, opf_path, opf_soup)
        toc_total = len(toc_articles)
        print(f"  📚 目录列示 {toc_total} 篇文章 (NCX/toc.xhtml)")

        opf_parent = str(Path(opf_path).parent)

        art_counter = 1
        toc_missing = []   # NCX 中有但 zip 找不到的文件
        toc_skipped = []   # NCX 中跳过 (索引页/标头)
        section_counts = {}  # 板块统计

        # ★ 主循环: 遍历 NCX 列出的每一篇文章 (NCX 是真源, 但仍跳索引页)
        for file_path, section in toc_articles.items():
            if file_path not in z.namelist():
                toc_missing.append(file_path)
                continue

            raw_html = z.read(file_path)
            cleaned_text = clean_html_content(raw_html)

            # 提取标题
            soup_title = BeautifulSoup(raw_html, "lxml").find(["h1", "h2"])
            title_eng = soup_title.get_text(strip=True) if soup_title else "Untitled Article"

            # 板块索引页判定 (仅跳索引, 真实文章一律保留):
            #   条件: 标题 == 板块名 (Leaders/Politics/Briefing 这种纯索引) OR
            #         标题命中 _SKIP_TITLE_PATTERNS (The world this week/Contents 等)
            #   例外: Economic & financial indicators 板块本身有图表, 不视为索引页
            is_indicators = _is_indicators_section(title_eng, section)
            is_index_page = (
                not is_indicators
                and (
                    title_eng.strip().lower() == section.strip().lower()
                    or any(pat.match(title_eng) for pat in _SKIP_TITLE_PATTERNS)
                )
            )
            if is_index_page:
                toc_skipped.append((title_eng[:40], section, len(cleaned_text)))
                continue

            # 检测是否为漫画专栏
            art_id = f"art_{issue_date}_{str(art_counter).zfill(3)}"
            cartoon_images = []
            if _is_cartoon_article(title_eng, section):
                section = "Cartoon"
                cartoon_images = _extract_cartoon_images(
                    z, opf_path, raw_html, issue_date, art_id, image_dir
                )
                if cartoon_images:
                    print(f"  🎨 {art_id} 检测到漫画, 提取 {len(cartoon_images)} 张图片")

            # 检测 Economic & financial indicators 板块 (图表密集页)
            indicator_images = []
            if _is_indicators_section(title_eng, section):
                section = "Indicators"
                indicator_images = _extract_indicator_images(
                    z, opf_path, raw_html, issue_date, art_id, image_dir
                )
                if indicator_images:
                    print(f"  📊 {art_id} indicators 板块, 提取 {len(indicator_images)} 张图表")

            # 分类: news (快讯, 只译标题) vs analysis (中文解读)
            # indicators 板块强制归 news (图片本身就是内容, 不需要解读)
            if section == "Indicators":
                category = "news"
            else:
                category = "news" if section.strip().lower() in _NEWS_CATEGORIES else "analysis"

            section_counts[section] = section_counts.get(section, 0) + 1

            # 构造文章 dict
            article = {
                "id": art_id,
                "section": section,
                "category": category,
                "title": title_eng,
                "title_zh": f"【大模型编译中】{title_eng}",
                "url": f"https://www.economist.com/node/{issue_date}/{art_counter}",
                "summary_md": "### 编译中...\n正在请求 LLM 集群进行中文解读编译,请稍候。",
                "content_raw": cleaned_text,
            }
            if cartoon_images:
                article["cartoon_images"] = cartoon_images
            if indicator_images:
                article["indicator_images"] = indicator_images
            articles.append(article)
            art_counter += 1

        # 4. 兜底: spine 中有但 NCX 中没有的 (可能是封面/版权, 不主动收录)
        #     不再补漏, 因为 NCX 才是真源

    # 打印统计 (NCX vs 解析数对比)
    parsed_count = len(articles)
    toc_skipped_count = len(toc_skipped)
    toc_total_real = toc_total - toc_skipped_count
    match_status = "✅ 完全对齐" if parsed_count == toc_total_real else f"⚠️ 差 {toc_total_real - parsed_count} 篇"
    print(f"  📊 拆解统计: 目录 {toc_total} 篇, 跳过 {toc_skipped_count} 索引页, 解析 {parsed_count} 篇 ({match_status})")
    if toc_missing:
        print(f"  ❌ 目录有但 zip 缺失: {len(toc_missing)} 个文件")
        for fp in toc_missing[:3]:
            print(f"     · {fp}")
    if toc_skipped:
        print(f"  🗑  跳过的索引页 (前 5):")
        for title, sec, n_chars in toc_skipped[:5]:
            print(f"     · [{sec}] {title} ({n_chars} 字)")
    if section_counts:
        print(f"  📑 板块分布:")
        for sec, n in sorted(section_counts.items(), key=lambda x: -x[1])[:10]:
            print(f"     · {sec}: {n}")
    cartoon_count = sum(1 for a in articles if a.get("cartoon_images"))
    if cartoon_count:
        print(f"  🎨 漫画专栏: {cartoon_count} 篇 (已提取图片)")

    return {
        "issue_date": issue_date,
        "issue_id": f"issue_{issue_date}",
        "issue_cover": cover_saved_path,
        "articles": articles,
    }
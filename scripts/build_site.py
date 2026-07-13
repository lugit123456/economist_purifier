#!/usr/bin/env python3
"""
构建 Netlify 部署包:把项目结构中的可公开资源汇总到 ./site/ 目录。

执行:  python3 scripts/build_site.py
产物:  ./site/  (index.html + assets/ + database.js + images/)

Netlify 配置 (netlify.toml) 会把 ./site/ 作为 publish 目录。
deploy 时自动跑这个脚本,所以 site/ 不用提交到 git。
"""

import re
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SITE = PROJECT_ROOT / "site"
FRONTEND = PROJECT_ROOT / "frontend"


def rewrite_paths(file: Path, old: str, new: str) -> int:
    """把文件中所有 old 替换为 new, 返回替换次数"""
    text = file.read_text(encoding="utf-8")
    new_text = text.replace(old, new)
    if new_text == text:
        return 0
    file.write_text(new_text, encoding="utf-8")
    return text.count(old)


def main():
    print(f"🏗  构建 Netlify 部署包 → {SITE}")

    # 0. 清空旧产物
    if SITE.exists():
        shutil.rmtree(SITE)
    SITE.mkdir(parents=True)

    # 1. index.html (项目根 → site/)
    src = PROJECT_ROOT / "index.html"
    dst = SITE / "index.html"
    shutil.copy(src, dst)
    n = rewrite_paths(dst, "frontend/", "")
    print(f"  ✅ index.html (改写 {n} 处路径)")

    # 2. database.js (frontend/ → site/)
    src = FRONTEND / "database.js"
    if src.exists():
        dst = SITE / "database.js"
        shutil.copy(src, dst)
        # 双重替换: 老格式 (raw/images/) + 新格式 (frontend/images/) 都映射到 images/
        n1 = rewrite_paths(dst, "frontend/images/", "images/")
        n2 = rewrite_paths(dst, "raw/images/", "images/")
        print(f"  ✅ database.js (改写 {n1 + n2} 处路径: frontend={n1}, raw={n2})")
    else:
        print(f"  ⚠️  frontend/database.js 不存在,跳过 (请先跑 kb_agent)")

    # 3. assets/ (frontend/assets/ → site/assets/)
    src = FRONTEND / "assets"
    if src.exists():
        dst = SITE / "assets"
        shutil.copytree(src, dst)
        print(f"  ✅ assets/ ({len(list(dst.iterdir()))} 个文件)")
    else:
        print(f"  ⚠️  frontend/assets/ 不存在")

    # 4. images/ (frontend/images/ → site/images/)
    src = FRONTEND / "images"
    if src.exists():
        dst = SITE / "images"
        shutil.copytree(src, dst)
        # 统计 (排除子目录)
        n_files = sum(1 for _ in dst.rglob("*") if _.is_file())
        print(f"  ✅ images/ ({n_files} 张图片, 含子目录)")
    else:
        print(f"  ⚠️  frontend/images/ 不存在")

    print(f"\n🎉 构建完成: {SITE}")

    # 5. 健壮性检查: 不应有残留的相对路径前缀
    bad_paths = []
    for f in (SITE / "index.html", SITE / "database.js"):
        if not f.exists():
            continue
        text = f.read_text(encoding="utf-8")
        if "frontend/" in text or "../" in text:
            bad_paths.append(f.name)
    if bad_paths:
        print(f"  ⚠️  残留路径引用: {bad_paths}")
        print("     请检查 frontend/database.js 是否最新")

    file_count = sum(1 for _ in SITE.rglob("*") if _.is_file())
    total_kb = sum(f.stat().st_size for f in SITE.rglob("*") if f.is_file()) / 1024
    print(f"   📦 文件总数: {file_count}  ·  总大小: {total_kb:.1f} KB")
    print(f"\n下一步:")
    print(f"  # 1. 本地预览")
    print(f"  cd {SITE.name} && python3 -m http.server 8000")
    print(f"  # 2. 部署到 Netlify (推荐先登录 netlify-cli)")
    print(f"  netlify deploy --dir={SITE.name} --prod")
    print(f"  # 或直接把 {SITE.name} 拖到 https://app.netlify.com/drop")


if __name__ == "__main__":
    main()
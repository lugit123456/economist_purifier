#!/usr/bin/env python3
"""
一键发布流程: 编译 → commit → push → Netlify 自动部署

架构 (已重构):
  - Netlify publish = 项目根, 不跑 build
  - index.html / frontend/* 等路径直接可用, 不需要路径改写
  - _redirects 文件挡住 .env / backend / raw / output 等敏感目录
  - site/ 和 build_site.py 已废弃

用法:
  python3 scripts/publish.py              # 完整流程
  python3 scripts/publish.py --no-compile # 跳过编译 (kb_agent 已跑过)
  python3 scripts/publish.py --no-push    # 只 commit (本机推送另做)

环境变量:
  SKIP_COMMIT=1    # 跳过 git commit (例如临时)
  FORCE_PUSH=1     # 强制 push (有冲突时)
  GIT_REMOTE_URL   # 首次 push 时自动配置 origin
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(cmd: list[str], check=True, cwd=None, capture=False) -> subprocess.CompletedProcess:
    """执行 shell 命令"""
    print(f'  $ {" ".join(cmd)}')
    result = subprocess.run(
        cmd, cwd=cwd or ROOT,
        capture_output=capture, text=True
    )
    if check and result.returncode != 0:
        if capture and result.stderr:
            print(result.stderr)
        print(f'  ❌ 失败,退出码 {result.returncode}')
        sys.exit(result.returncode)
    return result


def get_new_issues() -> list[str]:
    """从 database.js 提取最新一期日期, 用于 commit message"""
    db = ROOT / 'frontend' / 'database.js'
    if not db.exists():
        return []
    try:
        text = db.read_text(encoding='utf-8')
        return sorted(set(re.findall(r'"issue_date": "(\d{4}-\d{2}-\d{2})"', text)))
    except Exception:
        return []


def step_compile():
    print('🚀 Step 1/3: 编译新一期 ...')
    run(['python3', '-m', 'backend.kb_agent', '--once'])


def step_commit():
    print('\n📝 Step 2/3: 提交到 git ...')
    # git add -A 自动覆盖关键文件, 尊重 .gitignore (不会加 .env/raw/output)
    run(['git', 'add', '-A'])

    status = run(['git', 'diff', '--cached', '--quiet'], check=False)
    if status.returncode == 0:
        print('  (无新变更,跳过 commit)')
        return False

    issues = get_new_issues()
    latest = issues[-1] if issues else ''
    if latest:
        msg = f'auto: 更新双语研报库 ({latest})' if len(issues) == 1 \
            else f'auto: 更新双语研报库 ({len(issues)} 期, 最新 {latest})'
    else:
        msg = 'auto: 更新双语研报库'
    print(f'  💬 Commit message: {msg}')
    run(['git', 'commit', '-m', msg])
    return True


def step_push():
    print('\n📤 Step 3/3: 推送到 origin (触发 Netlify 自动部署) ...')

    # 检查 remote, 没配则从 .env 读 GIT_REMOTE_URL 自动添加
    remotes_output = run(['git', 'remote', '-v'], check=False, capture=True).stdout
    if 'origin' not in remotes_output:
        git_url = os.getenv('GIT_REMOTE_URL', '').strip()
        if not git_url:
            print('  ⚠️  未配置 git remote "origin",跳过 push')
            print('     设置方法 (.env): GIT_REMOTE_URL=https://github.com/<you>/<repo>.git')
            return
        print(f'  📡 自动配置 git remote: {git_url}')
        run(['git', 'remote', 'add', 'origin', git_url])

    branch = run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                 capture=True).stdout.strip() or 'main'
    run(['git', 'push', 'origin', branch])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-compile', action='store_true',
                        help='跳过编译步骤 (kb_agent 已单独跑过)')
    parser.add_argument('--no-push', action='store_true',
                        help='跳过 git push (本地调试场景)')
    args = parser.parse_args()

    print('🚀 economist_purifier 一键发布\n')

    if not args.no_compile:
        step_compile()

    if not os.environ.get('SKIP_COMMIT'):
        step_commit()

    if not args.no_push:
        step_push()

    print('\n✅ 全部完成!')
    print('   Netlify 检测到 push 后会自动部署项目根 (无需 build)')
    print('   大约 30-60 秒后生效 ↓')


if __name__ == '__main__':
    main()
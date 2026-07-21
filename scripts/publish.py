#!/usr/bin/env python3
"""
一键发布流程: 编译 → (commit? git push / Netlify CLI 直推) → 部署上线

双部署模式:
  - Netlify CLI 直推 (推荐, 多机协作):  避开 git, 避免多机器并发 push 冲突
  - GitHub → Netlify webhook (兼容保留): 走 git, 历史可追溯

Netlify CLI 模式 (.env 配 NETLIFY_AUTH_TOKEN + NETLIFY_SITE_ID 即启用):
  $ npm install -g netlify-cli       # 一次性
  $ netlify login                    # 一次性 (或用 NETLIFY_AUTH_TOKEN)
  $ python3 scripts/publish.py       # 自动检测 → 直推 Netlify

GitHub 模式 (兼容旧流程):
  $ python3 scripts/publish.py       # 无 Netlify 环境变量 → 走 git push

用法:
  python3 scripts/publish.py              # 完整流程
  python3 scripts/publish.py --no-compile # 跳过编译 (kb_agent 已跑过)
  python3 scripts/publish.py --no-push    # 只 commit / 不部署

环境变量:
  SKIP_COMMIT=1        # 跳过 git commit (Netlify 模式下不生效)
  FORCE_PUSH=1         # 强制 git push (有冲突时, 仅 GitHub 模式)
  GIT_REMOTE_URL       # 首次 push 时自动配置 origin (仅 GitHub 模式)
  NETLIFY_AUTH_TOKEN   # Netlify Personal Access Token (推荐)
  NETLIFY_SITE_ID      # Netlify 站点 ID
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
    _env_file = ROOT / '.env'
    if _env_file.exists():
        load_dotenv(_env_file)
except ImportError:
    pass  # 没有 dotenv 也能跑, 但要从 shell 导出环境变量


def run(cmd: list[str], check=True, cwd=None, capture=False, env=None) -> subprocess.CompletedProcess:
    """执行 shell 命令"""
    print(f'  $ {" ".join(cmd)}')
    result = subprocess.run(
        cmd, cwd=cwd or ROOT,
        capture_output=capture, text=True,
        env=env,
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


def _netlify_env_ready() -> bool:
    """检查 Netlify CLI 直推所需的环境变量是否就绪"""
    return bool(os.getenv('NETLIFY_AUTH_TOKEN', '').strip()
                and os.getenv('NETLIFY_SITE_ID', '').strip())


# Netlify 部署需要的所有文件 (白名单, 不在列表里的绝不上传)
_NETLIFY_DEPLOY_FILES = [
    'index.html',
    '_redirects',
    'netlify.toml',
    'frontend',         # 含 database.js + assets/ + images/
]


def _stage_for_netlify() -> Path:
    """
    复制白名单文件到临时 staging 目录, Netlify 只从这个目录部署

    这样从源头上就避免了 output/ backend/ raw/ scripts/ .env state.db 等敏感/本地产物
    被推到 Netlify, 完全不依赖 .netlifyignore 是否生效
    """
    stage = Path(tempfile.mkdtemp(prefix='netlify-stage-', dir='/tmp'))
    for name in _NETLIFY_DEPLOY_FILES:
        src = ROOT / name
        dst = stage / name
        if src.is_dir():
            shutil.copytree(src, dst)
        elif src.exists():
            shutil.copy2(src, dst)
        else:
            print(f'  ⚠️  跳过 {name} (本地不存在)')
    return stage


def _deploy_to_netlify():
    """用 Netlify CLI 直推 staging 目录到生产环境"""
    print('\n📤 Step 3/3: 部署到 Netlify (直推模式, 跳过 GitHub) ...')

    # 检查 netlify CLI 是否可用
    nl_check = run(['which', 'netlify'], check=False, capture=True).stdout.strip()
    if not nl_check:
        print('  ❌ netlify CLI 未安装')
        print('     安装: npm install -g netlify-cli')
        print('     登录: netlify login  (或在 .env 配 NETLIFY_AUTH_TOKEN)')
        sys.exit(1)

    site_id = os.getenv('NETLIFY_SITE_ID', '').strip()
    auth_token = os.getenv('NETLIFY_AUTH_TOKEN', '').strip()

    # ★ 关键: 新版 netlify-cli (>=10) 不再支持 --auth-token,
    #   改成 --auth (空格分隔) 或直接靠 NETLIFY_AUTH_TOKEN 环境变量
    #   用 env var 方式最稳, 跨版本都通
    env = os.environ.copy()
    env['NETLIFY_AUTH_TOKEN'] = auth_token

    # 先建 staging 目录, 只放白名单文件
    print('  📦 构建 staging 目录 (白名单文件)...')
    stage = _stage_for_netlify()
    print(f'     {stage}')

    try:
        run(
            [
                'netlify', 'deploy',
                '--prod',
                f'--dir={stage}',
                f'--site={site_id}',
            ],
            env=env,
        )
    finally:
        # 清理 staging (无论成功失败都删)
        shutil.rmtree(stage, ignore_errors=True)

    print('\n✅ Netlify 部署完成!')
    print('   注: 本次直推覆盖了 Netlify 上现有版本, 但旧版会继续服务直到原子切换完成')
    print('   30-60 秒后访问 ↓')


def _push_to_git():
    """走 GitHub → Netlify webhook 老路 (兼容保留)"""
    print('\n📤 Step 3/3: 推送到 origin (GitHub → Netlify webhook) ...')

    # 检查 remote, 没配则从 .env 读 GIT_REMOTE_URL 自动添加
    remotes_output = run(['git', 'remote', '-v'], check=False, capture=True).stdout
    if 'origin' not in remotes_output:
        git_url = os.getenv('GIT_REMOTE_URL', '').strip()
        if not git_url:
            print('  ⚠️  未配置 git remote "origin",跳过 push')
            print('     设置方法 (.env): GIT_REMOTE_URL=https://github.com/<you>/<repo>.git')
            print('     或者配 NETLIFY_AUTH_TOKEN + NETLIFY_SITE_ID 切换到直推模式')
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
                        help='跳过部署步骤 (本地调试场景)')
    parser.add_argument('--force-push-empty', action='store_true',
                        help='即使没有新变更也强制部署 '
                             '(默认行为:无变更则跳过)')
    args = parser.parse_args()

    print('🚀 economist_purifier 一键发布\n')

    if not args.no_compile:
        step_compile()

    # Netlify 直推模式: 不需要 git commit, 直接部署; GitHub 模式: 走 commit + push
    if not args.no_push:
        if _netlify_env_ready():
            _deploy_to_netlify()
        else:
            has_commit = False
            if not os.environ.get('SKIP_COMMIT'):
                has_commit = step_commit()
            if has_commit or args.force_push_empty:
                _push_to_git()
            else:
                print('\n📤 Step 3/3: 推送到 origin ...')
                print('  ⏭️  (无新 commit,跳过 push · 加 --force-push-empty 强制推送)')

    print('\n✅ 全部完成!')
    if _netlify_env_ready():
        print('   模式: Netlify CLI 直推 (绕过 GitHub, 多机器无冲突)')
    else:
        print('   模式: GitHub → Netlify webhook (经典流程)')
    print('   30-60 秒后访问 ↓')


if __name__ == '__main__':
    main()
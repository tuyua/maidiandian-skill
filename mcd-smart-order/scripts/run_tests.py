#!/usr/bin/env python3
"""mcd-smart-order 一键回归入口（只用标准库，不引入 pytest）。

用法：
    python run_tests.py                 跑一遍全部离线测试
    python run_tests.py --watch         监听文件变动，改完自动重跑
    python run_tests.py --check-sync    额外核对「源目录 vs 安装目录」是否漂移
    python run_tests.py --sync          测试通过后同步安装目录并重新打包 dist

编排说明：test_*.py 本身是独立可执行脚本（内部用 subprocess 调被测脚本），
这里只负责发现、执行、汇总，不改动它们的断言逻辑。
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPTS_DIR.parent
INSTALL_DIR = Path.home() / ".workbuddy" / "skills" / "mcd-smart-order"
DIST_DIR = SKILL_DIR.parent / "dist"
GREEN, RED, YELLOW, RESET = ("\033[32m", "\033[31m", "\033[33m", "\033[0m") if os.name != "nt" else ("", "", "", "")


def run(cmd, timeout=300):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout)


def run_suite(path):
    """执行一个 test_*.py，返回 (是否通过, 摘要行, 失败详情)。"""
    proc = run([sys.executable, str(path)])
    out = (proc.stdout or "") + (proc.stderr or "")
    summary = next((ln for ln in out.splitlines() if "共" in ln and "项检查" in ln), "")
    ok = "RESULT: ALL PASS" in out and proc.returncode == 0
    detail = "" if ok else out.strip()[-2000:]
    return ok, summary or f"退出码 {proc.returncode}", detail


def file_digest(path):
    return hashlib.md5(Path(path).read_bytes()).hexdigest()


def check_sync():
    """对比源目录与安装目录，发现漏同步/旧文件。"""
    def snapshot(root):
        out = {}
        for p in sorted(Path(root).rglob("*")):
            if p.is_file():
                out[str(p.relative_to(root)).replace("\\", "/")] = file_digest(p)
        return out

    if not INSTALL_DIR.exists():
        return False, f"安装目录不存在：{INSTALL_DIR}", ""

    src, inst = snapshot(SKILL_DIR), snapshot(INSTALL_DIR)
    problems = []
    for rel, dig in src.items():
        if rel not in inst:
            problems.append(f"安装版缺失：{rel}")
        elif inst[rel] != dig:
            problems.append(f"内容不一致：{rel}")
    for rel in inst:
        if rel not in src:
            problems.append(f"安装版多余：{rel}")
    ok = not problems
    return ok, f"同步核对：{len(src)} 个源文件，" + ("全部一致" if ok else f"{len(problems)} 处漂移"), "\n".join("  - " + p for p in problems)


def do_sync():
    """覆盖安装目录，并调用 skill-creator 的 package_skill.py 重新打包。"""
    if INSTALL_DIR.exists():
        shutil.rmtree(INSTALL_DIR)
    shutil.copytree(SKILL_DIR, INSTALL_DIR)

    candidates = sorted((Path.home() / ".workbuddy" / "plugins" / "cache" / "workbuddy-builtin")
                        .glob("skill-skill-creator/*/scripts/package_skill.py"))
    if not candidates:
        return False, f"已同步到 {INSTALL_DIR}，但找不到 package_skill.py，未打包", ""
    pkg = candidates[-1]
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    proc = run([sys.executable, str(pkg), str(SKILL_DIR), str(DIST_DIR)], timeout=120)
    ok = proc.returncode == 0
    return ok, (f"已同步安装目录并打包 → {DIST_DIR}" if ok else "打包失败"), "" if ok else (proc.stdout + proc.stderr)[-1500:]


def watch_files():
    """收集需要监听的文件：脚本 + 文档。"""
    files = list(SCRIPTS_DIR.glob("*.py")) + list((SKILL_DIR / "references").glob("*.md")) + [SKILL_DIR / "SKILL.md"]
    return [f for f in files if f.exists() and f.name != Path(__file__).name]


def snapshot_state(files):
    return {str(f): (f.stat().st_mtime, f.stat().st_size) for f in files}


def run_all(do_sync_check, do_sync_action):
    """跑一轮完整检查，返回是否全绿。"""
    started = time.time()
    results = []

    suites = sorted(SCRIPTS_DIR.glob("test_*.py"))
    # 各套件互不依赖（各自用独立临时文件），并行跑把总耗时压到最慢那个套件，watch 模式下才跟得上改动
    with ThreadPoolExecutor(max_workers=max(1, len(suites))) as pool:
        futures = {pool.submit(run_suite, p): p for p in suites}
        for fut in as_completed(futures):
            ok, summary, detail = fut.result()
            results.append((futures[fut].name, ok, summary, detail))

    for name, ok, summary, detail in sorted(results):
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<28} {summary}")
        if detail:
            print(YELLOW + detail + RESET)

    if do_sync_check:
        ok, summary, detail = check_sync()
        results.append(("sync-check", ok, summary, detail))
        print(f"  [{'PASS' if ok else 'WARN'}] {'sync-check':<28} {summary}")
        if detail:
            print(YELLOW + detail + RESET)

    all_green = all(ok for _, ok, _, _ in results)

    if all_green and do_sync_action:
        ok, summary, detail = do_sync()
        print(f"  [{'PASS' if ok else 'FAIL'}] {'sync+package':<28} {summary}")
        if detail:
            print(YELLOW + detail + RESET)
        all_green = ok

    total = len(results)
    failed_n = sum(1 for _, ok, _, _ in results if not ok)
    color = GREEN if all_green else RED
    print(color + f"\n{'ALL PASS' if all_green else 'HAS FAILURE'}：{total} 个套件，失败 {failed_n} 个，耗时 {time.time() - started:.1f}s" + RESET)
    return all_green


def main():
    parser = argparse.ArgumentParser(description="mcd-smart-order 一键回归")
    parser.add_argument("--watch", action="store_true", help="监听文件变动自动重跑")
    parser.add_argument("--check-sync", action="store_true", help="核对源目录与安装目录是否一致")
    parser.add_argument("--sync", action="store_true", help="全部通过后同步安装目录并重新打包 dist")
    args = parser.parse_args()

    if args.watch:
        files = watch_files()
        print(f"监听 {len(files)} 个文件，改动即重跑（Ctrl+C 退出）")
        last = snapshot_state(files)
        run_all(args.check_sync, args.sync)
        try:
            while True:
                time.sleep(1)
                now = snapshot_state(files)
                if now != last:
                    changed = [Path(k).name for k in now if now.get(k) != last.get(k)]
                    print(f"\n检测到改动：{', '.join(changed)} → 重跑")
                    last = now
                    run_all(args.check_sync, args.sync)
        except KeyboardInterrupt:
            print("\n已停止监听")
            return 0

    return 0 if run_all(args.check_sync, args.sync) else 1


if __name__ == "__main__":
    sys.exit(main())

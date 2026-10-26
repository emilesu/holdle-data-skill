#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发版前自检：SIDE_MODULES 里每个文件都必须真实存在且已入库。

由来（2026-10-26 代码审查）：judge.py 从"单文件模块"变成"模块 + 数据文件"后，
分发清单登记了文件名，但如果提交时漏了 git add，用户端下载会 404，
而 404 **只在用户端表现为"判定少了点东西"，看不出来**——主脚本照样报更新成功。

用法（在 skill 目录下）：
    python3 check_release.py            # 只查本地文件与 git 入库状态
    python3 check_release.py --remote   # 额外探测 GitHub raw 是否已可下载（推送后跑）

退出码 0 = 可以发版；1 = 有问题。
"""
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = "https://raw.githubusercontent.com/emilesu/holdle-data-skill/master/{}"


def read_side_modules():
    src = open(os.path.join(HERE, "holdle_data.py"), encoding="utf-8").read()
    i = src.index("SIDE_MODULES = [")
    j = src.index("]", i)
    block = src[i:j]
    out = []
    for line in block.splitlines()[1:]:
        line = line.strip()
        if line.startswith('"'):
            out.append(line.split('"')[1])
    return out


def main():
    check_remote = "--remote" in sys.argv
    mods = read_side_modules()
    print("SIDE_MODULES 共 %d 个文件" % len(mods))
    ok = True
    # 主脚本自身也算
    for name in ["holdle_data.py", "version.json"] + mods:
        fname = name.split(":")[0]
        path = os.path.join(HERE, fname)
        exists = os.path.exists(path)
        tracked = False
        if exists:
            r = subprocess.run(["git", "ls-files", "--error-unmatch", fname],
                               cwd=HERE, capture_output=True, text=True)
            tracked = (r.returncode == 0)
        size = os.path.getsize(path) if exists else 0
        mark = "✅" if (exists and tracked) else "❌"
        if not (exists and tracked):
            ok = False
        print("  %s %-34s 存在=%-5s 已入库=%-5s %d 字节" % (mark, fname, exists, tracked, size))

    if check_remote:
        import urllib.request
        print("\n探测 GitHub raw（推送后才有意义）：")
        for name in ["holdle_data.py", "version.json"] + mods:
            fname = name.split(":")[0]
            code = "?"
            try:
                req = urllib.request.Request(RAW.format(fname), headers={"User-Agent": "check/1"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    code = r.status
            except Exception as e:
                code = getattr(e, "code", str(e)[:30])
            good = (code == 200)
            if not good:
                ok = False
            print("  %s %-34s HTTP %s" % ("✅" if good else "❌", fname, code))

    # ── 静态检查（2026-10-26 代码审查建议）──
    # 🔴 由来：「删函数没删调用」已发生三次（_is_bj / _emit_finance_unchecked / …）。
    #    这类错误不会在测试里暴露（被 except 吞掉或被提前 return 跳过），
    #    但 F821（未定义名）一查就出来，成本 0 行新代码。
    print("\n静态检查（ruff F821 未定义名 / F401 未用 import / F841 未用变量）：")
    _files = [f for f in ("holdle_data.py", "datasource.py", "judge.py")
              if os.path.exists(os.path.join(HERE, f))]
    try:
        _ruff = shutil.which("ruff")
        if _ruff is None:
            # 优先用项目 venv 里的 ruff
            _cand = os.path.join(os.path.dirname(HERE), "..", ".venv", "bin", "ruff")
            _cand = os.path.abspath(_cand)
            _ruff = _cand if os.path.exists(_cand) else None
        if _ruff:
            _r = subprocess.run([_ruff, "check", "--select", "F821,F401,F841",
                                 "--output-format=concise"] + _files,
                                cwd=HERE, capture_output=True, text=True)
            if _r.returncode == 0:
                print("  ✅ 无未定义名 / 未用 import / 未用变量")
            else:
                ok = False
                print("  ❌ 静态检查未通过：")
                for _l in (_r.stdout or "").strip().splitlines()[:10]:
                    print("     " + _l)
        else:
            print("  ⚠️ 未找到 ruff，跳过静态检查（建议 pip install ruff）")
    except Exception as _e:      # noqa: BLE001
        print("  ⚠️ 静态检查执行失败：%s" % str(_e)[:80])

    print("\n" + ("✅ 可以发版" if ok else "❌ 存在问题，禁止发版"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

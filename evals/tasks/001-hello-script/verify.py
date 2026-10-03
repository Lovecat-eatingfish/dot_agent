# -*- coding: utf-8 -*-
import os, subprocess, sys
from pathlib import Path
ws = Path(os.environ["WORKSPACE"])
f = ws / "hello.py"
assert f.is_file(), "hello.py 不存在于工作目录"
proc = subprocess.run([sys.executable, str(f)], capture_output=True, text=True,
                      encoding="utf-8", errors="replace", timeout=60)
print("stdout:", proc.stdout.strip()[:200])
assert proc.returncode == 0, "运行失败 exit %d" % proc.returncode
assert proc.stdout.strip() == "hello, eval!", "输出不精确: %r" % proc.stdout.strip()
print("PASS")

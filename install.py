#!/usr/bin/env python3
"""
DoiHarvest 一键安装脚本
================================
自动完成：创建虚拟环境 → 安装 Python 依赖 → 安装 Playwright 浏览器 →
检查 Chrome → （可选）安装 MinerU OCR → 生成配置

用法：
    python install.py           # 交互式安装（推荐）
    python install.py --mineru  # 额外自动安装 MinerU（Phase 3 需要）
    python install.py --no-mineru  # 跳过 MinerU
"""

import os
import re
import subprocess
import sys
import venv
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
IS_WINDOWS = os.name == "nt"

# 主程序虚拟环境目录
VENV_DIR = PROJECT_ROOT / ".venv"
# MinerU 独立虚拟环境目录（避免与主程序依赖冲突）
MINERU_VENV_DIR = PROJECT_ROOT / "mineru_env"

# 常用 Chrome 安装路径（Windows）
CHROME_PATHS_WIN = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Users\{}\AppData\Local\Google\Chrome\Application\chrome.exe".format(
        os.environ.get("USERNAME", "")
    ),
]


def venv_python(venv_dir: Path) -> Path:
    """返回虚拟环境里的 Python 可执行文件路径。"""
    if IS_WINDOWS:
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def print_step(msg: str):
    print("\n" + "=" * 60)
    print(f"  {msg}")
    print("=" * 60)


def print_ok(msg: str):
    print(f"  [√] {msg}")


def print_warn(msg: str):
    print(f"  [!] {msg}")


def print_err(msg: str):
    print(f"  [×] {msg}")


def run(cmd, **kwargs):
    """运行命令，返回 (returncode, output)。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return -1, str(e)


def check_python_version() -> bool:
    """检查当前 Python 版本是否在 3.10~3.12。"""
    v = sys.version_info
    ver_str = f"{v.major}.{v.minor}.{v.micro}"
    print_step(f"检查 Python 版本（当前 {ver_str}）")
    if (v.major, v.minor) < (3, 10):
        print_err(f"Python {ver_str} 过旧，需要 3.10 或更高。")
        print_err("请到 https://www.python.org/downloads/ 安装 Python 3.10~3.12。")
        return False
    if (v.major, v.minor) > (3, 12):
        print_warn(f"Python {ver_str} 过新。")
        print_warn("MinerU（Phase 3）在 Windows 上仅支持 Python 3.10~3.12。")
        print_warn("若不用 Phase 3 OCR，可忽略此警告继续；否则请改用 3.10~3.12。")
    else:
        print_ok(f"Python {ver_str} 版本合适")
    return True


def create_venv() -> bool:
    """创建主程序虚拟环境。"""
    print_step("创建虚拟环境 (.venv)")
    if VENV_DIR.exists() and venv_python(VENV_DIR).exists():
        print_ok("虚拟环境已存在，跳过创建")
        return True
    try:
        venv.create(VENV_DIR, with_pip=True)
        print_ok("虚拟环境创建成功")
        return True
    except Exception as e:
        print_err(f"创建虚拟环境失败: {e}")
        return False


def install_requirements() -> bool:
    """安装 requirements.txt 依赖。"""
    print_step("安装 Python 依赖（可能需要几分钟）")
    py = venv_python(VENV_DIR)
    req = PROJECT_ROOT / "requirements.txt"
    if not req.exists():
        print_err("requirements.txt 不存在")
        return False

    # 升级 pip
    run([str(py), "-m", "pip", "install", "--upgrade", "pip"])

    # 安装依赖（用国内镜像加速，失败则回退官方源）
    rc, out = run([str(py), "-m", "pip", "install", "-r", str(req),
                   "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"])
    if rc != 0:
        print_warn("清华镜像安装失败，尝试官方 PyPI 源 ...")
        rc, out = run([str(py), "-m", "pip", "install", "-r", str(req)])
    if rc != 0:
        print_err("依赖安装失败。错误信息：")
        print(out[-800:])
        return False
    print_ok("Python 依赖安装完成")
    return True


def install_playwright() -> bool:
    """安装 Playwright 的 Chromium 浏览器。"""
    print_step("安装 Playwright Chromium 浏览器（约 150MB）")
    py = venv_python(VENV_DIR)
    rc, out = run([str(py), "-m", "playwright", "install", "chromium"])
    if rc != 0:
        print_warn("Playwright Chromium 安装失败（可能是网络问题）")
        print_warn("可稍后手动执行：")
        print(f"    {py} -m playwright install chromium")
        return False
    print_ok("Playwright Chromium 安装完成")
    return True


def check_chrome() -> bool:
    """检查系统是否安装了 Google Chrome（Phase 2 反爬需要）。"""
    print_step("检查 Google Chrome 浏览器")
    if IS_WINDOWS:
        for p in CHROME_PATHS_WIN:
            if Path(p).exists():
                print_ok(f"找到 Chrome: {p}")
                return True
        print_warn("未找到 Google Chrome。Phase 2（部分出版商反爬下载）需要真实 Chrome。")
        print_warn("请到 https://www.google.com/chrome/ 下载安装。")
        return False
    # macOS / Linux
    for p in ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              "/usr/bin/google-chrome", "/usr/bin/chromium-browser"]:
        if Path(p).exists():
            print_ok(f"找到 Chrome: {p}")
            return True
    print_warn("未找到 Chrome，Phase 2 部分功能可能受限。")
    return False


def install_mineru() -> bool:
    """（可选）安装 MinerU OCR 到独立虚拟环境。"""
    print_step("安装 MinerU OCR（Phase 3 需要，首次运行还会下载约 2GB 模型）")
    print("  这一步较大，约 20GB 磁盘空间，请耐心等待。")

    # 创建独立 venv
    if not (MINERU_VENV_DIR.exists() and venv_python(MINERU_VENV_DIR).exists()):
        try:
            venv.create(MINERU_VENV_DIR, with_pip=True)
        except Exception as e:
            print_err(f"创建 MinerU 环境失败: {e}")
            return False

    mpy = venv_python(MINERU_VENV_DIR)
    run([str(mpy), "-m", "pip", "install", "--upgrade", "pip"])

    # 安装 mineru[all]
    rc, out = run([str(mpy), "-m", "pip", "install", "mineru[all]",
                   "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"])
    if rc != 0:
        print_warn("镜像安装失败，尝试官方源 ...")
        rc, out = run([str(mpy), "-m", "pip", "install", "mineru[all]"])
    if rc != 0:
        print_err("MinerU 安装失败。错误信息：")
        print(out[-800:])
        print_warn("可稍后手动安装，参考 README 的「MinerU 安装」章节。")
        return False

    # 更新 config.py 里的 MinerU 路径
    mineru_exe = MINERU_VENV_DIR / ("Scripts" if IS_WINDOWS else "bin") / "mineru"
    if IS_WINDOWS:
        mineru_exe = Path(str(mineru_exe) + ".exe")
    update_config_mineru(mineru_exe)

    print_ok("MinerU 安装完成")
    print_ok(f"MinerU 可执行文件: {mineru_exe}")
    print_warn("首次运行 Phase 3 时 MinerU 会自动下载模型（约 2GB），请保持网络畅通。")
    return True


def update_config_mineru(mineru_exe: Path):
    """把 MinerU 路径写入 config.py。"""
    cfg = PROJECT_ROOT / "config.py"
    if not cfg.exists():
        return
    text = cfg.read_text(encoding="utf-8")
    # 替换 MINERU_EXECUTABLE 行
    new_text, n = re.subn(
        r'^MINERU_EXECUTABLE\s*=.*$',
        f'MINERU_EXECUTABLE = r"{mineru_exe}"',
        text, flags=re.M,
    )
    if n == 0:
        print_warn("未在 config.py 中找到 MINERU_EXECUTABLE，请手动设置。")
        return
    cfg.write_text(new_text, encoding="utf-8")
    print_ok("已更新 config.py 中的 MinerU 路径")


def print_next_steps():
    print_step("安装完成！下一步")
    print("  1. 配置 DeepSeek API Key（Phase 4 必需）：")
    print("     打开 Web 界面 → Phase 4 卡片 → 填入你的 DeepSeek Key")
    print("     （免费申请：https://platform.deepseek.com/ ）")
    print()
    print("  2. 启动程序：")
    if IS_WINDOWS:
        print("     双击 start.bat，或运行: python start.py")
    else:
        print("     运行: python start.py")
    print()
    print("  3. 浏览器会自动打开 http://127.0.0.1:8765")
    print()
    print("详细使用说明见 README.md")


def main():
    # 解析参数
    args = sys.argv[1:]
    do_mineru = None  # None=询问, True/False=强制
    if "--mineru" in args:
        do_mineru = True
    if "--no-mineru" in args:
        do_mineru = False

    print("=" * 60)
    print("  DoiHarvest — 一键安装脚本")
    print("=" * 60)

    # 1. 检查 Python 版本
    if not check_python_version():
        input("\n按回车键退出 ...")
        sys.exit(1)

    # 2. 创建虚拟环境
    if not create_venv():
        input("\n按回车键退出 ...")
        sys.exit(1)

    # 3. 安装依赖
    if not install_requirements():
        input("\n按回车键退出 ...")
        sys.exit(1)

    # 4. 安装 Playwright
    install_playwright()

    # 5. 检查 Chrome
    check_chrome()

    # 6. MinerU（可选）
    if do_mineru is None:
        ans = input("\n是否安装 MinerU OCR（Phase 3 需要，约 20GB 磁盘）？[y/N]: ").strip().lower()
        do_mineru = ans in ("y", "yes")
    if do_mineru:
        install_mineru()
    else:
        print_step("跳过 MinerU 安装")
        print("  Phase 3（PDF→Markdown OCR）需要 MinerU。")
        print("  需要时再运行: python install.py --mineru")

    # 7. 完成
    print_next_steps()
    input("\n按回车键退出 ...")


if __name__ == "__main__":
    main()

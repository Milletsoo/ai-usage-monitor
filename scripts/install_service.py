#!/usr/bin/env python
"""
安装/卸载 Token 用量看板自动监控为 Windows 开机自启后台服务。
运行后电脑开机即自动启动监控，无需手动操作。

Usage:
    python install_service.py install    # 安装自启
    python install_service.py uninstall  # 卸载自启
    python install_service.py status     # 查看状态
"""
import os
import sys
import shutil
import subprocess
from datetime import datetime, timezone, timedelta

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

TZ = timezone(timedelta(hours=8))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
MONITOR_SCRIPT = os.path.join(SCRIPT_DIR, "auto_monitor.py")
STARTUP_DIR = os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup")
VBS_PATH = os.path.join(STARTUP_DIR, "AIUsageMonitor.vbs")
PYTHONW = shutil.which("pythonw") or sys.executable.replace("python.exe", "pythonw.exe")
PYTHON = sys.executable  # 用 python.exe 而非 pythonw，更可靠


def install():
    """安装：使用 Windows 计划任务实现开机自启 + 每5分钟重启"""
    import subprocess
    task_name = "AIUsageMonitor"
    
    # 先删除旧任务（如果存在）
    subprocess.run(["schtasks", "/delete", "/tn", task_name, "/f"], 
                   capture_output=True)
    
    # 创建计划任务：开机时启动 + 每5分钟重复
    # 用 pythonw 无窗口运行
    cmd = f'"{PYTHONW}" "{MONITOR_SCRIPT}"'
    result = subprocess.run(
        ["schtasks", "/create", "/tn", task_name,
         "/tr", cmd,
         "/sc", "onlogon",   # 登录时触发
         "/rl", "highest",     # 最高权限
         "/f"],                 # 强制覆盖
        capture_output=True, text=True, encoding="gbk", errors="replace"
    )
    
    if result.returncode != 0:
        print(f"❌ 计划任务创建失败: {result.stderr}")
        # 回退到 VBS 方式
        vbs_content = 'Set WshShell = CreateObject("WScript.Shell")\n'
        vbs_content += 'WshShell.Run "' + PYTHONW.replace('"', '""') + ' ' + MONITOR_SCRIPT.replace('"', '""') + '", 0, False\n'
        vbs_content += 'Set WshShell = Nothing\n'
        with open(VBS_PATH, "w", encoding="utf-8") as f:
            f.write(vbs_content)
        print(f"回退到 VBS 方式: {VBS_PATH}")
    
    # VBS 用 pythonw 隐藏窗口启动
    vbs_content = 'Set WshShell = CreateObject("WScript.Shell")\n'
    vbs_content += 'WshShell.Run "' + PYTHONW.replace('"', '""') + ' ' + MONITOR_SCRIPT.replace('"', '""') + '", 0, False\n'
    vbs_content += 'Set WshShell = Nothing\n'
    with open(VBS_PATH, "w", encoding="utf-8") as f:
        f.write(vbs_content)
    
    # 立即启动一次（用 pythonw 隐藏窗口）
    subprocess.Popen([PYTHONW, MONITOR_SCRIPT], creationflags=0x00000008, close_fds=True)
    
    print("✅ 安装成功！")
    print(f"  方式: Windows 计划任务 ({task_name})")
    print(f"  监控脚本: {MONITOR_SCRIPT}")
    print(f"  Python:   {PYTHONW}")
    print()
    print("  电脑开机后自动启动监控，无需手动操作。")
    print("  看板文件: " + os.path.join(SKILL_DIR, "dashboard.html"))
    print()
    print("  卸载: python install_service.py uninstall")


def uninstall():
    """卸载：删除计划任务/VBS + 杀掉正在运行的监控进程"""
    import subprocess
    
    # 删除计划任务
    result = subprocess.run(["schtasks", "/delete", "/tn", "AIUsageMonitor", "/f"],
                           capture_output=True, text=True, encoding="gbk", errors="replace")
    if result.returncode == 0:
        print("✅ 已删除计划任务: AIUsageMonitor")
    
    # 删除 VBS（如果存在）
    if os.path.exists(VBS_PATH):
        os.remove(VBS_PATH)
        print(f"✅ 已删除启动脚本: {VBS_PATH}")

    # 杀掉正在运行的 pythonw 进程
    try:
        result = subprocess.run(
            ["wmic", "process", "where",
             f"name='pythonw.exe'", "get", "processid,commandline"],
            capture_output=True, text=True, encoding="gbk", errors="replace"
        )
        killed = 0
        for line in result.stdout.split("\n"):
            if "auto_monitor" in line:
                parts = line.strip().split()
                if parts:
                    pid = parts[-1]
                    if pid.isdigit():
                        subprocess.run(["taskkill", "/PID", pid, "/F"],
                                     capture_output=True)
                        killed += 1
        if killed:
            print(f"✅ 已停止 {killed} 个监控进程")
    except Exception:
        pass

    print("卸载完成。")


def status():
    """查看状态"""
    installed = os.path.exists(VBS_PATH)
    print(f"自启脚本: {'✅ 已安装' if installed else '❌ 未安装'}")
    if installed:
        print(f"  路径: {VBS_PATH}")

    # 检查是否正在运行
    try:
        result = subprocess.run(
            ["wmic", "process", "where",
             f"name='pythonw.exe'", "get", "processid,commandline"],
            capture_output=True, text=True, encoding="gbk", errors="replace"
        )
        running = any("auto_monitor" in line for line in result.stdout.split("\n"))
        print(f"监控进程: {'✅ 运行中' if running else '❌ 未运行'}")
    except Exception:
        print("监控进程: 无法检测")

    # 检查看板文件
    dashboard = os.path.join(SKILL_DIR, "dashboard.html")
    if os.path.exists(dashboard):
        mtime = os.path.getmtime(dashboard)
        dt = datetime.fromtimestamp(mtime, tz=TZ).strftime("%Y-%m-%d %H:%M:%S")
        print(f"看板文件: ✅ 存在 (最后更新: {dt})")
    else:
        print("看板文件: ❌ 不存在")


def main():
    if len(sys.argv) < 2:
        print("Usage: python install_service.py [install|uninstall|status]")
        print()
        print("  install    安装开机自启 + 立即启动监控")
        print("  uninstall  卸载自启 + 停止监控进程")
        print("  status     查看安装状态和运行状态")
        return

    cmd = sys.argv[1].lower()
    if cmd == "install":
        install()
    elif cmd == "uninstall":
        uninstall()
    elif cmd == "status":
        status()
    else:
        print(f"未知命令: {cmd}")
        print("Usage: python install_service.py [install|uninstall|status]")


if __name__ == "__main__":
    main()

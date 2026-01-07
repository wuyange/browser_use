from browser_use import Agent, BrowserSession, ChatOpenAI
import asyncio
import os
import re
import socket
import tempfile
import shutil
import subprocess
import platform
import glob
import json
import time
from datetime import datetime
import pytest


# ==================== 日志工具 ====================

class Timer:
    """简单的计时器"""
    def __init__(self, name: str):
        self.name = name
        self.start_time = None
        
    def __enter__(self):
        self.start_time = time.time()
        print(f"[{self._now()}] 开始: {self.name}")
        return self
        
    def __exit__(self, *args):
        elapsed = time.time() - self.start_time
        print(f"[{self._now()}] 完成: {self.name} (耗时: {elapsed:.2f}s)")
        
    def _now(self):
        return datetime.now().strftime("%H:%M:%S")


# ==================== 浏览器工具函数 ====================

def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def get_chrome_path() -> str | None:
    """自动检测 Chrome/Chromium 路径"""
    # 1. 环境变量
    chrome_path = os.environ.get('CHROME_PATH')
    if chrome_path and os.path.exists(chrome_path):
        return chrome_path
    
    system = platform.system()
    
    # 2. Playwright 安装
    playwright_path = os.environ.get('PLAYWRIGHT_BROWSERS_PATH')
    if not playwright_path:
        if system == 'Windows':
            playwright_path = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'ms-playwright')
        elif system == 'Darwin':
            playwright_path = os.path.expanduser('~/Library/Caches/ms-playwright')
        else:
            playwright_path = os.path.expanduser('~/.cache/ms-playwright')
    
    if playwright_path and os.path.exists(playwright_path):
        if system == 'Windows':
            patterns = [
                os.path.join(playwright_path, 'chromium-*', 'chrome-win64', 'chrome.exe'),
                os.path.join(playwright_path, 'chromium-*', 'chrome-win', 'chrome.exe'),
            ]
        elif system == 'Darwin':
            patterns = [os.path.join(playwright_path, 'chromium-*', 'chrome-mac', 'Chromium.app', 'Contents', 'MacOS', 'Chromium')]
        else:
            patterns = [os.path.join(playwright_path, 'chromium-*', 'chrome-linux', 'chrome')]
        
        for pattern in patterns:
            matches = glob.glob(pattern)
            if matches:
                matches.sort(reverse=True)
                return matches[0]
    
    # 3. 系统安装
    if system == 'Windows':
        possible_paths = [
            os.path.expandvars(r'%PROGRAMFILES%\Google\Chrome\Application\chrome.exe'),
            os.path.expandvars(r'%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe'),
            os.path.expandvars(r'%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe'),
        ]
    elif system == 'Darwin':
        possible_paths = ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome']
    else:
        possible_paths = ['/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser']
    
    for path in possible_paths:
        if os.path.exists(path):
            return path
    return None


async def start_browser_and_get_ws_url(headless: bool = False) -> tuple:
    """启动浏览器并从 stderr 获取 WebSocket URL"""
    browser_path = get_chrome_path()
    if not browser_path:
        raise RuntimeError("未找到 Chrome，请设置 CHROME_PATH 或安装 Chrome")
    
    port = find_free_port()
    temp_dir = tempfile.mkdtemp(prefix='browseruse-')
    
    args = [
        browser_path,
        f'--remote-debugging-port={port}',
        f'--user-data-dir={temp_dir}',
        '--no-first-run',
        '--no-default-browser-check',
        '--disable-background-networking',
        '--disable-sync',
    ]
    if headless:
        args.append('--headless=new')
    
    proc = await asyncio.create_subprocess_exec(*args, stderr=asyncio.subprocess.PIPE)
    
    ws_url = None
    start_time = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start_time < 30:
        try:
            line = await asyncio.wait_for(proc.stderr.readline(), timeout=1)
            if line:
                match = re.search(r'DevTools listening on (ws://[^\s]+)', line.decode(errors='ignore'))
                if match:
                    ws_url = match.group(1)
                    break
        except asyncio.TimeoutError:
            if proc.returncode is not None:
                raise RuntimeError(f"浏览器退出，返回码: {proc.returncode}")
    
    if not ws_url:
        proc.terminate()
        await proc.wait()
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise RuntimeError("无法获取 WebSocket URL")
    
    return proc, ws_url, temp_dir


async def cleanup_browser(proc, temp_dir):
    if proc and proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
    if temp_dir:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ==================== 报告功能 ====================

def save_report(history, output_dir: str = "reports"):
    """
    保存执行报告
    
    Args:
        history: AgentHistory 对象
        output_dir: 报告输出目录
    """
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # 1. 保存 JSON 报告
    json_path = os.path.join(output_dir, f"report_{timestamp}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        f.write(history.model_dump_json(indent=2))
    print(f"报告已保存: {json_path}")
    
    # 2. 保存可读的摘要报告
    summary_path = os.path.join(output_dir, f"summary_{timestamp}.txt")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(f"执行报告 - {timestamp}\n")
        f.write("=" * 50 + "\n\n")
        f.write(f"任务完成: {history.is_done()}\n")
        f.write(f"任务成功: {history.is_successful()}\n")
        f.write(f"最终结果: {history.final_result()}\n\n")
        
        f.write("执行步骤:\n")
        f.write("-" * 50 + "\n")
        for i, step in enumerate(history.steps, 1):
            f.write(f"\n步骤 {i}:\n")
            if hasattr(step, 'action') and step.action:
                f.write(f"  操作: {step.action}\n")
            if hasattr(step, 'result') and step.result:
                f.write(f"  结果: {step.result}\n")
    print(f"摘要已保存: {summary_path}")


# ==================== LLM 配置 ====================

llm = ChatOpenAI(
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    model="qwen-turbo",
)


# ==================== 测试用例 ====================

@pytest.mark.asyncio
async def test_baidu_search():
    """测试百度搜索"""
    total_start = time.time()
    print(f"\n{'='*60}")
    print(f"[开始测试] {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*60}")
    
    # 1. 启动浏览器
    with Timer("启动浏览器并获取 WebSocket URL"):
        proc, ws_url, temp_dir = await start_browser_and_get_ws_url()
    
    # 2. 创建 BrowserSession
    with Timer("创建 BrowserSession"):
        browser_session = BrowserSession(
            cdp_url=ws_url,
            keep_alive=False,
        )
    
    try:
        # 3. 创建 Agent
        with Timer("创建 Agent"):
            agent = Agent(
                task="""
                请执行以下步骤：
                1. 打开百度首页 https://www.baidu.com
                2. 在搜索框中输入 "deepseek"
                3. 点击搜索按钮进行搜索
                4. 打印搜索结果页面的标题
                """,
                llm=llm,
                browser=browser_session,
                use_vision=False,
            )
        
        # 4. 运行 Agent
        with Timer("Agent.run() 执行任务"):
            history = await agent.run()
        
        print(f"\n任务完成: {history.is_done()}")
        print(f"结果: {history.final_result()}")
        
        # 5. 保存报告
        with Timer("保存报告"):
            save_report(history)
        
        assert history.is_done()
        
    finally:
        print(f"\n{'-'*60}")
        print("开始清理资源...")
        
        # 6. 关闭 BrowserSession
        with Timer("关闭 BrowserSession (browser_session.stop)"):
            try:
                await browser_session.stop()
            except Exception as e:
                print(f"  警告: {e}")
        
        # 7. 清理浏览器进程
        with Timer("清理浏览器进程"):
            await cleanup_browser(proc, temp_dir)
    
    total_elapsed = time.time() - total_start
    print(f"\n{'='*60}")
    print(f"[测试完成] 总耗时: {total_elapsed:.2f}s")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    asyncio.run(test_baidu_search())
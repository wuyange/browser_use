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
import pytest


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
    proc, ws_url, temp_dir = await start_browser_and_get_ws_url()
    browser_session = BrowserSession(
        cdp_url=ws_url,
        keep_alive=False,  # 不保持连接
    )
    
    try:
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
        history = await agent.run()
        print(f"任务完成: {history.is_done()}")
        print(f"结果: {history.final_result()}")
        assert history.is_done()
    finally:
        # 先关闭 browser_use 的会话
        try:
            await browser_session.stop()
        except Exception:
            pass
        # 再清理浏览器进程
        await cleanup_browser(proc, temp_dir)


if __name__ == "__main__":
    asyncio.run(test_baidu_search())
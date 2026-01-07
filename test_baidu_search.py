"""
使用 browser_use 和 pytest 编写的百度搜索测试用例
功能：打开百度，搜索 deepseek

解决的问题：
- 新版 Chrome 的 CDP HTTP 端点 /json/version 返回 503
- 通过从 stderr 捕获 WebSocket URL 来绕过这个问题
"""

import asyncio
import os
import re
import socket
import tempfile
import shutil
import subprocess
import sys
import pytest
from browser_use import Agent, BrowserSession, ChatOpenAI


# ==================== 浏览器管理工具函数 ====================

def find_free_port() -> int:
    """查找空闲端口"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def get_chrome_path() -> str | None:
    """
    自动检测 Chrome/Chromium 浏览器路径
    优先级：环境变量 > Playwright 安装 > 系统安装
    """
    import platform
    import glob
    
    # 1. 优先使用环境变量指定的路径
    chrome_path = os.environ.get('CHROME_PATH')
    if chrome_path and os.path.exists(chrome_path):
        return chrome_path
    
    system = platform.system()
    
    # 2. 查找 Playwright 安装的浏览器
    playwright_path = os.environ.get('PLAYWRIGHT_BROWSERS_PATH')
    if not playwright_path:
        if system == 'Windows':
            playwright_path = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'ms-playwright')
        elif system == 'Darwin':  # macOS
            playwright_path = os.path.expanduser('~/Library/Caches/ms-playwright')
        else:  # Linux
            playwright_path = os.path.expanduser('~/.cache/ms-playwright')
    
    if playwright_path and os.path.exists(playwright_path):
        # 查找所有可能的 Chromium 路径模式
        if system == 'Windows':
            patterns = [
                os.path.join(playwright_path, 'chromium-*', 'chrome-win64', 'chrome.exe'),
                os.path.join(playwright_path, 'chromium-*', 'chrome-win', 'chrome.exe'),
            ]
        elif system == 'Darwin':
            patterns = [
                os.path.join(playwright_path, 'chromium-*', 'chrome-mac', 'Chromium.app', 'Contents', 'MacOS', 'Chromium'),
            ]
        else:  # Linux
            patterns = [
                os.path.join(playwright_path, 'chromium-*', 'chrome-linux', 'chrome'),
            ]
        
        for pattern in patterns:
            matches = glob.glob(pattern)
            if matches:
                matches.sort(reverse=True)  # 使用最新版本
                return matches[0]
    
    # 3. 查找系统安装的浏览器
    if system == 'Windows':
        possible_paths = [
            os.path.expandvars(r'%PROGRAMFILES%\Google\Chrome\Application\chrome.exe'),
            os.path.expandvars(r'%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe'),
            os.path.expandvars(r'%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe'),
            os.path.expandvars(r'%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe'),
            os.path.expandvars(r'%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe'),
        ]
    elif system == 'Darwin':
        possible_paths = [
            '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
            '/Applications/Chromium.app/Contents/MacOS/Chromium',
            '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
        ]
    else:  # Linux
        possible_paths = [
            '/usr/bin/google-chrome-stable',
            '/usr/bin/google-chrome',
            '/usr/bin/chromium',
            '/usr/bin/chromium-browser',
            '/snap/bin/chromium',
        ]
    
    for path in possible_paths:
        if os.path.exists(path):
            return path
    
    # 4. 尝试使用 which/where 命令查找
    try:
        cmd = 'where' if system == 'Windows' else 'which'
        for browser in ['chrome', 'google-chrome', 'chromium', 'chromium-browser']:
            result = subprocess.run([cmd, browser], capture_output=True, text=True)
            if result.returncode == 0:
                path = result.stdout.strip().split('\n')[0]
                if os.path.exists(path):
                    return path
    except Exception:
        pass
    
    return None


async def start_browser_and_get_ws_url(headless: bool = False, use_default_profile: bool = False) -> tuple:
    """
    启动浏览器并从 stderr 获取 WebSocket URL
    
    Args:
        headless: 是否以无头模式运行
        use_default_profile: 是否使用默认配置文件（保留登录状态）
    
    Returns:
        (process, ws_url, temp_dir) 元组，使用默认配置时 temp_dir 为 None
    
    Raises:
        RuntimeError: 如果找不到浏览器或无法获取 WebSocket URL
    """
    browser_path = get_chrome_path()
    if not browser_path:
        raise RuntimeError(
            "未找到 Chrome/Chromium 浏览器。\n"
            "请通过以下方式之一安装：\n"
            "1. 安装 Google Chrome: https://www.google.com/chrome/\n"
            "2. 使用 Playwright 安装: uv run playwright install chromium\n"
            "3. 设置环境变量 CHROME_PATH 指向浏览器可执行文件"
        )
    
    port = find_free_port()
    
    # 选择用户数据目录
    if use_default_profile:
        # 使用默认配置，保留登录状态
        # 注意：需要先关闭所有 Chrome 窗口
        import platform
        system = platform.system()
        if system == 'Windows':
            user_data_dir = os.path.expandvars(r'%LOCALAPPDATA%\Google\Chrome\User Data')
        elif system == 'Darwin':
            user_data_dir = os.path.expanduser('~/Library/Application Support/Google/Chrome')
        else:
            user_data_dir = os.path.expanduser('~/.config/google-chrome')
        temp_dir = None  # 不删除默认配置
        print(f"使用默认配置: {user_data_dir}")
    else:
        # 使用临时目录（干净环境）
        temp_dir = tempfile.mkdtemp(prefix='browseruse-')
        user_data_dir = temp_dir
        print(f"临时目录: {temp_dir}")
    
    print(f"使用浏览器: {browser_path}")
    print(f"调试端口: {port}")
    
    # 构建启动参数
    args = [
        browser_path,
        f'--remote-debugging-port={port}',
        f'--user-data-dir={user_data_dir}',
        '--no-first-run',
        '--no-default-browser-check',
        '--disable-background-networking',
        '--disable-sync',
        '--disable-translate',
        '--metrics-recording-only',
    ]
    
    if headless:
        args.append('--headless=new')
    
    proc = await asyncio.create_subprocess_exec(
        *args,
        stderr=asyncio.subprocess.PIPE,
    )
    
    # 从 stderr 读取 WebSocket URL
    ws_url = None
    start_time = asyncio.get_event_loop().time()
    timeout = 30  # 最多等待 30 秒
    
    while asyncio.get_event_loop().time() - start_time < timeout:
        try:
            line = await asyncio.wait_for(proc.stderr.readline(), timeout=1)
            if line:
                line_str = line.decode(errors='ignore')
                # 查找 DevTools URL
                match = re.search(r'DevTools listening on (ws://[^\s]+)', line_str)
                if match:
                    ws_url = match.group(1)
                    print(f"WebSocket URL: {ws_url}")
                    break
        except asyncio.TimeoutError:
            # 检查进程是否还在运行
            if proc.returncode is not None:
                raise RuntimeError(f"浏览器进程已退出，返回码: {proc.returncode}")
            continue
    
    if not ws_url:
        # 清理资源
        if proc.returncode is None:
            proc.terminate()
            await proc.wait()
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise RuntimeError("无法获取 WebSocket URL，浏览器可能启动失败")
    
    return proc, ws_url, temp_dir


async def cleanup_browser(proc, temp_dir: str):
    """清理浏览器资源"""
    if proc and proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
    
    if temp_dir:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ==================== LLM 配置 ====================

def get_llm():
    """
    获取 LLM 实例
    
    支持通过环境变量配置：
    - DASHSCOPE_API_KEY: 阿里通义 API Key
    - LLM_MODEL: 模型名称（默认 qwen-turbo，速度最快）
    - LLM_BASE_URL: API 基础 URL
    
    模型选择建议：
    - qwen-turbo: 速度最快，适合简单任务
    - qwen-plus: 平衡速度和能力
    - qwen-max/qwen3-max: 能力最强，但较慢
    """
    model = os.getenv('LLM_MODEL', 'qwen-turbo')  # 默认使用最快的模型
    base_url = os.getenv('LLM_BASE_URL', 'https://dashscope.aliyuncs.com/compatible-mode/v1')
    api_key = os.getenv('DASHSCOPE_API_KEY')
    
    if not api_key:
        raise ValueError(
            "未设置 DASHSCOPE_API_KEY 环境变量\n"
            "请在 https://bailian.console.aliyun.com/ 获取 API Key"
        )
    
    return ChatOpenAI(
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=0.3,
        frequency_penalty=None,
    )


# ==================== 连接已有浏览器 ====================

def get_existing_browser_session(port: int = 9222) -> BrowserSession:
    """
    连接到已运行的浏览器（保留登录状态）
    
    使用前需先用调试模式启动浏览器：
    chrome.exe --remote-debugging-port=9222
    
    Args:
        port: 调试端口，默认 9222
    """
    return BrowserSession(cdp_url=f"http://127.0.0.1:{port}")


# ==================== 测试类 ====================

class TestBaiduSearch:
    """百度搜索测试类"""

    @pytest.mark.asyncio
    async def test_baidu_search_deepseek(self):
        """测试用例：打开百度，搜索 deepseek"""
        task = """
        请执行以下步骤：
        1. 打开百度首页 https://www.baidu.com
        2. 在搜索框中输入 "deepseek"
        3. 点击搜索按钮进行搜索
        4. 等待搜索结果加载完成
        """

        proc, ws_url, temp_dir = await start_browser_and_get_ws_url(headless=False)
        
        try:
            agent = Agent(
                task=task,
                llm=get_llm(),
                browser=BrowserSession(cdp_url=ws_url),
                use_vision=False,  # 关闭视觉模式，提高速度
                max_actions_per_step=5,  # 每步执行更多动作，减少总步数
            )

            history = await agent.run(max_steps=20)

            assert history.is_done(), "任务未完成"
            
            final_result = history.final_result()
            print(f"\n任务完成！最终结果: {final_result}")
            print(f"任务是否成功: {history.is_successful()}")
            
        finally:
            await cleanup_browser(proc, temp_dir)


# ==================== 主函数 ====================

async def main():
    """直接运行脚本的入口函数"""
    print("开始执行百度搜索 deepseek 任务...")
    print()
    
    task = """
    请执行以下步骤：
    1. 打开百度首页 https://www.baidu.com
    2. 在搜索框中输入 "deepseek"
    3. 点击搜索按钮进行搜索
    4. 等待搜索结果加载完成
    """
    
    proc, ws_url, temp_dir = await start_browser_and_get_ws_url(headless=False)
    
    try:
        agent = Agent(
            task=task,
            llm=get_llm(),
            browser=BrowserSession(cdp_url=ws_url),
            use_vision=False,  # 关闭视觉模式，提高速度
            max_actions_per_step=5,  # 每步执行更多动作
        )
        
        history = await agent.run(max_steps=20)
        
        print(f"\n{'='*50}")
        print(f"任务完成: {history.is_done()}")
        print(f"任务成功: {history.is_successful()}")
        print(f"最终结果: {history.final_result()}")
        print(f"{'='*50}")
        
    finally:
        await cleanup_browser(proc, temp_dir)


if __name__ == "__main__":
    # 直接运行脚本
    asyncio.run(main())

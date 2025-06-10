from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from lzhgetlogger import get_logger
import logging
import asyncio
import subprocess
import sys
import random
from fnmatch import fnmatch
from urllib.parse import urlparse
import typing
from typing import Literal

class Browser:
    """
    一个`playwright`实例，支持`cookie`、自动代理(白名单)、远程调试、设定加载内容
    """
    def __init__(
            self,
            max_pages:int = 5,
            proxy: dict[str, str] = {"server": "socks5://127.0.0.1:1080"},
            white_list: set[str] = set(),
            headless:bool=True,
            executable_path:str = None,
            user_data_dir:str = './user_data',
            remote_debugging_port:int = 9222,
            remote_debugging_address:str = "127.0.0.1",
            max_retry_default:int = 2,
            page_timeout_default:int = 10000,
            logging_level=logging.ERROR
            ):
        """
        **务必调用`await Browser.create()`方法来初始化**
        """
        self._pages_semaphore = asyncio.Semaphore(max_pages)
        self._proxy = proxy
        self._white_list = white_list
        self._headless = headless
        self._executable_path = executable_path
        self._user_data_dir = user_data_dir
        self._remote_debugging_port = remote_debugging_port
        self._remote_debugging_address = remote_debugging_address
        self._max_retry_default = max_retry_default
        self._page_timeout_default = page_timeout_default
        self._logger = get_logger(level=logging_level)
        self.playwright_instance = None
        """playwright 实例"""
        self.context_direct = None
        """chromium 直连窗口实例"""
        self.context_proxy = None
        """chromium 代理窗口实例"""

    @staticmethod
    async def create(
        max_pages:int = 5,
        proxy:dict[str, str] = {"server": "socks5://127.0.0.1:1080"},
        white_list:set[str] = set(),
        headless:bool = True,
        executable_path:str = None,
        user_data_dir:str = './user_data',
        remote_debugging_port:int = 9222,
        remote_debugging_address:str = "127.0.0.1",
        max_retry_default:int = 2,
        page_timeout_default:int = 10000,
        logging_level = logging.ERROR
    ) -> "Browser":
        """
        - `max_pages` : 最大同时打开网页数
        - `proxy` : 代理配置
        - `remote_debugging_port` : 实际会占用`remote_debugging_port`和`remote_debugging_port + 1`两个端口，前者用于直连窗口，后者用于代理窗口
        - `logging_level` : [logging_levels](https://docs.python.org/3/library/logging.html#logging-levels)
        """
        browser = Browser(
            max_pages=max_pages,
            proxy=proxy,
            white_list=white_list,
            headless=headless,
            executable_path=executable_path,
            user_data_dir=user_data_dir,
            remote_debugging_port=remote_debugging_port,
            remote_debugging_address=remote_debugging_address,
            max_retry_default=max_retry_default,
            page_timeout_default=page_timeout_default,
            logging_level=logging_level
        )
        await browser._init()
        return browser

    async def fetch(
            self,
            url:str,
            retries = None,
            timeout: typing.Optional[float] = None,
            wait_until: typing.Optional[Literal["commit", "domcontentloaded", "load", "networkidle"]] = None,
            selector:str = None,
            abort:frozenset[str] = {}):
        """
        - 接收`url`，返回`html`
        - 会根据`url`和白名单自动选择是否走代理
        - 可以跳过设定类型内容的加载
        - 如果需要其他流程控制，可以操作`context_direct`或`context_proxy`两个窗口实例来加载网页
        - 参数:
            - `wait_until` : `Union["commit", "domcontentloaded", "load", "networkidle", None]`
            - `abort` : [ResourceType 的子集](https://playwright.dev/python/docs/api/class-request#request-resource-type)，跳过此集合内类型内容的加载，[ResourceType : `{"document", "stylesheet", "image", "media", "font", "script", "texttrack", "xhr", "fetch", "eventsource", "websocket", "manifest", "other"}`](https://playwright.dev/python/docs/api/class-request#request-resource-type)
        """
        retries = self._max_retry_default if not retries else retries
        timeout = self._page_timeout_default if not timeout else timeout
        async with self._pages_semaphore:
            context = self.context_proxy if self._is_whitelisted(url) else self.context_direct
            for attempt in range(1, retries + 2):
                page = await context.new_page()
                if abort:
                    async def handle_route(route, request):
                        if request.resource_type in abort:
                            await route.abort()
                        else:
                            await route.continue_()
                    await page.route("**/*", handle_route)
                try:
                    start = asyncio.get_event_loop().time()
                    await page.goto(url, timeout=timeout, wait_until=wait_until)
                    if selector:
                        await page.wait_for_selector(selector = selector, timeout=timeout)
                    content = await page.content()
                    elapsed = asyncio.get_event_loop().time() - start
                    self._logger.info(f"Success [{url}] in {elapsed:.2f}s")
                    asyncio.create_task(self._close_page_later(page, delay= 1 + 6 * random.random()))
                    return content
                except PlaywrightTimeoutError:
                    self._logger.warning(f"Timeout on attempt {attempt} for {url}")
                    asyncio.create_task(self._close_page_later(page, delay= 1 + 6 * random.random()))
                    if attempt > retries:
                        return None
                except Exception as e:
                    self._logger.error(f"Error on attempt {attempt} for {url}: {e}")
                    asyncio.create_task(self._close_page_later(page, delay= 1 + 6 * random.random()))
                    if attempt > retries:
                        return None
                    await asyncio.sleep(1)

    def white_list_update(self, whitel_list:set[str]):
        """
        更新代理白名单，只增不减，支持通配符
        """
        self._white_list.update(whitel_list)

    async def close(self):
        if self.context_direct:
            await self.context_direct.close()
        if self.context_proxy:
            await self.context_proxy.close()
        if self.playwright_instance:
            await self.playwright_instance.stop()

    async def _init(self):
        self._ensure_chromium_installed()
        self.playwright_instance = await async_playwright().start()
        self.context_direct = await self._get_context('/direct', self._remote_debugging_port)
        self.context_proxy = await self._get_context('/proxy', self._remote_debugging_port + 1, self._proxy)

    async def _close_page_later(self, page, delay=2):
        try:
            await asyncio.sleep(delay)
            await page.close()
        except Exception as e:
            self._logger.error(f"Error closing page: {e}")

    def _is_whitelisted(self, url: str) -> bool:
        hostname = urlparse(url).netloc
        path = urlparse(url).path
        target = f"{hostname}{path}"
        match = any(fnmatch(hostname, pattern) or fnmatch(target, pattern) for pattern in self._white_list)
        self._logger.debug(f"[Whitelist] URL: {url} → Host: {hostname}, Match: {match}")
        return match

    async def _get_context(self, sub_user_data_dir, remote_debugging_port, proxy = None):
        context = await self.playwright_instance.chromium.launch_persistent_context(
            user_data_dir = self._user_data_dir + sub_user_data_dir,
            headless = self._headless,
            executable_path = self._executable_path,
            user_agent = self._random_user_agent(),
            proxy=proxy,
            args = [
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-gpu',
                '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled',
                '--window-size=820,750',
                '--lang=zh-CN,zh',  # 加一个中文偏好，减少触发反爬
                f'--remote-debugging-port={remote_debugging_port}',
                f'--remote-debugging-address={self._remote_debugging_address}'
            ]
        )
        await context.set_extra_http_headers({
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip, deflate, br" # aiohttp客户端只支持这三个
        })
        page = await context.new_page()
        await page.set_content(f"""
            <html>
                <head>
                    <title>[{"Proxy" if proxy else "Direct"}] 🚫 DON'T CLOSE THE LAST TAB</title>
                </head>
                <body style="color: darkred; padding: 30px;">
                    <h2 style="color: grey;"><br/>此为{f"代理窗口 server : {proxy.get('server')}" if proxy else "直连窗口"}</h2>
                    <h1>请勿手动关闭最后一个标签页，否则浏览器将退出，服务会中断，<a href="about:blank" target="_blank">点击新建标签页</a></h1>
                    <h2>Do not close the last tab manually, otherwise the browser will exit and the service will be interrupted</h2>
                </body>
            </html>
        """)
        return context

    def _random_user_agent(self):
        agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        ]
        return random.choice(agents)

    def _ensure_chromium_installed(self):
        try:
            result = subprocess.run(
                # [sys.executable, "-m", "playwright", "install", "chromium-headless-shell"],
                [sys.executable, "-m", "playwright", "install", "chromium"],
                check=True,
                capture_output=True,
                text=True
            )
            result2 = subprocess.run(
                [sys.executable, "-m", "playwright", "install-deps"],
                check=True,
                capture_output=True,
                text=True
            )
        except subprocess.CalledProcessError as e:
            with open("playwright_install_error.log", "w", encoding="utf-8") as f:
                f.write("Playwright 安装失败！\n")
                f.write(f"退出码: {e.returncode}\n")
                f.write("标准输出:\n" + (e.stdout or '') + "\n")
                f.write("标准错误:\n" + (e.stderr or '') + "\n")
            raise

if __name__ == "__main__":

    async def main():
        browser = await Browser.create(
            headless=False,
            proxy= {"server": "socks5://127.0.0.1:1080"},
            logging_level=logging.DEBUG)
        white_list:set[str] = {
            "*.dmm.com",
            "*.dmm.co.jp",
            "www.prestige-av.com",
            "www.mgstage.com"
        }
        browser.white_list_update(white_list)
        content = await browser.fetch("https://video.dmm.co.jp/")
        content2 = await browser.fetch("https://www.google.com")
        await asyncio.sleep(10)
        await browser.close()

    asyncio.run(main())






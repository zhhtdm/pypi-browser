# browser
一个`playwright`实例，支持`cookie`、自动代理(白名单)、远程调试、设定加载内容

## 示例
```python
from lzhbrowser import Browser
import asyncio

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

```

## 安装 - [PyPI](https://pypi.org/project/lzhbrowser/)
```bash
pip install lzhbrowser
```

## API
[Document](https://zhhtdm.github.io/pypi-browser/)



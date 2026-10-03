# 建议设置每天凌晨自动运行
'''
new Env('E-Hentai 自动签到')
cron: 1 */6 * * *

脚本来源: https://github.com/AkiyaKiko/EhentaiAutoSignIn
作者: zephyr_xiao（基于原项目改写）
'''

import os
import ssl
import sys
import time
import logging
import requests
import http.cookies

from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter

# 青龙面板默认通知模块；本地调试/未部署环境找不到时降级为 None，不阻塞脚本
try:
    import notify  # type: ignore
except ImportError:
    _scripts_dir = '/ql/data/scripts'
    if os.path.isdir(_scripts_dir) and _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)
        try:
            import notify  # type: ignore
        except ImportError:
            notify = None
    else:
        notify = None


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

URL = 'https://e-hentai.org/news.php'

# 本地 Cookie 缓存文件（青龙 /ql/data/ 目录持久化，容器重启不丢）
COOKIE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'ehentai_cookie.txt'
)

# 服务器续期时允许合并更新的字段
COOKIE_FIELDS = [
    'ipb_member_id',
    'ipb_pass_hash',
    'sk',
    'hath_perks',
    'nw',
    'event'
]

headers = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/135.0.0.0 Safari/537.36'
    ),
    'Accept': (
        'text/html,application/xhtml+xml,application/xml;q=0.9,'
        'image/avif,image/webp,image/apng,*/*;q=0.8,'
        'application/signed-exchange;v=b3;q=0.7'
    ),
    'Upgrade-Insecure-Requests': '1',
    'Cookie': ''
}

proxies = {}


class TLS12Adapter(HTTPAdapter):
    """强制 HTTPS 使用 TLS 1.2"""

    def __init__(self):
        self.context = ssl.create_default_context()
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.maximum_version = ssl.TLSVersion.TLSv1_2
        super().__init__()

    def init_poolmanager(self, *args, **kwargs):
        kwargs['ssl_context'] = self.context
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy, **kwargs):
        kwargs['ssl_context'] = self.context
        return super().proxy_manager_for(proxy, **kwargs)


def send_notify(title, content):
    if notify is None:
        logging.info(f'未找到 notify 模块，跳过推送：{title}')
        return

    try:
        notify.send(f'Ehentai SignIn - {title}', content)
    except Exception as e:
        logging.error(f'发送通知失败：{e}')


def mask_cookie(cookie):
    result = []

    for item in cookie.split(';'):
        item = item.strip()

        if '=' not in item:
            result.append(item)
            continue

        key, value = item.split('=', 1)

        if len(value) > 8:
            value = value[:3] + '*' * (len(value) - 6) + value[-3:]
        elif value:
            value = value[0] + '*' * (len(value) - 1)

        result.append(f'{key}={value}')

    return '; '.join(result)


def load_cookie_from_file():
    """从本地缓存文件读取 Cookie；文件不存在或读取失败返回 None。"""
    try:
        with open(COOKIE_FILE, 'r', encoding='utf-8') as f:
            cookie = f.read().strip()
        return cookie or None
    except Exception:
        return None


def save_cookie_to_file(cookie):
    """把 Cookie 写入本地缓存文件；失败仅告警，不中断签到。"""
    try:
        with open(COOKIE_FILE, 'w', encoding='utf-8') as f:
            f.write(cookie)
    except Exception as e:
        logging.warning(f'Cookie 写入本地文件失败: {e}')


def merge_cookie(old_cookie, new_cookies, fields):
    """合并新旧 Cookie，返回 (合并后完整 Cookie, 是否有字段变化)。

    保留旧 Cookie 中全部键的顺序与值，仅把 fields 中出现在新
    Set-Cookie 里的键更新为新值。
    """
    current = {}

    for item in old_cookie.split(';'):
        if '=' in item:
            key, value = item.strip().split('=', 1)
            current[key] = value

    updated = False

    for field in fields:
        if field in new_cookies and current.get(field) != new_cookies[field]:
            current[field] = new_cookies[field]
            updated = True

    new_cookie = '; '.join(f'{k}={v}' for k, v in current.items())
    return new_cookie, updated


def init_config():
    proxy = os.getenv('E_PROXY')
    cookie = os.getenv('E_COOKIE')
    user_agent = os.getenv('E_USER_AGENT')

    # 环境变量为空时，回退读取本地缓存文件中的 Cookie
    if not cookie:
        cookie = load_cookie_from_file()
        if cookie:
            logging.info('环境变量未设置，使用本地缓存文件中的 Cookie。')

    if proxy:
        proxies.update(http=proxy, https=proxy)
        logging.info(f'使用代理: {proxy}')
    else:
        logging.info('未设置代理，将不使用代理。')

    if cookie:
        headers['Cookie'] = cookie
        logging.info(f'使用Cookie: {mask_cookie(cookie)}')
    else:
        logging.info('未设置Cookie，将不发送Cookie。')

    if user_agent:
        headers['User-Agent'] = user_agent
        logging.info(f'使用User-Agent: {user_agent}')
    else:
        logging.info('未设置User-Agent，将使用默认 User-Agent。')


def parse_set_cookie(raw_headers):
    cookies = {}
    expires = []

    for raw in raw_headers:
        parsed = http.cookies.SimpleCookie()
        parsed.load(raw)

        for key, value in parsed.items():
            cookies[key] = value.value

            if value['expires']:
                expires.append((key, value['expires']))

    return cookies, expires


def update_cookie(new_cookies, expires):
    old_cookie = os.getenv('E_COOKIE', '') or (load_cookie_from_file() or '')
    new_cookie, updated = merge_cookie(old_cookie, new_cookies, COOKIE_FIELDS)

    if not updated:
        logging.info('本地 Cookie 是最新的，无需更新。')
        return

    # 环境变量（当前进程）+ 本地文件（跨运行持久化）双写
    os.environ['E_COOKIE'] = new_cookie
    save_cookie_to_file(new_cookie)

    logging.info(f'已更新 E_COOKIE（环境变量 + 本地文件）：{mask_cookie(new_cookie)}')

    merged_keys = set()
    for item in new_cookie.split(';'):
        if '=' in item:
            merged_keys.add(item.strip().split('=', 1)[0])

    lines = [
        f'{key} 过期时间: {value}'
        for key, value in expires
        if key in merged_keys and key != 'event'
    ]

    if lines:
        send_notify('Cookie 更新提醒', '\n'.join(lines))


def do_request(session=None):
    client = session or requests

    response = client.get(
        URL,
        headers=headers,
        proxies=proxies,
        timeout=(10, 30)
    )

    response.raise_for_status()
    return response


def request_page():
    last_error = None

    # 默认 TLS，最多尝试 3 次
    for attempt in range(1, 4):
        logging.info(f'默认 TLS 请求，第 {attempt}/3 次')

        try:
            return do_request()
        except requests.exceptions.RequestException as e:
            last_error = e
            logging.warning(f'默认 TLS 请求失败（第 {attempt}/3 次）: {e}')

        if attempt < 3:
            time.sleep(2)

    # 默认 TLS 三次全部失败，回退 TLS 1.2
    logging.warning('默认 TLS 连续失败，回退到 TLS 1.2')

    for attempt in range(1, 4):
        logging.info(f'TLS 1.2 请求，第 {attempt}/3 次')

        try:
            with requests.Session() as session:
                session.mount('https://', TLS12Adapter())
                return do_request(session)

        except requests.exceptions.RequestException as e:
            last_error = e
            logging.warning(f'TLS 1.2 请求失败（第 {attempt}/3 次）: {e}')

        if attempt < 3:
            time.sleep(2)

    raise last_error


def scrape():
    init_config()

    try:
        response = request_page()

        set_cookies = response.raw.headers.getlist('Set-Cookie')

        if set_cookies:
            new_cookies, expires = parse_set_cookie(set_cookies)
            update_cookie(new_cookies, expires)
        else:
            logging.info('未检测到 Set-Cookie，说明当前 Cookie 有效，无需更新。')

        soup = BeautifulSoup(response.text, 'html.parser')
        event_pane = soup.find('div', id='eventpane')

        if not event_pane:
            msg = '没有事件，已经签到了！'
            logging.info(msg)
            send_notify('签到结果', msg)
            return msg

        event_text = event_pane.get_text(
            separator=' ',
            strip=True
        ).lower()

        if 'encounter' in event_text:
            msg = (
                '出现 Random Encounter！'
                '请手动前往处理，或者等待这一随机事件结束！'
            )

            logging.info('出现 Random Encounter！')
            send_notify('签到结果', msg)
            return 'Random Encounter'

        text_lines = [
            p.get_text(strip=True)
            for p in event_pane.find_all('p')
        ]

        if text_lines:
            result = '签到成功！\n' + '\n'.join(text_lines)
            logging.info(result)
            send_notify('签到结果', result)
            return text_lines

    except requests.exceptions.RequestException as e:
        msg = f'请求最终失败: {e}'
        logging.error(msg)
        send_notify('请求错误', msg)
        raise

    except Exception as e:
        msg = f'程序错误: {e}'
        logging.error(msg)
        send_notify('程序错误', msg)
        raise


if __name__ == '__main__':
    scrape()
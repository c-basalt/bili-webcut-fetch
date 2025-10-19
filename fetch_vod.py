#!/usr/bin/env python3
import hashlib
import base64
import json
import os
import re
import itertools
import subprocess
import time
import random
import collections
import threading
import argparse
import shutil
import http.cookiejar
import contextlib


try:
    import requests
    # `CookieCloudClient` requires Crypto.Cipher.AES upon use
except Exception:
    print('\n请检查是否正确安装 requests 包\n')
    raise


class CookieCloudClient:
    @staticmethod
    def _pass_digest(uuid: str, password: str):
        return hashlib.md5(f'{uuid}-{password}'.encode()).hexdigest()[:16].encode()

    @classmethod
    def _decrypt(cls, encrypted_data: bytes, uuid: str, password: str):

        try:
            from Crypto.Cipher import AES
        except Exception:
            print('\n请检查是否正确安装 pycryptodome 包\n')
            raise

        encrypted_data = base64.b64decode(encrypted_data)
        if encrypted_data[0:8] != b"Salted__":
            raise ValueError('Invalid encrypted data')
        passphrase = cls._pass_digest(uuid, password) + encrypted_data[8:16]
        key_iv = digest = b''
        for _ in range(3):
            digest = hashlib.md5(digest + passphrase).digest()
            key_iv += digest
        cipher = AES.new(key_iv[:32], AES.MODE_CBC, key_iv[32:48])
        decrypted = cipher.decrypt(encrypted_data[16:])
        if decrypted[:1] != b'{':
            raise ValueError('Failed to decrypt cookie data')
        return json.loads(decrypted[:-decrypted[-1]])

    @classmethod
    def load_to(cls, s: requests.Session, url: str, uuid: str, password: str):
        """Load remote cookie to requests session"""
        r = requests.get(f'{url}/get/{uuid}')
        for v in cls._decrypt(r.json()['encrypted'], uuid, password)['cookie_data'].values():
            for c in v:
                s.cookies.set(c['name'], c['value'], domain=c['domain'])


def load_netscape_cookie(s: requests.Session, cookie_file: str):
    """Load cookies from netscape-format file to requests session"""
    jar = http.cookiejar.MozillaCookieJar()
    jar.load(cookie_file)
    s.cookies.update(jar)


class StalledException(RuntimeError):
    pass


class VODLoader:
    _BROWSER_VERSION = None  # 可以修改这一行来手动管理请求使用的浏览器版本号

    def __init__(self, session: requests.Session, skip=None, tolerance_ratio=0.01) -> None:
        self.session = session
        self.session.headers.update({
            'user-agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                           f'Chrome/{self._chrome_ver}.0.0.0 Safari/537.36 Edg/{self._chrome_ver}.0.0.0'),
            'origin': 'https://live.bilibili.com',
            'referer': 'https://live.bilibili.com/',
        })
        print(f'API请求将使用浏览器版本号: v{self._chrome_ver}')
        self.skip = {str(s) for s in (skip or [])}
        self.tolerance_ratio = tolerance_ratio
        self.touched = set()

    @property
    def _chrome_ver(self):
        if self._BROWSER_VERSION:
            return self._BROWSER_VERSION
        for path in [
            'C:\\Program Files (x86)\\Microsoft\\Edge\\Application',
            '/mnt/c/Program Files (x86)/Microsoft/Edge/Application/',
            'C:\\Program Files\\Google\\Chrome\\Application',
            '/mnt/c/Program Files/Google/Chrome/Application/',
            '/Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Framework.framework/Versions',
        ]:
            if not os.path.isdir(path):
                continue
            ver_nums = [m[1] for m in (re.match(r'(\d+)(\.\d+){3}', f) for f in os.listdir(path)) if m]
            if ver_nums:
                self._BROWSER_VERSION = max(ver_nums, key=int)
                return self._BROWSER_VERSION
        with contextlib.suppress(Exception):
            r = requests.get(
                'https://msedgewebdriverstorage.blob.core.windows.net/edgewebdriver/LATEST_STABLE', timeout=5)
            self._BROWSER_VERSION = re.match(r'(\d+)(\.\d+){3}', r.text.strip())[1]  # type: ignore
            return self._BROWSER_VERSION
        print('无法获取本地/最新浏览器版本，使用旧版本号代替')
        self._BROWSER_VERSION = str(random.randint(131, 141))
        return self._BROWSER_VERSION

    @property
    def _ffmpeg_args(self):
        args = ['-headers', ''.join(f'{k}: {v}\r\n' for k, v in self.session.headers.items())]
        return args

    def _get(self, url, *args, **kwargs):
        for count in range(3):
            try:
                r = self.session.get(url, *args, **kwargs)
                break
            except requests.exceptions.ConnectionError:
                if count == 2:
                    raise
                time.sleep(5)
        print(' ', r.status_code, r.url)
        time.sleep(3 * (1 + random.random()))
        return r

    def _get_json(self, url, *args, **kwargs):
        return self._get(url, *args, **kwargs).json()

    def fetch_all_replay(self, uid):
        for pn in itertools.count(1):
            rsp = self._get_json(f'https://api.live.bilibili.com/xlive/web-room/v1/videoService/GetOtherSliceList?live_uid={uid}&time_range=3&page={pn}&page_size=20&web_location=444.194')
            assert rsp['code'] == 0, f'回放获取失败: {rsp}'
            data = rsp['data']

            for replay in data['replay_info']:
                self.fetch_replay(uid, replay['live_key'], replay['start_time'], replay['end_time'],
                                  replay['live_info']['title'], replay['live_info']['cover'])

            if pn * data['pagination']['page_size'] >= data['pagination']['total']:
                break
        return self.touched

    def _file_stat_tracking_worker(self, temp_fn: str, p: subprocess.Popen, stalled_flag: threading.Event, timeout=300):
        prev_size, prev_ts = 0, time.time()
        while p.poll() is None:
            try:
                current_size = os.stat(temp_fn).st_size
            except FileNotFoundError:
                current_size = 0

            if current_size == prev_size:
                if time.time() - prev_ts > timeout:
                    stalled_flag.set()
                    p.terminate()
                    return
            else:
                prev_size, prev_ts = current_size, time.time()
            time.sleep(10)

    @staticmethod
    def _parse_duration(line):
        if m := re.search(r'Duration: (\d+):(\d+):(\d+)\.\d+', line):
            duration = list(map(int, m.groups()))
            return duration[0] * 3600 + duration[1] * 60 + duration[2]
        print('Failed to parse duration')
        return 0

    def _duration_mismatch(self, actual: int, target: int):
        return abs(actual - target) > target * self.tolerance_ratio + 10

    def fetch_replay_item(self, uid, live_key, item):
        out_fn = f'{uid}_{live_key}-{item["start_time"]}.mp4'
        temp_fn = f'{out_fn}.tmp'
        duration = item['end_time'] - item['start_time']

        cmd = ['ffmpeg', '-y', *self._ffmpeg_args, '-i', item['stream'], '-c', 'copy', '-f', 'mp4', temp_fn]
        print(cmd)
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        assert p.stdout  # hint for linter

        stalled_flag = threading.Event()
        checker_thread = threading.Thread(target=self._file_stat_tracking_worker, args=(temp_fn, p, stalled_flag))
        checker_thread.start()

        lines = collections.deque(maxlen=5)
        for line in p.stdout:
            width = shutil.get_terminal_size().columns - 10
            print(f'{" "*width}\r', line.strip()[:width - 2], end='  \r', flush=True)
            lines.append(line)
            if 'Duration' in line:
                if self._duration_mismatch((parsed := self._parse_duration(line)), duration):
                    p.terminate()
                    raise ValueError(f'服务器文件时长和直播时长不符: {parsed} != {duration}\n{line}')
        try:
            if p.wait(timeout=600) != 0:
                if stalled_flag.is_set():
                    raise StalledException(f'ffmpeg 下载卡住: {out_fn}\n{"".join(lines)}')
                raise ValueError(f'ffmpeg 报错: {out_fn} returncode={p.returncode}\n{"".join(lines)}')
        except subprocess.TimeoutExpired:
            p.terminate()
            raise
        if stalled_flag.is_set():
            raise StalledException(f'ffmpeg 下载卡住: {out_fn}\n{"".join(lines)}')
        checker_thread.join(timeout=10)

        probe_text = subprocess.run(['ffmpeg', '-i', temp_fn], stderr=subprocess.PIPE, text=True).stderr
        if self._duration_mismatch((parsed := self._parse_duration(probe_text)), duration):
            raise ValueError(f'下载的视频文件和直播时长不符: {parsed} != {duration}\n{probe_text}')

        os.rename(temp_fn, out_fn)

    def fetch_replay(self, uid, live_key, start, end, title, cover_url):
        self.touched.add(f'{uid}_{live_key}')
        cover_fn = f'{uid}_{live_key}-{title}.jpg'
        if is_completed(uid, live_key, start, end, title):
            print(f'[{live_key}][{title}] 已下载，跳过')
            return False
        if live_key in self.skip:
            print(f'[{live_key}][{title}] 跳过该live_key')
            return False
        print(f'[{live_key}][{title}] 开始下载')
        rsp = self._get_json(f'https://api.live.bilibili.com/xlive/web-room/v1/videoService/GetUserSliceStream?live_key={live_key}&start_time={start}&end_time={end}&live_uid={uid}&web_location=444.194')
        assert rsp['code'] == 0, str(rsp)
        for item in rsp['data']['list']:
            for count in range(20, -1, -1):
                try:
                    self.fetch_replay_item(uid, live_key, item)
                    break
                except StalledException:
                    if not count:
                        raise
                    print(f'\n[{live_key}][{title}] ffmpeg 下载卡住，10分钟后重新启动下载', flush=True)
                    time.sleep(600)

        with open(cover_fn, 'wb') as f:
            f.write(self._get(cover_url).content)
        mark_completed(uid, live_key, start, end, title)
        return True


def is_completed(uid, live_key, start, end, title) -> bool:
    """检测回放是否已经完成下载，避免重复下载
    可以改为其他逻辑，比如用一个文件记录完成的 live_key 等"""
    return os.path.exists(f'{uid}_{live_key}-{title}.jpg')


def mark_completed(uid, live_key, start, end, title):
    """记录成功下载完成的回放，默认逻辑在下载成功后下载封面作为判断依据
    可以按需添加其他逻辑"""
    pass


if __name__ == '__main__':
    assert shutil.which('ffmpeg'), '脚本需要ffmpeg才能使用'

    parser = argparse.ArgumentParser()
    parser.add_argument('uids', nargs='+', type=int, help='要下载回放的主播的UID，注意是主站UID，不是直播间号')
    parser.add_argument('--skip', nargs='+', help='跳过特定的live_key')
    parser.add_argument('--tolerance', default=0.01, help='回放视频长度的容许误差，用于校验下载的回放是否完整')
    parser.add_argument('--cookie_file', help='包含小号登录的Netscape格式cookie文件')
    args = parser.parse_args()

    session = requests.Session()
    # 可以用其他的库如rookie等加载小号cookie
    # 也可以用自建的CookieCloud来存取cookie
    if os.environ.get('COOKIE_URL'):
        CookieCloudClient.load_to(session, os.environ['COOKIE_URL'],
                                  os.environ['COOKIE_UUID'], os.environ['COOKIE_PASSWD'])
    # 或者使用 EditThisCookie / Cookie-Editor 等扩展导出到文件后读取
    elif args.cookie_file:
        load_netscape_cookie(session, args.cookie_file)

    assert session.cookies.get('SESSDATA', domain='.bilibili.com'), '回放剪辑功能要求登录，必须提供B站cookie才能访问'

    loader = VODLoader(session, skip=args.skip, tolerance_ratio=args.tolerance)
    for uid in args.uids:
        loader.fetch_all_replay(uid)

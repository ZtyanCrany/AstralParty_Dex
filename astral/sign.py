# -*- coding: utf-8 -*-
"""星引擎 Party 国服 SDK 短信登录 —— 签名算法模块

签名算法（由客户端反编译 FUN_180793550 与 GetSignKey 确认）：
    sign = md5( 参数按「键名字母序」排成 k=v、用空字符串直接拼接 + 渠道密钥 )

用法:
    from astral.sign import send_code, authorize, make_sign
    send_code('13800138000')                      # 发送验证码（会真实发送短信）
    r = authorize('13800138000', '123456')        # 验证码登录 → 拿 sid
"""
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = 'https://m-sdk.feimogames.com'

# 渠道密钥表位于独立文件 astral/signkeys.py ——
#   ① 需要停止分发这些密钥时，删除该文件即可，本模块与其余源码不受影响；
#   ② 本模块原样转出，保持 `from astral.sign import SIGN_KEYS, DEFAULT_KEY` 的写法不变。
from .signkeys import SIGN_KEYS, DEFAULT_KEY        # noqa: E402,F401

APP_ID = '110001933'      # 国服 PC
GAME_ID = '120000182'
CHANNEL_ID = '110001933'
OS = 'windows'
GAME_VERSION = '3.2.0'      # 游戏客户端版本（不是 3.2.0 会被 ClientVerErr 拒）
SDK_VERSION = '1.0.0.9'     # 飞墨 SDK 版本（/api/init 必需）

UA = {
    # 游戏使用 Unity 的 UA，服务器可能据此判定"合法客户端"
    'User-Agent': 'UnityPlayer/2021.3.45f2 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)',
    'Content-Type': 'application/x-www-form-urlencoded',
    'Accept': '*/*',
    'Accept-Encoding': 'deflate, gzip',
    'X-Unity-Version': '2021.3.45f2',
}

# 直连（不走系统代理：内网或慢代理会显著拖慢请求）
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def make_sign(params, channel='test_junhai'):
    """核心算法: md5( 字母序 k=v 无分隔符拼接 + 渠道密钥 )"""
    key = SIGN_KEYS.get(channel, DEFAULT_KEY)
    src = ''.join('%s=%s' % (k, params[k]) for k in sorted(params))
    return hashlib.md5((src + key).encode('utf-8')).hexdigest()


def _post(path, params, channel='test_junhai', timeout=20):
    """POST 表单（自动计算 sign）。
    注意：UA 声明了 Accept-Encoding: gzip，响应可能为压缩体，必须解压 ——
      否则会把乱码当成错误信息。
    """
    body = dict(params)
    body['sign'] = make_sign(params, channel)
    data = urllib.parse.urlencode(body).encode('utf-8')

    def _dec(b):
        if b[:2] == b'\x1f\x8b':
            try:
                import gzip
                b = gzip.decompress(b)
            except Exception:
                pass
        return b.decode('utf-8', 'replace')

    req = urllib.request.Request(BASE + path, data=data, headers=UA, method='POST')
    try:
        with OPENER.open(req, timeout=timeout) as r:
            raw = _dec(r.read())
    except urllib.error.HTTPError as e:
        raw = _dec(e.read())
    try:
        return json.loads(raw)
    except Exception:
        return {'ret': -1, 'msg': raw[:300], 'content': []}


def send_code(tel_num, channel='test_junhai', type_='smslogin', **extra):
    """发送短信验证码（注意：会真实向该号码发送短信）
    type 必须为字符串 'smslogin'（不是数字），channel 必须为 'test_junhai'。
    """
    p = {'app_id': APP_ID, 'channel': channel, 'os': OS,
         'tel_num': str(tel_num), 'time': str(int(time.time())), 'type': str(type_)}
    p.update(extra)
    return _post('/account/sendCode', p, channel)


DEVICE_ID = '42af3d79b18141711023754cad1aaf3cb418d45f'
OS_VERSION = 'Windows 11  (10.0.29671) 64bit'
DEVICE_NAME = 'REDMI Book 16 2025(2.5K) (XIAOMI)'


def authorize(tel_num, code, login_type='3', channel='test_junhai', **extra):
    """验证码登录 → 返回含 authorize_code(=sid) / data.accessToken 的响应

    短信验证码登录使用 login_type=3，验证码参数名为 smscode（不是 code）。
      参数集必须与游戏完全一致：
        app_id / channel / sdk_version / device_id / time / os / login_type
        / os_version / device_name / and_id / tel_num / smscode  (+ sign)
      注：login_type=2 为「带 access_token 的自动登录」，使用它会回"登录态过期, 请重新登录"。
    """
    p = {'app_id': APP_ID, 'channel': channel, 'sdk_version': SDK_VERSION,
         'device_id': DEVICE_ID, 'time': str(int(time.time())), 'os': OS,
         'login_type': login_type, 'os_version': OS_VERSION,
         'device_name': DEVICE_NAME, 'and_id': DEVICE_ID,
         'tel_num': str(tel_num), 'smscode': str(code)}
    p.update(extra)
    return _post('/account/authorize', p, channel)


def pick(resp, *keys, default=None):
    """按点分路径从嵌套响应中取字段（SDK 返回结构层级较深）"""
    for k in keys:
        cur = resp
        ok = True
        for part in k.split('.'):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
                cur = cur[int(part)]
            else:
                ok = False
                break
        if ok and cur not in (None, '', []):
            return cur
    return default


if __name__ == '__main__':
    import sys
    ch = sys.argv[1] if len(sys.argv) > 1 else 'steam'
    tel = sys.argv[2] if len(sys.argv) > 2 else '1'
    # 密钥只显示前 6 位（渠道签名密钥不落日志、不进终端历史）
    print('渠道 %s  密钥 %s…' % (ch, str(SIGN_KEYS.get(ch, DEFAULT_KEY))[:6]))
    demo = {'app_id': APP_ID, 'channel': ch, 'os': OS, 'tel_num': tel,
            'time': str(int(time.time())), 'type': '0'}
    print('待签串: %s' % ''.join('%s=%s' % (k, demo[k]) for k in sorted(demo)))
    print('sign  : %s' % make_sign(demo, ch))
    print('\n--- 真发验证码（假号码 → 预期"手机号错误"）---')
    print(json.dumps(send_code(tel, ch), ensure_ascii=False)[:400])
    print('\n--- authorize（假验证码 → 预期不是签名错误）---')
    print(json.dumps(authorize(tel, '000000', 'smslogin', ch), ensure_ascii=False)[:500])

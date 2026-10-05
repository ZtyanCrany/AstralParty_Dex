# -*- coding: utf-8 -*-
"""手机短信登录（SDK 免客户端）—— 拿 sid 交给游戏协议用

流程:
    send_code(tel)                  → 发验证码
    phone_login(tel, code)          → 返回 sid（同时把原始响应存盘，方便排查）
"""
import hashlib
import json
import time
from datetime import datetime

from .sign import (APP_ID, GAME_VERSION, OS, SDK_VERSION,
                   _post, authorize, device_id, pick, send_code)

from astral import paths            # noqa: E402

SAVE_DIR = paths.DATA_DIR

# 设备号按本机机器名推导（同一台机器恒定，不含任何硬编码的机器标识）
DEFAULT_DEVICE_ID = device_id()
DEFAULT_EXTRA = 'bn'


class LoginError(Exception):
    pass


# SDK 即使业务失败也回 ret="0"，错误信息只写在 msg 中。
#   错误文案种类繁多（"操作不合法"、"验证码已过期"…），黑名单无法穷举，
#   因此改用白名单：只有 msg 为空或含"成功"/ok 才算成功。
OK_HINTS = ('成功', 'ok', 'OK')


def _check(resp):
    """返回 (是否成功, 错误信息)

    约定（依据客户端 SDK 日志）：ret="1" 为成功，ret="0" 为失败。
       成功: {"ret":"1","msg":"success"} / {"ret":"1","msg":"短信发送成功"}
       失败: {"ret":"0","msg":"操作不合法"} / {"ret":"0","msg":"登录态过期, 请重新登录"}
    """
    ret = str(resp.get('ret', '')).strip()
    msg = str(resp.get('msg', '') or '').strip()
    if ret == '1':
        return True, msg
    if ret in ('0', ''):
        return False, msg or 'ret=0（失败）'
    return False, msg or ('ret=%s' % ret)


CODE_TYPES = ('smslogin',)   # 必须为字符串 'smslogin'（数字形式会被拒）


def sdk_init(channel='test_junhai'):
    """前置步骤：调用 /api/init 建立 SDK 会话

    参数集：app_id / channel / game_version / os / sdk_version / time
    不需要签名。游戏每次启动的第一步即调用它，发验证码前缺少它会回"操作不合法"。
    返回 (是否成功, 原始响应)
    """
    r = _post('/api/init', {
        'app_id': APP_ID, 'channel': channel, 'os': OS,
        'game_version': GAME_VERSION, 'sdk_version': SDK_VERSION,
        'time': str(int(time.time())),      # 必需，缺少会回"参数错误:time参数不能为空"
    })
    good, msg = _check(r)
    return good, r


def request_code(tel_num, channel='test_junhai', types=CODE_TYPES, save=True, do_init=True):
    """发送短信验证码（会真实发送短信）

    sendCode 的 type 取值无法用无效号码探测（服务器先校验手机号），
    因此依次尝试，取第一个被接受的取值（成功即停，不会连发多条）。
    """
    last_err = None
    tried = []
    init_ok, init_resp = None, None
    if do_init:
        init_ok, init_resp = sdk_init(channel)
    for t in types:
        r = send_code(tel_num, channel, type_=t)
        good, msg = _check(r)
        tried.append({'type': t, 'resp': r})
        if good:
            if save:
                _save('sendCode', 'tel_masked=%s' % (str(tel_num)[:3] + '****' + str(tel_num)[-2:]),
                      {'init_ok': init_ok, 'init_resp': init_resp, 'type_used': t, 'tried': tried})
            return r
        last_err = msg or json.dumps(r, ensure_ascii=False)
        # 手机号本身有问题时，换 type 也没用，直接停
        if any(h in str(last_err) for h in ('手机号错误', '参数错误', '不能为空')):
            break
    if save:
        _save('sendCode', 'tel_masked=%s' % (str(tel_num)[:3] + '****' + str(tel_num)[-2:]),
              {'init_ok': init_ok, 'init_resp': init_resp, 'type_used': None, 'tried': tried})
    raise LoginError(last_err or '发送验证码失败')


def _save(kind, note, payload):
    try:
        SAVE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        out = SAVE_DIR / ('%s_%s.json' % (kind, stamp))
        out.write_text(json.dumps({'kind': kind, 'note': note, 'when': stamp, **payload},
                                  ensure_ascii=False, indent=2), encoding='utf-8')
        return out
    except Exception:
        return None


def _submit(tel_num, secret, login_type, channel='test_junhai', do_init=True,
            cred='smscode'):
    """登录公共段：init → authorize → 存档（只记凭据长度，绝不写凭据本身）→ 取 sid。

    login_type: 3=短信验证码（凭据字段 smscode）；1=手机号+密码（password）；
                2=带 access_token 的自动登录；4/7=短信注册；5=刷新登录态
    参与签名的参数必须与服务器期望的完全一致；多传一个参数（如 channel_id）
    会被算进签名，服务器回 signError。登录前必须先调 /api/init，否则回"操作不合法"。
    返回的 sid 位于 content.authorize_code。
    """
    if do_init:
        sdk_init(channel)
    r = authorize(tel_num, secret, login_type=login_type, channel=channel)
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    out = SAVE_DIR / ('authorize_%s.json' % stamp)
    out.write_text(json.dumps({
        'tel_masked': str(tel_num)[:3] + '****' + str(tel_num)[-2:],
        cred + '_len': len(str(secret)),        # 只记长度，凭据不落盘
        'login_type': login_type,
        'channel': channel,
        'resp': r,
    }, ensure_ascii=False, indent=2), encoding='utf-8')

    good, emsg = _check(r)
    if not good:
        raise LoginError('%s  (原始响应已存 %s)' % (emsg or json.dumps(r, ensure_ascii=False), out.name))

    # 短信登录成功响应示例（依据客户端 SDK 日志）：
    #   {"ret":"1","msg":"success","content":{"authorize_code":"04dOO…","user_name":"c:z27…",
    #    "user_id":"3Rku…","phone":"","reg_login":0,"data":{…}}}
    #   LoginHelper 打印的 sid 即该 authorize_code，故它排在取值列表首位。
    sid = pick(r,
               'content.authorize_code', 'authorize_code',
               'content.access_token', 'data.access_token',   # 网页端 login_type=18 的写法
               'content.accessToken', 'data.accessToken',
               'content.0.sid', 'content.sid', 'data.sid',
               'content.0.authorize_code', 'data.authorize_code',
               'content.0.accessToken', 'content.0.token', 'data.token',
               'sid', 'accessToken')
    if not sid:
        raise LoginError('登录返回里没找到 sid（存档：%s）' % out.name)
    return sid, r


def phone_login(tel_num, code, channel='test_junhai', login_type='3', do_init=True):
    """手机验证码登录（login_type=3，凭据字段 smscode）→ (sid, 原始响应)"""
    return _submit(tel_num, code, login_type, channel, do_init,
                   'password' if str(login_type) in ('1', '18') else 'smscode')


def pwd_hash(plain):
    """账号系统的密码摘要：md5(明文 + md5(明文))，小写十六进制。

    出处是官方网页端自己的实现（se.feimogames.com 的 useApi-*.js，pwdLogin 原文
    即 md5(e.password + md5(e.password))）；明文直发服务器只会回"密码错误"。
    """
    h1 = hashlib.md5(plain.encode('utf-8')).hexdigest()
    return hashlib.md5((plain + h1).encode('utf-8')).hexdigest()


def password_login(tel_num, password, channel='test_junhai', do_init=True):
    """手机号 + 密码登录（login_type=18，凭据字段 password）→ (sid, 原始响应)

    登录方式与密码摘要均由官网网页端源码确定（useApi-*.js 的 pwdLogin）：
        { login_type: 18, tel_num, password: md5(p + md5(p)) } → POST /account/authorize
    注意 login_type 是 18 而不是 1（用 1 恒回"密码错误"）。
    明文密码只在本机内存里过一遍 —— 不打印、不写日志、不落盘，存档里只记长度。
    """
    sid, resp = _submit(tel_num, pwd_hash(str(password)), '18', channel, do_init, 'password')
    # 18 只给 access_token，游戏协议要的是 authorize_code ⇒ 再走一次 login_type=2
    # （与 Steam/B站/TapTap 复用渠道登录态完全同一条路）
    tok = pick(resp, 'content.access_token', 'content.accessToken',
               'data.access_token', 'data.accessToken', 'accessToken')
    if not tok or str(sid).startswith('04'):        # 已经是 authorize_code 就不用再换
        return sid, resp
    return token_login(tok, tel_num, channel, do_init=True)


def token_login(access_token, tel='', channel='test_junhai', do_init=True):
    """用渠道客户端的会话票登录（login_type=2）→ (sid, 原始响应)

    用途：复用本机渠道客户端（Steam 国服 / 国际服 / B站渠道…）已经登录的那张票，
    免验证码进入协议。票由 astral/game_session.py 从运行中的游戏进程内存读取。

    参数名必须为 access_token（写成 token 会被服务器回 signError）。
    票一次一换：每次登录都会下发新的 accessToken，需存回 token.json 供下次使用。
    tel 只影响 SDK 侧记录的账号，留空即可（票本身已带账号）。
    """
    if do_init:
        sdk_init(channel)
    r = authorize(tel, '', login_type='2', channel=channel, access_token=access_token)
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    out = SAVE_DIR / ('authorize_%s.json' % stamp)
    out.write_text(json.dumps({
        'login_type': '2',
        'via': 'channel client token',
        'token_masked': '%s…%s(%d位)' % (str(access_token)[:5], str(access_token)[-4:],
                                        len(str(access_token))),
        'channel': channel,
        'resp': r,
    }, ensure_ascii=False, indent=2), encoding='utf-8')

    good, emsg = _check(r)
    if not good:
        raise LoginError('%s  (原始响应已存 %s)'
                         % (emsg or json.dumps(r, ensure_ascii=False), out.name))

    sid = pick(r,
               'content.authorize_code', 'authorize_code',
               'content.access_token', 'data.access_token',   # 网页端 login_type=18 的写法
               'content.accessToken', 'data.accessToken',
               'content.0.sid', 'content.sid', 'data.sid',
               'content.0.authorize_code', 'data.authorize_code',
               'content.0.accessToken', 'content.0.token', 'data.token',
               'sid', 'accessToken')
    if not sid:
        raise LoginError('登录返回里没找到 sid（存档：%s）' % out.name)
    return sid, r


def describe(resp):
    """把响应里能看懂的字段挑出来（不打印票据明文）"""
    info = {}
    for label, paths in (
        ('uid', ('content.0.uid', 'data.uid', 'uid')),
        ('role_id', ('content.0.role_id', 'data.role_id', 'role_id')),
        ('server', ('content.0.server', 'data.server', 'server')),
        ('nick', ('content.0.nick', 'data.nick', 'nick', 'content.0.nick_name')),
        ('expire_at', ('content.0.expire_at', 'data.expire_at')),
    ):
        v = pick(resp, *paths)
        if v not in (None, ''):
            info[label] = v
    sid = pick(resp, 'content.0.sid', 'data.sid', 'sid')
    info['sid'] = (str(sid)[:6] + '…(%d位)' % len(str(sid))) if sid else '(无)'
    return info


if __name__ == '__main__':
    import sys
    tel = sys.argv[1] if len(sys.argv) > 1 else '1'
    code = sys.argv[2] if len(sys.argv) > 2 else '000000'
    print('== sendCode(%s) ==' % tel)
    try:
        print(json.dumps(request_code(tel), ensure_ascii=False)[:300])
    except LoginError as e:
        print('  失败: %s' % (e))
    print('\n== authorize(%s, %s) ==' % (tel, code))
    try:
        sid, r = phone_login(tel, code)
        print('  sid = %s' % (sid[:8] + '…'))
        print('  字段: %s' % (describe(r)))
    except LoginError as e:
        print('  失败: %s' % (e))


# ══════════ 记住登录（accessToken 复用）══════════
_PROJ = paths.EXE_DIR   # 项目根 / exe 所在目录
TOKEN_FILE = paths.DATA_DIR / 'token.json'


def save_token(resp, tel=''):
    """从登录响应里抠出 accessToken 存本地（只在本机，不外传）。"""
    tok = pick(resp, 'content.data.accessToken',   # 即为该路径
               'content.accessToken', 'data.accessToken',
               'content.access_token', 'data.access_token',   # 网页端 login_type=18
               'content.0.accessToken', 'accessToken',
               'content.token', 'data.token')
    if not tok:
        return None
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(
        {'accessToken': tok, 'tel': str(tel) if tel else '',
         'tel_masked': (str(tel)[:3] + '****' + str(tel)[-2:]) if tel else '',
         'saved_at': datetime.now().isoformat(timespec='seconds')},
        ensure_ascii=False), encoding='utf-8')
    return tok


def load_token():
    try:
        return json.loads(TOKEN_FILE.read_text(encoding='utf-8')).get('accessToken')
    except Exception:
        return None


def load_token_info():
    try:
        return json.loads(TOKEN_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {}


def auto_login(channel='test_junhai'):
    """用本地保存的 accessToken 自动登录（免验证码）。失败返回 (None, 原因)。"""
    info = load_token_info()
    tok = info.get('accessToken')
    tel = info.get('tel') or ''
    if not tok:
        return None, '本机没有记住的登录态'
    last = ''
    for key in ('access_token', 'token'):
        try:
            sdk_init(channel)
            r = authorize(tel, '', login_type='2', channel=channel, **{key: tok})
            good, emsg = _check(r)
            if not good:
                last = str(emsg or r.get('msg') or '')[:60]
                continue
            sid = pick(r, 'content.authorize_code', 'authorize_code',
                       'content.accessToken', 'sid', 'accessToken')
            if sid:
                return sid, ''
            last = '响应里没有 sid'
        except Exception as e:
            last = str(e)[:60]
    return None, last


def patch_token_meta(**kw):
    """把 uid/昵称等补充信息写回 token.json（不动 accessToken）。"""
    try:
        info = load_token_info()
        if not info.get('accessToken'):
            return False
        info.update({k: v for k, v in kw.items() if v not in (None, '')})
        TOKEN_FILE.write_text(json.dumps(info, ensure_ascii=False), encoding='utf-8')
        return True
    except Exception:
        return False


def clear_token():
    """退出登录：删掉本机记住的登录态。"""
    try:
        TOKEN_FILE.unlink()
        return True
    except Exception:
        return False

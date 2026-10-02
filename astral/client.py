# -*- coding: utf-8 -*-
"""吉星派对客户端：TCP 连接 + 登录 + 数据查询

用法：
    python -m astral.client --uid 1000000                 # 查玩家资料+统计
    python -m astral.client --uid 1000000 --records 20    # 拉最近 20 局战绩
    python -m astral.client --read-sdk-log                # 读游戏 SDK 日志，核对登录请求的真实格式
"""
from __future__ import annotations

import argparse
import json
import re
import socket
import struct
import threading
import time
from pathlib import Path

from . import frame, proto_loader
from .log import debug

# ── 服务端地址（国服）──
WEB_HOST = 'se-web-cn.feimogames.com'
HOTADDRESS_PATH = '/api/hotaddressServer/get?route='   # 服务器列表接口
LOG_SERVER = 'se-client-log-cn.feimogames.com:8810'
REPLAY_SERVER = 'https://sereplaycn.feimogames.com/prod/'
DEFAULT_GAME_PORT = 8800

# 国服 SDK 参数（取自客户端字符串；Sid 需登录后获得）
APP_ID = '110001933'        # SDK appId（登录握手实际使用）
GAME_ID = '120000182'
CHANNEL_ID = '2'            # channelId 为个位数字符串（并非 appId）
SDK_BASE = 'https://m-sdk.feimogames.com'
SDK_LOG_DIR = Path.home() / 'AppData/LocalLow/feimo/AstralParty_CN/BnSdk/110001933/Log'


class ProtocolError(Exception):
    pass


class AstralClient:
    """TCP 客户端：登录握手 + RPC 调用"""

    def __init__(self, host: str, port: int, timeout: float = 15.0,
                 xor_mode: str = frame.XOR_NONE):
        self.host, self.port = host, port
        self.timeout = timeout
        self.xor_mode = xor_mode
        self.sock: socket.socket | None = None
        self.session_id = 0
        self.cipher_key = ''
        self.upsn = 0
        self.downsn = 0
        self._buf = b''
        self._lock = threading.Lock()
        self._waiters: dict[int, list] = {}    # cmd_id -> [msg, ...]

    # ── 连接 ──
    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(0.5)
        threading.Thread(target=self._recv_loop, daemon=True).start()
        return self

    def close(self):
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None

    # ── 收包：按 length 切帧 ──
    def _recv_loop(self):
        while self.sock:
            try:
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                self._buf += chunk
                self._drain()
            except socket.timeout:
                continue
            except OSError:
                break

    def _drain(self):
        while len(self._buf) >= frame.HEAD_LEN:
            body_len = frame.peek_length(self._buf, self.xor_mode)
            total = body_len + frame.HEAD_LEN      # 真帧长 = LENGTH + 35（HEAD_LEN）
            if body_len < 0 or total > 8 * 1024 * 1024:
                # 长度不像样，丢一个字节重新对齐
                self._buf = self._buf[1:]
                continue
            if len(self._buf) < total:
                return
            pkt, self._buf = self._buf[:total], self._buf[total:]
            try:
                f = frame.decode(pkt, self.xor_mode)
            except Exception:
                continue
            self._dispatch(f)

    def _dispatch(self, f: dict):
        # 记录收到的每一帧（含包体），用于超时排查与推送包解码
        try:
            self.received.append((f.get('cmd_id'), f.get('err')))
        except AttributeError:
            self.received = [(f.get('cmd_id'), f.get('err'))]
        try:
            _b = f.get('payload') or f.get('body') or f.get('data')
            self.raw_received.append((f.get('cmd_id'), f.get('err'), _b))
        except AttributeError:
            _b = f.get('payload') or f.get('body') or f.get('data')
            self.raw_received = [(f.get('cmd_id'), f.get('err'), _b)]
        cmd = f['cmd_id']
        if getattr(self, 'debug', False):
            debug('   ← 收到包 cmd=%s err=%s len=%d' % (cmd, f.get('err'), len(f.get('body') or b'')))
        if f['downsn']:
            self.downsn = f['downsn']
        with self._lock:
            # 用 setdefault 而不是 get：服务器回包可能**早于** wait_for 注册收件箱
            # （本地往返仅十几毫秒），用 get 会取到 None 而丢包，最终表现为超时。
            # 因此先建收件箱再投递。
            q = self._waiters.setdefault(cmd, [])
            if len(q) < 64:
                q.append(f)

    # ── 发包 ──
    def send(self, cmd_id: int, msg=None, wait: float | None = None, expect: int | None = None):
        """发包。协议规定 S2C 的 CMDID = C2S + 1，因此默认等待 cmd_id+1；
        同时兼容服务器以同号回错误包的情况（两个号都监听）。"""
        body = (bytes(msg) if isinstance(msg, (bytes, bytearray))
                else (msg.SerializeToString() if msg is not None else b''))
        self.upsn += 1
        pkt = frame.encode(cmd_id, body, session_id=self.session_id,
                           upsn=self.upsn, downsn=self.downsn, mode=self.xor_mode)
        if not self.sock:
            raise ProtocolError('未连接')
        self.sock.sendall(pkt)
        if wait is None:
            return None
        return self.wait_for(expect if expect is not None else cmd_id + 1, wait, also=cmd_id)

    def wait_for(self, cmd_id: int, timeout: float = 10.0, also: int | None = None) -> dict:
        ids = [cmd_id] + ([also] if also is not None and also != cmd_id else [])
        with self._lock:
            qs = [self._waiters.setdefault(i, []) for i in ids]
        t0 = time.time()
        while time.time() - t0 < timeout:
            for q in qs:
                if q:
                    return q.pop(0)
            time.sleep(0.05)
        raise ProtocolError('等待 CMDID %s 响应超时；期间收到: %s'
                            % ('/'.join(str(i) for i in ids),
                               getattr(self, 'received', [])[-8:] or '（一个包都没收到）'))

    # ── 登录（CMDID 5001）──
    def login_china(self, sid: str, device_id: str = '', extra: str = '',
                    client_ver: str = '3.2.0', game_id: str | None = None,
                    channel_id: str | None = None, app_id: str | None = None):
        """client_ver 必须为 '3.2.0'（其它取值会被服务器判为客户端版本错误）
        game_id/channel_id/app_id 可覆盖（排查 AuthErr 用；proto3 中传 '' 等于不传该字段）
        """
        m = proto_loader.new_msg('protocol.ConnectC2S')
        m.publicKey = proto_loader.PUBLIC_KEY
        m.auth = proto_loader.AUTH_TYPE['China']
        m.clientVer = client_ver
        m.china.gameId = GAME_ID if game_id is None else game_id
        m.china.channelId = CHANNEL_ID if channel_id is None else channel_id
        m.china.appId = APP_ID if app_id is None else app_id
        m.china.sid = sid
        m.china.extra = extra
        m.china.deviceId = device_id
        rsp = self.send(5001, m, wait=15.0)
        s2c = proto_loader.new_msg('protocol.ConnectS2C')
        s2c.ParseFromString(rsp['body'])
        if rsp['err'] != 0:
            raise ProtocolError('登录被拒，err=%d' % rsp['err'])
        self.session_id = s2c.sessionId
        self.cipher_key = s2c.cipherKey
        return s2c

    # ── 数据查询 ──
    def heartbeat(self, value: int | None = None):
        """心跳（CMDID 5003，载荷 = tag 0x09 + fixed64 小端），游戏约每 5 秒发一次。
        查询前先补一次，避免服务器判定会话已死。"""
        import struct as _s
        import time as _t
        v = value if value is not None else (int(_t.monotonic() * 1000) & 0xFFFFFFFFFFFF)
        self.send(5003, b'\x09' + _s.pack('<Q', v), wait=None)

    def get_show_player(self, player_id: int, timeout: float = 10.0):
        """玩家展示信息：统计（场次/胜场）+ 最近战绩 + 回放"""
        self.heartbeat()          # 先补心跳
        m = proto_loader.new_msg('protocol.GetShowPlayerC2S')
        m.player_id = player_id
        rsp = self.send(5153, m, wait=timeout)
        out = proto_loader.new_msg('protocol.GetShowPlayerS2C')
        out.ParseFromString(rsp['body'])
        return out

    def get_fight_records(self, player_id: int, index: int = 0, timeout: float = 10.0):
        """战绩明细（按 index 分页）"""
        m = proto_loader.new_msg('protocol.GetPlayerFightRecordC2S')
        m.player_id = player_id
        m.index = index
        rsp = self.send(5155, m, wait=timeout)
        out = proto_loader.new_msg('protocol.GetPlayerFightRecordS2C')
        out.ParseFromString(rsp['body'])
        return out

    def get_player_simple(self, player_id: int, timeout: float = 10.0):
        m = proto_loader.new_msg('protocol.GetPlayerSimpleC2S')
        m.player_id = player_id
        rsp = self.send(5263, m, wait=timeout)
        out = proto_loader.new_msg('protocol.GetPlayerSimpleS2C')
        out.ParseFromString(rsp['body'])
        return out


# ── SDK 登录（请求格式以客户端 SDK 日志为准）──
def read_sdk_log(lines: int = 200):
    """读游戏 SDK 日志 —— 用来确认真实登录请求的 URL/参数格式"""
    if not SDK_LOG_DIR.exists():
        return {'error': '日志目录不存在: %s' % SDK_LOG_DIR}
    logs = sorted(SDK_LOG_DIR.glob('*.log'), key=lambda p: p.stat().st_mtime, reverse=True)
    out = {}
    for p in logs[:3]:
        txt = p.read_text(encoding='utf-8', errors='replace')
        urls = sorted(set(re.findall(r'https?://[^\s"\'<>,)\]]+', txt)))
        out[p.name] = {'mtime': time.strftime('%Y-%m-%d %H:%M', time.localtime(p.stat().st_mtime)),
                       'urls': urls, 'tail': txt.splitlines()[-lines:]}
    return out


def main():
    ap = argparse.ArgumentParser(description='吉星派对 数据查询工具')
    ap.add_argument('--uid', type=int, help='玩家 playerId')
    ap.add_argument('--records', type=int, default=0, help='拉最近 N 局战绩')
    ap.add_argument('--host', default=None, help='游戏服地址（默认从服务器列表接口取）')
    ap.add_argument('--port', type=int, default=DEFAULT_GAME_PORT)
    ap.add_argument('--sid', default='', help='SDK 会话票（登录用）')
    ap.add_argument('--xor', choices=['none', 'body', 'full'], default='none')
    ap.add_argument('--read-sdk-log', action='store_true', help='读游戏日志看登录格式')
    args = ap.parse_args()

    if args.read_sdk_log:
        print(json.dumps(read_sdk_log(), ensure_ascii=False, indent=2)[:6000])
        return

    if not args.host:
        print('还没有拿到游戏服地址。可选两条途径：')
        print('   1) 用 --host 手动指定')
        print('   2) 由服务器列表接口自动获取（待实现：%s%s）' % (WEB_HOST, HOTADDRESS_PATH))
        return

    c = AstralClient(args.host, args.port, xor_mode=args.xor).connect()
    print('已连接 %s:%d' % (args.host, args.port))
    if args.sid:
        s2c = c.login_china(args.sid)
        print('登录成功: sessionId=%d cipherKey=%s' % (s2c.sessionId, s2c.cipherKey[:8] + '…'))
    if args.uid:
        sp = c.get_show_player(args.uid)
        print(sp)
        st = sp.statistics
        if st.fightCount:
            print('\n对战场次 %d | 胜场 %d | 胜率 %.1f%%' % (
                st.fightCount, st.winFightCount, st.winFightCount / st.fightCount * 100))
    c.close()


if __name__ == '__main__':
    main()

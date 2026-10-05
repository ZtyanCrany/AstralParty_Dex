# -*- coding: utf-8 -*-
"""吉星派对客户端协议 —— protobuf 描述符加载器

描述符取自 assets/proto/*.pb，与客户端热更程序集
AstralParty.Runtime.dll 中的定义一致
"""

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from astral import paths            # noqa: E402
from astral.log import log

PROTO_DIR = paths.asset('assets', 'proto')

# 依赖顺序（protocol 依赖 model + errcode）
ORDER = [
    'model/model.proto',
    'errcode/code.proto',
    'protocol/interior.proto',
    'protocol/protocol.proto',
]

_pool = None


def pool() -> descriptor_pool.DescriptorPool:
    """加载全部 .pb 描述符，返回已就绪的 DescriptorPool。"""
    global _pool
    if _pool is not None:
        return _pool

    fds = {}
    for p in PROTO_DIR.glob('*.pb'):
        fdp = descriptor_pb2.FileDescriptorProto()
        fdp.ParseFromString(p.read_bytes())
        fds[fdp.name] = fdp

    p = descriptor_pool.DescriptorPool()
    missing = []
    for name in ORDER:
        if name in fds:
            p.Add(fds[name])
        else:
            missing.append(name)
    # 兜底：把剩下的一并加上
    for name, fdp in fds.items():
        if name not in ORDER:
            try:
                p.Add(fdp)
            except Exception:
                pass
    if missing:
        log('[proto_loader] 缺少描述符: %s' % missing)

    _pool = p
    return _pool


def msg_class(full_name: str):
    """按全名拿消息类，例如 'protocol.GetShowPlayerC2S' / 'model.PlayerFightData'"""
    return message_factory.GetMessageClass(pool().FindMessageTypeByName(full_name))


def new_msg(full_name: str):
    """按全名造一个空消息"""
    return msg_class(full_name)()


# ── 命令号表（来自 Core.Net.*RPC 的 RPCCallStatic(req, CMDID)） ──
CMDS = {
    5001: ('ConnectC2S', 'ConnectS2C', '登录握手'),
    5003: ('HeartbeatC2S', 'HeartbeatS2C', '心跳'),
    5153: ('GetShowPlayerC2S', 'GetShowPlayerS2C', '玩家展示信息（统计+战绩）'),
    5155: ('GetPlayerFightRecordC2S', 'GetPlayerFightRecordS2C', '玩家战绩记录'),
    5263: ('GetPlayerSimpleC2S', 'GetPlayerSimpleS2C', '玩家简要信息'),
}

# 客户端要发的请求：CMDID -> 请求消息全名
REQ_NAMES = {
    5001: 'protocol.ConnectC2S',
    5003: 'protocol.HeartbeatC2S',
    5153: 'protocol.GetShowPlayerC2S',
    5155: 'protocol.GetPlayerFightRecordC2S',
    5263: 'protocol.GetPlayerSimpleC2S',
}

# 服务端回复：CMDID -> 响应消息全名
RSP_NAMES = {
    5001: 'protocol.ConnectS2C',
    5003: 'protocol.HeartbeatS2C',
    5153: 'protocol.GetShowPlayerS2C',
    5155: 'protocol.GetPlayerFightRecordS2C',
    5263: 'protocol.GetPlayerSimpleS2C',
}

# 已确认的枚举（来自 protocol.proto）
AUTH_TYPE = {'Dev': 0, 'Steam': 1, 'TapTap': 2, 'Abroad': 3, 'China': 4}

PUBLIC_KEY = 't4UDM%2Q'  # ConnectC2S.PublicKey 硬编码值


if __name__ == '__main__':
    p = pool()
    print('pool 文件数: %s' % (len(ORDER)))
    for name in ('protocol.ConnectC2S', 'protocol.ChinaInfo', 'protocol.ConnectS2C',
                 'protocol.GetShowPlayerC2S', 'protocol.ShowPlayer',
                 'protocol.ShowPlayerStatistics', 'protocol.ShowPlayerShortFight',
                 'model.PlayerFightData'):
        d = p.FindMessageTypeByName(name)
        print('  %-42s %2d 字段' % (name, len(d.fields)))
    print('\nCMD 表:')
    for k, v in sorted(CMDS.items()):
        print('  %-5d %-28s %s' % (k, v[0], v[2]))

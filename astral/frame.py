# -*- coding: utf-8 -*-
"""吉星派对帧编解码（大端）

包头 35 字节（来自 Core.Net.Frame，字段读顺序）：
    LENGTH   int32   4
    SESSIONID int64  8
    CMDID    int16   2
    VER1/2/3 uint8   3
    UPSN     int64   8
    DOWNSN   int64   8
    ERR      int16   2
合计 35

载荷：protobuf 二进制
可选混淆：ByteBuf.Xor(start, end) —— 从 start 起逐字节 XOR keyTable[0..255]（循环）
        具体作用范围位于 AOT 层，热更代码中未找到调用点，因此做成可配置项。
"""
from __future__ import annotations

import struct

from .keytable import KEY_TABLE

HEAD_LEN = 35
HEAD_STRUCT = struct.Struct('>iqhBBBqqh')  # LENGTH, SESSIONID, CMDID, VER1..3, UPSN, DOWNSN, ERR

XOR_NONE = 'none'      # 不混淆（默认）
XOR_BODY = 'body'      # 只混淆 protobuf 体
XOR_FULL = 'full'      # 整个包（含包头）都混淆


def xor_range(data: bytes, start: int = 0) -> bytes:
    """等价于 C# 的 ByteBuf.Xor(start, end)：从 start 开始逐字节异或 keyTable 循环"""
    out = bytearray(data)
    n = 0
    for i in range(start, len(out)):
        out[i] ^= KEY_TABLE[n % 256]
        n += 1
    return bytes(out)


def encode(cmd_id: int, body: bytes = b'', session_id: int = 0,
           ver: tuple = (1, 0, 0), upsn: int = 0, downsn: int = 0, err: int = 0,
           mode: str = XOR_NONE) -> bytes:
    """组一个完整帧"""
    # LENGTH 字段 = 载荷长度（不含 35 字节包头）—— 见 USocket.DealBuffer():
    #   int num2 = tempbuffer.GetInt(0); if (num >= num2 + 35) …真帧长 = num2 + 35
    head = HEAD_STRUCT.pack(len(body), session_id, cmd_id,
                            ver[0] & 0xFF, ver[1] & 0xFF, ver[2] & 0xFF,
                            upsn, downsn, err)
    pkt = head + body
    if mode == XOR_BODY:
        pkt = head + xor_range(body)
    elif mode == XOR_FULL:
        pkt = xor_range(pkt)
    return pkt


def decode(data: bytes, mode: str = XOR_NONE) -> dict:
    """拆一个完整帧；data 必须从帧头开始"""
    if mode == XOR_FULL:
        data = xor_range(data)
    if len(data) < HEAD_LEN:
        raise ValueError('数据不足 %d 字节: %d' % (HEAD_LEN, len(data)))
    (length, session_id, cmd_id, v1, v2, v3, upsn, downsn, err) = HEAD_STRUCT.unpack_from(data, 0)
    end = HEAD_LEN + length          # LENGTH 只算载荷，真帧长 = LENGTH + 35
    body = data[HEAD_LEN:end] if 0 < length and end <= len(data) else data[HEAD_LEN:]
    if mode == XOR_BODY:
        body = xor_range(body)
    return {
        'length': length, 'session_id': session_id, 'cmd_id': cmd_id,
        'ver': (v1, v2, v3), 'upsn': upsn, 'downsn': downsn, 'err': err,
        'body': body, 'raw_len': len(data),
    }


def peek_length(data: bytes, mode: str = XOR_NONE) -> int:
    """取 LENGTH 字段 = 载荷长度（真帧长 = 本值 + HEAD_LEN）"""
    head = xor_range(data[:4]) if mode == XOR_FULL else data[:4]
    if len(head) < 4:
        return -1
    return struct.unpack_from('>i', head, 0)[0]

# -*- coding: utf-8 -*-
"""渠道签名密钥表 —— 单独抽出的可替换文件。

为什么单独放一个文件：
    这些是**游戏官方客户端内置的常量**（并非本项目作者的任何账号或私密凭据），
    但公开分发仍属敏感。单独成文件的目的是——若需停止分发这些密钥，
    **删除本文件**即可，签名算法 / 协议 / 界面等其余源码完全不受影响。
    删除后本工具仍可运行，只需换成自己的渠道密钥（或改用别的登录方式）。

用法
----
    from .signkeys import SIGN_KEYS, DEFAULT_KEY

    `astral/sign.py` 会原样转出，因此下列写法同样有效：
    `from astral.sign import SIGN_KEYS, DEFAULT_KEY`
"""

# 渠道 → 密钥（签名 = md5(按字母序拼接的参数串 + 该渠道密钥)）
SIGN_KEYS = {
    'steam':     'bd531da6ddaac750577e7664c25d673c',
    'steam_cn':  '6e3892a2ba61014897f85c2de4530f9e',
    'wegame':    '3fa10741d50db497778bbb6c67fa05ad',
    'taptap':    '6586f8d6ab34f805997a06732fa181df',
    'taptap_gb': '67f6128727f2d8345a30d2d041797571',
    'bilibili':  'a4da22321c5f2eeb4501bd6c788f3e13',
    # 国服 Steam 客户端实际渠道为 test_junhai，
    # 密钥由游戏自身的成功签名（7d0c0890…）反推确认。
    'test_junhai': 'bd531da6ddaac750577e7664c25d673c',
}

DEFAULT_KEY = SIGN_KEYS['test_junhai']      # 默认渠道（test_junhai）的密钥

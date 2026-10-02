# -*- coding: utf-8 -*-
"""抓 Wiki 各张地图的「地图任务」列表 → assets/data/missions.json"""
import json
import re
import ssl
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, r'D:\Xun_crany\星趴档案')
BASE = 'https://wiki.biligame.com/starengine/'
HDR = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
       'Referer': 'https://wiki.biligame.com/starengine/'}
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

# 地图名 → 地图ID（与主程序的地图表一致）
MAPS = {81005: '龙宫游乐园（旧）', 81007: '梦想号-上层甲板', 82007: '星趴·梦想号',
        82008: '御魂庆典', 82010: '水乡古镇', 82012: '魔法学院', 82013: '龙宫游乐园',
        82014: '幽魂暗巷', 82015: '园林中庭', 82016: '异变图书馆',
        83001: '海选赛运动场', 83002: '淘汰赛运动场', 83003: '决赛大赛场'}
WIKI_TITLE = {81005: '龙宫游乐园', 81007: '梦想号-上层甲板', 82007: '星趴·梦想号'}


def raw(title):
    url = BASE + urllib.parse.quote(title) + '?action=raw'
    req = urllib.request.Request(url, headers=HDR)
    return urllib.request.urlopen(req, timeout=45, context=ctx).read().decode('utf-8', 'replace')


def parse_tasks(w):
    """返回 (普通难度任务名列表, 困难难度任务名列表)"""
    # 格式A：模板 {{Map1|...|任务=A，B，C|数量=0\4，0\9，0\2|...}}
    out = []
    for m in re.finditer(r'\|任务\s*=\s*([^\n|}]+)', w):
        names = [x.strip() for x in re.split(r'[，,]', m.group(1)) if x.strip()]
        out.append(names)
    if out:
        return out
    # 格式B：==地图任务== 下的 wikitable，第一列 “任务”
    sec = re.search(r'==\s*地图任务\s*==(.*?)(?=\n==[^=]|$)', w, re.S)
    if not sec:
        return []
    names = []
    for line in sec.group(1).splitlines():
        line = line.strip()
        if not line.startswith('|') or line.startswith('|-') or line.startswith('|}'):
            continue
        first = line[1:].split('||')[0]
        first = re.sub(r'<[^>]+>', '', first)
        first = re.sub(r'\{\{[^}]*\}\}', '', first)
        first = re.sub(r'\[\[[^\]|]*\|([^\]]*)\]\]', r'\1', first)
        first = re.sub(r'\[\[([^\]]*)\]\]', r'\1', first)
        first = re.sub(r"''+", '', first).strip()
        if first and first not in ('任务', '奖励'):
            names.append(first)
    return [names] if names else []


res, missing = {}, []
for mid, name in MAPS.items():
    title = WIKI_TITLE.get(mid, name)
    try:
        w = raw(title)
        tasks = parse_tasks(w)
    except Exception as e:
        missing.append((name, str(e)[:60]))
        continue
    if not tasks:
        missing.append((name, '页面里没找到地图任务段'))
        continue
    normal = tasks[0]
    hard = tasks[1] if len(tasks) > 1 else tasks[0]
    res[str(mid)] = {'name': name, 'title': title, 'normal': normal, 'hard': hard}
    print('◆ %-14s (%s)  普通 %d 个任务:' % (name, title, len(normal)))
    for i, t in enumerate(normal, 1):
        print('      %d. %s' % (i, t))

Path(__file__).resolve().parents[1].joinpath('assets', 'data', 'missions.json').write_text(
    json.dumps(res, ensure_ascii=False, indent=1), encoding='utf-8')
print('\n已写入 assets/data/missions.json（%d 张地图）' % len(res))
if missing:
    print('没抓到:', missing)

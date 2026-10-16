# -*- coding: utf-8 -*-
"""抓 Wiki 各张地图的「地图任务」列表，合并进 assets/data/missions.json。

默认只报告差异，`--apply` 才写文件（与 tools/sync_*.py 一致）。

两点注意：
1. Wiki 整份覆盖会丢数据——客户端同步补进来的地图（81003/81004/81006/81007/81009/
   81011/82017 等）Wiki 里没有对应页面，所以这里只更新抓到的地图，其余原样保留。
2. 新地图的 Wiki 页面可能是从别的地图复制来的（页面挂 {{施工中}}、预览图是别的编号、
   任务段与另一张图逐字相同）——这种数据先用客户端核对再采用（见 skill
   astral-party-reverse-engineering 的 references/map-mission-names.md）。
"""
import json
import re
import ssl
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from astral.paths import asset                                    # noqa: E402

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
    # 格式B：==地图任务== 下的 wikitable，第一列 "任务"
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
        first = re.sub(r'\s*\d\\\d+\s*$', '', first).strip()   # 去掉「0\3」这类目标数
        if first and first not in ('任务', '奖励'):
            names.append(first)
    return [names] if names else []


def main():
    apply = '--apply' in sys.argv
    f = asset('assets', 'data', 'missions.json')
    cur = json.loads(f.read_text(encoding='utf-8')) if f.exists() else {}
    fetched, missing, changed, locked = {}, [], [], []
    for mid, name in MAPS.items():
        title = WIKI_TITLE.get(mid, name)
        try:
            tasks = parse_tasks(raw(title))
        except Exception as e:
            missing.append((name, str(e)[:60]))
            continue
        if not tasks:
            missing.append((name, '页面里没找到地图任务段'))
            continue
        normal = tasks[0]
        hard = tasks[1] if len(tasks) > 1 else tasks[0]
        rec = dict(cur.get(str(mid)) or {})
        if rec.get('tasks'):
            # 已按客户端任务 ID 校准过的地图，Wiki 的顺序不可信（幽魂暗巷就是错位的）
            locked.append(rec.get('name') or name)
            continue
        rec.update({'name': rec.get('name') or name, 'title': title,
                    'normal': normal, 'hard': hard})
        fetched[str(mid)] = rec
        before = cur.get(str(mid)) or {}
        if before.get('normal') != normal or before.get('hard') != hard:
            changed.append((mid, rec.get('name'), before.get('normal') or [], normal))

    print('抓到 %d 张地图；缺失 %d 张 %s' % (len(fetched), len(missing), missing or ''))
    print('任务名有变化的地图：%d 张' % len(changed))
    for mid, name, old, new in changed:
        print('   ◆ %s %s' % (mid, name))
        print('      旧：%s' % (old or '（空）'))
        print('      新：%s' % new)
    untouched = [k for k in cur if k not in fetched]
    print('保留不动的地图（Wiki 里没有）：%s' % ', '.join(sorted(untouched)))
    if locked:
        print('已按客户端校准、跳过 Wiki 的地图：%s' % '、'.join(locked))

    if not apply:
        print('\n（只是报告；要写入加 --apply）')
        return
    cur.update(fetched)
    cur = {k: cur[k] for k in sorted(cur, key=lambda x: (len(x), x))}
    f.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding='utf-8')
    print('\n已写入 %s（共 %d 张地图）' % (f, len(cur)))


if __name__ == '__main__':
    main()

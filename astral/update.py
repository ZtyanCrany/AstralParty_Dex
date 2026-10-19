# -*- coding: utf-8 -*-
"""检查 GitHub Release 有没有新版本，并支持「就地更新」。

设计要点（踩过的坑都写在注释里）：
  · GitHub API 对未登录请求限流 60 次/小时/IP，而且国内经常被拦；
    所以先用网页的 /releases/latest 重定向拿 tag（不吃限流），API 只当补充。
  · Release 的 zip 名带 v（AstralParty_Dex_v1.2.6_win64.zip），tag 却可能是 1.2.6，
    两种写法都要能拼出来。
  · 更新包解压后整目录覆盖，但 **userdata/ 必须保留**（里面有免密登录态）。
  · exe 不能覆盖自己：等本进程退出后 robocopy 过去再重启。
    覆盖脚本用 powershell -EncodedCommand 而不是 .bat —— 路径含中文时
    cmd 按本地代码页解析脚本会乱码，robocopy 静默失败（实测）。
"""
import html as html_lib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

OWNER, REPO = 'ZtyanCrany', 'AstralParty_Dex'
API_LATEST = 'https://api.github.com/repos/%s/%s/releases/latest' % (OWNER, REPO)
PAGE_LATEST = 'https://github.com/%s/%s/releases/latest' % (OWNER, REPO)
PAGE_TAG = 'https://github.com/%s/%s/releases/tag/%s' % (OWNER, REPO, '%s')
EXPANDED = 'https://github.com/%s/%s/releases/expanded_assets/%s' % (OWNER, REPO, '%s')
CHANGELOG_RAW = 'https://raw.githubusercontent.com/%s/%s/main/CHANGELOG.md' % (OWNER, REPO)
UA = 'AstralParty_Dex-updater'
CHECK_TTL = 6 * 3600          # 检查结果缓存 6 小时，别每次开都打 GitHub
CACHE_VER = 2                 # 更新说明改用 Release 正文后，旧缓存作废重查
_VER_RE = re.compile(r'[vV]?(\d+)\.(\d+)\.(\d+)')


# ── 版本号 ─────────────────────────────────────────────────────
def parse_ver(s):
    """'v1.2.6' / '1.2.6' / 'release 1.2.6' → (1, 2, 6)；认不出就当 0。"""
    m = _VER_RE.search(str(s or ''))
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def newer_than(tag, current):
    return parse_ver(tag) > parse_ver(current)


# ── 网络 ───────────────────────────────────────────────────────
def _get(url, timeout=10, headers=None):
    """GET 一次；网络抖动时再试一遍（国内直连 GitHub 偶发连接被掐）。"""
    last = None
    for i in range(2):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:      # noqa: BLE001
            last = e
            if i == 0:
                time.sleep(0.6)
    raise last


def _div_block(html, marker):
    """截出带 marker（如 markdown-body）的那个 <div> 的完整内容，按嵌套配平。"""
    i = html.find(marker)
    if i < 0:
        return ''
    start = html.rfind('<div', 0, i)
    if start < 0:
        return ''
    open_end = html.find('>', start)
    if open_end < 0:
        return ''
    depth = 0
    for m in re.finditer(r'<div\b|</div>', html[start:]):
        if m.group(0) == '<div':
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return html[open_end + 1:start + m.start()]
    return html[open_end + 1:][:20000]


def _html_to_text(frag):
    """把 Release 正文的 HTML 变成纯文本：留空行分节、列表前加 ·，去掉图片。"""
    if not frag:
        return ''
    frag = re.sub(r'(?is)<(script|style)\b.*?</\1>', '', frag)
    frag = re.sub(r'(?is)<img\b[^>]*>', '', frag)
    frag = re.sub(r'(?is)<br\s*/?>', '\n', frag)
    frag = re.sub(r'(?is)<li\b[^>]*>', '\n· ', frag)
    frag = re.sub(r'(?is)</(p|div|li|h[1-6]|tr|ul|ol|blockquote)>', '\n', frag)
    frag = re.sub(r'(?is)<h[1-6]\b[^>]*>', '\n', frag)
    frag = re.sub(r'(?is)<code\b[^>]*>', ' `', frag)
    frag = re.sub(r'(?is)</code>', '` ', frag)
    frag = re.sub(r'(?s)<[^>]+>', '', frag)
    frag = html_lib.unescape(frag).replace('\xa0', ' ')
    out, blank = [], False
    for line in frag.splitlines():
        line = re.sub(r'[ \t]+', ' ', line).strip()
        if not line:
            if out and not blank:
                out.append('')
            blank = True
            continue
        out.append(line)
        blank = False
    return '\n'.join(out).strip()


def _notes_from_release_page(tag):
    """Release 正文（作者在 GitHub 上写的那份）——更新说明就用它，别自己改口径。"""
    try:
        html = _get(PAGE_TAG % tag, 10).decode('utf-8', 'replace')
    except Exception:
        return ''
    return _html_to_text(_div_block(html, 'markdown-body'))[:6000]


def _notes_from_changelog(tag):
    """Release 没写说明时，从仓库 CHANGELOG 里摘对应小节（国内直连 raw 也快）。"""
    want = parse_ver(tag)
    try:
        text = _get(CHANGELOG_RAW, 8).decode('utf-8', 'replace')
    except Exception:
        return ''
    cur, buf = None, []
    for line in text.splitlines():
        m = re.match(r'^##\s+([vV]?\d+\.\d+\.\d+)', line)
        if m:
            if cur is not None and parse_ver(cur) == want:
                break
            cur, buf = m.group(1), [line]
            continue
        if cur is not None:
            buf.append(line)
    return '\n'.join(buf).strip() if cur and parse_ver(cur) == want else ''


def _asset_from_expanded(tag):
    """从 Release 页的资源列表里找 zip 直链（不依赖 API）。"""
    try:
        html = _get(EXPANDED % tag, 8).decode('utf-8', 'replace')
    except Exception:
        return ''
    m = re.search(r'href="(/[^"]*?/releases/download/[^"]+?\.zip)"', html)
    if m:
        return 'https://github.com' + m.group(1)
    m = re.search(r'releases/download/[^"\s]+?\.zip', html)
    return ('https://github.com/' + m.group(0)) if m else ''


def _asset_guess(tag):
    """兜底：按命名规则拼（v1.2.6 / 1.2.6 两种都试）。"""
    v = parse_ver(tag)
    name = 'AstralParty_Dex_v%d.%d.%d_win64.zip' % v
    return 'https://github.com/%s/%s/releases/download/%s/%s' % (OWNER, REPO, tag, name)


def fetch_latest():
    """查最新版；返回 {tag, url, asset, notes, via}，查不到时 tag 为空。"""
    # ① API（能通就拿完整信息：说明 + 资源直链）
    try:
        d = json.loads(_get(API_LATEST, 8, {'Accept': 'application/vnd.github+json'}).decode('utf-8'))
        tag = d.get('tag_name') or ''
        assets = d.get('assets') or []
        asset = next((a.get('browser_download_url') for a in assets
                      if str(a.get('name', '')).lower().endswith('.zip')), '')
        notes = (d.get('body') or '').strip() or _notes_from_release_page(tag) \
            or _notes_from_changelog(tag)
        return {'tag': tag, 'url': d.get('html_url') or (PAGE_TAG % tag),
                'asset': asset, 'notes': notes, 'via': 'api'}
    except Exception:
        pass

    # ② 网页重定向（不限流）：/releases/latest → /releases/tag/<tag>
    try:
        req = urllib.request.Request(PAGE_LATEST, headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            final = r.geturl() or ''
        m = re.search(r'/releases/tag/([^/?#]+)', final)
        tag = m.group(1) if m else ''
    except Exception as e:
        return {'tag': '', 'url': PAGE_LATEST, 'asset': '', 'notes': '',
                'via': 'error', 'error': '%s' % e}
    if not tag:
        return {'tag': '', 'url': PAGE_LATEST, 'asset': '', 'notes': '', 'via': 'error',
                'error': '没从 Release 页解析出版本号'}
    return {'tag': tag, 'url': PAGE_TAG % tag,
            'asset': _asset_from_expanded(tag) or _asset_guess(tag),
            'notes': _notes_from_release_page(tag) or _notes_from_changelog(tag),
            'via': 'web'}


# ── 缓存 ───────────────────────────────────────────────────────
def _cache_file():
    try:
        from astral.paths import DATA_DIR
        return Path(DATA_DIR) / 'update_check.json'
    except Exception:
        return Path(tempfile.gettempdir()) / 'astraldex_update_check.json'


def check(current, force=False):
    """带缓存的检查。返回 {ok, newer, tag, url, asset, notes, checked, error}。"""
    info, ts = None, 0
    f = _cache_file()
    if not force:
        try:
            d = json.loads(f.read_text('utf-8'))
            ts = float(d.pop('ts', 0) or 0)
            if time.time() - ts < CHECK_TTL and d.get('cv') == CACHE_VER:
                info = d
        except Exception:
            info = None
    if info is None:
        info = fetch_latest()
        info['ts'] = time.time()
        info['cv'] = CACHE_VER
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(info, ensure_ascii=False), 'utf-8')
        except OSError:
            pass
        ts = info['ts']
    tag = info.get('tag') or ''
    return {'ok': bool(tag), 'newer': bool(tag) and newer_than(tag, current),
            'tag': tag, 'current': current,
            'url': info.get('url') or PAGE_LATEST,
            'asset': info.get('asset') or '',
            'notes': info.get('notes') or '',
            'checked': int(ts), 'via': info.get('via') or '',
            'error': info.get('error') or ''}


# ── 下载 / 安装 ────────────────────────────────────────────────
def app_dir():
    """打包运行时的程序目录；源码运行返回 None（源码模式不做就地更新）。"""
    if not getattr(sys, 'frozen', False):
        return None
    return Path(sys.executable).resolve().parent


def can_self_update():
    d = app_dir()
    if d is None:
        return False, '源码运行模式：请用 git pull 更新'
    try:
        probe = d / '.write_test'
        probe.write_bytes(b'x')
        probe.unlink()
    except OSError as e:
        return False, '程序目录不可写（是不是放在 Program Files 了？）：%s' % e
    return True, ''


def _download_urllib(url, dest, progress=None):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=30) as r, open(dest, 'wb') as f:
        total = int(r.headers.get('Content-Length') or 0)
        got = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if progress:
                progress(got, total)
    return dest


def _download_curl(url, dest, progress=None):
    """GitHub 的资源节点在国内常被掐 TLS；curl.exe（Win10+ 自带）重试更强。"""
    import shutil
    exe = shutil.which('curl') or r'C:\Windows\System32\curl.exe'
    p = subprocess.Popen([exe, '-L', '--fail', '--retry', '4', '--retry-delay', '1',
                          '--connect-timeout', '20', '-A', UA, '-o', str(dest), url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    while p.poll() is None:
        if progress:
            try:
                progress(dest.stat().st_size, 0)
            except OSError:
                pass
        time.sleep(0.4)
    if p.returncode != 0:
        raise RuntimeError('下载失败（curl 退出码 %s）' % p.returncode)
    size = dest.stat().st_size
    if progress:
        progress(size, size)
    return dest


def download(url, dest, progress=None):
    """流式下载到 dest；先走 urllib，失败再用 curl 兜底。progress(已下载, 总字节)。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    last = None
    for _ in range(2):
        try:
            return _download_urllib(url, dest, progress)
        except Exception as e:          # noqa: BLE001
            last = e
            time.sleep(0.5)
    try:
        return _download_curl(url, dest, progress)
    except Exception as e:              # noqa: BLE001
        raise RuntimeError('%s（curl 兜底也失败：%s）' % (last, e))


def prepare(zip_path):
    """解压更新包并校验结构；返回解压出的程序根目录。"""
    zip_path = Path(zip_path)
    tmp = Path(tempfile.mkdtemp(prefix='astraldex_upd_'))
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(tmp)
    root = tmp
    for p in tmp.iterdir():
        if (p / '星趴档案.exe').exists():
            root = p
            break
    if not (root / '星趴档案.exe').exists():
        raise RuntimeError('更新包里没有 星趴档案.exe，可能下载不完整')
    if not (root / '_internal').exists():
        raise RuntimeError('更新包缺少 _internal/，可能下载不完整')
    return root


def apply(root, dest, pid, restart=True):
    """等本进程退出 → robocopy 覆盖（保留 userdata）→ 重启。

    用 PowerShell -EncodedCommand（UTF-16LE + base64）而不是 .bat：
    cmd.exe 按本地代码页解析脚本文件，路径里有中文（星趴档案）时 .bat 会读成乱码，
    robocopy 直接静默失败 —— 这个坑实测过，别再改回 .bat。
    """
    import base64
    dest = Path(dest)
    tmp_root = Path(root)
    log = Path(tempfile.gettempdir()) / 'astraldex_update.log'
    start_exe = dest / '星趴档案.exe'

    def ps(s):
        return s.replace("'", "''")

    lines = [
        "$ErrorActionPreference = 'SilentlyContinue'",
        "$procId = %d" % pid,
        "for ($i = 0; $i -lt 900; $i++) { "
        "if (-not (Get-Process -Id $procId -ErrorAction SilentlyContinue)) { break }; "
        "Start-Sleep -Milliseconds 700 }",
        "robocopy '%s' '%s' /E /XD '%s' /R:2 /W:1 /NP /NFL /NDL /NJH /NJS *> '%s'"
        % (ps(str(tmp_root)), ps(str(dest)), ps(str(dest / 'userdata')), ps(str(log))),
        "Remove-Item -LiteralPath '%s' -Recurse -Force" % ps(str(tmp_root.parent)),
    ]
    if restart:
        lines.append("Start-Process -FilePath '%s' -WorkingDirectory '%s'"
                     % (ps(str(start_exe)), ps(str(dest))))
    script = "\n".join(lines)
    enc = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    flags = 0
    if os.name == 'nt':
        # DETACHED_PROCESS 没有控制台，powershell 的 -WindowStyle Hidden 会直接报错退出；
        # 用 CREATE_NO_WINDOW（0x08000000）更稳，且不会闪窗。
        flags = 0x08000000 | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    subprocess.Popen(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                      '-EncodedCommand', enc],
                     close_fds=True, creationflags=flags,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return log

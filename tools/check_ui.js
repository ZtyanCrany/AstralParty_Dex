// 校验 app.html 的复盘渲染：喂真实复盘数据 → 跑产品真实 JS → 断言生成的 HTML
//
// 用法:  node tools/check_ui.js [复盘缓存.json] [玩家uid]
//        两个参数都可省：默认取 userdata/review/ 里最新的一份 + 第一个玩家
//
// 说明: 纯 node，不需要浏览器；只用最小 DOM 桩把 renderReview 跑起来看结构。
//       （改了复盘数据结构后记得跑一下这个 + tools/verify_review_invariants.py）
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'ui', 'app.html'), 'utf8');

// ① 取最后一个 <script> 块（产品真实 JS）
const blocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const js = blocks[blocks.length - 1];

// ② 只抽出 renderReview 的函数体
const i = js.indexOf('window.renderReview =');
if (i < 0) { console.log('✗ app.html 里找不到 window.renderReview'); process.exit(1); }
const j = js.indexOf('\n  };', i);
const src = js.slice(i, j + 5);

// ②b 手牌视图（页签 + 每轮手牌矩阵 + 单人逐轮明细）也要一起跑，
//     否则 renderReview 里调的 window.drawRv 找不到
const i2 = js.indexOf('// ── 复盘页签 + 手牌视图');
const j2 = js.indexOf('\n  };', js.indexOf('window.handDetail ='));
if (i2 < 0 || j2 < 0) { console.log('✗ app.html 里找不到手牌视图（drawRv / handDetail）'); process.exit(1); }
const src2 = js.slice(i2, j2 + 5);

// ③ 最小 DOM 桩（renderReview 只用到 document / window.__esc / avOf）
const mkEl = () => ({
  textContent: '', innerHTML: '', src: '', scrollTop: 0, className: '', style: {},
  classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
  setAttribute() {}, getAttribute() { return ''; }, removeAttribute() {},
  appendChild() {}, querySelector() { return mkEl(); }, querySelectorAll() { return []; },
});
const store = {};
const document = {
  getElementById: id => (store[id] = store[id] || mkEl()),
  querySelector: () => mkEl(), querySelectorAll: () => [],
};
const window = {
  __esc: s => String(s == null ? '' : s)
    .replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])),
};
const avOf = () => '';            // 头像兜底桩：本校验只看结构，不需要真图
eval(src + '\n' + src2);          // eslint-disable-line no-eval

// ④ 数据：默认取 userdata/review/ 里最新的那份缓存（顺序 / 质量由程序自己保证）
const dir = path.join(ROOT, 'userdata', 'review');
let file = process.argv[2];
if (!file) {
  if (!fs.existsSync(dir)) { console.log('✗ 没有 userdata/review/（先在程序里点开一局复盘）'); process.exit(1); }
  const all = fs.readdirSync(dir).filter(f => f.endsWith('.json')).map(f => path.join(dir, f));
  if (!all.length) { console.log('✗ 缓存目录是空的（先在程序里点开一局复盘）'); process.exit(1); }
  file = all.sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs)[0];
}
const rv = JSON.parse(fs.readFileSync(file, 'utf8'));
// 玩家：优先命令行指定；否则**挑一个本局死过的玩家**（这样才能验到「(×_×;)」标记）
const withDeath = (rv.players || []).find(p => (p.rounds || []).some(r => r.died));
const uid = process.argv[3] || String((withDeath || rv.players[0] || {}).uid || '');
const pl = (rv.players || []).find(p => String(p.uid) === uid) || rv.players[0] || {};
console.log('复盘文件: ' + path.relative(ROOT, file));
console.log('玩家 uid: ' + uid + (withDeath && String(withDeath.uid) === uid ? '（本局死过，用来验阵亡标记）' : ''));
console.log('注: 角色名 / 地图名 / 立绘头像是程序里 load_review 补的，这里只验渲染结构');
window.renderReview(rv, uid);
const h = store.rvBody.innerHTML;

console.log('\n【头部】' + store.rvName.textContent + '   |   ' + store.rvSub.textContent);
console.log('       ' + store.rvUid.textContent);

console.log('\n【小节】');
(h.match(/<h4>[^<]*<\/h4>/g) || []).forEach(x => console.log('   ' + x));

console.log('\n【自检】');
const chk = (n, v) => { console.log((v ? '  ✓ ' : '  ✗ ') + n); if (!v) process.exitCode = 1; };
chk('UID 单独一行（在名字上方）', /^UID \d+$/.test(store.rvUid.textContent));
chk('三个小节齐全（轮次数据 / 筹码总览 / 获取筹码明细）',
  ['轮次数据', '筹码总览', '获取筹码明细'].every(x => h.includes(x)));
chk('轮次表 5 项指标表头齐全', ['击杀', '伤害', '承伤', '星币', '步数'].every(k => h.includes('<th>' + k + '</th>')));
chk('有合计行 <tfoot>', h.includes('<tfoot>'));
chk('筹码明细按轮次分块（第 N 轮）', /<h5>第 \d+ 轮<\/h5>/.test(h));
chk('该轮阵亡标 (×_×;)（只看本次渲染的玩家）',
  !(pl.rounds || []).some(r => r.died) || h.includes('(×_×;)'));
chk('来源用颜色类 s-(task|star|shop|cycle)', /class="src-txt s-(task|star|shop|cycle)"/.test(h));
chk('三选一：选中项涂色（cand pick q）', h.includes('cand pick q'));
chk('刷新链有「刷新」分隔', h.includes('class="rf">刷新<'));
chk('全篇无 emoji', !/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/u.test(h));
chk('全篇无「拿到」字样', !h.includes('拿到'));

// ⑥ 手牌视图：页签 → 每轮开局手牌矩阵（每张卡一行）→ 点玩家切逐轮明细
console.log('\n【手牌视图】');
chk('有「手牌」页签', (store.rvTabs.innerHTML || '').includes('手牌'));
window.rvTab('hand');
const hm = store.rvBody.innerHTML;
chk('默认是「每轮手牌」总览', hm.includes('每轮手牌'));
chk('矩阵按轮次分行', /<th class="hrt">第 \d+ 轮<\/th>/.test(hm));
chk('每个玩家一列表头（含头像 + 名字，可点）', /class="hpl" onclick="rvDetail\(/.test(hm));
chk('每张卡占一行（td 里是块级 .hc）', /class="hc t-\w+"/.test(hm));
chk('总览页只列「开局」、不显示获得/使用', hm.includes('>开局<') && !hm.includes('>获得<') && !hm.includes('>使用<'));
chk('表格里不标牌来源（无「商店购买」「开局发放」字样）',
  !/商店购买|开局发放|卡牌效果/.test(hm));
chk('矩阵里没有出牌记录表', !hm.includes('出牌记录'));
const uid2 = String(pl.uid);
window.rvDetail(uid2);
const hd = store.rvBody.innerHTML;
chk('点玩家 → 该玩家逐轮明细', hd.includes('的逐轮明细'));
chk('明细里有「开局」「获得」「使用」行',
  hd.includes('>开局<') && hd.includes('>获得<') && hd.includes('>使用<'));
chk('明细标题是「头像 + 名字」那一行（.rvtit；harness 里 avOf 是空桩故只验结构）',
  /class="rvtit"/.test(hd));
chk('明细：获得的牌用虚线框（.hc.dashed）', /class="hc t-\w+ dashed"/.test(hd));
chk('明细：用掉的牌加划掉线（.hc.used）', /class="hc t-\w+ used"/.test(hd));
chk('明细里有返回入口', hd.includes('rvbk'));
chk('明细里没有走势图', !hd.includes('走势'));
window.rvTab('stats');   // 还原，别影响后面的断言

// ⑤ 占位头像：新角色/新皮肤在本机游戏里还没有图时，必须走「占位头像」，
//    ★ 绝不能回落到 AV['Start'] —— 那是【地图的起点图标】，拿来当人物头像会让人一脸懵 ✗
console.log('\n【占位头像 avOf / phAv】');
{
  const q0 = js.indexOf('function phAv()');
  const q1 = js.indexOf('function avOf(');
  if (q0 < 0 || q1 < 0) {
    chk('app.html 里有 phAv / avOf', false);
  } else {
    const avSrc = js.slice(q0, js.indexOf('\n', q1) + 1);
    const AV = { '102': 'data:image/png;base64,AAAA', Start: 'data:image/png;base64,START' };
    const fn = new Function('AV', avSrc + '\nreturn { phAv: phAv, avOf: avOf };')(AV);
    const miss = fn.avOf('999');
    chk('缺角色 → 返回内联 SVG 占位图', /^data:image\/svg\+xml/.test(miss));
    chk('占位图不是地图的「起点」图标', miss !== AV.Start && miss !== '');
    chk('占位图里画的是问号', decodeURIComponent(miss).includes('>?<'));
    chk('已有角色仍返回真头像', fn.avOf('102') === AV['102']);
  }
}
console.log('\n' + (process.exitCode ? '✗ 有断言未通过' : '✓ 全部通过'));

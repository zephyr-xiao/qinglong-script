/**
 * ikuuu.js 单元测试（Node 内置 test runner，无需额外依赖）
 *
 * 运行： node --test ikuuu.test.js
 *
 * 覆盖重点：发布页域名提取（ikuuu 用 javascript-obfuscator 把域名拆成
 * 'ikuuu'+'.top' 再拼接，是本次踩坑的核心）+ 响应分类 + 账号解析
 * + 点选验证码配套（/load 解析、解法器输出校验、图片尺寸解析）。
 */

const test = require('node:test');
const assert = require('node:assert');

const {
  classify,
  classifyLoginBody,
  parseGeeLoadBody,
  imageSize,
  extractSolverJson,
  normalizeClicks,
  solverUnavailable,
  isRetryableStatus,
  solverOutputHint,
  firstErrorLine,
  typeLabel,
  CAPTCHA_TYPE_LABEL,
  buildCookieString,
  normalizeAccounts,
  parseCookieAccounts,
  accountLabel,
  maskName,
  maskEmail,
  extractExpireAt,
  cacheFileFor,
  readCookieCache,
  saveCookieCache,
  clearCookieCache,
  extractHosts,
  collapseStringConcat,
  getLatestHosts,
  PUBLISH_HOSTS,
  FALLBACK_HOSTS,
} = require('./ikuuu');
// 视觉解法器的纯函数（脚本被 require 时不会跑主流程）
const { parseModelReply, wantedCount, buildPrompt, computeMaxTokens } = require('./solver_vlm');

// 模拟发布页：域名被拆成多段字符串字面量后拼接（取自 ikuuu.win 真实页面结构）
const OBFUSCATED_PAGE = [
  '<html><head><title>iKuuuVPN最新域名 2026-09-11</title></head><body>',
  '<div>更新时间：2026-09-11</div>',
  '<script>',
  "const a={};a['url']='https://'+'ikuuu'+'.top/',a['name']='ikuuu'+'.top',a['description']='主要域名';",
  "const b={};b['url']='https://'+'ikuuu'+'.pw/',b['name']='ikuuu'+'.pw',b['description']='备用域名 1';",
  'const list=[a,b];',
  '</script></body></html>',
].join('');

test('collapseStringConcat: 折叠相邻字符串字面量拼接', () => {
  assert.strictEqual(collapseStringConcat("'ikuuu'+'.top'"), "'ikuuu.top'");
  assert.strictEqual(collapseStringConcat('"ikuuu"+".pw"'), "'ikuuu.pw'");
  // 多段拼接需要多轮折叠
  assert.strictEqual(collapseStringConcat("'https://'+'ikuuu'+'.top'+'/'"), "'https://ikuuu.top/'");
  // 拼接处带空白也要能折叠
  assert.strictEqual(collapseStringConcat("'ikuuu' + '.me'"), "'ikuuu.me'");
});

test('extractHosts: 能还原被字符串拼接隐藏的域名，并保持页面顺序', () => {
  assert.deepStrictEqual(extractHosts(OBFUSCATED_PAGE), ['ikuuu.top', 'ikuuu.pw']);
});

test('extractHosts: 明文域名同样能提取', () => {
  const html = '<a href="https://ikuuu.top/">主要</a><a href="https://ikuuu.pw/">备用</a>';
  assert.deepStrictEqual(extractHosts(html), ['ikuuu.top', 'ikuuu.pw']);
});

test('extractHosts: 发布页自身域名永不作为签到候选', () => {
  const html = '<a href="https://ikuuu.win/">发布页</a> 备用 ikuuu.eu ikuuu.win';
  assert.deepStrictEqual(extractHosts(html), []);
});

test('extractHosts: 空/非法输入返回空数组', () => {
  assert.deepStrictEqual(extractHosts(''), []);
  assert.deepStrictEqual(extractHosts(null), []);
  assert.deepStrictEqual(extractHosts('没有任何域名'), []);
});

test('常量表自洽：兜底域名不能是发布页域名', () => {
  for (const host of FALLBACK_HOSTS) {
    assert.ok(!PUBLISH_HOSTS.has(host), `兜底域名 ${host} 不应出现在 PUBLISH_HOSTS 中`);
  }
  assert.deepStrictEqual(FALLBACK_HOSTS, ['ikuuu.top', 'ikuuu.pw']);
});

test('classify: HTTP 405 判定为「该域名不是签到面板」', () => {
  // 发布页是 nginx 静态站，POST /user/checkin 会返回 405
  const result = classify('<html><head><title>405 Not Allowed</title></head></html>', 405);
  assert.strictEqual(result.status, 'domain_block');
});

test('classify: 302 跳登录页 → cookie 失效', () => {
  assert.strictEqual(classify('', 302, '/auth/login').status, 'cookie_dead');
});

test('classify: 302 跳非登录地址 → 域名异常', () => {
  const result = classify('', 302, 'https://example.com/');
  assert.strictEqual(result.status, 'domain_block');
});

test('classify: HTTP 错误码分档', () => {
  assert.strictEqual(classify('', 401).status, 'cookie_dead');
  assert.strictEqual(classify('', 403).status, 'domain_block');
  assert.strictEqual(classify('', 404).status, 'domain_block');
  assert.strictEqual(classify('', 429).status, 'network_err');
  assert.strictEqual(classify('', 502).status, 'network_err');
});

test('classify: JSON 业务结果分档', () => {
  assert.strictEqual(classify('{"ret":1,"msg":"签到成功"}', 200).status, 'success');
  assert.strictEqual(classify('{"ret":"1","msg":"ok"}', 200).status, 'success');
  assert.strictEqual(classify('{"ret":0,"msg":"您今天已经签到过了"}', 200).status, 'already');
  // 真实响应样本，注意用词是「已经签到」而非「已签到」
  assert.strictEqual(classify('{"ret":0,"msg":"您似乎已经签到过了..."}', 200).status, 'already');
  assert.strictEqual(classify('{"ret":0,"msg":"未知错误"}', 200).status, 'parse_err');
});

test('classify: 实测真实响应样本', () => {
  // 签到成功（unicode 转义后的原文，JSON.parse 会还原）
  const ok = classify('{"msg":"\\u4f60\\u83b7\\u5f97\\u4e86 581 MB\\u6d41\\u91cf","ret":1}', 200);
  assert.strictEqual(ok.status, 'success');
  assert.match(ok.msg, /你获得了 581 MB流量/);

  // 重复签到
  const dup = classify('{"ret":0,"msg":"\\u60a8\\u4f3c\\u4e4e\\u5df2\\u7ecf\\u7b7e\\u5230\\u8fc7\\u4e86..."}', 200);
  assert.strictEqual(dup.status, 'already');
});

test('classify: 响应为 HTML 登录页 → cookie 失效', () => {
  const html = '<!DOCTYPE html><html><head><title>login</title></head></html>';
  assert.strictEqual(classify(html, 200).status, 'cookie_dead');
});

test('maskName: 账号名脱敏', () => {
  assert.strictEqual(maskName('abcdef'), 'a****f');
  assert.strictEqual(maskName('ab'), 'a*');
  assert.strictEqual(maskName(''), '未命名账号');
});

test('getLatestHosts: 配置 HOST 时强制锁定并跳过网络抓取', async () => {
  const backup = process.env.HOST;
  process.env.HOST = 'ikuuu.example ';
  try {
    assert.deepStrictEqual(await getLatestHosts(), ['ikuuu.example']);
  } finally {
    if (backup === undefined) delete process.env.HOST;
    else process.env.HOST = backup;
  }
});

// ==========================================
// 账号密码登录改造的新增用例
// ==========================================
const fs = require('node:fs');
const nodePath = require('node:path');

// 缓存测试目录用 .test_tmp/（.gitignore 已覆盖）
function makeCacheDir() {
  const base = nodePath.join(__dirname, '.test_tmp');
  fs.mkdirSync(base, { recursive: true });
  return fs.mkdtempSync(nodePath.join(base, 'cache-'));
}

test('normalizeAccounts: 单账号 邮箱#密码', () => {
  assert.deepStrictEqual(normalizeAccounts('a@b.com#pw1'), [{ email: 'a@b.com', password: 'pw1' }]);
});

test('normalizeAccounts: 多账号 & 与换行可混用', () => {
  const arr = normalizeAccounts('a@b.com#p1&c@d.com#p2\ne@f.com#p3');
  assert.deepStrictEqual(arr, [
    { email: 'a@b.com', password: 'p1' },
    { email: 'c@d.com', password: 'p2' },
    { email: 'e@f.com', password: 'p3' },
  ]);
});

test('normalizeAccounts: 按首个 # 切分（密码可含 #），邮箱与条目空白被 trim', () => {
  const one = normalizeAccounts('  a@b.com  #p#1  ');
  assert.strictEqual(one[0].email, 'a@b.com');
  assert.strictEqual(one[0].password, 'p#1');
});

test('normalizeAccounts: 非法输入报错并提示迁移到新格式', () => {
  assert.throws(() => normalizeAccounts(''));
  assert.throws(() => normalizeAccounts(null));
  // 旧的 cookie 格式要报错提醒迁移，不能静默当账密用
  assert.throws(() => normalizeAccounts('uid=1; ip=2'), /邮箱#密码/);
  // 缺 #、空段、空密码都要报错
  assert.throws(() => normalizeAccounts('a@b.com'), /邮箱#密码/);
  assert.throws(() => normalizeAccounts('a@b.com#p1&&#p2'), /邮箱#密码/);
  assert.throws(() => normalizeAccounts('a@b.com#'), /空密码/);
});

test('maskEmail / accountLabel: 邮箱脱敏与账号标识', () => {
  assert.strictEqual(maskEmail('1987654321@qq.com'), '19********@qq.com');
  assert.strictEqual(maskEmail('a@qq.com'), 'a*@qq.com');
  assert.strictEqual(accountLabel({ email: '1987654321@qq.com' }), '19********@qq.com');
  assert.match(accountLabel({ cookie: 'uid=1234567; key=k' }), /^Cookie\(1\*{5}7\)$/);
  assert.strictEqual(accountLabel({ cookie: 'ip=x; key=k' }), 'Cookie(未命名)');
  assert.strictEqual(accountLabel({}), '未知账号');
});

test('parseCookieAccounts: 单条与换行多条', () => {
  const one = parseCookieAccounts('uid=1; ip=2; expire_in=3');
  assert.deepStrictEqual(one, [{ cookie: 'uid=1; ip=2; expire_in=3' }]);

  const multi = parseCookieAccounts('uid=1; key=a\n\nuid=2; key=b\n');
  assert.deepStrictEqual(multi, [{ cookie: 'uid=1; key=a' }, { cookie: 'uid=2; key=b' }]);

  assert.deepStrictEqual(parseCookieAccounts(''), []);
  assert.deepStrictEqual(parseCookieAccounts(null), []);
});

test('parseCookieAccounts: 缺 uid= 的行直接报错（签到必然 302，早失败早定位）', () => {
  assert.throws(() => parseCookieAccounts('email=x; key=k'), /uid=/);
  assert.throws(() => parseCookieAccounts('uid=1; key=a\n垃圾行'), /uid=/);
});

test('extractExpireAt: 解析 expire_in / expire_time（Unix 秒 → 毫秒）', () => {
  assert.strictEqual(extractExpireAt('uid=1; expire_in=1791347708; ip=x'), 1791347708 * 1000);
  assert.strictEqual(extractExpireAt('expire_time=1791347708'), 1791347708 * 1000);
  assert.strictEqual(extractExpireAt('uid=1; key=k'), null);
  assert.strictEqual(extractExpireAt(''), null);
  assert.strictEqual(extractExpireAt(null), null);
});

test('cookie 缓存: 写入→命中→清除，按邮箱隔离', () => {
  const dir = makeCacheDir();
  const cookie = 'uid=1234567; key=abc; expire_in=1991347708';
  saveCookieCache('a@b.com', cookie, 'ikuuu.top', dir);

  const hit = readCookieCache('a@b.com', dir);
  assert.strictEqual(hit.cookie, cookie);
  assert.strictEqual(hit.host, 'ikuuu.top');
  assert.strictEqual(hit.expireAt, 1991347708 * 1000);

  // 多账号隔离：别的邮箱读不到
  assert.strictEqual(readCookieCache('other@b.com', dir), null);

  clearCookieCache('a@b.com', dir);
  assert.strictEqual(readCookieCache('a@b.com', dir), null);
  // 重复清除不报错
  clearCookieCache('a@b.com', dir);
});

test('cookie 缓存: 余量不足视为失效（30 分钟 margin）', () => {
  const dir = makeCacheDir();
  // 只剩 10 分钟 → 不应命中
  const soon = Date.now() + 10 * 60 * 1000;
  saveCookieCache('x@b.com', 'uid=1', 'ikuuu.top', dir);
  // 直接改写 expireAt 模拟临近到期
  const file = cacheFileFor('x@b.com', dir);
  const data = JSON.parse(fs.readFileSync(file, 'utf8'));
  data.expireAt = soon;
  fs.writeFileSync(file, JSON.stringify(data));
  assert.strictEqual(readCookieCache('x@b.com', dir), null);
});

test('cookie 缓存: 缺 expireAt / 文件损坏 → 视为无缓存', () => {
  const dir = makeCacheDir();
  fs.writeFileSync(cacheFileFor('y@b.com', dir), JSON.stringify({ cookie: 'uid=1' }));
  assert.strictEqual(readCookieCache('y@b.com', dir), null);

  fs.writeFileSync(cacheFileFor('z@b.com', dir), '{broken json');
  assert.strictEqual(readCookieCache('z@b.com', dir), null);
});

test('classifyLoginBody: 各 phase 分档', () => {
  assert.deepStrictEqual(classifyLoginBody('{"phase":"authenticated"}', 200), { ok: true, msg: '登录成功' });

  const badPass = classifyLoginBody('{"phase":"password","result":"user_not_found","msg":"邮箱或密码错误"}', 200);
  assert.strictEqual(badPass.ok, false);
  assert.match(badPass.msg, /密码阶段被拒/);

  assert.strictEqual(classifyLoginBody('{"phase":"totp"}', 200).ok, false);
  assert.match(classifyLoginBody('{"phase":"totp"}', 200).msg, /两步验证/);
  assert.strictEqual(classifyLoginBody('{"phase":"email_code"}', 200).ok, false);
  assert.strictEqual(classifyLoginBody('{"phase":"reverse_email_verify"}', 200).ok, false);

  const unknown = classifyLoginBody('{"phase":"wat","msg":"?"}', 200);
  assert.strictEqual(unknown.ok, false);
  assert.match(unknown.msg, /phase=wat/);
});

test('classifyLoginBody: 非 JSON / 5xx', () => {
  const html = classifyLoginBody('<html>502</html>', 502);
  assert.strictEqual(html.ok, false);

  const notJson = classifyLoginBody('oops', 200);
  assert.strictEqual(notJson.ok, false);
  assert.match(notJson.msg, /非 JSON/);

  assert.strictEqual(classifyLoginBody('{"phase":"authenticated"}', 503).ok, false);
});

test('buildCookieString: 只取本站 cookie 并剔除统计噪音', () => {
  const cookies = [
    { name: 'uid', value: '1234567', domain: 'ikuuu.top' },
    { name: 'email', value: 'a%40b.com', domain: 'ikuuu.top' },
    { name: 'key', value: 'k', domain: '.ikuuu.top' },
    { name: '_ga', value: 'GA1.1', domain: '.ikuuu.top' },
    { name: '_gid', value: 'x', domain: '.ikuuu.top' },
    { name: 'captcha_v4_user', value: 'zzz', domain: 'gcaptcha4.geetest.com' },
  ];
  const str = buildCookieString(cookies, 'ikuuu.top');
  assert.strictEqual(str, 'uid=1234567; email=a%40b.com; key=k');
  assert.strictEqual(buildCookieString([], 'ikuuu.top'), '');
});

// ==========================================
// 点选验证码配套（/load 解析、解法器输出校验、图片尺寸）
// ==========================================

test('parseGeeLoadBody: JSONP 与纯 JSON 两种响应体都能解析出 data', () => {
  const jsonp = 'geetest_1791516170996({"status": "success", "data": {"lot_number":"abc", "captcha_type":"word",'
    + ' "ques":["static_resources/word/a.png"]}});';
  const d1 = parseGeeLoadBody(jsonp);
  assert.strictEqual(d1.captcha_type, 'word');
  assert.strictEqual(d1.lot_number, 'abc');

  const plain = JSON.stringify({ status: 'success', data: { captcha_type: 'icon' } });
  assert.strictEqual(parseGeeLoadBody(plain).captcha_type, 'icon');

  // 没有 data 包裹时按顶层对象返回
  assert.strictEqual(parseGeeLoadBody('{"captcha_type":"nine"}').captcha_type, 'nine');
});

test('parseGeeLoadBody: 空串/非 JSON 返回 null', () => {
  assert.strictEqual(parseGeeLoadBody(''), null);
  assert.strictEqual(parseGeeLoadBody(undefined), null);
  assert.strictEqual(parseGeeLoadBody('<html>error</html>'), null);
});

test('typeLabel: 已知形式给中文名，未知/空值不抛异常', () => {
  assert.match(typeLabel('word'), /文字点选/);
  assert.match(typeLabel('icon'), /图标点选/);
  assert.match(typeLabel('nine'), /九宫格点选/);
  assert.match(typeLabel('ai'), /一键通过/);
  assert.strictEqual(typeLabel('brandnew'), 'brandnew');
  assert.strictEqual(typeLabel(null), '未知形式');
  assert.ok(Object.keys(CAPTCHA_TYPE_LABEL).includes('word'));
});

test('imageSize: 解析 PNG 与 JPEG 尺寸', () => {
  // PNG：8 字节签名 + IHDR（宽高为大端 uint32，偏移 16/20）
  const png = Buffer.alloc(32);
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]).copy(png, 0);
  png.writeUInt32BE(300, 16);
  png.writeUInt32BE(200, 20);
  assert.deepStrictEqual(imageSize(png), { w: 300, h: 200 });

  // JPEG：SOI + APP0 + SOF0（高/宽为 uint16，位于段内偏移 5/7）
  const sof = Buffer.from([0xff, 0xc0, 0x00, 0x11, 0x08, 0x00, 0xc8, 0x01, 0x2c, 0x03, 0x01, 0x11, 0x00, 0x02, 0x11, 0x01, 0x03, 0x11, 0x01]);
  const jpg = Buffer.concat([Buffer.from([0xff, 0xd8]), Buffer.from([0xff, 0xe0, 0x00, 0x04, 0x00, 0x00]), sof, Buffer.from([0xff, 0xd9])]);
  assert.deepStrictEqual(imageSize(jpg), { w: 300, h: 200 });

  assert.strictEqual(imageSize(Buffer.from('not an image at all')), null);
  assert.strictEqual(imageSize(null), null);
});

test('extractSolverJson: 允许解法器 stdout 前后夹带日志行', () => {
  assert.deepStrictEqual(extractSolverJson('{"ok":true,"clicks":[[1,2]]}'), { ok: true, clicks: [[1, 2]] });
  assert.deepStrictEqual(
    extractSolverJson('[solver] loading model...\n{"ok":true,"clicks":[[10,20],[30,40]]}\n[solver] done\n'),
    { ok: true, clicks: [[10, 20], [30, 40]] },
  );
  assert.deepStrictEqual(extractSolverJson('{"ok":false,"msg":"模型缺失"}'), { ok: false, msg: '模型缺失' });
  assert.strictEqual(extractSolverJson('no json here'), null);
  assert.strictEqual(extractSolverJson('{ broken json '), null);
  assert.strictEqual(extractSolverJson(''), null);
});

test('normalizeClicks: 过滤非法坐标，越界点位丢弃', () => {
  const size = { w: 300, h: 200 };
  const got = normalizeClicks([[10, 20], [299, 199], [301, 100], [-1, 5], ['50', '60'], [1, 2, 3], null, 'x'], size);
  assert.deepStrictEqual(got, [[10, 20], [299, 199], [50, 60]]);
  assert.deepStrictEqual(normalizeClicks('not-array', size), []);
  assert.deepStrictEqual(normalizeClicks([], size), []);
});

test('solverUnavailable: 区分"解法器不可用"与"内容类失败"', () => {  // 不可用：重抽没意义，应当直接失败
  assert.strictEqual(solverUnavailable('视觉模型解法失败: HTTP 401: {"error":{"message":"无效的令牌"}}'), true);
  assert.strictEqual(solverUnavailable('视觉模型解法失败: HTTP 429: inference exceeds tpm/rpm limit'), true);
  assert.strictEqual(solverUnavailable('视觉模型解法失败: HTTP 400: field MaxTokens invalid, should be in [1, 65536]'), true);
  assert.strictEqual(solverUnavailable('视觉模型解法失败: HTTP 503: upstream unavailable'), true);
  assert.strictEqual(solverUnavailable('未配置 IKUUU_VLM_API_KEY'), true);
  assert.strictEqual(solverUnavailable('解法器无法启动: spawn node ENOENT'), true);
  assert.strictEqual(solverUnavailable('解法器超时（180s）'), true);
  assert.strictEqual(solverUnavailable('视觉模型解法失败: fetch failed'), true);
  // 内容类：值得换个抽取再试
  assert.strictEqual(solverUnavailable('视觉模型解法失败: 模型无正文输出（finish_reason=length，可能 token 不足）'), false);
  assert.strictEqual(solverUnavailable('各次调用结果分歧过大（最大偏差 40px）'), false);
  assert.strictEqual(solverUnavailable('各次调用给出的点数不一致（1/3）'), false);
  assert.strictEqual(solverUnavailable('模型输出无法解析为 JSON: ```json {"clicks": [[1,2]]```'), false);
  assert.strictEqual(solverUnavailable(''), false);
  assert.strictEqual(solverUnavailable(undefined), false);
});

test('isRetryableStatus: 只重试"可能过一会儿就好"的失败', () => {
  assert.strictEqual(isRetryableStatus('login_fail'), true);   // 解法器/网关抖动
  assert.strictEqual(isRetryableStatus('network_err'), true);  // 网络异常
  // 这些重跑一遍没用：密码错、2FA、域名异常、响应解析不了、已成功
  assert.strictEqual(isRetryableStatus('success'), false);
  assert.strictEqual(isRetryableStatus('already'), false);
  assert.strictEqual(isRetryableStatus('cookie_dead'), false);
  assert.strictEqual(isRetryableStatus('domain_block'), false);
  assert.strictEqual(isRetryableStatus('parse_err'), false);
});

test('solverOutputHint / firstErrorLine: 把 Node 堆栈变成人话提示', () => {  // 实测踩过的坑：IKUUU_SOLVER_CMD 写成 wuang-wu_Ikuuu/solver.js → 文件名/路径双错
  const raw = 'node:internal/modules/cjs/loader:1210\n  throw err;\n  ^\n'
    + "Error: Cannot find module '/ql/data/scripts/wuang-wu_Ikuuu/wuang-wu_Ikuuu/solver.js'\n    at Module._resolveFilename";
  assert.match(solverOutputHint(raw), /文件名是 solver_vlm\.js/);
  assert.strictEqual(firstErrorLine(raw), "Error: Cannot find module '/ql/data/scripts/wuang-wu_Ikuuu/wuang-wu_Ikuuu/solver.js'");

  assert.match(solverOutputHint('sh: node: command not found'), /可执行程序不存在/);
  assert.match(solverOutputHint('{"ok":false,"msg":"未配置 IKUUU_VLM_API_KEY"}'), /IKUUU_VLM_BASE_URL/);
  assert.strictEqual(solverOutputHint('普通输出'), '');
});

// ==========================================
// 视觉解法器 solver_vlm.js 的纯函数
// ==========================================

test('parseModelReply: 正常 / 从思考里捞回 / 截断 / 无法解析', () => {  const mk = (message, finish) => ({ choices: [{ message, finish_reason: finish }] });
  // 正常：正文里有 JSON
  assert.deepStrictEqual(
    parseModelReply(mk({ content: '{"clicks":[[1,2],[3,4]]}' }, 'stop')).data,
    { clicks: [[1, 2], [3, 4]] },
  );
  // 正文空、思考里写了答案 → 捞回来（推理模型常见的"结论在 reasoning 里"）
  const salvaged = parseModelReply(mk({ content: '', reasoning: '看图…\n{"clicks":[[10,20],[30,40]]}' }, 'length'));
  assert.strictEqual(salvaged.ok, true);
  assert.strictEqual(salvaged.salvaged, true);
  assert.deepStrictEqual(salvaged.data.clicks, [[10, 20], [30, 40]]);
  // 正文空、思考里也没有答案 → 标记 truncated（上层按双倍 token 上限 + 强制指令重试一次）
  const cut = parseModelReply(mk({ content: '', reasoning: '还在推理…' }, 'length'));
  assert.strictEqual(cut.ok, false);
  assert.strictEqual(cut.truncated, true);
  // 正文有内容但不是 JSON → 不是截断，交给换题重抽
  const bad = parseModelReply(mk({ content: '这张图我看不清' }, 'stop'));
  assert.strictEqual(bad.ok, false);
  assert.strictEqual(bad.truncated, false);
  // 空响应不抛异常
  assert.strictEqual(parseModelReply(null).ok, false);
  assert.strictEqual(parseModelReply({}).ok, false);
});

test('computeMaxTokens: token 上限按倍数放大但夹在网关上限内', () => {
  // 实测新 API 类网关上限 65536（100000 会被判非法），故夹到 60000
  assert.strictEqual(computeMaxTokens(50000, 1), 50000);
  assert.strictEqual(computeMaxTokens(50000, 2), 60000);   // 双倍重试不会撞上限
  assert.strictEqual(computeMaxTokens(12000, 2), 24000);   // 未触顶时正常放大
  assert.strictEqual(computeMaxTokens(500, 1), 1000);      // 兜底下限
  assert.strictEqual(computeMaxTokens(12000, 0), 12000);   // 倍数不低于 1
});

test('wantedCount: 从提示语里取"要选几个"', () => {
  assert.strictEqual(wantedCount('选 3 个符合右图的图片'), 3);
  assert.strictEqual(wantedCount('请选出2个相同的'), 2);
  assert.strictEqual(wantedCount('请在下图依次点击'), 0);   // 没有数量要求
  assert.strictEqual(wantedCount(''), 0);
  assert.strictEqual(wantedCount(undefined), 0);
});

test('buildPrompt: 按形式与提示语生成提示词', () => {
  const nine = buildPrompt('nine', 1, { w: 300, h: 261 }, '选 3 个符合右图的图片');
  assert.match(nine, /3×3 九个格子/);
  assert.match(nine, /\*\*3 个\*\*/);           // 数量取自站点提示语
  assert.match(nine, /"cells"/);                 // 逐格枚举（提升定位准确率）
  assert.match(nine, /选 3 个符合右图的图片/);    // 提示语原文透传

  const word = buildPrompt('word', 3, { w: 300, h: 200 }, '请在下图依次点击');
  assert.match(word, /\[1\]\[2\]\[3\]/);          // 三个待点图案按序
  assert.match(word, /第 1 步/);                  // 先枚举
  assert.match(word, /"clicks"/);
  assert.match(word, /300 像素、高 200 像素/);     // 坐标系说明与 imageSize 一致

  const icon = buildPrompt('icon', 3, { w: 300, h: 200 }, '');
  assert.match(icon, /线条图形/);                 // icon 形状描述
});

/**
 * ikuuu.js 单元测试（Node 内置 test runner，无需额外依赖）
 *
 * 运行： node --test ikuuu.test.js
 *
 * 覆盖重点：发布页域名提取（ikuuu 用 javascript-obfuscator 把域名拆成
 * 'ikuuu'+'.top' 再拼接，是本次踩坑的核心）+ 响应分类 + 账号解析。
 */

const test = require('node:test');
const assert = require('node:assert');

const {
  classify,
  classifyLoginBody,
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

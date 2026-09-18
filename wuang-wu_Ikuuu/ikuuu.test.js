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
  normalizeAccounts,
  maskName,
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

test('normalizeAccounts: 支持三种格式', () => {
  const arr = normalizeAccounts('[{"name":"主号","cookie":"a=1"}]');
  assert.deepStrictEqual(arr, [{ name: '主号', cookie: 'a=1' }]);

  const obj = normalizeAccounts('{"name":"主号","cookie":"a=1"}');
  assert.deepStrictEqual(obj, [{ name: '主号', cookie: 'a=1' }]);

  const raw = normalizeAccounts('uid=1; key=abc');
  assert.deepStrictEqual(raw, [{ name: '默认账号', cookie: 'uid=1; key=abc' }]);

  assert.throws(() => normalizeAccounts(''));
  assert.throws(() => normalizeAccounts(null));
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

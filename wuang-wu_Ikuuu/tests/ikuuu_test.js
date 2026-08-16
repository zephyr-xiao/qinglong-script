/**
 * ikuuu.js 单元测试（node:test，Node 18+，零新依赖）。
 *
 * 运行方式（在 wuang-wu_Ikuuu 目录下）：
 *   node --test tests/
 */
const test = require('node:test');
const assert = require('node:assert');
const { classify, normalizeAccounts, maskName } = require('../ikuuu.js');

// ============ classify：HTTP 状态码分类 ============
test('classify: 401 → cookie_dead', () => {
  const r = classify('', 401);
  assert.strictEqual(r.status, 'cookie_dead');
});

test('classify: 403 → domain_block', () => {
  const r = classify('', 403);
  assert.strictEqual(r.status, 'domain_block');
});

test('classify: 404 → domain_block', () => {
  const r = classify('', 404);
  assert.strictEqual(r.status, 'domain_block');
});

test('classify: 429 → network_err（限流）', () => {
  const r = classify('', 429);
  assert.strictEqual(r.status, 'network_err');
});

test('classify: 5xx → network_err（服务端故障）', () => {
  assert.strictEqual(classify('', 502).status, 'network_err');
  assert.strictEqual(classify('', 503).status, 'network_err');
});

// ============ classify：业务判定 ============
test('classify: ret=1 → success', () => {
  const r = classify('{"ret":1,"msg":"签到成功"}', 200);
  assert.strictEqual(r.status, 'success');
});

test('classify: ret 为字符串 "1" → success（兼容）', () => {
  const r = classify('{"ret":"1","msg":"ok"}', 200);
  assert.strictEqual(r.status, 'success');
});

test('classify: success=true → success', () => {
  const r = classify('{"success":true}', 200);
  assert.strictEqual(r.status, 'success');
});

test('classify: "未获得任何奖励" 不得误判 success', () => {
  // bug 回归：原 /获得/ 正则会把失败文案误判为成功
  const r = classify('{"msg":"未获得任何奖励"}', 200);
  assert.notStrictEqual(r.status, 'success');
});

test('classify: 已签到文案 → already', () => {
  assert.strictEqual(classify('{"msg":"今日已签到"}', 200).status, 'already');
  assert.strictEqual(classify('{"msg":"重复签到"}', 200).status, 'already');
});

test('classify: HTML 登录页 → cookie_dead', () => {
  const r = classify('<!doctype html><html><body>login form</body></html>', 200);
  assert.strictEqual(r.status, 'cookie_dead');
});

test('classify: HTML 重定向 → domain_block', () => {
  const r = classify('<html><head><title>redirect</title></head></html>', 200);
  assert.strictEqual(r.status, 'domain_block');
});

test('classify: 非 JSON → parse_err', () => {
  const r = classify('not json at all', 200);
  assert.strictEqual(r.status, 'parse_err');
});

test('classify: 无成功特征的中文失败文案 → parse_err', () => {
  const r = classify('{"msg":"签到失败，请稍后再试"}', 200);
  assert.strictEqual(r.status, 'parse_err');
});

// ============ normalizeAccounts：三种格式 ============
test('normalizeAccounts: JSON 数组', () => {
  const accounts = normalizeAccounts('[{"name":"a","cookie":"c1"},{"name":"b","cookie":"c2"}]');
  assert.strictEqual(accounts.length, 2);
  assert.strictEqual(accounts[1].cookie, 'c2');
});

test('normalizeAccounts: JSON 单对象', () => {
  const accounts = normalizeAccounts('{"name":"主号","cookie":"cc"}');
  assert.strictEqual(accounts.length, 1);
  assert.strictEqual(accounts[0].name, '主号');
});

test('normalizeAccounts: 原始 cookie 字符串', () => {
  const accounts = normalizeAccounts('uid=1; token=abc');
  assert.strictEqual(accounts.length, 1);
  assert.strictEqual(accounts[0].cookie, 'uid=1; token=abc');
});

test('normalizeAccounts: 缺失 → throw', () => {
  assert.throws(() => normalizeAccounts(''));
});

// ============ maskName：脱敏 ============
test('maskName: 长名', () => {
  const masked = maskName('abcdefghij');
  assert.strictEqual(masked, 'a******j');
});

test('maskName: 短名', () => {
  assert.strictEqual(maskName('ab'), 'a*');
});

test('maskName: 空', () => {
  assert.strictEqual(maskName(''), '未命名账号');
});

/**
 * pt_checkin.js 单元测试（node tests/pt_checkin_test.js，无外部依赖）
 *
 * 用真实抓取的页面片段做 fixture，覆盖各站解析函数的核心判定。
 * `node --check` 只保证语法，本文件保证解析正则与真实页面结构不脱节。
 */
const assert = require('assert');
const {
  parseLoginPage,
  parseCrabptAttendance,
  parseNovahdAttendance,
  parseHdareaAttendance,
  mergeCookies,
  judgeLoginSuccess,
  judgeNovahdSign,
  judgeBtschoolIndex,
  credentialFingerprint,
} = require('../pt_checkin.js');

let passed = 0;
function test(name, fn) {
  try {
    fn();
    passed += 1;
    console.log(`PASS ${name}`);
  } catch (err) {
    console.error(`FAIL ${name}`);
    console.error(`  ${err.message}`);
    process.exitCode = 1;
  }
}

// ----------------------------------------
// CrabPT（crabpt.vip）真实页面片段（2026-09-28 实测抓取）
// ----------------------------------------
// 签到成功响应：数字被 <b> 包裹（签到后/已签重复访问均显示此文案）
const CRABPT_SUCCESS_HTML = `
<p>签到成功 这是您的第 <b>1</b> 次签到，已连续签到 <b>1</b> 天，本次签到获得 <b>100</b> 个蟹币值。
点击白色背景的圆点进行补签。你目前拥有补签卡 <b>0</b> 张。
<span style="float:right">今日签到排名：<b>988</b> / <b>988</b></span></p>
<li>首次签到获得 100 个蟹币值。</li>
<li>每次连续签到可额外获得 30 个蟹币值，直到 2000 封顶。</li>
<li>连续签到第 10 天，额外获得 200 蟹币值。</li>`;
// 已签首页：导航栏「签到已得」标记
const CRABPT_INDEX_HTML = `
蟹币值 [<a href="mybonus.php">使用</a>]: 0.0
[<a href="attendance.php">签到已得100, 补签卡: 0</a>]`;
// 登录页：标准 NexusPHP regimage 表单
const CRABPT_LOGIN_HTML = `
<form id="login-form" method="post" action="takelogin.php">
<input type="hidden" name="secret" value="">
<input type="text" class="username" name="username" />
<input type="password" class="password" name="password" />
<input type="text" name="two_step_code" />
image.php?action=regimage&amp;imagehash=233fb2a3562df2857b6e0c7e372734c1&amp;secret=
<input type="text" name="imagestring" value="" />
<input type="hidden" name="imagehash" value="233fb2a3562df2857b6e0c7e372734c1" />
你还有 <b><font color="green" size="2">[10]</font></b> 次尝试机会</p>`;

test('CrabPT 签到成功页解析（数字带 <b> 标签）', () => {
  const info = parseCrabptAttendance(CRABPT_SUCCESS_HTML);
  assert.strictEqual(info.hasSignSuccess, true);
  assert.strictEqual(info.reward, '100');
  assert.strictEqual(info.continuousDays, '1');
  assert.strictEqual(info.totalSignCount, '1');
  assert.strictEqual(info.rank, '988');
  assert.strictEqual(info.isLoginPage, false);
});

test('CrabPT 签到成功页不误匹配底部规则文案', () => {
  // 规则区「连续签到第 10 天，额外获得 200 蟹币值」不能盖过正文统计
  const info = parseCrabptAttendance(CRABPT_SUCCESS_HTML);
  assert.strictEqual(info.continuousDays, '1');
  assert.strictEqual(info.reward, '100');
});

test('CrabPT 已签首页「签到已得」判定', () => {
  const info = parseCrabptAttendance(CRABPT_INDEX_HTML);
  assert.strictEqual(info.isAlreadySigned, true);
  assert.strictEqual(info.hasSignSuccess, false);
});

test('CrabPT 登录页识别与 imagehash/限次解析', () => {
  const info = parseCrabptAttendance(CRABPT_LOGIN_HTML);
  assert.strictEqual(info.isLoginPage, true);
  const page = parseLoginPage(CRABPT_LOGIN_HTML);
  assert.strictEqual(page.imagehash, '233fb2a3562df2857b6e0c7e372734c1');
  assert.strictEqual(page.remainAttempts, 10);
  assert.strictEqual(page.secret, '');
});

// ----------------------------------------
// 通用工具
// ----------------------------------------
test('mergeCookies 新值覆盖旧值且保留无关键', () => {
  const merged = mergeCookies('a=1; b=old', ['b=new; Path=/', 'c=3']);
  assert.strictEqual(merged, 'a=1; b=new; c=3');
});

test('parseLoginPage 兼容 BTSchool 限次文案', () => {
  const page = parseLoginPage('<input type="hidden" name="imagehash" value="aaaabbbbccccddddeeeeffff00001111">你还有 <b><font color="green" size="2">[20]</font></b> 次尝试机会');
  assert.strictEqual(page.imagehash, 'aaaabbbbccccddddeeeeffff00001111');
  assert.strictEqual(page.remainAttempts, 20);
});

// ----------------------------------------
// 其他站点解析冒烟（防正则回退）
// ----------------------------------------
test('parseNovahdAttendance 统计文案', () => {
  const info = parseNovahdAttendance('本次签到获得 <b>50</b> 个魔力值，已连续签到 <b>3</b> 天');
  assert.strictEqual(info.reward, '50魔力值');
  assert.strictEqual(info.continuousDays, '3');
});

test('parseHdareaAttendance 签到响应', () => {
  const info = parseHdareaAttendance('此次签到您获得了10魔力值，已连续签到5天');
  assert.strictEqual(info.hasSignSuccess, true);
  assert.strictEqual(info.reward, '10魔力值');
  assert.strictEqual(info.continuousDays, '5');
});

// ----------------------------------------
// 正向证据制回归（复现三处「假成功」）
// ----------------------------------------

// 假成功点 1：登录把裸 302 当成功（NexusPHP 验证码错误时常 302 回 login.php）
test('登录判定：302 回登录页不算成功', () => {
  assert.strictEqual(
    judgeLoginSuccess({ location: 'login.php?returnto=index.php', cookie: 'sid=abc', text: '' }),
    false
  );
});
test('登录判定：302 无 Location 不算成功', () => {
  assert.strictEqual(judgeLoginSuccess({ location: '', cookie: 'sid=abc', text: '' }), false);
});
test('登录判定：302 跳离登录页算成功', () => {
  assert.strictEqual(judgeLoginSuccess({ location: 'index.php', cookie: 'sid=abc', text: '' }), true);
});
test('登录判定：下发登录态 Cookie（uid / c_secure_pass）算成功', () => {
  assert.strictEqual(judgeLoginSuccess({ location: '', cookie: 'uid=12345; pass=x', text: '' }), true);
  assert.strictEqual(judgeLoginSuccess({ location: '', cookie: 'c_secure_pass=deadbeef', text: '' }), true);
});
test('登录判定：pageSaysOk 需显式开启且正文不含 login.php', () => {
  assert.strictEqual(
    judgeLoginSuccess({ location: '', cookie: 'sid=abc', text: '<a href="index.php">首页</a>' }),
    false
  );
  assert.strictEqual(
    judgeLoginSuccess({ location: '', cookie: 'sid=abc', text: '<a href="index.php">首页</a>', allowPageSaysOk: true }),
    true
  );
  assert.strictEqual(
    judgeLoginSuccess({ location: '', cookie: 'sid=abc', text: '<form action="login.php">', allowPageSaysOk: true }),
    false
  );
});

// 假成功点 2：验证码错时未签页含「N 个魔力值」规则文案，旧逻辑凭 reward 正则误判成功
const NOVAHD_UNSIGNED_HTML = `
<form method="post" action="attendance.php">
<input type="hidden" name="imagehash" value="0123456789abcdef0123456789abcdef">
<input type="text" name="imagestring" value="">
</form>
<p>签到规则：每天签到可得 5 个魔力值，连续签到 7 天可额外获得 100 个魔力值。</p>`;
test('NovaHD 未签页含规则文案不判成功（strongSuccess 为假）', () => {
  const info = parseNovahdAttendance(NOVAHD_UNSIGNED_HTML);
  assert.strictEqual(info.strongSuccess, false);
  assert.strictEqual(judgeNovahdSign(NOVAHD_UNSIGNED_HTML), 'unknown');
});
test('NovaHD 签到响应「图片代码无效」优先判失败', () => {
  assert.strictEqual(judgeNovahdSign(`${NOVAHD_UNSIGNED_HTML}<p>图片代码无效！</p>`), 'captcha_err');
});
test('NovaHD 明确成功文案才判成功', () => {
  assert.strictEqual(
    judgeNovahdSign('签到成功！本次签到获得 <b>50</b> 个魔力值，已连续签到 <b>3</b> 天'),
    'success'
  );
});

// 假成功点 3：BTSchool 首页加载异常/维护页无「每日签到」入口，旧逻辑直接算已签
test('BTSchool 首页有签到入口=未签', () => {
  assert.strictEqual(judgeBtschoolIndex('<a href="index.php?action=addbonus">每日签到</a>'), 'unsigned');
});
test('BTSchool 无入口但为正常首页=已签', () => {
  assert.strictEqual(judgeBtschoolIndex('<a href="logout.php">退出</a> 魔力值：100'), 'already');
});
test('BTSchool 无入口且非正常首页=结果未知（不冒报已签）', () => {
  assert.strictEqual(judgeBtschoolIndex('<html><body>502 Bad Gateway</body></html>'), 'unknown');
});

// 缓存来源指纹：种子/账号变化即失效
test('credentialFingerprint 随种子变化且稳定', () => {
  process.env.PT_FP_TEST_CK = 'seedA';
  const a = credentialFingerprint('PT_FP_TEST_CK', 'user1');
  assert.strictEqual(a, credentialFingerprint('PT_FP_TEST_CK', 'user1'));
  process.env.PT_FP_TEST_CK = 'seedB';
  assert.notStrictEqual(a, credentialFingerprint('PT_FP_TEST_CK', 'user1'));
  delete process.env.PT_FP_TEST_CK;
  assert.strictEqual(credentialFingerprint('PT_FP_TEST_CK', ''), '');
});

console.log(`\n${passed} 个用例全部通过`);

/**
 * @name iKuuu 自动签到
 * @description 青龙面板自动签到脚本（ikuuu 机场），账号密码自动登录 + 动态域名 + 多账号 + 状态分档 + 失败才推送
 * @cron 8 8 * * *
 *
 * 环境变量：
 *   ACCOUNTS                 【与 IKUUU_COOKIE 二选一】账密账号列表，格式：邮箱#密码，
 *                              多账号用 & 或换行分隔
 *                              例：a@qq.com#pwd1&b@163.com#pwd2
 *                              按首个 # 切分（密码里可含 #）；密码请避免包含 & 和换行
 *                              Cookie 失效时自动开浏览器（Playwright 过 Geetest
 *                              验证码）重新登录续期，无需手工抓 cookie。
 *   IKUUU_COOKIE             【与 ACCOUNTS 二选一】cookie 字符串直填模式（旧行为），
 *                              多账号用换行分隔；此模式 Cookie 失效后只能手工更新，
 *                              适合不想装 playwright 或临时兜底的场景。
 *                              两个变量都配时可同时签到（cookie 账号在前）。
 *   IKUUU_SOLVER_CMD         【可选】点选验证码外部解法器命令（详见 README）。
 *                              极验按站点配置可能下发"点选"验证码（word/icon/nine），
 *                              脚本自身不做图形识别：配置本命令后，脚本把挑战图与
 *                              待点图案交给它，由它返回点击坐标。
 *   IKUUU_SOLVER_CMD_FALLBACK【可选】备用解法器命令：主解法器"不可用"（没配 key /
 *                              key 失效 / 网关不通 / 超时）时自动改用它。
 *   IKUUU_RETRY_TIMES        【可选】失败账号延迟重试次数，默认 0（不重试）。
 *                              限流、网关抖动这类"过一会儿就好"的故障靠它兜底。
 *   IKUUU_RETRY_DELAY        【可选】每次重试前等待秒数，默认 300。
 *   HOST                     【可选】强制锁定签到域名，留空则自动从发布页抓取
 *   IKUUU_PUBLISH_URL        【可选】发布页地址，默认 https://ikuuu.win/
 *                                      ikuuu 的"最新域名"发布页会整体迁移域名，
 *                                      迁移时改这里即可
 *   IKUUU_PROXY              【可选】HTTP 代理（ikuuu 被墙，建议配置），如 http://172.17.0.1:7890
 *                                      未配置时自动回退青龙全局代理（HTTPS_PROXY /
 *                                      HTTP_PROXY / ALL_PROXY / GLOBAL_AGENT_*）
 *                                      登录用浏览器同样走该代理
 *   IKUUU_HEADFUL            【可选】1 = 登录用有头浏览器（本地调试用，容器内勿开）
 *   IKUUU_CHROMIUM_PATH      【可选】系统 Chromium 路径（青龙容器装系统包后填 /usr/bin/chromium）
 *   IKUUU_NOTIFY_ONLY_FAIL   【可选】1 = 仅失败时推送，0/留空 = 全部推送
 *   IKUUU_DEBUG              【可选】1 = 打印详细调试信息（域名抓取/响应原文/登录截屏等）
 *
 * 登录会话 Cookie 缓存在脚本目录 .token/ 下（按邮箱隔离），会话有效期 24h，
 * 每天任务时若缓存仍在余量内直接复用，过期才重新走浏览器登录。
 *
 * 推送：复用同目录 sendNotify.js（青龙官方 Notify），在青龙变量里配 DD_BOT_TOKEN 等即可。
 */

const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');
const { sendNotify } = require(path.join(__dirname, 'sendNotify'));
// undici（sendNotify.js 已依赖）用于代理支持：ikuuu 被墙，青龙容器需走代理
const { fetch: undiciFetch, ProxyAgent } = require('undici');

const UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';

const DEBUG = process.env.IKUUU_DEBUG === '1';
const NOTIFY_ONLY_FAIL = process.env.IKUUU_NOTIFY_ONLY_FAIL === '1';
const HEADFUL = process.env.IKUUU_HEADFUL === '1';
const CHROMIUM_PATH = (process.env.IKUUU_CHROMIUM_PATH || '').trim();
const TIMEOUT_MS = 15000;

// 点选验证码解法器（外部命令插件）：见 README「点选验证码与解法器」
// 命令链：主解法器"不可用"（没配 key / key 失效 / 网关不通 / 超时）时，自动改用备用解法器
const SOLVER_CMDS = [
  (process.env.IKUUU_SOLVER_CMD || '').trim(),
  (process.env.IKUUU_SOLVER_CMD_FALLBACK || '').trim(),
].filter(Boolean);
// 失败后延迟重试：对限流、短暂抖动这类"过一会儿就好"的故障最有效（0 = 不重试）
const RETRY_TIMES = Math.max(0, Number(process.env.IKUUU_RETRY_TIMES || 0));
const RETRY_DELAY_MS = Math.max(10, Number(process.env.IKUUU_RETRY_DELAY || 300)) * 1000;
// 解法器超时（秒）：默认按"视觉大模型解法器"的耗时给（多次调用+重试），
// 轻量解法器（纯 HTTP 打码平台）可调小
const SOLVER_TIMEOUT_MS = Math.max(10, Number(process.env.IKUUU_SOLVER_TIMEOUT || 180)) * 1000;
// 点选挑战最多整页重来几次（每次刷新会重新抽取验证形式）
const MAX_CAPTCHA_ROUNDS = Math.max(1, Number(process.env.IKUUU_CAPTCHA_ROUNDS || 6));

// 登录会话 Cookie 缓存：脚本目录 .token/（.gitignore 已覆盖），按账号邮箱隔离
// ikuuu 会话有效期 24h（expire_in），留 30 分钟余量；cron 每天一次时约每天重登一次
const COOKIE_CACHE_DIR = path.join(__dirname, '.token');
const COOKIE_CACHE_MARGIN_MS = 30 * 60 * 1000;

// 代理支持（ikuuu 被墙，直连签到域名需代理）：
// 优先级 IKUUU_PROXY > 青龙全局代理变量 > 无代理（走公共 CORS 通道兜底）。
// undici 的 fetch 不读取环境变量，global-agent 也管不到它，故此处显式解析。
const GLOBAL_PROXY_KEYS = [
  'HTTPS_PROXY', 'https_proxy',
  'HTTP_PROXY', 'http_proxy',
  'ALL_PROXY', 'all_proxy',
  'GLOBAL_AGENT_HTTPS_PROXY', 'GLOBAL_AGENT_HTTP_PROXY',
];

// 返回 { url, source }，source 为命中的变量名，未命中则两者均为空串
function resolveProxy(specificKeys = []) {
  for (const key of [...specificKeys, ...GLOBAL_PROXY_KEYS]) {
    const value = (process.env[key] || '').trim();
    if (value) return { url: value, source: key };
  }
  return { url: '', source: '' };
}

const { url: PROXY, source: PROXY_SOURCE } = resolveProxy(['IKUUU_PROXY']);
const proxyAgent = PROXY ? new ProxyAgent(PROXY) : null;
const FETCH_OPTIONS = proxyAgent ? { dispatcher: proxyAgent } : {};

// 统一带代理的 fetch（代理与超时在此注入，调用处保持简洁）
function fetchWithProxy(url, options = {}) {
  return undiciFetch(url, { ...options, ...FETCH_OPTIONS });
}

// 睡眠（毫秒）
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 随机整数 [min, max]
function rand(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

// 账号名脱敏：保留首尾各1字符，中间打码
function maskName(name) {
  if (!name) return '未命名账号';
  if (name.length <= 2) return name[0] + '*';
  return name[0] + '*'.repeat(Math.min(name.length - 2, 6)) + name[name.length - 1];
}

// 日志（DEBUG 模式才打印）
function dbg(...args) {
  if (DEBUG) console.log('[DEBUG]', ...args);
}

// ==========================================
// 1. 账号解析：ACCOUNTS = 邮箱#密码，多账号用 & 或换行分隔（青龙通用惯例）
// ==========================================
function maskEmail(email) {
  if (!email) return '';
  const at = email.indexOf('@');
  if (at <= 0) return maskName(email);
  const head = email.slice(0, at);
  const visible = head.slice(0, 2);
  return `${visible}${'*'.repeat(Math.max(1, head.length - 2))}${email.slice(at)}`;
}

// 推送/日志里的账号标识：账密账号用脱敏邮箱；cookie 账号用 uid 脱敏
function accountLabel(account) {
  if (account.email) return maskEmail(account.email) || '未知账号';
  if (account.cookie) {
    const m = /(?:^|;\s*)uid=([^;]+)/.exec(account.cookie);
    if (m) return `Cookie(${maskName(m[1].trim())})`;
    return 'Cookie(未命名)';
  }
  return '未知账号';
}

function normalizeAccounts(rawAccounts) {
  if (!rawAccounts) throw new Error('missing ACCOUNTS');
  const trimmed = rawAccounts.trim();
  if (!trimmed) throw new Error('empty ACCOUNTS');

  const accounts = [];
  for (const part of trimmed.split(/[&\n]+/)) {
    const entry = part.trim();
    if (!entry) continue;
    // 按首个 # 切分：密码本身含 # 时，余下部分整体属于密码
    const hash = entry.indexOf('#');
    if (hash <= 0) {
      throw new Error(`「${entry.substring(0, 20)}」不是 邮箱#密码 格式（ACCOUNTS 需要改为 邮箱#密码，多账号用 & 或换行分隔）`);
    }
    const email = entry.slice(0, hash).trim();
    const password = entry.slice(hash + 1);
    if (!email || !password) {
      throw new Error('存在空邮箱或空密码的账号项');
    }
    accounts.push({ email, password });
  }
  if (accounts.length === 0) throw new Error('ACCOUNTS 没有解析到任何账号');
  return accounts;
}

// IKUUU_COOKIE：cookie 字符串直填，多账号用换行分隔（cookie 值本身不含换行）
// 必须含 uid=，否则签到一定 302，提前报错避免排查迷茫
function parseCookieAccounts(raw) {
  if (!raw) return [];
  const accounts = [];
  for (const part of raw.split(/\n+/)) {
    const entry = part.trim();
    if (!entry) continue;
    if (!/(?:^|;\s*)uid=[^;\s]+/.test(entry)) {
      throw new Error(`「${entry.substring(0, 20)}」不是有效 cookie（应包含 uid=...）`);
    }
    accounts.push({ cookie: entry });
  }
  return accounts;
}

// ==========================================
// 1.5 登录会话 Cookie 缓存（按邮箱隔离，只存 cookie 不存密码）
// ==========================================
// 文件名安全化：邮箱里可能出现的字符有限，但多账号别互相覆盖
// dir 参数默认脚本目录 .token/，单测可注入临时目录
function cacheFileFor(email, dir = COOKIE_CACHE_DIR) {
  const safe = String(email).replace(/[\\/:*?"<>|\s@]+/g, '_');
  return path.join(dir, `ikuuu_${safe}.json`);
}

// 从 cookie 字符串里解析会话到期时间（expire_in / expire_time，Unix 秒），返回毫秒
function extractExpireAt(cookieStr) {
  const m = /(?:^|;\s*)(?:expire_in|expire_time)=(\d{9,12})(?:;|$)/.exec(cookieStr || '');
  return m ? Number(m[1]) * 1000 : null;
}

function readCookieCache(email, dir = COOKIE_CACHE_DIR) {
  try {
    const data = JSON.parse(fs.readFileSync(cacheFileFor(email, dir), 'utf8'));
    if (!data || !data.cookie) return null;
    // 到期时间缺字段时按"已过期"处理，直接走重登，不用可疑凭证去撞站点
    if (!data.expireAt) return null;
    if (Date.now() >= data.expireAt - COOKIE_CACHE_MARGIN_MS) return null;
    return data;
  } catch (e) {
    return null;
  }
}

function saveCookieCache(email, cookie, host, dir = COOKIE_CACHE_DIR) {
  try {
    fs.mkdirSync(dir, { recursive: true });
    const expireAt = extractExpireAt(cookie) || Date.now() + 24 * 3600 * 1000;
    fs.writeFileSync(cacheFileFor(email, dir), JSON.stringify({ cookie, host, expireAt, savedAt: Date.now() }));
    const until = new Date(expireAt).toLocaleString('zh-CN', { hour12: false });
    console.log(`  📦 已缓存登录 Cookie（服务端到期 ${until}）`);
  } catch (e) {
    console.log(`  ⚠️ Cookie 缓存写入失败: ${e.message}`);
  }
}

function clearCookieCache(email, dir = COOKIE_CACHE_DIR) {
  try {
    fs.unlinkSync(cacheFileFor(email, dir));
  } catch (e) {
    /* 缓存不存在属正常 */
  }
}

// ==========================================
// 1.6 浏览器自动登录（Playwright）
//   ikuuu 登录是分阶段流程（POST /auth/login, phase=password），且强制 Geetest V4。
//   验证形式由极验按站点配置下发（2026-10 实测为 icon/word/nine 三种点选，随机分配）：
//     · 一键通过（ai）：点一下验证码按钮即通过，无需识别，脚本可独立完成；
//     · 点选类（word/icon/nine）：必须点击图中指定图案，脚本自身不做图形识别，
//       需要外部解法器（IKUUU_SOLVER_CMD）提供坐标，否则直接失败并说明形式。
//   —— 详细机制与可选方案见 README「点选验证码与解法器」。
// ==========================================
// 解析 /auth/login 响应体（纯函数，便于单测）
function classifyLoginBody(text, httpStatus) {
  if (httpStatus >= 500) return { ok: false, msg: `HTTP ${httpStatus} 服务端错误` };
  let data;
  try {
    data = JSON.parse(text);
  } catch (e) {
    return { ok: false, msg: `登录响应非 JSON（HTTP ${httpStatus}）: ${(text || '').substring(0, 60)}` };
  }
  switch (data.phase) {
    case 'authenticated':
      return { ok: true, msg: '登录成功' };
    case 'password':
      return { ok: false, msg: `密码阶段被拒: ${data.msg || data.result || '未知原因'}` };
    case 'totp':
      return { ok: false, msg: '账号开启了两步验证(2FA)，脚本无法自动完成' };
    case 'email_code':
      return { ok: false, msg: `站点要求邮箱验证码登录: ${data.msg || ''}` };
    case 'reverse_email_verify':
      return { ok: false, msg: '站点要求反向邮件验证（风控升级），请浏览器手动登录一次后重试' };
    default:
      return { ok: false, msg: `未知登录流程 phase=${data.phase}: ${data.msg || ''}` };
  }
}

// 极验验证形式（riskType 枚举）→ 中文名；未识别时原样回显
const CAPTCHA_TYPE_LABEL = {
  ai: '一键通过',
  slide: '滑动拼图',
  match: '消消乐',
  winlinze: '五子棋',
  word: '文字点选',
  phrase: '语序点选',
  nine: '九宫格点选',
  icon: '图标点选',
};

function typeLabel(type) {
  if (!type) return '未知形式';
  return CAPTCHA_TYPE_LABEL[type] ? `${CAPTCHA_TYPE_LABEL[type]}(${type})` : type;
}

// 解析 gcaptcha4 /load 的响应体（JSONP 或纯 JSON），返回 data 段（纯函数，便于单测）
function parseGeeLoadBody(text) {
  if (!text) return null;
  const trimmed = String(text).trim();
  // 形如 geetest_1791516170996({...});
  const m = /\(([\s\S]*)\)\s*;?\s*$/.exec(trimmed);
  const jsonText = m && trimmed.startsWith('geetest_') ? m[1] : trimmed;
  try {
    const data = JSON.parse(jsonText);
    return data && data.data ? data.data : data;
  } catch (e) {
    return null;
  }
}

// 读取图片宽高（PNG/JPEG，纯函数，便于单测）；无法识别返回 null
function imageSize(buf) {
  if (!buf || buf.length < 24) return null;
  if (buf.readUInt32BE(0) === 0x89504e47 && buf.readUInt32BE(4) === 0x0d0a1a0a) {
    return { w: buf.readUInt32BE(16), h: buf.readUInt32BE(20) };
  }
  if (buf[0] === 0xff && buf[1] === 0xd8) {
    let off = 2;
    while (off + 9 < buf.length) {
      if (buf[off] !== 0xff) { off++; continue; }
      const marker = buf[off + 1];
      if (marker === 0xd8 || marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) { off += 2; continue; }
      const len = buf.readUInt16BE(off + 2);
      if ((marker >= 0xc0 && marker <= 0xc3) || (marker >= 0xc5 && marker <= 0xc7) ||
          (marker >= 0xc9 && marker <= 0xcb) || (marker >= 0xcd && marker <= 0xcf)) {
        return { h: buf.readUInt16BE(off + 5), w: buf.readUInt16BE(off + 7) };
      }
      off += 2 + len;
    }
  }
  return null;
}

// 从解法器 stdout 提取 JSON（允许前后夹带日志行）（纯函数，便于单测）
function extractSolverJson(text) {
  if (!text) return null;
  const start = text.indexOf('{');
  const end = text.lastIndexOf('}');
  if (start < 0 || end <= start) return null;
  try {
    return JSON.parse(text.slice(start, end + 1));
  } catch (e) {
    return null;
  }
}

// 点击点：解法器输出校验（[[x,y],...]，坐标需落在图片自然尺寸内）（纯函数，便于单测）
function normalizeClicks(clicks, size) {
  if (!Array.isArray(clicks)) return [];
  const ok = (n, max) => Number.isFinite(n) && n >= 0 && n <= max;
  return clicks
    .filter((c) => Array.isArray(c) && c.length === 2 && ok(Number(c[0]), size.w) && ok(Number(c[1]), size.h))
    .map((c) => [Number(c[0]), Number(c[1])]);
}

// 验证码是否已通过（站点包装对象 window.Captcha 的 isReady 为 true）
async function isCaptchaReady(page) {
  return page
    .evaluate(() => !!(window.Captcha && window.Captcha.isReady && window.Captcha.isReady()))
    .catch(() => false);
}

// 读取点选挑战面板：挑战图地址、待点图案、显示区域与图片自然尺寸
// 验证形式（含挑战图与待点图案）在页面加载时的 /load 就已确定，点按钮只是校验，
// 因此这里要能等面板渲染出来，并对不同形式的 DOM 结构做兼容（背景图/普通 img）。
async function readClickChallenge(page) {
  return page.evaluate(async () => {
    const win = document.querySelector('.geetest_window');
    let target = null;
    let imageUrl = null;
    const fromBg = (el) => {
      const cs = el ? getComputedStyle(el) : null;
      const m = cs ? /url\("([^"]+)"\)/.exec(cs.backgroundImage || '') : null;
      return m ? m[1] : null;
    };
    // 优先 .geetest_bg，其次窗口内任意带背景图的元素，最后退化到 <img>
    const primary = document.querySelector('.geetest_bg');
    if (fromBg(primary)) {
      target = primary;
      imageUrl = fromBg(primary);
    } else if (win) {
      for (const el of win.querySelectorAll('*')) {
        const u = fromBg(el);
        if (u) { target = el; imageUrl = u; break; }
      }
      if (!imageUrl) {
        const img = win.querySelector('img');
        if (img && /^https?:/.test(img.src)) { target = img; imageUrl = img.src; }
      }
    }
    const rect = target ? target.getBoundingClientRect() : null;
    let natural = null;
    if (imageUrl) {
      // 只取 naturalWidth/Height，不设 crossOrigin（避免触发 CORS 失败）
      natural = await new Promise((resolve) => {
        const img = new Image();
        const done = () => resolve(img.naturalWidth ? { w: img.naturalWidth, h: img.naturalHeight } : null);
        img.onload = done;
        img.onerror = done;
        img.src = imageUrl;
        setTimeout(done, 4000);
      });
    }
    return {
      visible: !!(rect && rect.width > 40 && rect.height > 40 && imageUrl),
      imageUrl,
      tplUrls: Array.from(document.querySelectorAll('.geetest_ques_tips img')).map((i) => i.src),
      // 面板提示语（"请在下图依次点击" / "选 3 个符合右图的图片" …）：解法器据此判断点几个、要不要按顺序
      promptText: (() => {
        const el = document.querySelector('.geetest_text_tips') || document.querySelector('.geetest_tip');
        return el ? el.textContent.trim() : '';
      })(),
      rect: rect ? { x: rect.x, y: rect.y, w: rect.width, h: rect.height } : null,
      natural,
    };
  }).catch(() => ({ visible: false, imageUrl: null, tplUrls: [], rect: null, natural: null }));
}

// 等点选面板渲染出来（面板可能晚于 isReady 轮询才出现）
async function waitClickChallenge(page, timeoutMs = 10000) {
  const deadline = Date.now() + timeoutMs;
  let last = null;
  while (Date.now() < deadline) {
    last = await readClickChallenge(page);
    if (last.visible) return last;
    await page.waitForTimeout(500);
  }
  return last || { visible: false, imageUrl: null, tplUrls: [], rect: null, natural: null };
}

// 下载到本地文件（给外部解法器读）
async function downloadToFile(url, file) {
  const resp = await fetchWithProxy(url, {
    method: 'GET',
    headers: { 'User-Agent': UA, Referer: 'https://ikuuu.top/' },
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  fs.writeFileSync(file, Buffer.from(await resp.arrayBuffer()));
  return file;
}

// 调单个外部解法器：stdin 传 JSON，stdout 收 JSON
function runSolver(cmd, payload) {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(cmd, { shell: true, windowsHide: true });
    } catch (e) {
      return resolve({ ok: false, msg: `解法器无法启动: ${e.message}` });
    }
    let out = '';
    let err = '';
    let done = false;
    const finish = (r) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      resolve(r);
    };
    const timer = setTimeout(() => {
      try { child.kill('SIGKILL'); } catch (e) { /* 已退出 */ }
      finish({ ok: false, msg: `解法器超时（${SOLVER_TIMEOUT_MS / 1000}s）` });
    }, SOLVER_TIMEOUT_MS);
    child.stdout.on('data', (d) => { out += d; });
    child.stderr.on('data', (d) => {
      err += d;
      // 解法器的日志（投票结果/重试/换模型等）转发到主日志：出问题时才有线索可查
      String(d).split('\n').map((l) => l.trim()).filter(Boolean).forEach((l) => console.log(`  [解法器] ${l}`));
    });
    child.on('error', (e) => finish({ ok: false, msg: `解法器无法启动: ${e.message}` }));
    child.on('close', (code) => {
      const parsed = extractSolverJson(out);
      if (!parsed) {
        // 非 0 退出且没给出结果 → 解法器本身坏了（路径写错/依赖缺失/被 kill），换题无用
        const raw = `${out}\n${err}`;
        return finish({
          ok: false,
          unavailable: code !== 0,
          msg: `解法器输出无法解析（exit=${code}）: ${firstErrorLine(raw)}${solverOutputHint(raw)}`,
        });
      }
      if (parsed.ok === false) return finish({ ok: false, msg: parsed.msg || '解法器返回失败' });
      finish({ ok: true, clicks: parsed.clicks || [] });
    });
    child.stdin.end(JSON.stringify(payload));
  });
}

// 准备挑战图片并调用解法器，返回 { ok, clicks }（clicks 为图片自然坐标）
async function solveChallenge(challenge, type) {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ikuuu-captcha-'));
  try {
    const ext = (challenge.imageUrl.match(/\.(jpe?g|png)(\?|$)/i) || [null, 'jpg'])[1];
    const imagePath = path.join(tmpDir, `challenge.${ext}`);
    await downloadToFile(challenge.imageUrl, imagePath);
    const tplPaths = [];
    for (let i = 0; i < challenge.tplUrls.length; i++) {
      const p = path.join(tmpDir, `tpl${i + 1}.png`);
      await downloadToFile(challenge.tplUrls[i], p);
      tplPaths.push(p);
    }
    const size = imageSize(fs.readFileSync(imagePath)) || challenge.natural;
    if (!size) return { ok: false, msg: '无法读取挑战图尺寸' };
    const payload = {
      type,
      promptText: challenge.promptText || '',
      imagePath,
      tplPaths,
      imageSize: size,
      displaySize: { w: Math.round(challenge.rect.w), h: Math.round(challenge.rect.h) },
    };
    dbg('解法器入参:', JSON.stringify({ type, promptText: payload.promptText, imageSize: size, tplCount: tplPaths.length }));

    // 依次尝试命令链：主解法器不可用时换备用；内容类失败（解错/分歧）不再换（换了解法器也没用，让主流程换题）
    let last = null;
    for (let i = 0; i < SOLVER_CMDS.length; i++) {
      const r = await runSolver(SOLVER_CMDS[i], payload);
      if (r.ok) {
        const clicks = normalizeClicks(r.clicks, size);
        if (clicks.length === 0) {
          return { ok: false, msg: `解法器返回的坐标无效: ${JSON.stringify(r.clicks).substring(0, 120)}`, unavailable: false };
        }
        return { ok: true, clicks, size };
      }
      last = r;
      const unavailable = r.unavailable === true || solverUnavailable(r.msg);
      const hasNext = i < SOLVER_CMDS.length - 1;
      if (hasNext && unavailable) {
        console.log(`  ↪ 解法器#${i + 1} 不可用（${String(r.msg).substring(0, 60)}），改用备用解法器`);
        continue;
      }
      break;
    }
    return { ok: false, msg: last.msg, unavailable: last.unavailable === true || solverUnavailable(last.msg) };
  } catch (e) {
    return { ok: false, msg: `准备挑战图片失败: ${e.message}`, unavailable: solverUnavailable(e.message) };
  } finally {
    try { fs.rmSync(tmpDir, { recursive: true, force: true }); } catch (e) { /* 忽略清理失败 */ }
  }
}

// 解法器失败信息分类（纯函数，便于单测）：
//   true  = 解法器本身不可用（未配 key / 网关不通 / 认证失败 / 超时）→ 换题重抽没意义
//   false = 内容类失败（投票分歧、思考被截断、输出无法解析）→ 值得换个抽取再试
function solverUnavailable(msg) {
  return /未配置|无法启动|超时|fetch failed|abort|ECONN|ENOTFOUND|HTTP (400|401|403|404|429|5\d\d)/i.test(msg || '');
}

// 解法器原始输出 → 针对常见配置错误的提示（纯函数，便于单测）
// 目的：让"文件名写错/路径写错"这类问题一眼可辨，而不是让用户去读 Node 堆栈
function solverOutputHint(raw) {
  const s = String(raw || '');
  if (/Cannot find module|MODULE_NOT_FOUND/i.test(s)) {
    return '（提示：IKUUU_SOLVER_CMD 指向的文件不存在——本目录的解法器文件名是 solver_vlm.js，'
      + '可填 node solver_vlm.js 或绝对路径 node /ql/data/scripts/wuang-wu_Ikuuu/solver_vlm.js）';
  }
  if (/command not found|is not recognized|No such file or directory/i.test(s)) {
    return '（提示：IKUUU_SOLVER_CMD 里的可执行程序不存在，确认 node 在容器 PATH 里）';
  }
  if (/未配置 IKUUU_VLM/.test(s)) {
    return '（提示：视觉解法器缺环境变量，按 README 补齐 IKUUU_VLM_BASE_URL / IKUUU_VLM_API_KEY / IKUUU_VLM_MODEL）';
  }
  return '';
}

// 把解法器的原始输出压成一行（去掉 Node 堆栈噪音，保留最关键的报错行）
function firstErrorLine(raw) {
  const lines = String(raw || '').split('\n').map((l) => l.trim()).filter(Boolean);
  const key = lines.find((l) => /error|cannot find|not found|未配置|失败|invalid/i.test(l));
  return (key || lines[0] || '').substring(0, 200);
}

// 拟人点击（分步移动 + 抖动 + 按下延时），坐标已是屏幕坐标
async function humanClick(page, x, y) {
  // 起点随机落在目标左上方，但要留在视口内（负坐标会丢事件）
  const vp = page.viewportSize() || { width: 1280, height: 900 };
  const from = {
    x: Math.min(Math.max(10, x - 80 - Math.random() * 120), vp.width - 10),
    y: Math.min(Math.max(10, y - 60 - Math.random() * 80), vp.height - 10),
  };
  await page.mouse.move(from.x, from.y);
  const steps = 10 + Math.floor(Math.random() * 6);
  for (let i = 1; i <= steps; i++) {
    const t = i / steps;
    await page.mouse.move(
      from.x + (x - from.x) * t + (Math.random() * 3 - 1.5),
      from.y + (y - from.y) * t + (Math.random() * 3 - 1.5),
    );
    await page.waitForTimeout(8 + Math.floor(Math.random() * 18));
  }
  await page.waitForTimeout(100 + Math.floor(Math.random() * 180));
  await page.mouse.down();
  await page.waitForTimeout(50 + Math.floor(Math.random() * 60));
  await page.mouse.up();
}

// 按解法器坐标点击图案 → 点"确定" → 等验证通过
// 坐标映射每次点击前即时换算：面板可能因滚动/重排移动，之前缓存的 rect 不可靠
async function clickImagePoint(page, imageUrl, ix, iy, size) {
  const pt = await page.evaluate(({ url, x, y, w, h }) => {
    const bgUrlOf = (el) => {
      const cs = el ? getComputedStyle(el) : null;
      const m = cs ? /url\("([^"]+)"\)/.exec(cs.backgroundImage || '') : null;
      return m ? m[1] : null;
    };
    let target = document.querySelector('.geetest_bg');
    if (!target || bgUrlOf(target) !== url) {
      target = null;
      for (const el of document.querySelectorAll('.geetest_window, .geetest_window *')) {
        if (bgUrlOf(el) === url) { target = el; break; }
      }
    }
    if (!target) return null;
    const r = target.getBoundingClientRect();
    if (r.width < 40 || r.height < 40) return null;
    return { x: r.x + (x / w) * r.width, y: r.y + (y / h) * r.height };
  }, { url: imageUrl, x: ix, y: iy, w: size.w, h: size.h }).catch(() => null);
  if (!pt) return false;
  await humanClick(page, pt.x, pt.y);
  return true;
}

async function submitClicks(page, challenge, clicks, size) {
  let clicked = 0;
  for (const [ix, iy] of clicks) {
    const ok = await clickImagePoint(page, challenge.imageUrl, ix, iy, size);
    if (!ok) {
      console.log(`  ⚠️ 第 ${clicked + 1} 个点映射失败（面板已消失或结构变化）`);
      break;
    }
    clicked += 1;
    console.log(`  🖱 点击图片坐标 (${Math.round(ix)},${Math.round(iy)})`);
    await page.waitForTimeout(350 + Math.floor(Math.random() * 500));
  }
  if (clicked < clicks.length) return false;
  await page.locator('.geetest_submit').click({ timeout: 8000 }).catch(() => { /* 某些形式无提交键 */ });
  for (let i = 0; i < 24; i++) {
    await page.waitForTimeout(500);
    if (await isCaptchaReady(page)) return true;
  }
  return false;
}

// 从 Playwright cookie 对象数组拼 Cookie 头字符串；只取本站域名、剔除统计类噪音
function buildCookieString(cookies, host) {
  const hostLower = (host || '').toLowerCase();
  return (cookies || [])
    .filter((c) => {
      const d = (c.domain || '').toLowerCase().replace(/^\./, '');
      const sameSite = d === hostLower || hostLower.endsWith(`.${d}`) || d.endsWith(`.${hostLower}`);
      return sameSite && !/^_(ga|gid|gat)/.test(c.name);
    })
    .map((c) => `${c.name}=${c.value}`)
    .join('; ');
}

// 浏览器登录：成功返回 { ok, cookie }，失败返回 { ok: false, msg }
async function browserLogin({ host, email, password }) {
  let playwright;
  try {
    playwright = require('playwright');
  } catch (e) {
    return {
      ok: false,
      msg: '未安装 playwright（自动登录需要）。在青龙容器执行: npm i --prefix /ql/data/scripts/wuang-wu_Ikuuu && cd /ql/data/scripts/wuang-wu_Ikuuu && npx playwright install chromium',
    };
  }

  const debugDir = path.join(__dirname, '_ikuuu_debug');
  const shot = async (page, name) => {
    if (!DEBUG) return;
    try {
      fs.mkdirSync(debugDir, { recursive: true });
      await page.screenshot({ path: path.join(debugDir, `${Date.now()}_${name}.png`) });
    } catch (e) {
      dbg('截屏失败:', e.message);
    }
  };

  let browser;
  try {
    const launchArgs = {
      headless: !HEADFUL,
      args: [
        '--no-sandbox',
        '--disable-blink-features=AutomationControlled',
        '--disable-dev-shm-usage',
      ],
    };
    if (CHROMIUM_PATH) launchArgs.executablePath = CHROMIUM_PATH;
    if (PROXY) launchArgs.proxy = { server: PROXY };
    browser = await playwright.chromium.launch(launchArgs);

    const context = await browser.newContext({
      userAgent: UA,
      locale: 'zh-CN',
      viewport: { width: 1280, height: 800 },
      timezoneId: 'Asia/Shanghai',
    });
    const page = await context.newPage();

    // 监听登录接口响应（phase=authenticated 即成功）
    let loginPhaseBody = null;
    page.on('response', async (resp) => {
      if (resp.url().includes('/auth/login') && resp.request().method() === 'POST') {
        try {
          loginPhaseBody = { status: resp.status(), text: await resp.text() };
        } catch (e) {
          /* body 可能已被消费 */
        }
      }
    });

    // 监听验证码组件的 /load 响应：captcha_type 决定本次下发哪种验证形式
    let captchaType = null;
    page.on('response', async (resp) => {
      if (resp.url().includes('gcaptcha4.geetest.com/load')) {
        try {
          const data = parseGeeLoadBody(await resp.text());
          dbg('/load 响应:', resp.url().substring(0, 70), '→ captcha_type =', data && data.captcha_type);
          if (data && data.captcha_type) captchaType = data.captcha_type;
        } catch (e) {
          dbg('/load 响应读取失败:', e.message.split('\n')[0]);
        }
      }
    });

    // 记录 /verify 结果：点选失败时给出极验的判定原因，便于排查（行为轨迹/超时等）
    page.on('response', async (resp) => {
      if (resp.url().includes('gcaptcha4.geetest.com/verify')) {
        try {
          const data = parseGeeLoadBody(await resp.text());
          dbg('/verify:', JSON.stringify({ result: data && data.result, reason: data && data.reason, fail_count: data && data.fail_count }));
        } catch (e) {
          /* 忽略：body 可能已被消费 */
        }
      }
    });

    console.log(`  🌐 打开登录页 https://${host}/auth/login ...`);
    await page.goto(`https://${host}/auth/login`, { waitUntil: 'domcontentloaded', timeout: 45000 });
    await page.waitForTimeout(2500);

    // 验证码处理：按"抽到哪种形式"分支，最多整页重来 MAX_CAPTCHA_ROUNDS 次
    //   · 一键通过（ai）：点按钮后自行通过，无需解法器（站点配置回退时走这条）；
    //   · 点选类（word/icon/nine）：需要外部解法器给坐标，没有解法器就直接失败并说明形式。
    let captchaReady = false;
    let failMsg = null;
    for (let round = 1; round <= MAX_CAPTCHA_ROUNDS && !captchaReady; round++) {
      if (round > 1) {
        captchaType = null;
        await page.reload({ waitUntil: 'domcontentloaded' });
        await page.waitForTimeout(2500);
      }
      await page.fill('#email', email);
      await page.fill('#password', password);
      await page.check('#remember-me').catch(() => {});
      dbg(`第 ${round}/${MAX_CAPTCHA_ROUNDS} 轮点击验证码按钮`);

      const btn = page.locator('.geetest_btn_click');
      if ((await btn.count()) === 0) {
        await page.waitForTimeout(4000);
        if ((await btn.count()) === 0) {
          failMsg = '页面未出现验证码按钮（页面结构可能变化）';
          continue;
        }
      }
      await btn.first().click({ timeout: 10000 }).catch(async (e) => {
        dbg('常规点击被拦截，改用 force:', e.message.split('\n')[0]);
        await btn.first().click({ force: true, timeout: 10000 });
      });

      // 1) 先等"一键通过"：isReady 变 true 即成功
      for (let i = 0; i < 6 && !captchaReady; i++) {
        await page.waitForTimeout(1000);
        captchaReady = await isCaptchaReady(page);
      }
      if (captchaReady) {
        dbg(`验证码通过（${typeLabel(captchaType)}）`);
        break;
      }

      // 2) 没直过 → 等点选挑战面板
      const challenge = await waitClickChallenge(page);
      dbg('挑战面板:', JSON.stringify({
        visible: challenge.visible, type: captchaType,
        natural: challenge.natural, tpl: challenge.tplUrls.length,
      }));
      await shot(page, `round${round}_${captchaType || 'unknown'}`);
      if (!challenge.visible) {
        failMsg = `验证码未通过，且未识别到点选面板（本次形式 ${typeLabel(captchaType)}）`;
        continue;
      }
      console.log(`  🧩 站点下发点选验证码：${typeLabel(captchaType)}（第 ${round}/${MAX_CAPTCHA_ROUNDS} 次抽取）`);
      if (SOLVER_CMDS.length === 0) {
        failMsg = `站点已启用点选类验证码（${typeLabel(captchaType)}），脚本自身不做图形识别：`
          + '请配置 IKUUU_SOLVER_CMD 外部解法器，或在青龙里用 IKUUU_COOKIE 直填 cookie（见 README）';
        break;
      }
      const solved = await solveChallenge(challenge, captchaType);
      if (!solved.ok) {
        failMsg = `解法器未解决验证码: ${solved.msg}`;
        console.log(`  ⚠️ ${failMsg}`);
        // 解法器链整体不可用（没配 key / 网关不通 / 认证失败 / 超时）时换题重抽没有意义，
        // 只会把整轮任务空转几百秒 —— 直接失败并如实报出原因
        if (solved.unavailable) {
          console.log('  ⛔ 解法器不可用（非题目本身问题），停止重抽');
          break;
        }
        continue;
      }
      captchaReady = await submitClicks(page, challenge, solved.clicks, solved.size);
      if (!captchaReady) failMsg = `解法器给出的 ${solved.clicks.length} 个点击未被通过`;
    }
    if (!captchaReady) {
      await shot(page, 'captcha_failed');
      return { ok: false, msg: failMsg || '验证码未能通过' };
    }

    dbg('验证码通过，提交登录');
    await page.click('.login.btn', { timeout: 10000 });

    // 登录成功有三个信号：接口 phase=authenticated / 页面跳转 /user / 会话 Cookie 就位。
    // 响应体常因页面跳转提前销毁而读不到（实测如此），跳转与 Cookie 才是可靠信号
    const landedOnUser = () => {
      try {
        return new URL(page.url()).pathname === '/user';
      } catch (e) {
        return false;
      }
    };
    const deadline = Date.now() + 20000;
    while (Date.now() < deadline && !loginPhaseBody && !landedOnUser()) {
      await page.waitForTimeout(500);
    }
    await page.waitForTimeout(2000);
    await shot(page, 'after_submit');

    const allCookies = await context.cookies();
    const cookieStr = buildCookieString(allCookies, host);
    const hasSession = cookieStr.includes('uid=') && cookieStr.includes('key=');

    let verdict = loginPhaseBody ? classifyLoginBody(loginPhaseBody.text, loginPhaseBody.status) : null;
    if ((!verdict || !verdict.ok) && landedOnUser() && hasSession) {
      verdict = { ok: true, msg: '登录成功' };
    }
    if (!verdict) {
      verdict = { ok: false, msg: `提交后未捕获登录响应（当前页面 ${page.url()}）` };
    }
    if (!verdict.ok) return verdict;
    if (!hasSession) {
      return {
        ok: false,
        msg: `登录成功但未拿到会话 Cookie（拿到: ${allCookies.map((c) => c.name).join(',')}）`,
      };
    }
    dbg('登录 Cookie:', cookieStr.replace(/(uid|key|ip)=[^;]{4}[^;]*/g, '$1=****'));
    return { ok: true, cookie: cookieStr };
  } catch (err) {
    const hint = /Executable doesn't exist/.test(err.message)
      ? '。浏览器未安装：容器内执行 `npx playwright install chromium`，或设 IKUUU_CHROMIUM_PATH=/usr/bin/chromium'
      : '';
    return { ok: false, msg: `浏览器登录异常: ${String(err.message || err).substring(0, 200)}${hint}` };
  } finally {
    if (browser) await browser.close().catch(() => {});
  }
}

// ==========================================
// 2. 动态获取最新签到主域名
// ==========================================

// 发布页地址。ikuuu 的"最新域名"发布页会整体迁移域名，迁移时优先用环境变量覆盖，
// 避免必须改代码。
const DEFAULT_PUBLISH_URL = 'https://ikuuu.win/';
const PUBLISH_URL = (process.env.IKUUU_PUBLISH_URL || '').trim() || DEFAULT_PUBLISH_URL;

// 发布页自身域名（含已知的备用域名）：只能用来抓取，绝不能当签到面板。
// 发布页是 nginx 静态站，POST /user/checkin 会返回 405，误当作面板会直接签到失败。
const PUBLISH_HOSTS = new Set(['ikuuu.win', 'ikuuu.eu']);
try {
  PUBLISH_HOSTS.add(new URL(PUBLISH_URL).hostname.toLowerCase());
} catch (e) {
  dbg('IKUUU_PUBLISH_URL 不是合法 URL，仅按已知发布页域名过滤:', PUBLISH_URL);
}

// 静态兜底面板域名，数组顺序即尝试顺序（与发布页当前标注的"主要域名 / 备用域名 1"一致）。
// 发布页已改用 javascript-obfuscator 把域名拆成多段字符串再拼接（如 'ikuuu'+'.top'），
// 静态正则只能还原出其中一部分，所以必须保留兜底列表，否则抓取失败时无可用目标。
const FALLBACK_HOSTS = ['ikuuu.top', 'ikuuu.pw'];

// 折叠 JS 里相邻字符串字面量的拼接：'ikuuu'+'.top' → 'ikuuu.top'
// 发布页靠这种拼接隐藏域名，不折叠则一条都匹配不到
function collapseStringConcat(code) {
  let prev;
  let out = code;
  do {
    prev = out;
    out = out.replace(/(['"])([^'"]*)\1\s*\+\s*(['"])([^'"]*)\3/g, (m, q1, s1, q2, s2) => `'${s1}${s2}'`);
  } while (out !== prev);
  return out;
}

// 从发布页 HTML 提取候选签到域名（排除发布页自身域名，去重，保持页面上的先后顺序）
function extractHosts(html) {
  const normalized = collapseStringConcat(html || '');
  const hosts = [];
  const push = (host) => {
    const h = host.toLowerCase();
    if (!PUBLISH_HOSTS.has(h) && !hosts.includes(h)) hosts.push(h);
  };

  // 优先取链接里的域名（带协议），顺序更贴近页面标注的"主要/备用"
  const urlRe = /https?:\/\/(ikuuu\.[a-z]+)/gi;
  let m;
  while ((m = urlRe.exec(normalized)) !== null) push(m[1]);

  // 补齐只以纯文本/裸字符串出现的域名
  const bareRe = /ikuuu\.[a-z]+/gi;
  while ((m = bareRe.exec(normalized)) !== null) push(m[0]);

  return hosts;
}

// 公共 CORS 代理列表（发布页被墙时靠它们绕过，多备选提高可达率）
const CORS_PROXIES = [
  { name: 'allorigins', build: (u) => `https://api.allorigins.win/raw?url=${encodeURIComponent(u)}` },
  { name: 'cors.lol', build: (u) => `https://api.cors.lol/?url=${encodeURIComponent(u)}` },
  { name: 'whateverorigin', build: (u) => `https://www.whateverorigin.org/get?url=${encodeURIComponent(u)}` },
];

// 抓取发布页 HTML，各通道都失败时返回空串（调用方按抓取失败处理）
async function fetchPublishPage(publishUrl) {
  // 通道 1：配了代理时优先直连发布页（用户自己的代理最可靠，兑现「打不开用代理」）
  if (PROXY) {
    try {
      dbg(`通道1: 走代理(${PROXY_SOURCE})直连发布页`, publishUrl);
      const response = await fetchWithProxy(publishUrl, {
        method: 'GET',
        headers: { 'User-Agent': UA },
        signal: AbortSignal.timeout(TIMEOUT_MS),
      });
      const html = await response.text();
      dbg('通道1 发布页 HTML 长度:', html.length);
      if (extractHosts(html).length > 0) return html;
      dbg('通道1 未匹配到有效域名');
    } catch (err) {
      dbg('通道1 异常:', err.message);
    }
  }

  // 通道 2：公共 CORS 代理逐个尝试（发布页被墙时靠它们绕过）
  for (const p of CORS_PROXIES) {
    try {
      dbg(`通道2: 公共代理 ${p.name} 抓发布页`);
      const response = await fetchWithProxy(p.build(publishUrl), {
        method: 'GET',
        headers: { 'User-Agent': UA },
        signal: AbortSignal.timeout(TIMEOUT_MS),
      });
      const html = await response.text();
      if (extractHosts(html).length > 0) {
        dbg(`通道2 公共代理 ${p.name} 成功`);
        return html;
      }
      dbg(`通道2 公共代理 ${p.name} 未匹配到有效域名`);
    } catch (err) {
      dbg(`通道2 公共代理 ${p.name} 异常:`, err.message);
    }
  }

  return '';
}

async function getLatestHosts() {
  // 1. 环境变量强制锁定优先（trim 掉意外尾随空格，避免拼出无效 URL）
  const forcedHost = (process.env.HOST || '').trim();
  if (forcedHost) {
    console.log(`[域名加载] 检测到环境变量 HOST, 使用强制域名: ${forcedHost}`);
    return [forcedHost];
  }

  console.log(`[域名加载] 尝试从发布页 ${PUBLISH_URL} 获取最新主域名...`);
  const hosts = [];
  try {
    const html = await fetchPublishPage(PUBLISH_URL);
    if (!html) {
      console.log('[域名加载] ⚠️ 发布页抓取失败（各通道均不可用）');
    } else {
      const found = extractHosts(html);
      if (found.length > 0) {
        console.log(`[域名加载] 🎉 成功获取当前候选域名: ${found.join(', ')}`);
        hosts.push(...found);
      } else {
        console.log('[域名加载] ⚠️ 发布页解析成功但未匹配到域名（域名被混淆，仅能靠兜底列表）');
      }
    }
  } catch (err) {
    console.log(`[域名加载] ❌ 获取动态域名异常: ${err.message}`);
  }

  // 兜底域名始终补齐（发布页已改混淆 JS 渲染，静态抓取只能还原部分域名，兜底保证可用）
  for (const h of FALLBACK_HOSTS) {
    if (!hosts.includes(h)) hosts.push(h);
  }

  // 最后一道保险：发布页自身域名绝不进签到候选（它是静态页，签到只会拿到 405）
  const finalHosts = hosts.filter((h) => !PUBLISH_HOSTS.has(h));
  console.log(`[域名加载] 最终域名列表: ${finalHosts.join(', ')}`);
  return finalHosts;
}

// ==========================================
// 3. 签到核心：返回统一结构 { status, msg }
//   status 取值：
//     success      签到成功
//     already      已签到（幂等视为成功）
//     cookie_dead  cookie 失效（302 到登录页等）
//     domain_block 域名被墙 / 重定向到非业务页面
//     parse_err    响应非 JSON 且非 HTML 登录页，无法判定
//     network_err  请求异常
// ==========================================
function classify(text, httpStatus, location) {
  dbg('签到响应 httpStatus=', httpStatus, '原文前120字:', text.substring(0, 120).replace(/\s+/g, ' '));

  // ★ HTTP 状态码是强信号，优先分类（网关错误页/风控页可能不是 HTML 头，正文解析会漏）
  // 重定向：cookie 失效会跳到登录页；登录页是 base64 混淆 SPA，正文无 login/登录/auth 关键词，
  // 必须靠 Location 头精确区分「Cookie 失效跳登录」与「域名被墙/重定向」。
  if ([301, 302, 303, 307, 308].includes(httpStatus)) {
    const loc = (location || '').toLowerCase();
    if (loc.includes('login') || loc.includes('auth')) {
      return { status: 'cookie_dead', msg: 'Cookie 已失效（跳转登录页）' };
    }
    return { status: 'domain_block', msg: `HTTP ${httpStatus} 重定向到 ${location || '未知地址'}` };
  }
  if (httpStatus === 401) {
    return { status: 'cookie_dead', msg: 'HTTP 401 未授权，Cookie 已失效' };
  }
  if (httpStatus === 403) {
    return { status: 'domain_block', msg: 'HTTP 403 被拒绝（疑似风控或域名被墙）' };
  }
  if (httpStatus === 404) {
    return { status: 'domain_block', msg: 'HTTP 404 接口路径不存在（域名或路径变化）' };
  }
  if (httpStatus === 405) {
    // 发布页是 nginx 静态站，不接受 POST —— 说明当前域名压根不是签到面板
    return { status: 'domain_block', msg: 'HTTP 405 该域名不是签到面板（疑似发布页）' };
  }
  if (httpStatus === 429) {
    return { status: 'network_err', msg: 'HTTP 429 请求过于频繁（限流）' };
  }
  if (httpStatus >= 500) {
    return { status: 'network_err', msg: `HTTP ${httpStatus} 服务端错误` };
  }

  const lower = (text || '').toLowerCase();

  // HTML 重定向/登录页：cookie 失效或域名被墙
  if (lower.startsWith('<!doctype') || lower.includes('<html') || lower.includes('<head')) {
    // 含 login 字样优先判 cookie 失效
    if (lower.includes('login') || lower.includes('登录') || lower.includes('auth')) {
      return { status: 'cookie_dead', msg: 'Cookie 已失效，需重新获取' };
    }
    return { status: 'domain_block', msg: '响应为 HTML，疑似域名被墙或重定向' };
  }

  // 尝试解析 JSON
  let data;
  try {
    data = JSON.parse(text);
  } catch (e) {
    return { status: 'parse_err', msg: `响应解析失败，原文: ${text.substring(0, 40)}` };
  }

  const raw = (data.msg || data.message || '').toString();

  // 已签到的常见文案 → 幂等成功
  // （"已经签到" 与 "已签到" 是两种常见写法，SSPanel 默认文案为前者的变体，两种都要认）
  if (/已(经)?签到|重复|already|signed|今日已|明天再来/.test(raw)) {
    return { status: 'already', msg: raw || '今日已签到' };
  }

  // 业务返回 ret=1 或 success=true 视为成功
  // （兼容 ret 为字符串 "1"；"获得/奖励" 会误伤 "未获得任何奖励" 类失败文案，不可用作成功特征）
  if (Number(data.ret) === 1 || data.success === true || /成功/.test(raw)) {
    return { status: 'success', msg: raw || '签到成功' };
  }

  // 其它有 msg 但判定不了的，按返回原文透出，标记为 parse_err 便于排查
  return { status: 'parse_err', msg: raw || `未知响应: ${text.substring(0, 40)}` };
}

async function checkInOnce(cookieStr, host) {
  const checkInUrl = `https://${host}/user/checkin`;
  dbg('请求签到:', checkInUrl);

  const response = await fetchWithProxy(checkInUrl, {
    method: 'POST',
    // 不跟随重定向：保留 302 + Location 才能区分「Cookie 失效跳登录」与「域名被墙」
    redirect: 'manual',
    headers: {
      Cookie: cookieStr,
      'User-Agent': UA,
      Accept: 'application/json, text/plain, */*',
      Referer: `https://${host}/user`,
    },
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });

  // 302 时 Location 是分类关键（登录页为混淆 SPA，正文无关键词可判）
  const location = response.headers.get('location');
  const text = await response.text();
  return classify(text, response.status, location);
}

// 带重试的签到（仅网络错误重试，业务结果直接返回）
async function checkIn(cookieStr, host) {
  const maxRetry = 2;
  let lastResult;
  for (let i = 0; i <= maxRetry; i++) {
    try {
      lastResult = await checkInOnce(cookieStr, host);
      // 网络错误才重试，业务结果直接返回
      return lastResult;
    } catch (err) {
      lastResult = { status: 'network_err', msg: err.message };
      dbg(`第 ${i + 1} 次请求异常: ${err.message}`);
      if (i < maxRetry) {
        const wait = rand(2000, 4000);
        dbg(`等待 ${wait}ms 后重试`);
        await sleep(wait);
      }
    }
  }
  return lastResult;
}

// ==========================================
// 4. 状态 → 标记 / 通知文案
// ==========================================
const STATUS_FLAG = {
  success: '✅',
  already: '✅',
  cookie_dead: '❌',
  login_fail: '❌',
  domain_block: '❌',
  parse_err: '⚠️',
  network_err: '❌',
};

const STATUS_LABEL = {
  success: '成功',
  already: '已签到',
  cookie_dead: 'Cookie失效',
  login_fail: '登录失败',
  domain_block: '域名异常',
  parse_err: '响应异常',
  network_err: '网络异常',
};

function resultText(result) {
  const flag = STATUS_FLAG[result.status] || '⚠️';
  const label = STATUS_LABEL[result.status] || '未知';
  return `${flag} ${label}：${result.msg}`;
}

// ==========================================
// 5. 主入口
// ==========================================
// 单账号完整流程：确认 Cookie → 轮换域名签到 → Cookie 失效则重登一次
// 返回 { name, result, line, usedHost }
async function runAccount(acc, targetHosts) {
  const label = accountLabel(acc);
  let usedHost = targetHosts[0];

  // ---- 第一步：确定认证 Cookie —— cookie 型直接用；账密型缓存优先，没有则先登录 ----
  let cookie = acc.cookie || null;
  if (!cookie && acc.email) {
    const cached = readCookieCache(acc.email);
    if (cached) {
      cookie = cached.cookie;
      const until = new Date(cached.expireAt).toLocaleString('zh-CN', { hour12: false });
      console.log(`  📦 命中 Cookie 缓存（服务端 ${until} 到期），跳过登录`);
    }
  }
  if (!cookie && acc.email && acc.password) {
    console.log('  🔐 无可用 Cookie 缓存，先走浏览器自动登录...');
    const login = await browserLogin({ host: targetHosts[0], email: acc.email, password: acc.password });
    if (login.ok) {
      saveCookieCache(acc.email, login.cookie, targetHosts[0]);
      cookie = login.cookie;
    } else {
      console.log(`  ❌ 自动登录失败: ${login.msg}`);
      const r = { status: 'login_fail', msg: `自动登录失败: ${login.msg}` };
      return { name: label, result: r, line: resultText(r), usedHost };
    }
  }
  if (!cookie) {
    // 理论上到不了：解析层保证账号要么有 cookie 要么有 email+password
    const r = { status: 'parse_err', msg: '账号缺少认证方式' };
    return { name: label, result: r, line: resultText(r), usedHost };
  }

  // ---- 第二步：依次尝试候选域名签到；仅域名/网络类问题才轮换 ----
  let result = null;
  let resultHost = null;
  for (const host of targetHosts) {
    const r = await checkIn(cookie, host);
    if (r.status === 'domain_block' || r.status === 'network_err') {
      console.log(`  ↪ ${host} 返回 ${r.status}（${r.msg}），尝试下一个域名...`);
      result = r;
      continue;
    }
    usedHost = host;
    result = r;
    resultHost = host;
    break;
  }

  // ---- 第三步：Cookie 失效且有账密 → 浏览器重登一次（仅一次，防死循环）----
  // cookie 直填账号没有重登能力，保持 cookie_dead 报告原样透出
  if (result && result.status === 'cookie_dead' && acc.email && acc.password && resultHost) {
    console.log('  🔐 Cookie 已失效，尝试账号密码自动登录...');
    clearCookieCache(acc.email);
    const login = await browserLogin({ host: resultHost, email: acc.email, password: acc.password });
    if (login.ok) {
      saveCookieCache(acc.email, login.cookie, resultHost);
      usedHost = resultHost;
      result = await checkIn(login.cookie, resultHost);
    } else {
      console.log(`  ❌ 自动登录失败: ${login.msg}`);
      result = { status: 'login_fail', msg: `自动登录失败: ${login.msg}` };
    }
  }

  const line = resultText(result);
  console.log(`账号 [${label}] 结果: ${line}\n`);
  return { name: label, result, line, usedHost };
}

// 值得隔一会儿重跑的状态：登录失败（解法器/网关抖动）与网络异常，都可能"过一会儿就好"
function isRetryableStatus(status) {
  return status === 'login_fail' || status === 'network_err';
}

async function main() {
  console.log('=== iKuuu 青龙自动签到开始 ===\n');

  if (PROXY) {
    console.log(PROXY_SOURCE === 'IKUUU_PROXY'
      ? `代理: ${PROXY}`
      : `代理: ${PROXY}（来源: ${PROXY_SOURCE}）`);
  } else {
    console.log('代理: 未配置（发布页走公共 CORS 通道兜底）');
  }

  // 两种账号来源可共存：ACCOUNTS（邮箱#密码，可自动重登）+ IKUUU_COOKIE（直填，失效需手工更新）
  const accounts = [];
  if ((process.env.ACCOUNTS || '').trim()) {
    try {
      accounts.push(...normalizeAccounts(process.env.ACCOUNTS));
    } catch (err) {
      console.error(`❌ ACCOUNTS 解析失败: ${err.message}`);
      process.exit(1);
    }
  }
  if ((process.env.IKUUU_COOKIE || '').trim()) {
    try {
      accounts.push(...parseCookieAccounts(process.env.IKUUU_COOKIE));
    } catch (err) {
      console.error(`❌ IKUUU_COOKIE 解析失败: ${err.message}`);
      process.exit(1);
    }
  }
  if (accounts.length === 0) {
    console.error('❌ 未配置账号：请设置 ACCOUNTS（邮箱#密码，推荐）或 IKUUU_COOKIE（cookie 直填）');
    process.exit(1);
  }
  const pwdCount = accounts.filter((a) => a.email).length;
  console.log(`共解析到 ${accounts.length} 个账号（账密 ${pwdCount} / cookie ${accounts.length - pwdCount}），将串行签到\n`);

  // 动态域名列表（依次尝试：域名被墙/网络异常时自动轮换下一个）
  const targetHosts = await getLatestHosts();
  console.log(`\n▶ 本次签到域名列表: ${targetHosts.join(', ')}\n`);

  const results = [];
  let usedHost = targetHosts[0];

  for (let i = 0; i < accounts.length; i++) {
    console.log(`[${i + 1}/${accounts.length}] 账号 [${accountLabel(accounts[i])}] 签到中...`);
    const r = await runAccount(accounts[i], targetHosts);
    results.push({ name: r.name, result: r.result, line: r.line });
    if (r.usedHost) usedHost = r.usedHost;

    // 多账号间随机间隔，降低风控
    if (i < accounts.length - 1) {
      const gap = rand(3000, 8000);
      dbg(`账号间隔 ${gap}ms`);
      await sleep(gap);
    }
  }

  // ---- 失败重试：限流/网关抖动这类故障等一会儿往往就好了（默认关闭，见 IKUUU_RETRY_TIMES）----
  for (let attempt = 1; attempt <= RETRY_TIMES; attempt++) {
    const retryIdx = results
      .map((_, i) => i)
      .filter((i) => isRetryableStatus(results[i].result.status));
    if (retryIdx.length === 0) break;
    console.log(`\n⏳ 第 ${attempt}/${RETRY_TIMES} 次重试：${RETRY_DELAY_MS / 1000}s 后重跑 ${retryIdx.length} 个失败账号...`);
    await sleep(RETRY_DELAY_MS);
    for (const i of retryIdx) {
      console.log(`[重试 ${attempt}] 账号 [${results[i].name}] 签到中...`);
      const r = await runAccount(accounts[i], targetHosts);
      results[i] = { name: r.name, result: r.result, line: r.line };
      if (r.usedHost) usedHost = r.usedHost;
    }
  }

  // ---- 分档判定 ----
  const successCount = results.filter((r) => r.result.status === 'success' || r.result.status === 'already').length;
  const failCount = results.length - successCount;

  let title;
  if (failCount === 0) {
    title = `✅ iKuuu 签到全部成功（${successCount}/${results.length}）`;
  } else if (successCount === 0) {
    title = `❌ iKuuu 签到全部失败（域名: ${usedHost}）`;
  } else {
    title = `⚠️ iKuuu 签到部分失败（成功 ${successCount} / 失败 ${failCount}）`;
  }

  // ---- 通知正文（markdown 友好） ----
  let desp = `**签到域名**：\`${usedHost}\`\n\n`;
  desp += `**统计**：成功 ${successCount} / 失败 ${failCount} / 共 ${results.length}\n\n`;
  desp += `**明细**：\n\n`;
  for (const r of results) {
    desp += `- **[${r.name}]** ${r.line}\n`;
  }
  desp += `\n---\n*时间：${new Date().toLocaleString('zh-CN', { hour12: false })}*`;

  console.log('=== 签到任务执行完毕 ===');
  console.log(`标题: ${title}`);

  // 失败才推送 / 全部推送
  if (NOTIFY_ONLY_FAIL && failCount === 0) {
    console.log('全部成功且已开启 IKUUU_NOTIFY_ONLY_FAIL, 跳过推送');
  } else {
    try {
      await sendNotify(title, desp);
      dbg('推送完成');
    } catch (err) {
      console.log(`⚠️ 推送异常: ${err.message}`);
    }
  }

  // 全失败才以非0退出（青龙便于识别失败任务红色标记）
  // 用 exitCode 而非 process.exit：退出时 undici 代理连接可能仍在收尾，
  // 强行退出会与 libuv 句柄关闭竞态（Windows 下实测触发断言崩溃，且会截断推送）
  if (successCount === 0) {
    process.exitCode = 1;
  }
}

// 导出纯函数供单元测试使用（青龙直接运行不受影响）
module.exports = {
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
};

if (require.main === module) {
  main().catch((err) => {
    console.error('主流程异常:', err);
    process.exit(1);
  });
}
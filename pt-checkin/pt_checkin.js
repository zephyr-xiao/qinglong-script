/**
 * @name PT 站点自动签到
 * @description NovaHD / HDArea / BTSchool / CrabPT 四站签到，Cookie 失效自动登录兜底（视觉模型识别验证码），限次保护防封 IP
 * @cron 30 8 * * *
 *
 * 环境变量：
 *   PT_SITE_NOVAHD_CK         【可选】NovaHD（pt.novahd.top）Cookie 种子；自动登录成功后会缓存到 novahd_cookie.json 并优先使用
 *   PT_SITE_HDAREA_CK         【可选】HDArea（hdarea.club）Cookie；该站无自动登录，失效需重新导出
 *   PT_SITE_BTSCHOOL_CK       【可选】BTSchool（pt.btschool.club）Cookie 种子；自动登录成功后会缓存到 btschool_cookie.json 并优先使用
 *   PT_SITE_CRABPT_CK         【可选】CrabPT（crabpt.vip）Cookie 种子；自动登录成功后会缓存到 crabpt_cookie.json 并优先使用
 *   PT_NOVAHD_ACCOUNTS        【可选】NovaHD 账密，格式 用户名#密码，Cookie 失效时自动登录兜底用
 *                                      兼容旧写法：PT_NOVAHD_USERNAME + PT_NOVAHD_PASSWORD
 *   PT_BTSCHOOL_ACCOUNTS      【可选】BTSchool 账密，格式 用户名#密码，Cookie 失效时自动登录兜底用
 *                                      兼容旧写法：PT_BTSCHOOL_USERNAME + PT_BTSCHOOL_PASSWORD
 *   PT_CRABPT_ACCOUNTS        【可选】CrabPT 账密，格式 用户名#密码，Cookie 失效时自动登录兜底用
 *                                      兼容旧写法：PT_CRABPT_USERNAME + PT_CRABPT_PASSWORD
 *   PT_OCR_API_URL            【可选】OpenAI 兼容视觉接口地址（如 https://xx/v1/chat/completions），多站共用
 *   PT_OCR_API_KEY            【可选】对应 API Key；未配置或调用失败时自动降级到本地 ddddocr（需 Python + ddddocr + opencv-python）
 *   PT_OCR_MODEL              【可选】视觉模型名，默认 gpt-4o-mini
 *   PT_PYTHON                 【可选】ddddocr 降级用的 Python 解释器路径，默认 python
 *   PT_PROXY                  【可选】HTTP 代理，如 http://172.17.0.1:7890
 *                                      未配置时自动回退青龙全局代理（HTTPS_PROXY /
 *                                      HTTP_PROXY / ALL_PROXY / GLOBAL_AGENT_*）
 *   PT_NOTIFY_ONLY_FAIL       【可选】1 = 仅失败时推送
 *   PT_DEBUG                  【可选】1 = 打印详细调试信息（含页面片段，注意勿泄露）
 *   PT_TIMEOUT                【可选】HTTP 超时秒数，默认 20
 *
 * 登录限次保护（防止连续失败封 IP）：
 *   - BTSchool 允许连续失败 20 次，NovaHD 仅 10 次；脚本每次运行前解析登录页「你还有 [N] 次尝试机会」
 *   - 剩余次数低于阈值（BTSchool 10 / NovaHD 5 / CrabPT 5）立即放弃登录并推送警告
 *   - 单次运行登录尝试轮数上限 = 阈值一半（BTSchool 5 / NovaHD 2 / CrabPT 3），成功登录后站点计数自动清零
 *
 * 说明：
 *   - 未配置 Cookie 且无账密的站点自动跳过，不影响其他站点。
 *   - BTSchool 签到端点为 GET index.php?action=addbonus（非标准 NexusPHP attendance.php）。
 *   - CrabPT 签到端点为 GET attendance.php 直接触发签到（无表单无验证码），奖励名为蟹币值。
 *   - NovaHD 登录为 challenge-response 挑战认证：response = HMAC-SHA256(challenge, SHA256(secret + SHA256(password)))。
 *   - 验证码识别两级链路：视觉模型（PT_OCR_API_URL）→ 本地 ddddocr 降级（同目录 ddddocr_ocr.py，
 *     颜色过滤 + 连通域去噪 + 双模型投票，实测 30 样本 80.0%）。
 *   - 推送：复用同目录 sendNotify.js（青龙官方 Notify）。
 * 作者: zephyr_xiao
 */

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { sendNotify } = require(path.join(__dirname, 'sendNotify'));
// undici（sendNotify.js 已依赖）用于代理支持；redirect: manual 需要它对 Set-Cookie 的透传
const { fetch: undiciFetch, ProxyAgent } = require('undici');

const DEBUG = process.env.PT_DEBUG === '1';
const NOTIFY_ONLY_FAIL = process.env.PT_NOTIFY_ONLY_FAIL === '1';
const TIMEOUT_MS = (Number(process.env.PT_TIMEOUT) || 20) * 1000;

// 登录限次保护参数：安全阈值（剩余次数低于阈值即放弃）与单次运行登录轮数上限
const LOGIN_LIMITS = {
  novahd: { safeFloor: 5, maxRounds: 2 },
  btschool: { safeFloor: 10, maxRounds: 5 },
  crabpt: { safeFloor: 5, maxRounds: 3 },
};

// UA 池：每次运行随机取一个，保持整次运行内一致（同一会话换 UA 反而是异常特征）
const UA_LIST = [
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0',
];
const UA = UA_LIST[Math.floor(Math.random() * UA_LIST.length)];

// 代理注入（青龙容器内常用 http://172.17.0.1:7890 走宿主机代理）
// 优先级：PT_PROXY > 青龙全局代理变量 > 直连。
// undici 的 fetch 不读取环境变量，global-agent 也管不到它，故此处显式解析；
// GLOBAL_AGENT_* 是青龙给 Node 脚本准备的全局代理变量名。
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

const { url: PROXY, source: PROXY_SOURCE } = resolveProxy(['PT_PROXY']);
const proxyAgent = PROXY ? new ProxyAgent(PROXY) : null;
const FETCH_OPTIONS = proxyAgent ? { dispatcher: proxyAgent } : {};

// 统一带代理的 fetch（代理与超时在此注入，调用处保持简洁）
function apiFetch(url, options = {}) {
  return undiciFetch(url, { ...options, ...FETCH_OPTIONS });
}

// 解析「用户名#密码」合并式账号变量；未配置时回退旧的分开变量写法
function resolveAccount(accountsKey, userKey, passwordKey) {
  const raw = (process.env[accountsKey] || '').trim();
  if (raw) {
    const first = raw.split(/[&\n]/).map((s) => s.trim()).filter(Boolean)[0] || '';
    const idx = first.indexOf('#');
    const username = idx > 0 ? first.slice(0, idx).trim() : '';
    const password = idx > 0 ? first.slice(idx + 1).trim() : '';
    if (username && password) return { username, password };
    console.log(`⚠️ ${accountsKey} 格式应为 用户名#密码，当前无法解析，将按未配置处理`);
    return { username: '', password: '' };
  }
  return {
    username: (process.env[userKey] || '').trim(),
    password: (process.env[passwordKey] || '').trim(),
  };
}

// 睡眠（毫秒）；风控间隔 / 重试退避用
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// 随机整数 [min, max]
function rand(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

// 日志（DEBUG 模式才打印）
function dbg(...args) {
  if (DEBUG) console.log('[DEBUG]', ...args);
}

// ==========================================
// 1. Cookie 缓存（自动登录成功后的新 Cookie 优先于环境变量种子）
// ==========================================
function cookieFileFor(siteKey) {
  return path.join(__dirname, `${siteKey}_cookie.json`);
}

function readCachedCookie(siteKey) {
  try {
    const data = JSON.parse(fs.readFileSync(cookieFileFor(siteKey), 'utf8'));
    if (data && data.cookie) {
      return { cookie: data.cookie, savedAt: data.savedAt || 0 };
    }
  } catch (e) {
    // 无缓存文件或损坏，视为无缓存
  }
  return null;
}

function saveCookie(siteKey, cookie) {
  try {
    // 只缓存 Cookie 与时间，绝不缓存密码
    fs.writeFileSync(cookieFileFor(siteKey), JSON.stringify({ cookie, savedAt: Date.now() }));
  } catch (e) {
    console.log(`⚠️ ${siteKey} Cookie 缓存写入失败: ${e.message}`);
  }
}

// 环境变量种子 Cookie：缓存优先，无缓存才用种子
function resolveCookie(siteKey, envName) {
  const cached = readCachedCookie(siteKey);
  if (cached) {
    console.log(`  ↪ 使用自动登录缓存的 Cookie（${new Date(cached.savedAt).toLocaleString('zh-CN', { hour12: false })}）`);
    return cached.cookie;
  }
  return (process.env[envName] || '').trim().replace(/[\r\n\t]/g, '') || null;
}

// ==========================================
// 2. 通用请求工具
// ==========================================
// 从 Set-Cookie 数组提取「名=值」对拼成请求用 Cookie 串
function setCookiesToCookieString(setCookieList) {
  if (!setCookieList) return '';
  const pairs = [];
  for (const line of setCookieList) {
    const first = String(line).split(';')[0].trim();
    if (first && first.includes('=')) pairs.push(first);
  }
  return pairs.join('; ');
}

// 合并 Cookie 串（新值覆盖同名旧值，保留未涉及的旧值）
function mergeCookies(oldCookie, setCookieList) {
  if (!setCookieList || setCookieList.length === 0) return oldCookie || '';
  const jar = {};
  for (const pair of (oldCookie || '').split(';')) {
    const idx = pair.indexOf('=');
    if (idx > 0) jar[pair.slice(0, idx).trim()] = pair.slice(idx + 1).trim();
  }
  for (const pair of setCookiesToCookieString(setCookieList).split(';')) {
    const idx = pair.indexOf('=');
    if (idx > 0) jar[pair.slice(0, idx).trim()] = pair.slice(idx + 1).trim();
  }
  return Object.entries(jar).map(([k, v]) => `${k}=${v}`).join('; ');
}

// GET 单次请求（内部供 getPage 跟随重定向用）
async function rawGet(url, cookie, extraHeaders = {}) {
  const response = await apiFetch(url, {
    headers: {
      'User-Agent': UA,
      Accept: 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
      'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
      Referer: new URL(url).origin + '/',
      ...(cookie ? { Cookie: cookie } : {}),
      ...extraHeaders,
    },
    redirect: 'manual',
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });
  const text = await response.text();
  return {
    httpStatus: response.status,
    text,
    setCookie: response.headers.getSetCookie?.() || [],
    location: response.headers.get('location') || '',
  };
}

// GET 页面并手动跟随重定向（最多 5 跳，逐跳合并 Cookie）。
// 必须手动跟随的原因：Cookie 失效时站点 302 到登录页，自动跟随会丢失「曾被重定向」这一关键信号；
// finalUrl 供调用方判定登录态。
async function getPage(url, cookie, extraHeaders = {}) {
  let currentUrl = url;
  let currentCookie = cookie;
  let accumulated = [];
  let last = null;
  try {
    for (let hop = 0; hop < 5; hop++) {
      last = await rawGet(currentUrl, currentCookie, extraHeaders);
      accumulated = accumulated.concat(last.setCookie);
      if ([301, 302, 303, 307, 308].includes(last.httpStatus) && last.location) {
        currentCookie = mergeCookies(currentCookie, last.setCookie);
        currentUrl = new URL(last.location, currentUrl).toString();
        continue;
      }
      break;
    }
    dbg(`GET ${url} → ${last.httpStatus}，finalUrl=${currentUrl}，长度 ${last.text.length}`);
    return { httpStatus: last.httpStatus, text: last.text, setCookie: accumulated, finalUrl: currentUrl };
  } catch (err) {
    return { error: err.message };
  }
}

// POST 表单，返回 {httpStatus, text, setCookie, location}
async function postForm(url, cookie, body, extraHeaders = {}) {
  try {
    const response = await apiFetch(url, {
      method: 'POST',
      headers: {
        'User-Agent': UA,
        Accept: 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        Referer: new URL(url).origin + '/',
        'Content-Type': 'application/x-www-form-urlencoded',
        ...(cookie ? { Cookie: cookie } : {}),
        ...extraHeaders,
      },
      body,
      redirect: 'manual',
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    const text = await response.text();
    const location = response.headers.get('location') || '';
    dbg(`POST ${url} → ${response.status}，Location=${location}，长度 ${text.length}`);
    return { httpStatus: response.status, text, setCookie: response.headers.getSetCookie?.() || [], location };
  } catch (err) {
    return { error: err.message };
  }
}

// 网络层异常才重试（退避 2-4s，最多 2 次）；HTTP 业务错误在分类层处理，不在此重试
async function withRetry(fn, maxRetry = 2) {
  let lastErr = null;
  for (let i = 0; i <= maxRetry; i++) {
    try {
      const res = await fn();
      if (res.error) throw new Error(res.error);
      return res;
    } catch (err) {
      lastErr = err;
      dbg(`第 ${i + 1} 次网络异常: ${err.message}`);
      if (i < maxRetry) await sleep(rand(2000, 4000));
    }
  }
  return { error: lastErr.message };
}

// ==========================================
// 3. 登录页解析（NovaHD / BTSchool 同为 NexusPHP regimage 结构）
// ==========================================
// 从登录页 HTML 提取验证码 imagehash、隐藏 secret（NovaHD）、剩余尝试次数
function parseLoginPage(html) {
  const imagehash = (html.match(/name="imagehash"\s+value="([0-9a-f]{32})"/i)
    || html.match(/imagehash=([0-9a-f]{32})/i) || [])[1] || null;
  const secret = (html.match(/name="secret"\s+value="([^"]*)"/i) || [])[1] || '';
  // 「你还有 [N] 次尝试机会」；解析不到视为未知（null），由调用方决定策略
  const remainMatch = html.match(/你还有\s*<b><font[^>]*>\[?(\d+)\]?<\/font><\/b>\s*次尝试机会/)
    || html.match(/你还有[^<]*(?:<[^>]+>\s*)*\[?(\d+)\]?[^<]*次尝试机会/);
  const remainAttempts = remainMatch ? parseInt(remainMatch[1], 10) : null;
  return { imagehash, secret, remainAttempts };
}

// OCR 结果清洗：剥离所有非字母数字字符（模型常带「验证码是 XXX」等说明文字）
function cleanOcrText(text) {
  return String(text || '').replace(/[^a-zA-Z0-9]/g, '');
}

// ==========================================
// 4. 验证码识别：视觉模型优先，ddddocr 降级兜底
// ==========================================
// ddddocr 降级识别：调同目录 ddddocr_ocr.py 子进程（Python + ddddocr 环境）
// 实测 30 样本 80.0%（颜色过滤预处理 + 双模型投票）；视觉模型不可用/失败时启用
const { execFile } = require('child_process');

function ddddocrRecognize(pngBuffer) {
  return new Promise((resolve) => {
    const tmpFile = path.join(__dirname, `.captcha_tmp_${process.pid}.png`);
    try {
      fs.writeFileSync(tmpFile, pngBuffer);
    } catch (e) {
      return resolve({ error: `验证码临时文件写入失败: ${e.message}` });
    }
    // Linux 容器通常只有 python3 命令，Windows 是 python
    const DEFAULT_PYTHON = process.platform === 'win32' ? 'python' : 'python3';
    const pythonBin = (process.env.PT_PYTHON || DEFAULT_PYTHON).trim();
    const timeoutMs = 30000;
    const child = execFile(
      pythonBin,
      [path.join(__dirname, 'ddddocr_ocr.py'), tmpFile],
      { timeout: timeoutMs, windowsHide: true },
      (err, stdout) => {
        try { fs.unlinkSync(tmpFile); } catch (e) { /* 临时文件清理失败不影响主流程 */ }
        if (err) {
          return resolve({ error: `ddddocr 识别失败: ${err.message.substring(0, 120)}` });
        }
        try {
          const line = stdout.trim().split('\n').pop();
          const data = JSON.parse(line);
          if (data.code) return resolve({ code: data.code });
          return resolve({ error: data.error || 'ddddocr 未返回识别结果' });
        } catch (e) {
          return resolve({ error: `ddddocr 输出解析失败: ${String(stdout).substring(0, 80)}` });
        }
      }
    );
    child.on('error', () => { /* execFile 回调已处理 err */ });
  });
}

async function recognizeCaptcha(pngBuffer) {
  const apiUrl = (process.env.PT_OCR_API_URL || '').trim();
  const apiKey = (process.env.PT_OCR_API_KEY || '').trim();
  const model = (process.env.PT_OCR_MODEL || 'gpt-4o-mini').trim();

  // 一级：视觉模型（配置了才启用）
  if (apiUrl && apiKey) {
    const b64 = pngBuffer.toString('base64');
    try {
      const response = await apiFetch(apiUrl, {
        method: 'POST',
        headers: {
          'User-Agent': UA,
          Authorization: `Bearer ${apiKey}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          model,
          messages: [
            {
              role: 'user',
              content: [
                { type: 'text', text: '识别图片中的验证码字符，只输出验证码本身（字母和数字），不要任何其他文字。' },
                { type: 'image_url', image_url: { url: `data:image/png;base64,${b64}` } },
              ],
            },
          ],
          max_tokens: 32,
          temperature: 0,
        }),
        // 视觉模型推理较慢，超时独立于页面请求（60s）
        signal: AbortSignal.timeout(60000),
      });
      const text = await response.text();
      if (response.status === 200) {
        const data = JSON.parse(text);
        const raw = data.choices?.[0]?.message?.content || '';
        const code = cleanOcrText(raw);
        if (code) {
          dbg(`视觉模型识别结果: ${code}（原文: ${raw.trim().substring(0, 40)}）`);
          return { code };
        }
        dbg(`视觉模型未识别出字符: ${raw.substring(0, 60)}，尝试 ddddocr 降级`);
      } else {
        dbg(`视觉模型 HTTP ${response.status}: ${text.substring(0, 120)}，尝试 ddddocr 降级`);
      }
    } catch (err) {
      dbg(`视觉模型请求异常: ${err.message}，尝试 ddddocr 降级`);
    }
  }

  // 二级：ddddocr 降级（本地 Python，无需联网）
  return ddddocrRecognize(pngBuffer);
}

// 下载验证码图片并识别，返回 {code} 或 {error}
async function fetchAndRecognizeCaptcha(baseUrl, imagehash, cookie) {
  const captchaUrl = `${baseUrl}/image.php?action=regimage&imagehash=${imagehash}`;
  try {
    const response = await apiFetch(captchaUrl, {
      headers: {
        'User-Agent': UA,
        Accept: 'image/avif,image/webp,image/png,image/*,*/*;q=0.8',
        Referer: `${baseUrl}/login.php`,
        ...(cookie ? { Cookie: cookie } : {}),
      },
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    if (!response.ok) return { error: `验证码下载 HTTP ${response.status}` };
    const buf = Buffer.from(await response.arrayBuffer());
    // PNG 魔数校验：站点返回非图片（如错误页）时直接报错，不浪费 OCR 调用
    if (buf.length < 500 || buf[0] !== 0x89 || buf[1] !== 0x50 || buf[2] !== 0x4e || buf[3] !== 0x47) {
      return { error: `验证码内容异常（${buf.length} 字节，非 PNG）` };
    }
    return await recognizeCaptcha(buf);
  } catch (err) {
    return { error: `验证码下载异常: ${err.message}` };
  }
}

// ==========================================
// 5. NovaHD 登录（challenge-response 挑战认证）
// ==========================================
// response = HMAC-SHA256(key=challenge, msg=SHA256(secret + SHA256(password)))，全 hex 小写
function novahdChallengeResponse(secret, challenge, password) {
  const sha256hex = (s) => crypto.createHash('sha256').update(s, 'utf8').digest('hex');
  const serverSideHash = sha256hex(secret + sha256hex(password));
  return crypto.createHmac('sha256', challenge).update(serverSideHash, 'utf8').digest('hex');
}

// 请求 NovaHD 挑战接口；成功返回 {challengeData, cookie}（cookie 已并入补发 Cookie），失败返回 {error}
async function requestNovahdChallenge(base, username, sessionCookie) {
  try {
    const chResp = await apiFetch(`${base}/api/challenge`, {
      method: 'POST',
      headers: {
        'User-Agent': UA,
        'Content-Type': 'application/json',
        Origin: base,
        Referer: `${base}/login.php`,
        ...(sessionCookie ? { Cookie: sessionCookie } : {}),
      },
      body: JSON.stringify({ username }),
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    const chSetCookie = chResp.headers.getSetCookie?.() || [];
    const chText = await chResp.text();
    const challengeData = JSON.parse(chText);
    if (challengeData.ret !== 0 || !challengeData.data?.challenge) {
      return { error: `挑战接口返回异常: ${chText.substring(0, 120)}` };
    }
    // 挑战接口若补发 Cookie，并入会话
    return { challengeData, cookie: mergeCookies(sessionCookie, chSetCookie) };
  } catch (err) {
    return { error: `挑战请求异常: ${err.message}` };
  }
}

// 自动登录 NovaHD；成功返回新 Cookie，失败返回 null（原因已推送日志）
async function loginNovahd(username, password) {
  const base = 'https://pt.novahd.top';
  const limit = LOGIN_LIMITS.novahd;

  for (let round = 1; round <= limit.maxRounds; round++) {
    console.log(`  ↪ NovaHD 自动登录第 ${round}/${limit.maxRounds} 轮...`);
    // 1. 取登录页：imagehash + secret + 剩余次数
    const loginPage = await withRetry(() => getPage(`${base}/login.php`));
    if (loginPage.error) {
      console.log(`  ⚠️ 登录页获取失败: ${loginPage.error}`);
      return null;
    }
    const page = parseLoginPage(loginPage.text);
    if (!page.imagehash) {
      console.log('  ⚠️ 登录页未解析到 imagehash，页面结构可能已变化');
      return null;
    }
    if (page.remainAttempts != null && page.remainAttempts < limit.safeFloor) {
      console.log(`  ❌ 剩余尝试机会仅 ${page.remainAttempts} 次（低于安全阈值 ${limit.safeFloor}），放弃登录防止封 IP`);
      return null;
    }

    // 2. 下载并识别验证码
    const captcha = await fetchAndRecognizeCaptcha(base, page.imagehash, loginPage.setCookie.length ? mergeCookies('', loginPage.setCookie) : undefined);
    if (captcha.error) {
      console.log(`  ⚠️ ${captcha.error}`);
      return null;
    }

    // 3. 请求挑战（携带登录页下发的 Cookie，接续会话；补发 Cookie 由函数内并入）
    const loginCookie = mergeCookies('', loginPage.setCookie);
    const challenge = await requestNovahdChallenge(base, username, loginCookie);
    if (challenge.error) {
      console.log(`  ⚠️ ${challenge.error}`);
      return null;
    }
    const { challengeData, cookie: challengeCookie } = challenge;

    // 4. 计算 response 并全字段提交（模拟浏览器表单：secret 取登录页表单值，
    //    挑战返回的 secret 仅参与 response 计算；two_step_code 未设置时为空）
    const response = novahdChallengeResponse(
      challengeData.data.secret || page.secret,
      challengeData.data.challenge,
      password
    );
    const form = new URLSearchParams({
      secret: page.secret,
      response,
      username,
      password,
      two_step_code: '',
      imagestring: captcha.code,
      imagehash: page.imagehash,
    });
    const post = await postForm(`${base}/takelogin.php`, challengeCookie || undefined, form.toString());

    // 5. 判定：302 跳转非登录页 或 Set-Cookie 发登录态 即视为成功
    const newCookie = mergeCookies(challengeCookie, post.setCookie);
    const hasUid = /(?:^|;\s*)uid=\d+/.test(newCookie) || /c_secure_login=1/.test(newCookie);
    const redirectedOut = post.location && !/login\.php|takelogin\.php/i.test(post.location);
    const pageSaysOk = post.text && !/login\.php/i.test(post.text.slice(0, 3000)) && /logout|控制面板|index\.php/i.test(post.text);
    if (post.httpStatus === 302 || post.httpStatus === 303 || redirectedOut || hasUid || pageSaysOk) {
      if (newCookie) {
        console.log('  ✅ NovaHD 自动登录成功');
        return newCookie;
      }
    }
    dbg(`登录失败详情: httpStatus=${post.httpStatus} location=${post.location} cookie=${newCookie.substring(0, 80)} body=${(post.text || '').substring(0, 300).replace(/\s+/g, ' ')}`);
    console.log(`  ⚠️ 第 ${round} 轮登录未成功（验证码错误或判定未命中），换新验证码重试`);
    await sleep(rand(3000, 6000));
  }
  console.log(`  ❌ NovaHD 自动登录 ${limit.maxRounds} 轮均失败，放弃（保护站点尝试次数）`);
  return null;
}

// ==========================================
// 6. BTSchool 登录（标准 NexusPHP 表单 + regimage 验证码）
// ==========================================
async function loginBtschool(username, password) {
  const base = 'https://pt.btschool.club';
  const limit = LOGIN_LIMITS.btschool;

  for (let round = 1; round <= limit.maxRounds; round++) {
    console.log(`  ↪ BTSchool 自动登录第 ${round}/${limit.maxRounds} 轮...`);
    const loginPage = await withRetry(() => getPage(`${base}/login.php`));
    if (loginPage.error) {
      console.log(`  ⚠️ 登录页获取失败: ${loginPage.error}`);
      return null;
    }
    const page = parseLoginPage(loginPage.text);
    if (!page.imagehash) {
      console.log('  ⚠️ 登录页未解析到 imagehash，页面结构可能已变化');
      return null;
    }
    if (page.remainAttempts != null && page.remainAttempts < limit.safeFloor) {
      console.log(`  ❌ 剩余尝试机会仅 ${page.remainAttempts} 次（低于安全阈值 ${limit.safeFloor}），放弃登录防止封 IP`);
      return null;
    }

    const captcha = await fetchAndRecognizeCaptcha(base, page.imagehash, loginPage.setCookie.length ? mergeCookies('', loginPage.setCookie) : undefined);
    if (captcha.error) {
      console.log(`  ⚠️ ${captcha.error}`);
      return null;
    }

    const form = new URLSearchParams({
      username,
      password,
      imagestring: captcha.code,
      imagehash: page.imagehash,
    });
    const loginCookie = mergeCookies('', loginPage.setCookie);
    const post = await postForm(`${base}/takelogin.php`, loginCookie || undefined, form.toString());

    const newCookie = mergeCookies(loginCookie, post.setCookie);
    const hasUid = /(?:^|;\s*)uid=\d+/.test(newCookie) || /c_secure_login=1/.test(newCookie);
    const redirectedOut = post.location && !/login\.php|takelogin\.php/i.test(post.location);
    if (post.httpStatus === 302 || post.httpStatus === 303 || redirectedOut || hasUid) {
      if (newCookie) {
        console.log('  ✅ BTSchool 自动登录成功');
        return newCookie;
      }
    }
    dbg(`登录失败详情: httpStatus=${post.httpStatus} location=${post.location} cookie=${newCookie.substring(0, 80)} body=${(post.text || '').substring(0, 300).replace(/\s+/g, ' ')}`);
    console.log(`  ⚠️ 第 ${round} 轮登录未成功（验证码错误或判定未命中），换新验证码重试`);
    await sleep(rand(3000, 6000));
  }
  console.log(`  ❌ BTSchool 自动登录 ${limit.maxRounds} 轮均失败，放弃（保护站点尝试次数）`);
  return null;
}

// ==========================================
// 7. NovaHD 签到（attendance.php + sign_in POST）
// ==========================================
// 解析签到详情（连续天数 / 奖励 / 总次数），多 pattern 兜底适配不同皮肤
function parseNovahdAttendance(html) {
  let continuousDays = null;
  let reward = null;
  let totalSignCount = null;

  // 已签到状态：多 pattern 兜底（原脚本精华，保留）
  const successPatterns = [
    /签到成功/i, /签到完成/i, /attendance.*success/i, /恭喜.*签到/i,
    /今日签到获得/i, /本次签到获得/i, /签到奖励/i, /连续签到.*天/i,
  ];
  const hasSignSuccess = successPatterns.some((p) => p.test(html));

  const continuousPatterns = [
    /已连续签到\s*<b>(\d+)<\/b>\s*天/i,
    /连续签到\s*<b>(\d+)<\/b>\s*天/i,
    /连续签到[：:\s]*(\d+)\s*天/i,
    /已连续签到[：:\s]*(\d+)\s*天/i,
    /连续\s*(\d+)\s*天签到/i,
    /(\d+)\s*天连续签到/i,
    /<td[^>]*>连续签到天数<\/td>\s*<td[^>]*>(\d+)/i,
    /连续签到\s*(\d+)\s*天/i,
    /已连续\s*(\d+)\s*天/i,
  ];
  for (const p of continuousPatterns) {
    const m = html.match(p);
    if (m && m[1] && parseInt(m[1], 10) > 0) { continuousDays = m[1]; break; }
  }

  const rewardPatterns = [
    /本次签到获得\s*<b>(\d+)<\/b>\s*个魔力值/i,
    /今日签到获得\s*<b>(\d+)<\/b>\s*个魔力值/i,
    /签到奖励[：:\s]*<b>(\d+)<\/b>\s*魔力值/i,
    /获得[：:\s]*<b>(\d+)<\/b>\s*魔力值/i,
    /<td[^>]*>今日奖励<\/td>\s*<td[^>]*>(\d+)\s*魔力值/i,
    /本次签到获得\s*(\d+)\s*个魔力值/i,
    /今日签到获得\s*(\d+)\s*个魔力值/i,
    /签到奖励[：:\s]*(\d+)\s*魔力值/i,
    /获得[：:\s]*(\d+)\s*魔力值/i,
    /[+增加得到获得]\s*(\d+)\s*魔力值/i,
    /(\d+)\s*个魔力值/i,
  ];
  for (const p of rewardPatterns) {
    const m = html.match(p);
    if (m && m[1] && parseInt(m[1], 10) > 0) { reward = `${m[1]}魔力值`; break; }
  }

  const totalPatterns = [
    /这是您的第\s*<b>(\d+)<\/b>\s*次签到/i,
    /第\s*<b>(\d+)<\/b>\s*次签到/i,
    /这是您的第\s*(\d+)\s*次签到/i,
    /第\s*(\d+)\s*次签到/i,
    /总计签到[：:\s]*(\d+)\s*次/i,
  ];
  for (const p of totalPatterns) {
    const m = html.match(p);
    if (m && m[1] && parseInt(m[1], 10) > 0) { totalSignCount = m[1]; break; }
  }

  return { continuousDays, reward, totalSignCount, hasSignSuccess };
}

// Cookie 失效判定：页面出现登录提示文案
function isCookieDead(html) {
  return /需要启用cookies才能登录|连续登录失败/i.test(html)
    || (/<title>[^<]*登录[^<]*<\/title>/i.test(html) && /name="imagehash"/i.test(html));
}

function detailFrom(continuousDays, reward, totalSignCount) {
  let msg = '';
  if (totalSignCount) msg += `\n  📊 第 ${totalSignCount} 次签到`;
  if (continuousDays) msg += `\n  🎯 连续签到 ${continuousDays} 天`;
  if (reward) msg += `\n  🎁 获得 ${reward}`;
  if (!msg) msg = '\n  ⚠️ 未能解析详细签到信息';
  return msg;
}

// 单次签到提交：从签到页 HTML 提取 imagehash → 识别验证码 → POST imagehash+imagestring 表单。
// 真实机制（抓包确认）：未签状态 attendance.php 是验证码表单，无 action 字段；
// POST action=sign_in 会被站点回「图片代码无效！」。已签状态页面无表单、直接显示统计文案。
// maxRounds=验证码重试上限；retryDelayMs=轮间延迟（毫秒，测试传 0 免等待）
// 返回 {status:'posted', info} | {status:'captcha_err', error} | {error}
async function novahdSignOnce(base, cookie, html, maxRounds = 2, retryDelayMs = null) {
  const pageHash = parseLoginPage(html).imagehash;
  if (!pageHash) {
    return { error: '签到页未解析到 imagehash（可能已签到或页面结构变化）' };
  }

  for (let round = 1; round <= maxRounds; round++) {
    if (round > 1) {
      console.log(`  ↪ 签到验证码疑似错误，换新验证码重试 ${round}/${maxRounds}...`);
      await sleep(retryDelayMs == null ? rand(3000, 6000) : retryDelayMs);
    }
    const captcha = await fetchAndRecognizeCaptcha(base, pageHash, cookie);
    if (captcha.error) return { error: captcha.error };

    const form = new URLSearchParams({
      imagehash: pageHash,
      imagestring: captcha.code,
    });
    const post = await postForm(
      `${base}/attendance.php`,
      cookie,
      form.toString(),
      { Referer: `${base}/attendance.php` }
    );
    if (post.error) return { error: `签到请求失败: ${post.error}` };

    const info = parseNovahdAttendance(post.text || '');
    if (info.hasSignSuccess || info.reward || info.totalSignCount) {
      return { status: 'posted', info };
    }
    if (/图片代码无效/.test(post.text || '')) {
      if (round === maxRounds) return { status: 'captcha_err', error: `验证码连续 ${maxRounds} 轮识别失败（站点提示图片代码无效）` };
      continue;
    }
    return { status: 'captcha_err', error: '签到响应未含成功文案，请开 PT_DEBUG 查看页面' };
  }
  return { status: 'captcha_err', error: '签到重试耗尽' };
}

// 单站执行：Cookie → 签到 → Cookie 失效时自动登录兜底后重试
async function processNovahd() {
  const siteKey = 'novahd';
  let cookie = resolveCookie(siteKey, 'PT_SITE_NOVAHD_CK');
  const { username, password } = resolveAccount('PT_NOVAHD_ACCOUNTS', 'PT_NOVAHD_USERNAME', 'PT_NOVAHD_PASSWORD');

  if (!cookie && !(username && password)) {
    return { status: 'skipped', msg: '未配置 Cookie 与账密，跳过' };
  }

  const base = 'https://pt.novahd.top';
  const page = await withRetry(() => getPage(`${base}/attendance.php`, cookie || undefined));
  if (page.error) return { status: 'network_err', msg: `访问签到页失败: ${page.error}` };

  // Cookie 失效判定：登录提示文案 或 被重定向到登录页（getPage 手动跟随，finalUrl 暴露跳转终点）
  const cookieDead = isCookieDead(page.text) || /login\.php/i.test(page.finalUrl || '');
  if (cookieDead) {
    if (!(username && password)) {
      return { status: 'cookie_dead', msg: 'Cookie 已失效且未配置账密，请重新导出 Cookie' };
    }
    console.log('  ↪ Cookie 已失效，尝试自动登录兜底...');
    cookie = await loginNovahd(username, password);
    if (!cookie) return { status: 'cookie_dead', msg: 'Cookie 失效且自动登录未成功，请检查账密/OCR 配置' };
    saveCookie(siteKey, cookie);
    const retry = await withRetry(() => getPage(`${base}/attendance.php`, cookie));
    if (retry.error) return { status: 'network_err', msg: `重新访问签到页失败: ${retry.error}` };
    if (isCookieDead(retry.text)) return { status: 'cookie_dead', msg: '自动登录后仍判定未登录，账号可能有异常' };
    page.text = retry.text;
  }

  // 已签判定：页面无验证码表单（未签页必含 name="imagehash"）且含统计/成功文案 → 已签
  const alreadyPatterns = [
    /今日已签到/i, /签到已得/i, /already signed/i, /今天已经签到/i,
    /您今日已签到/i, /今日签到完成/i, /今日已打卡/i, /已经签到/i,
  ];
  const info = parseNovahdAttendance(page.text);
  const unsignedForm = /name="imagehash"/i.test(page.text);
  if (!unsignedForm && (info.hasSignSuccess || info.reward || info.totalSignCount || alreadyPatterns.some((p) => p.test(page.text)))) {
    return {
      status: 'already',
      msg: `今日已签到${detailFrom(info.continuousDays, info.reward, info.totalSignCount).replace(/\n/g, '')}`,
      detail: detailFrom(info.continuousDays, info.reward, info.totalSignCount),
    };
  }

  // 未签 → 走验证码表单签到（含「图片代码无效」重试）
  const sign = await novahdSignOnce(base, cookie, page.text);
  if (sign.error) {
    console.log(`  ⚠️ ${sign.error}`);
    // 提交失败后回读签到页确认是否其实已签上（防止误报）
    await sleep(1000);
    const refresh = await withRetry(() => getPage(`${base}/attendance.php`, cookie));
    const refreshInfo = parseNovahdAttendance(refresh.text || '');
    if (!/name="imagehash"/i.test(refresh.text || '') && (refreshInfo.hasSignSuccess || refreshInfo.totalSignCount)) {
      return {
        status: 'success',
        msg: `签到成功${detailFrom(refreshInfo.continuousDays, refreshInfo.reward, refreshInfo.totalSignCount).replace(/\n/g, '')}`,
        detail: detailFrom(refreshInfo.continuousDays, refreshInfo.reward, refreshInfo.totalSignCount),
      };
    }
    return { status: 'parse_err', msg: sign.error };
  }
  return {
    status: 'success',
    msg: `签到成功${detailFrom(sign.info.continuousDays, sign.info.reward, sign.info.totalSignCount).replace(/\n/g, '')}`,
    detail: detailFrom(sign.info.continuousDays, sign.info.reward, sign.info.totalSignCount),
  };
}

// ==========================================
// 8. HDArea 签到（首页判状态 + sign_in.php POST，无自动登录）
// ==========================================
// 区分「签到接口响应」与「首页响应」（HDArea 首页含 css3menu 导航）
function parseHdareaAttendance(html) {
  let continuousDays = null;
  let reward = null;
  let totalSignCount = null;

  const isIndexPage = /HDArea.*?首页|css3menu/i.test(html);
  const isSignInResponse = /此次签到您获得了|已连续签到.*?天.*?获得了|签到成功|请不要重复签到/i.test(html) && !isIndexPage;

  const hasSignSuccess = /此次签到您获得了|已连续签到.*?天.*?获得了|请不要重复签到/i.test(html);
  const isAlreadySignedToday = /请不要重复签到/i.test(html);

  const continuousPatterns = [
    /已连续签到(\d+)天/i,
    /\[已签到\]\s*\((\d+)\)/i,
    /已签到\s*\((\d+)\)/i,
  ];
  for (const p of continuousPatterns) {
    const m = html.match(p);
    if (m && m[1] && parseInt(m[1], 10) > 0) { continuousDays = m[1]; break; }
  }

  const rewardPatterns = [
    /获得了(\d+)魔力值/i,
    /此次签到您获得了(\d+)魔力值/i,
  ];
  for (const p of rewardPatterns) {
    const m = html.match(p);
    if (m && m[1]) { reward = `${m[1]}魔力值`; break; }
  }

  const totalPatterns = [
    /第\s*(\d+)\s*次签到/i,
    /签到次数.*?(\d+)/i,
  ];
  for (const p of totalPatterns) {
    const m = html.match(p);
    if (m && m[1]) { totalSignCount = m[1]; break; }
  }

  return { continuousDays, reward, totalSignCount, hasSignSuccess, isSignInResponse, isIndexPage, isAlreadySignedToday };
}

async function processHdarea() {
  const cookie = resolveCookie('hdarea', 'PT_SITE_HDAREA_CK');
  if (!cookie) {
    return { status: 'skipped', msg: '未配置 Cookie，跳过' };
  }

  const base = 'https://hdarea.club';
  const page = await withRetry(() => getPage(`${base}/index.php`, cookie));
  if (page.error) return { status: 'network_err', msg: `访问首页失败: ${page.error}` };
  if (isCookieDead(page.text) || /login\.php/i.test(page.finalUrl || '')) {
    return { status: 'cookie_dead', msg: 'Cookie 已失效（该站不支持自动登录），请重新导出 Cookie' };
  }

  const info = parseHdareaAttendance(page.text);
  // 只有确认响应来自签到接口时才信「已签到」，首页响应需 POST 签到后再判
  if ((info.isSignInResponse && info.hasSignSuccess) || info.isAlreadySignedToday) {
    const detail = detailFrom(info.continuousDays, info.reward, info.totalSignCount);
    return { status: 'already', msg: `今日已签到${detail.replace(/\n/g, '')}`, detail };
  }

  // 首页显示未签 → POST 签到
  const post = await postForm(
    `${base}/sign_in.php`,
    cookie,
    'action=sign_in',
    { Referer: `${base}/index.php` }
  );
  if (post.error) return { status: 'network_err', msg: `签到请求失败: ${post.error}` };

  const postInfo = parseHdareaAttendance(post.text || '');
  if (postInfo.hasSignSuccess) {
    const detail = detailFrom(postInfo.continuousDays, postInfo.reward, postInfo.totalSignCount);
    return { status: 'success', msg: `签到成功${detail.replace(/\n/g, '')}`, detail };
  }

  // POST 响应未确认 → 回读首页
  await sleep(1000);
  const refresh = await withRetry(() => getPage(`${base}/index.php`, cookie));
  const refreshInfo = parseHdareaAttendance(refresh.text || '');
  if (refreshInfo.continuousDays || refreshInfo.hasSignSuccess) {
    const detail = detailFrom(refreshInfo.continuousDays, refreshInfo.reward, refreshInfo.totalSignCount);
    return { status: 'success', msg: `签到成功${detail.replace(/\n/g, '')}`, detail };
  }
  return { status: 'parse_err', msg: '签到请求已发出但未能确认结果，请开 PT_DEBUG 查看页面' };
}

// ==========================================
// 9. BTSchool 签到（index.php?action=addbonus，非标准 NexusPHP）
// ==========================================
async function processBtschool() {
  const siteKey = 'btschool';
  let cookie = resolveCookie(siteKey, 'PT_SITE_BTSCHOOL_CK');
  const { username, password } = resolveAccount('PT_BTSCHOOL_ACCOUNTS', 'PT_BTSCHOOL_USERNAME', 'PT_BTSCHOOL_PASSWORD');

  if (!cookie && !(username && password)) {
    return { status: 'skipped', msg: '未配置 Cookie 与账密，跳过' };
  }

  const base = 'https://pt.btschool.club';
  const page = await withRetry(() => getPage(`${base}/index.php`, cookie || undefined));
  if (page.error) return { status: 'network_err', msg: `访问首页失败: ${page.error}` };

  // Cookie 失效判定（MoviePilot 判据：首页含 login.php 链接即未登录；重定向到登录页同理）
  const notLoggedIn = /href=["']login\.php["']|location.*login\.php/i.test(page.text)
    || /login\.php/i.test(page.finalUrl || '');
  if (notLoggedIn) {
    if (!(username && password)) {
      return { status: 'cookie_dead', msg: 'Cookie 已失效且未配置账密，请重新导出 Cookie' };
    }
    console.log('  ↪ Cookie 已失效，尝试自动登录兜底...');
    cookie = await loginBtschool(username, password);
    if (!cookie) return { status: 'cookie_dead', msg: 'Cookie 失效且自动登录未成功，请检查账密/OCR 配置' };
    saveCookie(siteKey, cookie);
    const retry = await withRetry(() => getPage(`${base}/index.php`, cookie));
    if (retry.error) return { status: 'network_err', msg: `重新访问首页失败: ${retry.error}` };
    if (/href=["']login\.php["']/i.test(retry.text) || /login\.php/i.test(retry.finalUrl || '')) {
      return { status: 'cookie_dead', msg: '自动登录后仍判定未登录，账号可能有异常' };
    }
    page.text = retry.text;
  }

  // 首页不含「每日签到」入口 → 已签过
  if (!/每日签到/.test(page.text)) {
    return { status: 'already', msg: '今日已签到（首页无签到入口）' };
  }

  // 签到：GET addbonus（该站为纯 GET，无 formhash）
  const sign = await withRetry(() => getPage(`${base}/index.php?action=addbonus`, cookie, { Referer: `${base}/index.php` }));
  if (sign.error) return { status: 'network_err', msg: `签到请求失败: ${sign.error}` };

  const rewardMatch = (sign.text || '').match(/今天签到[^<]*获得\s*(\d+)\s*个?魔力值/);
  if (rewardMatch) {
    return { status: 'success', msg: `签到成功，获得 ${rewardMatch[1]} 魔力值`, detail: `\n  🎁 获得 ${rewardMatch[1]} 魔力值` };
  }

  // addbonus 响应未含奖励文案 → 回读首页确认签到入口是否消失
  await sleep(1000);
  const refresh = await withRetry(() => getPage(`${base}/index.php`, cookie));
  if (!/每日签到/.test(refresh.text || '')) {
    return { status: 'success', msg: '签到成功（签到入口已消失）' };
  }
  return { status: 'parse_err', msg: '签到请求已发出但未能确认结果，请开 PT_DEBUG 查看页面' };
}

// ==========================================
// 10. CrabPT 登录与签到（attendance.php GET 即签到）
// ==========================================
// 蟹黄堡（crabpt.vip）：标准 NexusPHP 表单登录（takelogin.php + regimage 验证码），
// 成功后 302 → index.php 并下发新版 c_secure_pass 单枚登录 Cookie（旧版为 uid/pass 两枚）。
// 签到无需表单：GET attendance.php 即触发，响应正文「签到成功…本次签到获得 N 个蟹币值」；
// 已签日重复 GET 幂等（返回与签到成功页相同的当日结果，不重复计数），脚本仍先查首页避免多余请求。
function parseCrabptAttendance(html) {
  // 数字可能被 <b>/<font> 等标签包裹（实测「第 <b>1</b> 次签到」），pattern 允许可选标签
  const num = '(?:<[^>]+>\\s*)?(\\d+)(?:\\s*</[^>]+>)?';
  // 登录页特征：验证码表单 + 账号输入框（用于 Cookie 失效判定）
  const isLoginPage = /name="imagehash"/i.test(html) && /name="username"/i.test(html);
  const reward = (html.match(new RegExp(`本次签到获得\\s*${num}\\s*个?\\s*蟹币值`, 'i'))
    || html.match(new RegExp(`首次签到获得\\s*${num}\\s*个?\\s*蟹币值`, 'i')) || [])[1] || null;
  const continuousDays = (html.match(new RegExp(`已连续签到\\s*${num}\\s*天`, 'i')) || [])[1] || null;
  const totalSignCount = (html.match(new RegExp(`这是您的第\\s*${num}\\s*次签到`, 'i')) || [])[1] || null;
  const rank = (html.match(new RegExp(`今日签到排名[：:]\\s*${num}`, 'i')) || [])[1] || null;
  const hasSignSuccess = /签到成功|本次签到获得/i.test(html);
  // 已签提示覆盖常见 NexusPHP 文案；「签到已得」实测来自首页导航栏
  const isAlreadySigned = /今天已经签到|已经签到过|今日已签到|您今天已经|明天再来|明日再来|签到已得/.test(html);
  return { isLoginPage, reward, continuousDays, totalSignCount, rank, hasSignSuccess, isAlreadySigned };
}

async function loginCrabpt(username, password) {
  const base = 'https://crabpt.vip';
  const limit = LOGIN_LIMITS.crabpt;

  for (let round = 1; round <= limit.maxRounds; round++) {
    console.log(`  ↪ CrabPT 自动登录第 ${round}/${limit.maxRounds} 轮...`);
    const loginPage = await withRetry(() => getPage(`${base}/login.php`));
    if (loginPage.error) {
      console.log(`  ⚠️ 登录页获取失败: ${loginPage.error}`);
      return null;
    }
    const page = parseLoginPage(loginPage.text);
    if (!page.imagehash) {
      console.log('  ⚠️ 登录页未解析到 imagehash，页面结构可能已变化');
      return null;
    }
    if (page.remainAttempts != null && page.remainAttempts < limit.safeFloor) {
      console.log(`  ❌ 剩余尝试机会仅 ${page.remainAttempts} 次（低于安全阈值 ${limit.safeFloor}），放弃登录防止封 IP`);
      return null;
    }

    const captcha = await fetchAndRecognizeCaptcha(base, page.imagehash, loginPage.setCookie.length ? mergeCookies('', loginPage.setCookie) : undefined);
    if (captcha.error) {
      console.log(`  ⚠️ ${captcha.error}`);
      return null;
    }

    // 表单含 secret/two_step_code 空字段（与浏览器提交一致；secret 登录页为空串）
    const form = new URLSearchParams({
      secret: page.secret || '',
      username,
      password,
      two_step_code: '',
      imagestring: captcha.code,
      imagehash: page.imagehash,
    });
    const loginCookie = mergeCookies('', loginPage.setCookie);
    const post = await postForm(`${base}/takelogin.php`, loginCookie || undefined, form.toString());

    const newCookie = mergeCookies(loginCookie, post.setCookie);
    const hasLoginCookie = /c_secure_pass=|(?:^|;\s*)uid=\d+/.test(newCookie);
    const redirectedOut = post.location && !/login\.php|takelogin\.php/i.test(post.location);
    if (post.httpStatus === 302 || post.httpStatus === 303 || redirectedOut || hasLoginCookie) {
      if (newCookie) {
        console.log('  ✅ CrabPT 自动登录成功');
        return newCookie;
      }
    }
    dbg(`登录失败详情: httpStatus=${post.httpStatus} location=${post.location} cookie=${newCookie.substring(0, 80)} body=${(post.text || '').substring(0, 300).replace(/\s+/g, ' ')}`);
    console.log(`  ⚠️ 第 ${round} 轮登录未成功（验证码错误或判定未命中），换新验证码重试`);
    await sleep(rand(3000, 6000));
  }
  console.log(`  ❌ CrabPT 自动登录 ${limit.maxRounds} 轮均失败，放弃（保护站点尝试次数）`);
  return null;
}

async function processCrabpt() {
  const siteKey = 'crabpt';
  let cookie = resolveCookie(siteKey, 'PT_SITE_CRABPT_CK');
  const { username, password } = resolveAccount('PT_CRABPT_ACCOUNTS', 'PT_CRABPT_USERNAME', 'PT_CRABPT_PASSWORD');

  if (!cookie && !(username && password)) {
    return { status: 'skipped', msg: '未配置 Cookie 与账密，跳过' };
  }

  const base = 'https://crabpt.vip';
  // 该站 attendance.php GET 即触发签到，故先回首页判登录态与已签态，已签日不再请求签到端点
  let indexPage = await withRetry(() => getPage(`${base}/index.php`, cookie || undefined));
  if (indexPage.error) return { status: 'network_err', msg: `访问首页失败: ${indexPage.error}` };

  let indexInfo = parseCrabptAttendance(indexPage.text);
  const cookieDead = indexInfo.isLoginPage || /login\.php/i.test(indexPage.finalUrl || '');
  if (cookieDead) {
    if (!(username && password)) {
      return { status: 'cookie_dead', msg: 'Cookie 已失效且未配置账密，请重新导出 Cookie' };
    }
    console.log('  ↪ Cookie 已失效，尝试自动登录兜底...');
    cookie = await loginCrabpt(username, password);
    if (!cookie) return { status: 'cookie_dead', msg: 'Cookie 失效且自动登录未成功，请检查账密/OCR 配置' };
    saveCookie(siteKey, cookie);
    indexPage = await withRetry(() => getPage(`${base}/index.php`, cookie));
    if (indexPage.error) return { status: 'network_err', msg: `重新访问首页失败: ${indexPage.error}` };
    indexInfo = parseCrabptAttendance(indexPage.text);
    if (indexInfo.isLoginPage || /login\.php/i.test(indexPage.finalUrl || '')) {
      return { status: 'cookie_dead', msg: '自动登录后仍判定未登录，账号可能有异常' };
    }
  }

  // 首页导航「签到已得N」为已签标记（实测确认）
  if (indexInfo.isAlreadySigned) {
    return { status: 'already', msg: '今日已签到（首页显示签到已得）' };
  }

  // 未签 → GET attendance.php 触发签到（无表单无验证码）
  const sign = await withRetry(() => getPage(`${base}/attendance.php`, cookie, { Referer: `${base}/index.php` }));
  if (sign.error) return { status: 'network_err', msg: `签到请求失败: ${sign.error}` };
  const signInfo = parseCrabptAttendance(sign.text);
  if (signInfo.hasSignSuccess && (signInfo.reward || signInfo.totalSignCount)) {
    let detail = '';
    if (signInfo.totalSignCount) detail += `\n  📊 第 ${signInfo.totalSignCount} 次签到`;
    if (signInfo.continuousDays) detail += `\n  🎯 连续签到 ${signInfo.continuousDays} 天`;
    if (signInfo.reward) detail += `\n  🎁 获得 ${signInfo.reward} 蟹币值`;
    if (signInfo.rank) detail += `\n  🏁 今日签到排名 ${signInfo.rank}`;
    return { status: 'success', msg: `签到成功${detail.replace(/\n/g, '')}`, detail };
  }

  // 签到响应未确认 → 回读首页兜底（签前首页无已签标记，签后出现即成功）
  await sleep(1000);
  const refresh = await withRetry(() => getPage(`${base}/index.php`, cookie));
  const refreshInfo = parseCrabptAttendance(refresh.text || '');
  if (!refresh.error && refreshInfo.isAlreadySigned) {
    return { status: 'success', msg: '签到成功（首页已显示签到已得）' };
  }
  return { status: 'parse_err', msg: '签到请求已发出但未能确认结果，请开 PT_DEBUG 查看页面' };
}

// ==========================================
// 11. 状态 → 标记 / 通知文案
// ==========================================
const STATUS_FLAG = {
  success: '✅',
  already: '✅',
  skipped: '⏭️',
  cookie_dead: '❌',
  network_err: '❌',
  parse_err: '⚠️',
};

const STATUS_LABEL = {
  success: '签到成功',
  already: '已签到',
  skipped: '跳过',
  cookie_dead: 'Cookie 失效',
  network_err: '网络异常',
  parse_err: '结果未知',
};

function resultText(result) {
  const flag = STATUS_FLAG[result.status] || '⚠️';
  const label = STATUS_LABEL[result.status] || '未知';
  return `${flag} ${label}：${result.msg || ''}`;
}

// ==========================================
// 12. 主入口
// ==========================================
async function main() {
  console.log('=== PT 站点自动签到开始 ===');
  console.log(`UA: ${UA.substring(0, 60)}...`);
  if (PROXY) {
    console.log(PROXY_SOURCE === 'PT_PROXY'
      ? `代理: ${PROXY}`
      : `代理: ${PROXY}（来源: ${PROXY_SOURCE}）`);
  } else {
    console.log('代理: 未配置（直连）');
  }

  const tasks = [
    { name: 'NovaHD', fn: processNovahd },
    { name: 'HDArea', fn: processHdarea },
    { name: 'BTSchool', fn: processBtschool },
    { name: 'CrabPT', fn: processCrabpt },
  ];

  const results = [];
  for (const task of tasks) {
    console.log(`\n--- ${task.name} ---`);
    let r;
    try {
      r = await task.fn();
    } catch (err) {
      r = { status: 'parse_err', msg: `执行异常: ${err.message}` };
    }
    console.log(`${task.name} 结果: ${resultText(r)}`);
    results.push({ name: task.name, result: r });

    // 站点间随机间隔，降低风控
    const gap = rand(3000, 8000);
    dbg(`站点间隔 ${gap}ms`);
    await sleep(gap);
  }

  // ---- 分档判定（skipped 不计入成败） ----
  const counted = results.filter((r) => r.result.status !== 'skipped');
  const successCount = counted.filter((r) => ['success', 'already'].includes(r.result.status)).length;
  const failCount = counted.length - successCount;

  let title;
  if (counted.length === 0) {
    title = '⏭️ PT 签到未配置任何站点';
  } else if (failCount === 0) {
    title = `✅ PT 签到全部成功（${successCount}/${counted.length}）`;
  } else if (successCount === 0) {
    title = `❌ PT 签到全部失败（0/${counted.length}）`;
  } else {
    title = `⚠️ PT 签到部分失败（成功 ${successCount} / 失败 ${failCount}）`;
  }

  // ---- 通知正文（markdown 友好） ----
  let desp = `**统计**：成功 ${successCount} / 失败 ${failCount} / 共 ${counted.length}（另有 ${results.length - counted.length} 站跳过）\n\n`;
  desp += '**明细**：\n\n';
  for (const r of results) {
    desp += `- **[${r.name}]** ${resultText(r.result).replace(/\n/g, ' ')}\n`;
  }
  desp += `\n---\n*时间：${new Date().toLocaleString('zh-CN', { hour12: false })}*`;

  console.log('\n=== 签到任务执行完毕 ===');
  console.log(`标题: ${title}`);

  // 失败才推送 / 全部推送
  if (NOTIFY_ONLY_FAIL && failCount === 0) {
    console.log('全部成功且已开启 PT_NOTIFY_ONLY_FAIL，跳过推送');
  } else {
    try {
      await sendNotify(title, desp);
      dbg('推送完成');
    } catch (err) {
      console.log(`⚠️ 推送异常: ${err.message}`);
    }
  }

  // 有效站点全失败才以非 0 退出（青龙红色标记）；未配置任何站点视为配置错误
  if (counted.length === 0 || successCount === 0) {
    process.exit(1);
  }
}

// 导出纯函数供单元测试使用（青龙直接运行不受影响）
module.exports = {
  parseLoginPage,
  cleanOcrText,
  novahdChallengeResponse,
  requestNovahdChallenge,
  novahdSignOnce,
  parseNovahdAttendance,
  parseHdareaAttendance,
  parseCrabptAttendance,
  isCookieDead,
  mergeCookies,
  setCookiesToCookieString,
  readCachedCookie,
  saveCookie,
  resolveCookie,
};

if (require.main === module) {
  main().catch((err) => {
    console.error('主流程异常:', err);
    process.exit(1);
  });
}

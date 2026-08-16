/**
 * @name iKuuu 自动签到
 * @description 青龙面板自动签到脚本（ikuuu 机场），动态域名 + 多账号 + 状态分档 + 失败才推送
 * @cron 8 8 * * *
 *
 * 环境变量：
 *   ACCOUNTS                 【必填】账号列表，支持三种格式：
 *                              1) JSON 数组: [{"name":"主号","cookie":"..."},{"name":"副号","cookie":"..."}]
 *                              2) JSON 单对象: {"name":"主号","cookie":"..."}
 *                              3) 原始 cookie 字符串（自动当作"默认账号"）
 *   HOST                     【可选】强制锁定签到域名，留空则自动从发布页抓取
 *   IKUUU_PROXY              【可选】HTTP 代理（ikuuu 被墙必备），如 http://172.17.0.1:7890
 *   IKUUU_NOTIFY_ONLY_FAIL   【可选】1 = 仅失败时推送，0/留空 = 全部推送
 *   IKUUU_DEBUG              【可选】1 = 打印详细调试信息（域名抓取/响应原文/重试等）
 *
 * 推送：复用同目录 sendNotify.js（青龙官方 Notify），在青龙变量里配 DD_BOT_TOKEN 等即可。
 */

const crypto = require('crypto');
const path = require('path');
const { sendNotify } = require(path.join(__dirname, 'sendNotify'));
// undici（sendNotify.js 已依赖）用于代理支持：ikuuu 被墙，青龙容器需走代理
const { fetch: undiciFetch, ProxyAgent } = require('undici');

const UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36';

const DEBUG = process.env.IKUUU_DEBUG === '1';
const NOTIFY_ONLY_FAIL = process.env.IKUUU_NOTIFY_ONLY_FAIL === '1';
const TIMEOUT_MS = 15000;

// 代理支持（ikuuu 被墙，青龙容器需 IKUUU_PROXY=http://代理地址 才能直连签到域名）
const IKUUU_PROXY = (process.env.IKUUU_PROXY || '').trim();
const proxyAgent = IKUUU_PROXY ? new ProxyAgent(IKUUU_PROXY) : null;
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
// 1. 账号解析
// ==========================================
function normalizeAccounts(rawAccounts) {
  if (!rawAccounts) throw new Error('missing ACCOUNTS');
  const trimmed = rawAccounts.trim();
  if (!trimmed) throw new Error('empty ACCOUNTS');

  try {
    const parsed = JSON.parse(trimmed);
    if (Array.isArray(parsed)) return parsed;
    if (parsed && typeof parsed === 'object' && parsed.cookie) return [parsed];
  } catch (e) {
    dbg('ACCOUNTS 非合法 JSON，按原始 cookie 字符串处理');
  }

  return [{ name: '默认账号', cookie: trimmed }];
}

// ==========================================
// 2. 动态获取最新签到主域名
// ==========================================
async function fetchHostFromProxy() {
  // 通过 allorigins 公共代理绕过网络墙抓发布页
  const targetUrl = encodeURIComponent('https://ikuuu.eu/');
  const proxyUrl = `https://api.allorigins.win/raw?url=${targetUrl}`;
  dbg('正则抓取发布页域名, 代理:', proxyUrl);

  const response = await fetchWithProxy(proxyUrl, {
    method: 'GET',
    headers: { 'User-Agent': UA },
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });

  const html = await response.text();
  dbg('发布页 HTML 长度:', html.length, '前80字符:', html.substring(0, 80).replace(/\s+/g, ' '));

  if (!html) return null;

  // 收紧匹配：优先带 https 的链接里出现的 ikuuu.xxx 域名，避免抓到脚注释里的兜底域名
  // 例: https://ikuuu.win  或 href="https://ikuuu.ac" target="_blank"
  const candidates = [];
  const linkRe = /https?:\/\/(ikuuu\.[a-z]+)/gi;
  let m;
  while ((m = linkRe.exec(html)) !== null) {
    candidates.push(m[1].toLowerCase());
  }
  // 兜底：若无带协议的，退回纯域名匹配
  if (candidates.length === 0) {
    const fallback = html.match(/ikuuu\.([a-z]+)/i);
    if (fallback) candidates.push(fallback[0].toLowerCase());
  }

  // 去掉"发布页自身"域名 ikuuu.eu，返回去重后的候选列表（可能为空）
  return [...new Set(candidates.filter((c) => c !== 'ikuuu.eu'))];
}

async function getLatestHosts() {
  // 1. 环境变量强制锁定优先（trim 掉意外尾随空格，避免拼出无效 URL）
  const forcedHost = (process.env.HOST || '').trim();
  if (forcedHost) {
    console.log(`[域名加载] 检测到环境变量 HOST, 使用强制域名: ${forcedHost}`);
    return [forcedHost];
  }

  console.log('[域名加载] 尝试从发布页 https://ikuuu.eu/ 获取最新主域名...');
  let candidates = [];
  try {
    candidates = await fetchHostFromProxy();
    if (candidates.length > 0) {
      console.log(`[域名加载] 🎉 成功获取当前候选域名: ${candidates.join(', ')}`);
    } else {
      console.log('[域名加载] ⚠️ 发布页解析失败，未匹配到有效域名');
    }
  } catch (err) {
    console.log(`[域名加载] ❌ 获取动态域名异常: ${err.message}`);
  }

  // 兜底域名始终追加（发布页已改混淆 JS 渲染，静态抓取常失败，兜底保证可用）
  const fallbackHost = 'ikuuu.win';
  if (!candidates.includes(fallbackHost)) {
    candidates.push(fallbackHost);
  }
  console.log(`[域名加载] 最终域名列表: ${candidates.join(', ')}`);
  return candidates;
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
function classify(text, httpStatus) {
  dbg('签到响应 httpStatus=', httpStatus, '原文前120字:', text.substring(0, 120).replace(/\s+/g, ' '));

  // ★ HTTP 状态码是强信号，优先分类（网关错误页/风控页可能不是 HTML 头，正文解析会漏）
  if (httpStatus === 401) {
    return { status: 'cookie_dead', msg: 'HTTP 401 未授权，Cookie 已失效' };
  }
  if (httpStatus === 403) {
    return { status: 'domain_block', msg: 'HTTP 403 被拒绝（疑似风控或域名被墙）' };
  }
  if (httpStatus === 404) {
    return { status: 'domain_block', msg: 'HTTP 404 接口路径不存在（域名或路径变化）' };
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
  if (/已签到|重复|already|signed|今日已|明天再来/.test(raw)) {
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

async function checkInOnce(account, host) {
  const checkInUrl = `https://${host}/user/checkin`;
  dbg('请求签到:', checkInUrl, '账号', maskName(account.name));

  const response = await fetchWithProxy(checkInUrl, {
    method: 'POST',
    headers: {
      Cookie: account.cookie,
      'User-Agent': UA,
      Accept: 'application/json, text/plain, */*',
      Referer: `https://${host}/user`,
    },
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });

  const text = await response.text();
  return classify(text, response.status);
}

// 带重试的签到
async function checkIn(account, host) {
  const maxRetry = 2;
  let lastResult;
  for (let i = 0; i <= maxRetry; i++) {
    try {
      lastResult = await checkInOnce(account, host);
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
  domain_block: '❌',
  parse_err: '⚠️',
  network_err: '❌',
};

const STATUS_LABEL = {
  success: '成功',
  already: '已签到',
  cookie_dead: 'Cookie失效',
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
async function main() {
  console.log('=== iKuuu 青龙自动签到开始 ===\n');

  if (!process.env.ACCOUNTS) {
    console.error('❌ 未配置 ACCOUNTS 环境变量');
    process.exit(1);
  }

  let accounts;
  try {
    accounts = normalizeAccounts(process.env.ACCOUNTS);
  } catch (err) {
    console.error(`❌ ACCOUNTS 解析失败: ${err.message}`);
    process.exit(1);
  }

  console.log(`共解析到 ${accounts.length} 个账号, 将串行签到\n`);

  // 动态域名列表（依次尝试：域名被墙/网络异常时自动轮换下一个）
  const targetHosts = await getLatestHosts();
  console.log(`\n▶ 本次签到域名列表: ${targetHosts.join(', ')}\n`);

  const results = [];
  let usedHost = targetHosts[0];

  for (let i = 0; i < accounts.length; i++) {
    const acc = accounts[i];
    const masked = maskName(acc.name);
    console.log(`[${i + 1}/${accounts.length}] 账号 [${masked}] 签到中...`);

    // 每个账号依次尝试候选域名；仅域名/网络类问题才轮换，业务结果直接采用
    let result = null;
    for (const host of targetHosts) {
      const r = await checkIn(acc, host);
      if (r.status === 'domain_block' || r.status === 'network_err') {
        console.log(`  ↪ ${host} 返回 ${r.status}（${r.msg}），尝试下一个域名...`);
        result = r;
        continue;
      }
      usedHost = host;
      result = r;
      break;
    }

    const line = resultText(result);
    console.log(`账号 [${masked}] 结果: ${line}\n`);

    results.push({ name: masked, result, line });

    // 多账号间随机间隔，降低风控
    if (i < accounts.length - 1) {
      const gap = rand(3000, 8000);
      dbg(`账号间隔 ${gap}ms`);
      await sleep(gap);
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
  if (successCount === 0) {
    process.exit(1);
  }
}

// 导出纯函数供单元测试使用（青龙直接运行不受影响）
module.exports = { classify, normalizeAccounts, maskName };

if (require.main === module) {
  main().catch((err) => {
    console.error('主流程异常:', err);
    process.exit(1);
  });
}
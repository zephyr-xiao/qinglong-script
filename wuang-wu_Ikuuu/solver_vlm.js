#!/usr/bin/env node
/**
 * iKuuu 点选验证码解法器 · 视觉大模型版
 *
 * 由 ikuuu.js 通过 IKUUU_SOLVER_CMD 调用（协议见 README「点选验证码与解法器」）：
 *   stdin  : {"type","imagePath","tplPaths":[...],"imageSize":{w,h},"displaySize":{w,h}}
 *   stdout : {"ok":true,"clicks":[[x,y],...]} 或 {"ok":false,"msg":"..."}
 *
 * 做法：把挑战图放大并叠加像素坐标网格、下方拼上带编号的待点图案 → 交给视觉大模型
 *       → 先枚举图中所有字/图案（定位），再做对应（识别）→ 取多次调用中位数投票。
 *
 * 自检：
 *   node solver_vlm.js --check        检查环境变量 / playwright+chromium / 模型视觉通道，打印 ✅/❌ 清单
 *
 * 环境变量：
 *   IKUUU_VLM_BASE_URL  必填，OpenAI 兼容网关地址（如 http://host:3000/v1）
 *   IKUUU_VLM_API_KEY   必填
 *   IKUUU_VLM_MODEL     必填，视觉模型名；可用逗号给多个做兜底（前一个不可用自动换下一个）
 *   IKUUU_VLM_TIMEOUT   单次请求超时秒数，默认 120
 *   IKUUU_VLM_VOTES     投票次数，默认 2（1 = 不投票）
 *   IKUUU_VLM_ZOOM      合成图放大倍数，默认 3
 *   IKUUU_VLM_MAX_TOKENS 单次回复上限，默认 8000
 *   IKUUU_VLM_DEBUG     1 = 把合成图与模型原始回复写到本目录 _vlm_debug/
 */
const fs = require('fs');
const path = require('path');

const BASE_URL = (process.env.IKUUU_VLM_BASE_URL || '').trim().replace(/\/+$/, '');
const API_KEY = (process.env.IKUUU_VLM_API_KEY || '').trim();
// 模型名可用逗号分隔给多个：当前模型"不可用"（认证/限流/不存在/网络）时自动改用下一个。
// 备用模型不一定要很准 —— 投票 + 服务端校验会把错答案挡掉，聊胜于无。
const MODELS = (process.env.IKUUU_VLM_MODEL || '')
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean);
const TIMEOUT_MS = Math.max(10, Number(process.env.IKUUU_VLM_TIMEOUT || 120)) * 1000;
const VOTES = Math.max(1, Number(process.env.IKUUU_VLM_VOTES || 2));
const ZOOM = Math.min(6, Math.max(1, Number(process.env.IKUUU_VLM_ZOOM || 3)));
const DEBUG = process.env.IKUUU_VLM_DEBUG === '1';
// 网关报错里出现这些字样时，说明是"模型/渠道级"问题：重试无用，直接换下一个模型。
// 不同网关措辞不同，可用环境变量覆盖（逗号分隔）或置空关闭该判定。
const MODEL_ERROR_HINT = (process.env.IKUUU_VLM_MODEL_ERROR_HINT === undefined
  ? 'model_not_found,No available channel,invalid_request_error'
  : process.env.IKUUU_VLM_MODEL_ERROR_HINT)
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean);

// 日志一律走 stderr，stdout 只留最终 JSON（主脚本按 stdout 取结果）
const log = (...a) => console.error('[solver-vlm]', ...a);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 从模型回复里抠出 JSON（可能带 ```json 围栏或前后解释）
function extractJson(text) {
  if (!text) return null;
  const cleaned = String(text).replace(/```json/gi, '```').replace(/```/g, '');
  const start = cleaned.indexOf('{');
  const end = cleaned.lastIndexOf('}');
  if (start < 0 || end <= start) return null;
  try {
    return JSON.parse(cleaned.slice(start, end + 1));
  } catch (e) {
    return null;
  }
}

// 两种提示词：多图案形式（word/icon）与九宫格形式（nine）
// 推理模型会把思路也写进 token，故用一行"简洁推理"指令压住思考长度（否则容易撞 max_tokens 被截断）
const BRIEF = '请用最简短的思路直接得出结论（推理不超过 6 行，不要复述题目）。';

// 从站点提示语里取"要选几个"（如「选 3 个符合右图的图片」「请选出2个相同的」）；取不到返回 0
function wantedCount(promptText) {
  const m = /选[出取]?\s*(\d+)\s*个/.exec(promptText || '');
  return m ? Number(m[1]) : 0;
}

function buildPrompt(type, tplCount, size, promptText) {
  const head = `上图是验证图（宽 ${size.w} 像素、高 ${size.h} 像素；红色网格线每 25 像素一条，边缘标注的数字就是像素坐标，`
    + '左上角为 (0,0)，x 向右增大，y 向下增大）。';
  const site = promptText ? `站点给出的提示语是「${promptText}」。\n` : '';
  if (type === 'nine') {
    const want = wantedCount(promptText);
    const pick = want > 0
      ? `第 2 步：从匹配的格子中按与待找图案的相似度挑出 **${want} 个**（即使有更多看着相似的，也只给最像的这几个），给出它们的中心像素坐标（整图坐标，x 范围 0-${size.w}，y 范围 0-${size.h}）。\n`
      : `第 2 步：给出所有匹配格子的中心像素坐标（整图坐标，x 范围 0-${size.w}，y 范围 0-${size.h}）。\n`;
    return `${head}\n图下方是待找图案 [1]。${site}`
      + '验证图被均分为 3×3 九个格子（横为列 i=0..2 从左到右，纵为行 j=0..2 从上到下）。\n'
      + '请分两步完成：\n'
      + '第 1 步：逐格检查九个格子，指出每一格的内容以及是否与待找图案相同（注意区分相似但不同的物品）；\n'
      + pick
      + `${BRIEF}\n`
      + '只输出 JSON（不要解释）：{"cells": [{"i": 0, "j": 0, "match": true, "x": 0, "y": 0}], "clicks": [[x1,y1],...]}';
  }
  const nums = Array.from({ length: tplCount }, (_, i) => `[${i + 1}]`).join('');
  return `${head}\n图下方是要在验证图里找的${type === 'icon' ? '图标（线条图形，颜色可能与图中不一致）' : '汉字'}，编号 ${nums}，需要按该编号顺序点击。\n`
    + `${site}请分两步完成：\n`
    + `第 1 步：通读验证图，找出图中**所有**与待点图案同类的图形（含形状相近的干扰项），写出每个的中心像素坐标；\n`
    + `第 2 步：把图案 ${nums} 与第 1 步找到的位置一一对应（形状必须完全一致，不要选成相似但不同的干扰项），给出它们的中心像素坐标。\n`
    + `${BRIEF}\n`
    + `只输出 JSON（不要解释）：{"all": [{"x": 0, "y": 0}], "clicks": [[x1,y1],${tplCount > 1 ? '...,' : ''}[x${tplCount},y${tplCount}]]}`;
}

// 用 headless Chromium 渲染合成图（复用 playwright：挑战图是 JPEG，浏览器解码/编码最省事）
async function composeImage({ type, imagePath, tplPaths, imageSize }) {
  const { chromium } = require('playwright');
  const dataUrl = (file) => {
    const buf = fs.readFileSync(file);
    const mime = /\.png$/i.test(file) ? 'image/png' : 'image/jpeg';
    return `data:${mime};base64,${buf.toString('base64')}`;
  };
  const tpl = (f) => {
    const buf = fs.readFileSync(f);
    const mime = /\.png$/i.test(f) ? 'image/png' : 'image/jpeg';
    return `data:${mime};base64,${buf.toString('base64')}`;
  };
  const w = imageSize.w * ZOOM;
  const h = imageSize.h * ZOOM;
  const html = `<!doctype html><html><head><meta charset="utf-8"><style>
    body{margin:0;background:#fff;font:12px/1.5 "Microsoft YaHei",sans-serif;color:#222}
    #wrap{padding:6px 8px}
    #stage{position:relative;display:inline-block;border:1px solid #999}
    #bg{display:block;width:${w}px;height:${h}px}
    #grid{position:absolute;left:0;top:0}
    .tplbox{display:flex;gap:20px;align-items:flex-end;margin-top:4px}
    .tpl{display:flex;flex-direction:column;align-items:center}
    .tpl img{height:${Math.round(110 * ZOOM / 3)}px;image-rendering:auto}
    .tpl b{font-size:${Math.round(16 * ZOOM / 3)}px;color:#c00}
  </style></head><body><div id="wrap">
    <div>验证图（网格刻度 = 像素坐标，左上角 0,0）</div>
    <div id="stage">
      <img id="bg" src="${dataUrl(imagePath)}">
      <canvas id="grid" width="${w}" height="${h}"></canvas>
    </div>
    <div style="margin-top:6px">待点图案${type === 'nine' ? '（找出所有包含它的格子）' : '（按编号顺序点击）'}：</div>
    <div class="tplbox">${tplPaths.map((f, i) => `<div class="tpl"><b>[${i + 1}]</b><img src="${tpl(f)}"></div>`).join('')}</div>
  </div></body></html>`;

  const browser = await chromium.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
  });
  try {
    const page = await browser.newPage({ viewport: { width: Math.max(w + 40, 420), height: h + 260 } });
    await page.setContent(html, { waitUntil: 'load', timeout: 20000 });
    // 画网格（线在放大尺度上，标注用原始像素坐标）
    await page.evaluate(({ imageSize, zoom }) => {
      const cv = document.getElementById('grid');
      const g = cv.getContext('2d');
      g.font = `${Math.round(11 * zoom / 3)}px sans-serif`;
      for (let x = 0; x <= imageSize.w; x += 25) {
        g.strokeStyle = x % 100 === 0 ? 'rgba(255,40,40,0.85)' : 'rgba(255,120,120,0.5)';
        g.beginPath(); g.moveTo(x * zoom + 0.5, 0); g.lineTo(x * zoom + 0.5, imageSize.h * zoom); g.stroke();
        if (x % 50 === 0) {
          g.fillStyle = 'rgba(255,255,0,0.95)';
          g.fillText(String(x), x * zoom + 2, 12 * zoom / 3 + 2);
        }
      }
      for (let y = 0; y <= imageSize.h; y += 25) {
        g.strokeStyle = y % 100 === 0 ? 'rgba(255,40,40,0.85)' : 'rgba(255,120,120,0.5)';
        g.beginPath(); g.moveTo(0, y * zoom + 0.5); g.lineTo(imageSize.w * zoom, y * zoom + 0.5); g.stroke();
        if (y % 50 === 0) {
          g.fillStyle = 'rgba(255,255,0,0.95)';
          g.fillText(String(y), 2, y * zoom + 12 * zoom / 3 + 2);
        }
      }
    }, { imageSize, zoom: ZOOM });
    const el = await page.$('#wrap');
    return await el.screenshot({ type: 'png' });
  } finally {
    await browser.close().catch(() => {});
  }
}

// token 上限：默认 50000（新 API 类网关上限 65536，实测 50000 可用、100000 被拒）
// 截断重试会按 factor 放大，但**必须夹在上限内**，否则请求直接被判非法
const MAX_TOKENS_CEILING = 60000;
function computeMaxTokens(baseTokens, factor = 1) {
  return Math.min(Math.max(1000, baseTokens) * Math.max(1, factor), MAX_TOKENS_CEILING);
}

// 解析模型响应（纯函数，便于单测）：
//   正常 → { ok:true, data }
//   思考吃满 token 且正文为空 → 先尝试从 reasoning 里捞答案；捞不到则 { ok:false, truncated:true }
//   正文有内容但不是 JSON → { ok:false, truncated:false }
function parseModelReply(data) {
  const msg = (data && data.choices && data.choices[0] && data.choices[0].message) || {};
  const content = msg.content || '';
  const reasoning = msg.reasoning || '';
  const finish = data && data.choices && data.choices[0] && data.choices[0].finish_reason;
  if (!content.trim()) {
    const salvaged = extractJson(reasoning);
    if (salvaged && Array.isArray(salvaged.clicks) && salvaged.clicks.length) {
      return { ok: true, data: salvaged, salvaged: true, finish };
    }
    return { ok: false, truncated: finish === 'length', finish, msg: `模型无正文输出（finish_reason=${finish}，可能 token 不足）` };
  }
  const parsed = extractJson(content);
  if (!parsed) {
    return { ok: false, truncated: false, finish, msg: `模型输出无法解析为 JSON: ${content.substring(0, 160)}` };
  }
  return { ok: true, data: parsed, finish };
}

// 调单个模型一次（失败抛异常）；网关有限速（429 tpm/rpm），内部退避重试
// tokenFactor：token 上限倍数（思考被截断时上层会按双倍重试一次）
// hardPrompt：附加在提示词末尾的强制指令（截断重试时用）
async function callModelOnce(model, imageBuffer, prompt, attempt = 1, tokenFactor = 1, hardPrompt = '') {
  const baseTokens = Math.max(1000, Number(process.env.IKUUU_VLM_MAX_TOKENS || 50000));
  const body = {
    model,
    temperature: 0,
    // 推理模型：思考过程也占 token，给小了会在思考中途被截断（finish_reason=length）
    max_tokens: computeMaxTokens(baseTokens, tokenFactor),
    messages: [{
      role: 'user',
      content: [
        { type: 'text', text: prompt + hardPrompt },
        { type: 'image_url', image_url: { url: `data:image/png;base64,${imageBuffer.toString('base64')}` } },
      ],
    }],
  };
  let lastErr = null;
  for (let i = 0; i < 3; i++) {
    try {
      const resp = await fetch(`${BASE_URL}/chat/completions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${API_KEY}` },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(TIMEOUT_MS),
      });
      const text = await resp.text();
      if (!resp.ok) {
        let msg = `HTTP ${resp.status}: ${text.substring(0, 160)}`;
        // max_tokens 超网关上限会被判非法请求：这是配置问题，直接告诉用户怎么修
        if (/MaxTokens invalid|max_tokens/i.test(text)) {
          msg += '（提示：IKUUU_VLM_MAX_TOKENS 超出网关上限，按报错里的区间调小）';
        }
        const err = new Error(msg);
        // "模型不存在/无可用渠道"这类问题在本模型上重试无用（跳过退避，直接交给上层换模型）；
        // 400 属于请求非法（配置错），同样不重试
        err.modelLevel = resp.status === 400 || MODEL_ERROR_HINT.some((h) => text.includes(h));
        // 限速/网关繁忙才在同一模型上退避重试
        if ((resp.status === 429 || resp.status >= 500) && !err.modelLevel) {
          lastErr = err;
          const wait = [4000, 12000][i];
          if (wait) {
            log(`模型 ${model} 第 ${i + 1} 次请求失败（${resp.status}），${wait / 1000}s 后重试`);
            await sleep(wait);
            continue;
          }
        }
        throw err;
      }
      let data;
      try {
        data = JSON.parse(text);
      } catch (e) {
        throw new Error(`响应非 JSON: ${text.substring(0, 120)}`);
      }
      const msg = (data.choices && data.choices[0] && data.choices[0].message) || {};
      const verdict = parseModelReply(data);
      const finish = verdict.finish;
      const reasoning = msg.reasoning || '';
      if (DEBUG) {
        const dir = path.join(__dirname, '_vlm_debug');
        fs.mkdirSync(dir, { recursive: true });
        fs.writeFileSync(path.join(dir, `${Date.now()}_attempt${attempt}.txt`),
          `model=${model}\nmax_tokens=${body.max_tokens}\nfinish_reason=${finish}\n`
          + `verdict=${verdict.ok ? 'ok' : 'fail'}${verdict.salvaged ? '(从思考里捞回)' : ''}\n`
          + `content:\n${msg.content || ''}\n\nreasoning:\n${reasoning.substring(0, 4000)}`, 'utf8');
      }
      if (verdict.ok) {
        if (verdict.salvaged) log(`模型 ${model} 正文为空（finish_reason=${finish}），已从思考内容里取回答案`);
        return verdict.data;
      }
      const err = new Error(verdict.msg);
      err.truncated = verdict.truncated === true;
      throw err;
    } catch (e) {
      lastErr = e;
      // 模型级故障不重试（已标记），直接抛给上层换模型
      if (i < 2 && !e.modelLevel && /429|HTTP 5\d\d|fetch failed|timeout|aborted/i.test(String(e.message))) {
        const wait = [4000, 12000][i];
        log(`请求异常（${String(e.message).substring(0, 60)}），${wait / 1000}s 后重试`);
        await sleep(wait);
        continue;
      }
      throw e;
    }
  }
  throw lastErr || new Error(`模型 ${model} 请求失败`);
}

// 模型列表兜底：当前模型"不可用"（认证/限流/不存在/网络）时改用下一个模型；
// 内容类失败（答非所问）不换模型 —— 那是模型能力问题，交给投票/换题
// 特例：思考被 token 截断（truncated）先按双倍上限 + 强制指令重试一次同一模型，通常就能过
async function callModel(imageBuffer, prompt, attempt = 1) {
  let lastErr = null;
  for (let i = 0; i < MODELS.length; i++) {
    try {
      return await callModelOnce(MODELS[i], imageBuffer, prompt, attempt);
    } catch (e) {
      lastErr = e;
      if (e.truncated) {
        try {
          log(`模型 ${MODELS[i]} 思考被截断，按双倍 token 上限重试一次`);
          return await callModelOnce(MODELS[i], imageBuffer, prompt, attempt, 2,
            '\n\n（重要：上一次因为思考过长被截断。这次请**不要长篇推理**，读完图直接输出那个 JSON，不要任何解释。）');
        } catch (e2) {
          lastErr = e2;
        }
      }
      const hasNext = i < MODELS.length - 1;
      const unavailable = /HTTP (400|401|403|404|429|5\d\d)|fetch failed|timeout|aborted/i.test(String(lastErr.message));
      if (hasNext && unavailable) {
        log(`模型 ${MODELS[i]} 不可用（${String(lastErr.message).substring(0, 70)}），改用 ${MODELS[i + 1]}`);
        continue;
      }
      throw lastErr;
    }
  }
  throw lastErr || new Error('视觉模型请求失败');
}

// 从模型输出里取点击数组（{clicks:[[x,y],...]}）
function pickClicks(parsed, size) {
  const arr = Array.isArray(parsed.clicks) ? parsed.clicks : null;
  if (!arr) return null;
  const ok = (n, max) => Number.isFinite(n) && n >= 0 && n <= max;
  const clicks = arr
    .filter((c) => Array.isArray(c) && c.length === 2 && ok(Number(c[0]), size.w) && ok(Number(c[1]), size.h))
    .map((c) => [Number(c[0]), Number(c[1])]);
  return clicks.length ? clicks : null;
}

// 多轮取中位数；返回 { ok, clicks?, msg? }
function vote(votesClicks) {
  const counts = new Set(votesClicks.map((v) => v.length));
  if (counts.size !== 1) {
    return { ok: false, msg: `各次调用给出的点数不一致（${votesClicks.map((v) => v.length).join('/')}）` };
  }
  const n = votesClicks[0].length;
  const out = [];
  let maxDev = 0;
  for (let i = 0; i < n; i++) {
    const xs = votesClicks.map((v) => v[i][0]).sort((a, b) => a - b);
    const ys = votesClicks.map((v) => v[i][1]).sort((a, b) => a - b);
    const mid = Math.floor(xs.length / 2);
    const mx = xs.length % 2 ? xs[mid] : (xs[mid - 1] + xs[mid]) / 2;
    const my = ys.length % 2 ? ys[mid] : (ys[mid - 1] + ys[mid]) / 2;
    for (const v of votesClicks) {
      maxDev = Math.max(maxDev, Math.hypot(v[i][0] - mx, v[i][1] - my));
    }
    out.push([mx, my]);
  }
  if (maxDev > 35) {
    return { ok: false, msg: `各次调用结果分歧过大（最大偏差 ${maxDev.toFixed(0)}px）` };
  }
  return { ok: true, clicks: out, spread: maxDev };
}

// ==========================================
// 自检：node solver_vlm.js --check
// 逐项检查环境变量 / playwright+chromium / 模型的视觉通道，打印 ✅/❌ 清单
// ==========================================
// 自检用的测试图：48x24，左半红、右半蓝（能答对"左边什么颜色"才说明视觉通道真的可用）
const PING_IMAGE_B64 =
  'iVBORw0KGgoAAAANSUhEUgAAADAAAAAYCAIAAAAzn+mLAAAAL0lEQVR4nO3OQQ0AAAgEIONcHPvPMJbwJxsBqElOpOdECQkJCQkJCQkJCQkJ/QotInegTIApt6UAAAAASUVORK5CYII=';

async function runCheck() {
  const ok = (m) => console.log(`[solver-vlm] ✅ ${m}`);
  const bad = (m) => console.log(`[solver-vlm] ❌ ${m}`);
  const hint = (m) => console.log(`[solver-vlm]    ↳ ${m}`);
  console.log('[solver-vlm] 自检模式：只检查配置与连通性，不会发起点选解题');
  let pass = true;

  // 1) 环境变量
  const miss = [];
  if (!API_KEY) miss.push('IKUUU_VLM_API_KEY');
  if (!BASE_URL) miss.push('IKUUU_VLM_BASE_URL');
  if (MODELS.length === 0) miss.push('IKUUU_VLM_MODEL');
  if (miss.length) {
    pass = false;
    bad(`环境变量缺失：${miss.join(' / ')}`);
    hint('在青龙「环境变量」里补上；模型名可用逗号给多个做兜底，如 IKUUU_VLM_MODEL=主模型,备用模型');
  } else {
    ok(`环境变量齐全：BASE_URL=${BASE_URL}  MODEL=${MODELS.join(' , ')}  API_KEY=已配置(${API_KEY.length} 字符)`);
  }

  // 2) playwright 与 chromium（合成挑战图要用）
  try {
    const { chromium } = require('playwright');
    ok('playwright 可加载');
    const exe = chromium.executablePath();
    if (exe && fs.existsSync(exe)) {
      ok(`chromium 已安装：${exe}`);
    } else {
      pass = false;
      bad(`chromium 未安装（期望路径：${exe || '未知'}）`);
      hint('在脚本目录执行：npx playwright install chromium（或设 IKUUU_CHROMIUM_PATH 指向系统 chromium）');
    }
  } catch (e) {
    pass = false;
    bad(`playwright 不可用：${String(e.message).split('\n')[0]}`);
    hint('在脚本目录执行：npm i && npx playwright install chromium');
  }

  // 3) 模型视觉通道（发一张左红右蓝的小图，要求答出左边颜色）
  if (BASE_URL && API_KEY && MODELS.length) {
    let anyOk = false;
    for (const model of MODELS) {
      try {
        const resp = await fetch(`${BASE_URL}/chat/completions`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${API_KEY}` },
          body: JSON.stringify({
            model,
            temperature: 0,
            max_tokens: 500,
            messages: [{
              role: 'user',
              content: [
                { type: 'text', text: '这张图左边一半是什么颜色？只答颜色，两个字。' },
                { type: 'image_url', image_url: { url: `data:image/png;base64,${PING_IMAGE_B64}` } },
              ],
            }],
          }),
          signal: AbortSignal.timeout(60000),
        });
        const text = await resp.text();
        if (!resp.ok) {
          bad(`模型 ${model} 调用失败：HTTP ${resp.status} ${text.substring(0, 120)}`);
          continue;
        }
        const data = JSON.parse(text);
        const answer = ((data.choices && data.choices[0] && data.choices[0].message) || {}).content || '';
        const okAnswer = /红/.test(answer);
        ok(`模型 ${model} 视觉通道可用（测试图应答："${String(answer).trim().substring(0, 20)}"${okAnswer ? '' : ' —— 答的不是"红色"，可能看图不稳，实测解题时投票会兜住'}）`);
        anyOk = true;
        break;
      } catch (e) {
        bad(`模型 ${model} 请求异常：${String(e.message).substring(0, 120)}`);
      }
    }
    if (!anyOk) {
      pass = false;
      hint('检查网关地址能否从本容器直连、API Key 是否有效、模型名是否正确（模型名列表里只要有一个可用即可）');
    }
  }

  console.log(pass
    ? '[solver-vlm] 检查通过，可以把本脚本作为 IKUUU_SOLVER_CMD 使用'
    : '[solver-vlm] 检查未通过，按上面 ❌ 的提示修完再跑一次');
  return pass;
}

// ==========================================
// 主流程（被 require 时不执行，便于单测纯函数）
// ==========================================
async function main() {
  // 自检模式：node solver_vlm.js --check（不解题，只检查环境变量/依赖/模型视觉通道）
  if (process.argv.includes('--check')) {
    process.exitCode = (await runCheck()) ? 0 : 1;
    return;
  }
  let input = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', (d) => { input += d; });
  process.stdin.on('end', async () => {
    let req;
    try {
      req = JSON.parse(input);
    } catch (e) {
      process.stdout.write(JSON.stringify({ ok: false, msg: '解法器入参不是合法 JSON' }));
      return;
    }
    const size = req.imageSize || { w: 300, h: 200 };
    const tplPaths = req.tplPaths || [];
    // 环境相关参数一律来自环境变量，不设内置默认值，缺了就明确报错
    const missing = [];
    if (!API_KEY) missing.push('IKUUU_VLM_API_KEY');
    if (!BASE_URL) missing.push('IKUUU_VLM_BASE_URL');
    if (MODELS.length === 0) missing.push('IKUUU_VLM_MODEL');
    if (missing.length) {
      process.stdout.write(JSON.stringify({ ok: false, msg: `未配置 ${missing.join(' / ')}` }));
      return;
    }
    if (!req.imagePath || !fs.existsSync(req.imagePath)) {
      process.stdout.write(JSON.stringify({ ok: false, msg: '挑战图不存在' }));
      return;
    }
    try {
      const composed = await composeImage({
        type: req.type, imagePath: req.imagePath, tplPaths, imageSize: size,
      });
      if (DEBUG) {
        const dir = path.join(__dirname, '_vlm_debug');
        fs.mkdirSync(dir, { recursive: true });
        fs.writeFileSync(path.join(dir, `${Date.now()}_${req.type || 'unknown'}.png`), composed);
      }
      const prompt = buildPrompt(req.type, Math.max(1, tplPaths.length), size, req.promptText);
      const results = [];
      for (let i = 0; i < VOTES; i++) {
        const parsed = await callModel(composed, prompt, i + 1);
        const clicks = pickClicks(parsed, size);
        if (!clicks) throw new Error(`第 ${i + 1} 次调用未给出有效坐标: ${JSON.stringify(parsed).substring(0, 160)}`);
        log(`第 ${i + 1}/${VOTES} 次: ${JSON.stringify(clicks)}`);
        results.push(clicks);
        if (i < VOTES - 1) await sleep(300);
      }
      const v = vote(results);
      if (!v.ok) {
        process.stdout.write(JSON.stringify({ ok: false, msg: v.msg }));
        return;
      }
      log(`最终坐标: ${JSON.stringify(v.clicks)}（各次最大偏差 ${v.spread.toFixed(0)}px）`);
      process.stdout.write(JSON.stringify({ ok: true, clicks: v.clicks }));
    } catch (e) {
      process.stdout.write(JSON.stringify({ ok: false, msg: `视觉模型解法失败: ${String(e.message || e).substring(0, 200)}` }));
    }
  });
}

if (require.main === module) {
  main().catch((e) => {
    process.stdout.write(JSON.stringify({ ok: false, msg: `解法器异常: ${String(e && e.message || e).substring(0, 200)}` }));
    process.exitCode = 1;
  });
}

// 导出纯函数供单元测试使用（青龙/主脚本直接运行时不受影响）
module.exports = {
  parseModelReply,
  extractJson,
  wantedCount,
  buildPrompt,
  computeMaxTokens,
};

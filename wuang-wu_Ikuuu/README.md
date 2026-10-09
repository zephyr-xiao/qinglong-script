# iKuuu 自动签到（Node.js）

青龙面板自动签到脚本，针对 ikuuu 机场（面板域名随发布页轮换，当前为 `ikuuu.top` / `ikuuu.pw`）。Node.js 实现，复用目录内 `sendNotify.js` 推送全通道。

> 改写自 [wuang-wu/Ikuuu](https://github.com/wuang-wu/Ikuuu)，按青龙面板规范适配。
> **2026-10 起支持账号密码自动登录**：Cookie 失效不再需要手工重新抓取，脚本自动开浏览器过验证码续期（验证码由解法器处理，见下文）。

## 快速上手（五步）

1. **装依赖**（青龙容器内，一次即可）

   ```bash
   docker exec -it qinglong bash
   cd /ql/data/scripts/wuang-wu_Ikuuu
   npm i && npx playwright install chromium
   ```

2. **放文件**：把本目录整个传进青龙的 `/ql/data/scripts/wuang-wu_Ikuuu/`
   （至少要有 `ikuuu.js`、`sendNotify.js`、`package.json`；要用视觉解法器再加 `solver_vlm.js`）

3. **配环境变量**（青龙「环境变量」）——最小可用集：

   ```text
   ACCOUNTS            = 邮箱#密码
   IKUUU_PROXY         = http://<代理>:7890      # ikuuu 被墙；不填则回退青龙全局代理
   IKUUU_SOLVER_CMD    = node solver_vlm.js      # 点选验证码解法器
   IKUUU_VLM_BASE_URL  = http://<网关>:3000/v1   # 下面三个是视觉解法器要的
   IKUUU_VLM_API_KEY   = sk-xxxxx
   IKUUU_VLM_MODEL     = <你的视觉模型>
   ```

   不想用视觉解法器也可以：只填 `IKUUU_COOKIE`（cookie 直填，每天手工更新一次），或看「点选验证码与解法器」的其它路线。

4. **自检**（确认解法器配好，输出全 ✅ 才算过）

   ```bash
   docker exec -it qinglong bash -lc 'cd /ql/data/scripts/wuang-wu_Ikuuu && \
   IKUUU_VLM_BASE_URL=http://<网关>:3000/v1 IKUUU_VLM_API_KEY=sk-xxx IKUUU_VLM_MODEL=<模型> \
   node solver_vlm.js --check'
   ```

5. **跑任务**：青龙「定时任务」里新建一条，命令填 `task wuang-wu_Ikuuu/ikuuu.js`，定时规则建议 `8 8 * * *`（随脚本头部 `@cron` 注解；会话 24 小时，每天跑一次即可）。想更抗挂可以再加一条 `8 20 * * *` 补签（已签到会被识别为成功，重跑无副作用）。

- **第一次**会做一次完整登录，约 1~4 分钟；之后 24 小时内直接复用 `.token/` 缓存，几秒跑完。
- 日志里应该依次出现：`🔐 无可用 Cookie 缓存，先走浏览器自动登录...` → `🌐 打开登录页...` → `🧩 站点下发点选验证码：文字点选(word)` → `🖱 点击图片坐标 (x,y)` → 最后 `账号 [xx] 结果: ✅ 成功/已签到`。
- 推送标题分三档：`✅ 全部成功` / `⚠️ 部分失败` / `❌ 全部失败`；退出码 `0` = 有成功、`1` = 全部失败（青龙标红）。

## 脚本文件

| 文件 | 说明 |
|---|---|
| `ikuuu.js` | 签到主脚本（含浏览器自动登录） |
| `solver_vlm.js` | 点选验证码解法器 · 视觉大模型版（可选，见「点选验证码与解法器」路线 A′）；支持 `--check` 自检 |
| `ikuuu.test.js` | 单元测试，`node --test ikuuu.test.js` |
| `sendNotify.js` | 青龙官方 Notify（推送通道），勿删 |
| `package.json` / `package-lock.json` | Node 依赖声明（容器内 `npm i` 用） |
| `.token/` | 登录会话 Cookie 缓存（运行时生成，按邮箱隔离，**含敏感凭证勿提交勿外传**） |
| `_ikuuu_debug/` `_vlm_debug/` | 调试产物（`IKUUU_DEBUG=1` / `IKUUU_VLM_DEBUG=1` 时生成，已在 .gitignore 里） |

## 青龙任务命令

```text
task wuang-wu_Ikuuu/ikuuu.js
```

> 本脚本为 Node.js（`.js`），其余子目录脚本为 Python。命令路径需带子目录前缀。

## 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `ACCOUNTS` | 二选一 | 账密账号列表，格式 `邮箱#密码`，多账号用 `&` 或换行分隔（推荐，失效自动重登） |
| `IKUUU_COOKIE` | 二选一 | cookie 字符串直填（旧行为），多账号用换行分隔；失效需手工更新 |
| `IKUUU_SOLVER_CMD` | | **点选验证码外部解法器**（见下文「点选验证码与解法器」），如 `node solver_vlm.js`。脚本自身不做图形识别，站点下发点选验证码时靠它提供点击坐标 |
| `IKUUU_SOLVER_CMD_FALLBACK` | | 备用解法器命令：主解法器"不可用"（key 失效 / 网关不通 / 超时）时自动改用（例如以后接打码平台） |
| `IKUUU_RETRY_TIMES` | | 失败账号延迟重试次数，默认 `0`（关）。建议 `1~2`：限流/网关抖动这类几分钟就恢复的故障靠它兜底 |
| `IKUUU_RETRY_DELAY` | | 每次重试前等待秒数，默认 `300` |
| `IKUUU_VLM_API_KEY` | 用视觉解法器时必填 | API Key（OpenAI 兼容网关的 `sk-...`） |
| `IKUUU_VLM_BASE_URL` | 用视觉解法器时必填 | 网关地址，如 `http://<网关>:3000/v1`。**脚本内不内置任何网关地址**，不填直接报错 |
| `IKUUU_VLM_MODEL` | 用视觉解法器时必填 | 视觉模型名（**脚本内不内置模型名**）。可用逗号给多个：前一个"不可用"时自动改用下一个（备用模型不必很准，投票+服务端校验会把错答案挡掉） |
| `IKUUU_VLM_TIMEOUT` | | 单次模型请求超时秒数，默认 `120` |
| `IKUUU_VLM_VOTES` | | 投票次数，默认 `2`（同一张图多问几次取中位数；分歧过大会放弃本次并重抽）。嫌费 token 可设 `1` |
| `IKUUU_VLM_ZOOM` | | 合成图放大倍数，默认 `3`（越大越利于看细节，但 token 也越多；范围 1~6） |
| `IKUUU_VLM_MAX_TOKENS` | | 单次回复上限，默认 `50000`（推理型模型的**思考也占 token**，偶尔会思考过长被截断：脚本会先从思考内容里捞答案，捞不到再按**双倍上限 + "别长篇推理直接出 JSON"** 重试一次，仍不行才当"这题没解出来"换题重抽）。放大时会夹在 `60000` 以内（新 API 类网关上限 `65536`，超了会被判非法请求） |
| `IKUUU_VLM_DEBUG` | | `1` = 把合成图与模型原始回复（含思考过程）写到脚本目录 `_vlm_debug/`，排查识别问题时用 |
| `IKUUU_VLM_MODEL_ERROR_HINT` | | 网关报错里出现哪些字样就判定为"模型/渠道级故障、直接换下一个模型"，默认 `model_not_found,No available channel,invalid_request_error`。换网关措辞不同可覆盖，置空关闭该判定 |
| `IKUUU_SOLVER_TIMEOUT` | | 解法器超时秒数，默认 `180`（视觉大模型解法器建议 ≥180；纯 HTTP 打码平台可调小） |
| `IKUUU_CAPTCHA_ROUNDS` | | 点选验证码最多刷新重抽次数，默认 `6`。验证形式每次刷新都会重新随机抽取 |
| `HOST` | | 强制锁定签到域名（锁定时不轮换），留空则从发布页自动抓取 + 兜底 `ikuuu.top` / `ikuuu.pw` |
| `IKUUU_PUBLISH_URL` | | 发布页地址，默认 `https://ikuuu.win/`。发布页整体迁移域名时改这里，不必改代码 |
| `IKUUU_PROXY` | | HTTP 代理（ikuuu 被墙，建议配置），如 `http://172.17.0.1:7890`；留空时自动回退青龙全局代理。HTTP 签到与浏览器登录**都会走该代理** |
| `IKUUU_HEADFUL` | | `1` = 登录用有头浏览器（仅本地调试，容器内勿开） |
| `IKUUU_CHROMIUM_PATH` | | 系统 Chromium 路径，如 `/usr/bin/chromium`（青龙容器装系统包后推荐此项） |
| `IKUUU_NOTIFY_ONLY_FAIL` | | `1` = 仅失败时推送，留空/`0` = 全部推送 |
| `IKUUU_DEBUG` | | `1` = 详细调试信息 + 登录过程截屏到 `_ikuuu_debug/` |

### ACCOUNTS 格式（账密模式，推荐）

```text
# 单账号
you@example.com#password123

# 多账号：& 或换行分隔
you@example.com#password123&other@163.com#pw456
```

- 按首个 `#` 切分，密码里可以包含 `#`；密码请避免包含 `&` 和换行（会被当作账号分隔符）
- Cookie 失效时自动开浏览器重新登录续期，**无需手工抓 cookie**

### IKUUU_COOKIE 格式（cookie 直填模式）

```text
# 单账号
uid=xxx; email=yyy; key=zzz; ip=aaa; expire_in=1234567890

# 多账号：换行分隔
uid=xxx; email=yyy; key=zzz
uid=aaa; email=bbb; key=ccc
```

- 每行必须包含 `uid=`，否则解析报错（缺 uid 的 cookie 签到必然 302）
- 此模式 Cookie 失效后**只能手工更新**（推送会提醒）；无需 playwright 依赖

两个变量可同时配置（cookie 账号排在前面先签到）。

## 登录原理（为什么需要浏览器）

ikuuu 的登录是**分阶段流程**（`POST /auth/login`，`phase=password`），且**强制 Geetest V4 验证码**。纯 HTTP 无法复现，因此：

1. 平时签到走**纯 HTTP**（快、省资源），会话 Cookie 缓存在 `.token/` 下按邮箱隔离；
2. ikuuu 会话有效期 **24 小时**（cookie 里 `expire_in` 字段），缓存余量不足 30 分钟或签到被 302 弹回登录页时，自动开 Playwright 浏览器：填表 → 过验证码 → 提交 → 抓取会话 Cookie → 回写缓存 → 重试签到（仅重试一次，防死循环）；
3. 缓存文件只存 Cookie 与到期时间，**不存密码**；密码只活在青龙环境变量里。

## 点选验证码与解法器

**2026-10 起站点把验证形式改成了「点选类」**（实测池子为 `word` 文字点选 / `icon` 图标点选 / `nine` 九宫格点选，三者随机分配，**不再有 `ai` 一键通过**）。极验的验证形式在**页面加载时**的 `/load` 请求里就已确定（点按钮只是发起校验），每次刷新页面会重新随机抽取。

这带来两个后果：

- 旧的"点一下按钮即过"不再出现，脚本必须**点击图中的指定图案**才能通过；
- 图案识别是图形算法问题，本脚本**刻意不做**（自研识别的准确率不足以支撑无人值守，误点还会消耗尝试次数）。

因此给出四条路线，**怎么选**：想省事就用 A′（本目录已附带 `solver_vlm.js`，配好网关和模型即可）；要接打码平台或自研识别用 A；只想手工维护 cookie 用 B；什么都不想做就等 C（站点改回宽松配置后账密模式自动可用）。

### 路线 A：自写外部解法器（`IKUUU_SOLVER_CMD`）

脚本把挑战图与待点图案交给你的命令，你用什么解都行（打码平台 API / 自研模型 / 本地视觉大模型）。协议：

**入参（stdin，JSON）**

```json
{
  "type": "word",
  "promptText": "请在下图依次点击",
  "imagePath": "/tmp/ikuuu-captcha-xxxx/challenge.jpg",
  "tplPaths": ["/tmp/.../tpl1.png", "/tmp/.../tpl2.png", "/tmp/.../tpl3.png"],
  "imageSize": { "w": 300, "h": 200 },
  "displaySize": { "w": 300, "h": 197 }
}
```

- `type`：本次验证形式（`word` / `icon` / `nine` / …）
- `promptText`：面板提示语原文（如「请在下图依次点击」「选 3 个符合右图的图片」）。缺省为空串，解法器可据此判断"点几个、要不要按顺序"
- `imagePath`：挑战图（带图案的图片，尺寸即 `imageSize`）
- `tplPaths`：待点图案，**按此顺序依次点击**
- 坐标一律用 `imageSize` 系的像素坐标（左上角为原点）
- 解法器是主脚本的**子进程，继承任务的环境变量**（所以 `IKUUU_VLM_*` 直接配在青龙里即可，不用写进命令）
- 多账号时**每个需要重新登录的账号各求解一次**，视觉调用次数按账号数累加

**出参（stdout，JSON，可夹带日志行）**

```json
{"ok": true, "clicks": [[91, 48], [105, 148], [35, 143]]}
```

失败时返回 `{"ok": false, "msg": "原因"}`（脚本会换个抽取再试）。返回非 0 退出码不影响解析，但 `stdout` 里必须能找到这个 JSON。

**最小示例**（自己写的解法器，文件名随意，这里叫 `my_solver.js`；只演示协议，识别逻辑要自己填）：

```js
// 用法：IKUUU_SOLVER_CMD='node my_solver.js'（与 ikuuu.js 同目录时）
let input = '';
process.stdin.on('data', (d) => { input += d; });
process.stdin.on('end', () => {
  const req = JSON.parse(input);
  if (req.type !== 'word') {           // 只解文字点选，其它形式让脚本换一次抽取
    process.stdout.write(JSON.stringify({ ok: false, msg: `不支持 ${req.type}` }));
    return;
  }
  // TODO: 读 req.imagePath 与 req.tplPaths，算出点击坐标
  const clicks = [[91, 48], [105, 148], [35, 143]];
  process.stdout.write(JSON.stringify({ ok: true, clicks }));
});
```

> 注意别和本目录自带的 `solver_vlm.js`（视觉大模型解法器）混淆：那个是现成能用的，不用自己写。

接商业打码平台时，把上面的 TODO 换成一次 HTTP 调用（上传 `imagePath` + `tplPaths`，按其文档取回坐标）即可，主脚本不用改。容器里注意把 `IKUUU_SOLVER_TIMEOUT` 调到平台响应时间之上。

### 路线 A′：内置视觉大模型解法器（`solver_vlm.js`，本目录已附带）

不想自己写解法器时，直接把本目录的 `solver_vlm.js` 当解法器挂上去即可（网关与模型都要自己填，脚本里不内置）：

```text
# 文件名是 solver_vlm.js（不是 solver.js）；青龙跑任务时工作目录就是本脚本目录，
# 所以下面两种写法等价，任选其一：
IKUUU_SOLVER_CMD  = node solver_vlm.js
# IKUUU_SOLVER_CMD = node /ql/data/scripts/wuang-wu_Ikuuu/solver_vlm.js

IKUUU_VLM_BASE_URL = http://<你的网关>:3000/v1        # 必填
IKUUU_VLM_API_KEY  = sk-xxxxx                          # 必填
IKUUU_VLM_MODEL    = <你的视觉模型>[,备用模型]          # 必填，可逗号给多个
```

**部署后先自检**（一条命令，输出全是 ✅ 才说明配好；有 ❌ 会直接指出缺什么、怎么修）：

```bash
docker exec -it qinglong bash -lc 'cd /ql/data/scripts/wuang-wu_Ikuuu && \
IKUUU_VLM_BASE_URL=http://<网关>:3000/v1 IKUUU_VLM_API_KEY=sk-xxx IKUUU_VLM_MODEL=<模型> \
node solver_vlm.js --check'
```

```text
[solver-vlm] 自检模式：只检查配置与连通性，不会发起点选解题
[solver-vlm] ✅ 环境变量齐全：BASE_URL=... MODEL=... API_KEY=已配置(NN 字符)
[solver-vlm] ✅ playwright 可加载
[solver-vlm] ✅ chromium 已安装：/root/.cache/ms-playwright/chromium-XXXX/chrome-linux/chrome
[solver-vlm] ✅ 模型 <模型> 视觉通道可用（测试图应答："红色"）
[solver-vlm] 检查通过，可以把本脚本作为 IKUUU_SOLVER_CMD 使用
```

> 自检那行里的三个环境变量要和青龙面板里配的一致（`docker exec` 的 shell 不会自动带上面板的环境变量）。
> 模型那一项是**真的发一张图问一句**（"这张图左边一半是什么颜色"），答出红色才算视觉通道可用——所以看到 ✅ 就是真通，不用靠猜。

工作方式：把挑战图放大 3 倍、叠加**像素坐标网格**，下方拼上带编号的待点图案 → 交给视觉模型「先枚举图中所有同类图形（定位），再做一一对应（识别）」；面板提示语原文（`promptText`）也会一并给模型，九宫格那种「选 N 个」的题会按 N 精确给点 → 同一张图问 `IKUUU_VLM_VOTES` 次取中位数，各次结果偏差过大就放弃这一次、让主脚本换个抽取重来（不消耗服务端失败次数）。网关限速（429）会自动退避重试。

实测（2026-10-09，`sensenova-6.8-flash-lite`）：

- 离线真值样本：`nine` 偏差多在 10px 内（偶发贴到网格整数刻度上、最大约 20px，仍落在格子里）；`word` 偏差 5~18px；`icon` 坐标经裁剪核对正确；
- 真机多轮测试：**多数轮次第 1~2 次抽取就通过**；
- **单次抽取不是 100% 准**（视觉模型看图本身有波动），所以脚本做了投票 + 重抽：日志里 `[解法器] ...` 行会写明每次投票结果与偏差，出问题先看这几行；
- 测试期间也遇到过一次**连续 6 次都没过**（当时伴随两次"投票分歧 53px / 点数不一致"，判断是网关侧那段时间响应质量下降）——这类波动靠 `IKUUU_RETRY_TIMES` 延迟重试和第二条 cron 兜底，不是脚本能完全消除的。

每次解题消耗 1~2 次视觉调用（推理模型一次约 1~2k token）；嫌费 token 可把 `IKUUU_VLM_VOTES` 设为 `1`（精度略降、失败重抽兜底）。

注意：

- 网关要是**容器能直连**的地址（局域网地址别配代理，脚本对网关请求不注入代理）；
- 模型必须支持图片输入；推理型模型的思考也占 token，`IKUUU_VLM_MAX_TOKENS` 给小了会在思考中途截断；
- 排查时设 `IKUUU_VLM_DEBUG=1`，会把合成图与模型原始回复（含思考过程）写到脚本目录 `_vlm_debug/`（已 gitignore）。

### 模型/解法器挂了怎么办（想"挂了也能签上"就配这三层）

| 层 | 配置 | 能扛住什么 |
|---|---|---|
| ① 模型列表兜底 | `IKUUU_VLM_MODEL = 主模型,备用模型` | 单个模型**渠道**故障：配额耗尽、模型下线、单模型限流（同网关换模型） |
| ② 解法器命令链 | `IKUUU_SOLVER_CMD` + `IKUUU_SOLVER_CMD_FALLBACK` | **整个网关**不可用：换一个完全独立的解法器（打码平台 API / 自研 / 另一台机器） |
| ③ 延迟重试 | `IKUUU_RETRY_TIMES = 2`、`IKUUU_RETRY_DELAY = 600` | **短暂故障**：限流窗口、网关重启、网络抖动——等 10 分钟再跑一遍就过了 |
| ④ 多跑几次 | 青龙再加一条 cron（如 `8 20 * * *`） | **长时间故障**：早上挂到晚上还没好，第二条 cron 会在晚上补签（已签到会被识别为成功，重跑无副作用） |

三层都没有接上（没备用模型、没备用解法器）时，就只能靠 ③+④；两层都只覆盖"过一会儿就好"的故障。真正独立于模型的自动兜底是**打码平台**——把它包成几十行的 `IKUUU_SOLVER_CMD_FALLBACK` 即可，主脚本不用改。

### 解法器不可用时会发生什么（2026-10-09 实测）

- **只有"需要重新登录"时才会用到解法器**：会话 Cookie 还在 24h 有效期内时，签到走纯 HTTP，模型/解法器完全不参与；站点若改回 `ai` 一键通过也不需要它。
- **解法器本身不可用**（没配 key / key 失效 / 网关不通 / 模型名写错 / 超时）：脚本**立刻停止换题重抽**（换题解决不了这类问题），任务判失败、推送里写明原因、退出码 1（青龙标红）。实测三种场景各 45~61 秒结束，不会空转几百秒：
  - 401 `Invalid token`（key 失效）→ 45s
  - `fetch failed`（网关不通）→ 60s
  - 503 `model_not_found`（模型名写错）→ 61s
- **题目本身没解出来**（投票分歧 / 思考被 token 截断 / 输出无法解析）：这类才换一题重抽，最多 `IKUUU_CAPTCHA_ROUNDS`（默认 6）次。
- 解法器失败时**不会提交任何点击**（服务端失败计数不受影响）；只有"点错并被服务端判错"才会计入那一题的 `fail_count`，重抽即换新题、从 0 开始。
- 手工兜底：模型长期不可用时，用 `IKUUU_COOKIE` 粘一个浏览器登录后的 cookie（24h 有效）即可继续签到，验证码与模型都不需要。

### 路线 B：`IKUUU_COOKIE` 直填（零依赖兜底）

不用浏览器：自己在能过验证码的浏览器里登录一次，F12 复制 cookie（必须含 `uid=`），填进青龙的环境变量 `IKUUU_COOKIE`。会话 24 小时，所以需要每天更新一次。

### 路线 C：等站点改回宽松配置

脚本保留了"一键通过"路径：一旦站点重新启用 `ai` 形式，`ACCOUNTS` 账密模式即可自行登录，无需任何解法器。

## 依赖安装（青龙容器）

Node ≥ 18（青龙面板自带版本满足）；依赖：`undici`（`package.json` 已含，需在容器内 `npm i`）+ `playwright`（自动登录用；视觉解法器 `solver_vlm.js` 也复用它渲染合成图）：

```bash
docker exec -it qinglong bash
cd /ql/data/scripts/wuang-wu_Ikuuu
npm i
npx playwright install chromium        # 或用系统 chromium（下一条）
apt update && apt install -y chromium  # 可选：系统包方式
```

用系统 chromium 时设置环境变量 `IKUUU_CHROMIUM_PATH=/usr/bin/chromium`。浏览器未就绪时签到照常可跑（cookie 未失效期间无需浏览器），登录环节会报引导性错误。

## 推送通道

复用同目录 `sendNotify.js`，在青龙「环境变量」里配置以下任意通道即可（与青龙官方 Notify 一致）：

- `DD_BOT_TOKEN` / `DD_BOT_SECRET`（钉钉）
- `BARK_PUSH`（Bark）
- `PUSH_KEY` / `TG_BOT_TOKEN` / `TG_USER_ID`（Server 酱 / TG）
- `QYWX_AM` / `QYWX_KEY`（企业微信）
- ……

具体支持通道见 `sendNotify.js` 顶部注释。

## 功能特性

- **账号密码自动登录**：Geetest V4 验证码自动处理——`ai` 一键通过时脚本点一下即过；下发点选类（word/icon/nine）时交给 `IKUUU_SOLVER_CMD` 外部解法器，并自动刷新重抽（验证形式每次刷新重新随机）。
- **验证码自动过**：本目录自带视觉大模型解法器 `solver_vlm.js`（见「路线 A′」），配好网关即可全自动；另有 `IKUUU_VLM_MODEL` 多模型兜底、`IKUUU_SOLVER_CMD_FALLBACK` 备用解法器链。
- **失败重试**：`IKUUU_RETRY_TIMES` / `IKUUU_RETRY_DELAY` 可让失败的账号隔一会儿自动重跑（限流、网关抖动这类瞬时故障靠它兜底）。
- **Cookie 缓存复用**：24h 会话内不重复登录；到期时间直接取服务端 `expire_in`。
- **动态域名**：自动从发布页 `https://ikuuu.win/`（2026-09 起由 `ikuuu.eu` 迁到这里，可用 `IKUUU_PUBLISH_URL` 覆盖）抓取当前可用主域名。抓取多通道：配了代理则优先代理直连发布页，否则公共 CORS 代理逐个试（allorigins → cors.lol → whateverorigin）。
  - **发布页域名永不作为签到目标**：`ikuuu.win` / `ikuuu.eu` 是 nginx 静态发布页，`POST /user/checkin` 只会返回 405，进候选必定失败，脚本会显式过滤掉。
  - **域名混淆还原**：发布页用 `javascript-obfuscator` 把域名拆成多段字符串拼接（`'ikuuu'+'.top'`），脚本会先把相邻字符串字面量折叠再匹配。
  - **兜底列表**：`ikuuu.top` → `ikuuu.pw`。抓取失败时靠它保证可用；也可用 `HOST` 强制锁定。
- **多账号串行**：账号间随机 3–8 秒间隔，降低风控。
- **状态分档**：
  | 状态 | 含义 |
  |---|---|
  | ✅ `success` | 签到成功 |
  | ✅ `already` | 已签到（幂等视为成功）|
  | ❌ `cookie_dead` | Cookie 失效（302 跳登录页 / 401）；账密模式自动重登后重试，cookie 模式推送提醒手工更新 |
  | ❌ `login_fail` | 自动登录失败（密码错 / 2FA / 邮箱验证码 / 站点下发点选验证码但未配解法器）|
  | ❌ `domain_block` | 域名墙、重定向到非业务页、405（该域名不是面板）|
  | ⚠️ `parse_err` | 响应解析不了 |
  | ❌ `network_err` | 网络异常（自动重试 2 次）|
- **标题分档推送**：`✅ 全部成功` / `⚠️ 部分失败` / `❌ 全部失败`；`IKUUU_NOTIFY_ONLY_FAIL=1` 时全部成功不推送。
- **账号脱敏**：日志与推送中邮箱/账号名自动打码（`19********@qq.com`）。

## 排错指引

| 日志现象 | 排查方向 |
|---|---|
| `[解法器] [solver-vlm] 第 N/2 次: [[x,y],...]` / `最终坐标: ...（各次最大偏差 NNpx）` | 这是解法器自己的日志（主脚本原样转发）。**排错先看这几行**：两次投票偏差大 → 该次不会提交、自动重抽；`改用 xxx` → 换模型/换解法器了 |
| `模型无正文输出（finish_reason=length，可能 token 不足）` | 推理模型这次"想太久"，token 全花在思考上、正文为空。**不是致命错误**：脚本会自动（1）从思考内容里捞答案 →（2）双倍 token 上限 + 强制指令重试一次 →（3）仍不行则换题重抽。默认上限已是 `50000`，偶发无需处理 |
| `HTTP 400 ... MaxTokens invalid, should be in [1, 65536]` | `IKUUU_VLM_MAX_TOKENS` 超过网关上限。调小到报错区间内即可（脚本默认 50000、放大时夹 60000，正常不会触发；手改过才可能遇到） |
| `未安装 playwright（自动登录需要）` | 按上面「依赖安装」在容器内 `npm i && npx playwright install chromium`，或配 `IKUUU_CHROMIUM_PATH` |
| `未配置 IKUUU_VLM_BASE_URL / IKUUU_VLM_API_KEY / IKUUU_VLM_MODEL` | 视觉解法器的必填变量没配。按「快速上手」第 3 步补齐，或先跑 `node solver_vlm.js --check` 看缺哪一项 |
| `站点已启用点选类验证码（文字点选(word)），脚本自身不做图形识别` | 站点把验证形式改成了点选类，但没配 `IKUUU_SOLVER_CMD`。按「快速上手」第 3 步配视觉解法器，或改用 `IKUUU_COOKIE` 直填 cookie |
| `解法器未解决验证码: 不支持 icon` | 解法器只支持部分形式。脚本会刷新重抽 `IKUUU_CAPTCHA_ROUNDS` 次；命中概率低时可调大次数，或让解法器支持更多形式 |
| `解法器给出的 N 个点击未被通过` | 解法器坐标不准或点击顺序不对。真值顺序是 `tplPaths` 数组顺序；坐标是挑战图像素坐标 |
| `⛔ 解法器不可用（非题目本身问题），停止重抽` | 解法器侧故障（key/网关/模型名/超时），换题无用故立刻失败；按后面的错误原文修（`Invalid token` → 查 `IKUUU_VLM_API_KEY`；`model_not_found` → 查 `IKUUU_VLM_MODEL`；`fetch failed` → 查容器到网关的连通性） |
| `Cannot find module ...'.../wuang-wu_Ikuuu/wuang-wu_Ikuuu/solver.js'` | `IKUUU_SOLVER_CMD` 的文件名/路径写错。正确值是 `node solver_vlm.js`（青龙的工作目录已是脚本目录，不要再带 `wuang-wu_Ikuuu/` 前缀），或用绝对路径 |
| `解法器超时` / `解法器输出无法解析` | 调大 `IKUUU_SOLVER_TIMEOUT`（默认 180s）；确认 stdout 里有且只有一个 JSON 对象 |
| `密码阶段被拒: 邮箱或密码错误` | 核对 `ACCOUNTS` 里的 email/password；注意密码里含特殊字符时推荐用 JSON 格式 |
| `账号开启了两步验证(2FA)` / `要求邮箱验证码登录` / `反向邮件验证` | 站点要求额外交互，脚本无法代填，需人工处理（关闭 2FA 或调整账号安全设置） |
| `[域名加载] ⚠️ 发布页抓取失败（各通道均不可用）` | 发布页被墙或 DNS 污染。兜底 `ikuuu.top` / `ikuuu.pw` 仍可用；建议配 `IKUUU_PROXY` 或 `HOST` 锁定 |
| `❌ 域名异常：HTTP 405 该域名不是签到面板` | 当前域名是发布页。检查 `IKUUU_PUBLISH_URL` 是否被误设成面板域名 |
| `⚠️ 响应异常` | 接口返回结构变化，开 `IKUUU_DEBUG=1` 看原文 |

`IKUUU_DEBUG=1` 时登录过程截屏存到脚本目录 `_ikuuu_debug/`（该目录已被 .gitignore 忽略）。

## 本地调试

```bash
export ACCOUNTS='you@example.com#password123'   # 或 IKUUU_COOKIE='uid=xxx; ...'
export IKUUU_DEBUG=1
# 本机调试登录环节时，解法器变量也要一起给（缺了会在"点选验证码"这步停下并提示）
export IKUUU_SOLVER_CMD='node solver_vlm.js'
export IKUUU_VLM_BASE_URL='http://<网关>:3000/v1'
export IKUUU_VLM_API_KEY='sk-xxx'
export IKUUU_VLM_MODEL='<你的视觉模型>'
node ikuuu.js
```

Windows PowerShell：

```powershell
$env:ACCOUNTS='you@example.com#password123'   # 或 $env:IKUUU_COOKIE='uid=xxx; ...'
$env:IKUUU_DEBUG='1'
$env:IKUUU_SOLVER_CMD='node solver_vlm.js'
$env:IKUUU_VLM_BASE_URL='http://<网关>:3000/v1'
$env:IKUUU_VLM_API_KEY='sk-xxx'
$env:IKUUU_VLM_MODEL='<你的视觉模型>'
node ikuuu.js
```

调试登录弹窗过程可加 `$env:IKUUU_HEADFUL='1'`（有头浏览器，仅本机）。
只想验证解法器本身时，直接 `node solver_vlm.js --check`（自检）或喂一份协议 JSON 手工试：

```bash
echo '{"type":"word","imagePath":"…/bg.jpg","tplPaths":["…/tpl1.png","…/tpl2.png","…/tpl3.png"],"imageSize":{"w":300,"h":200}}' | node solver_vlm.js
```

## 单元测试

```bash
node --test ikuuu.test.js
```

覆盖（38 项）：发布页域名提取（字符串拼接还原、发布页域名过滤）、签到响应分类、登录响应 phase 分类、账号解析（邮箱#密码 单/多账号与非法输入）、Cookie 缓存读写与余量判定、Cookie 串拼装过滤、邮箱脱敏，以及点选验证码配套（`/load` JSONP 解析、验证形式中文名、PNG/JPEG 尺寸解析、解法器 stdout JSON 提取与坐标校验、失败类型分类与提示生成）。

## 接口备忘（2026-10 实测）

| 项目 | 值 |
|---|---|
| 发布页 | `https://ikuuu.win/`（nginx 静态站，标题「iKuuuVPN最新域名」，`POST /user/checkin` → 405） |
| 面板域名 | `ikuuu.top`（主要）、`ikuuu.pw`（备用 1） |
| 签到接口 | `POST https://{面板域名}/user/checkin`（带会话 Cookie） |
| 签到成功 | `{"msg":"你获得了 475 MB流量","ret":1}` |
| 重复签到 | `{"ret":0,"msg":"您似乎已经签到过了..."}` ← 注意是「已**经**签到」 |
| 未登录表现 | `302 → /auth/login`（分类为 `cookie_dead`） |
| 登录接口 | `POST /auth/login`，**分阶段**：请求带 `phase=password` + `host` + `email` + `passwd` + `remember_me` + `pageLoadedAt`（页面加载毫秒时间戳，防重放）；响应 `phase=authenticated`（成功）/ `password`（密码错）/ `totp` / `email_code` / `reverse_email_verify` |
| 登录验证码 | **Geetest V4**（captchaId `cc96d05ba8b60f9112f76e18526fcb73`）。服务端强制校验 `captcha_result`，纯 HTTP 无验证码提交返回 `captcha_failed` |
| 验证形式（2026-10-09 实测） | 站点配置已改为**点选类多选**：`icon` / `word` / `nine` 三者随机分配，**不再下发 `ai`**。形式在**页面加载时**由 `gcaptcha4.geetest.com/load` 决定（响应里的 `captcha_type` 字段），点按钮只是发起校验；**刷新页面会重新随机抽取**。与出口 IP、浏览器指纹、有头/无头**无关**（三个不同出口 + 有头浏览器实测分布一致） |
| 点选校验位置 | 点击的正确性**只在服务端提交时校验**（客户端 `geetest_mark_no` 标记只是"你点了这里"，点错不产生任何网络请求；点"确定"才发 `/verify`）。返回 `{"result":"success"/"fail","fail_count":N}`，失败后组件会自行 `/load` 换一题 |
| 点选挑战数据 | `/load` 响应已含挑战图与待点图案：`imgs`（合成图，jpg，300x200 或 300x261）、`ques`（待点图案 PNG，**按顺序**点击）。挑战图是**照片背景 + 形状/颜色随机迷彩化的图案**，同一张图里不同图案颜色还可能不同——这也是脚本不做自研识别的原因 |
| 会话有效期 | **24 小时**（`expire_in` = 登录时刻 + 86400 秒，`remember_me` 不延长） |
| Cookie 组成 | `uid` / `email`(URL编码) / `key`(40位hex) / `ip`(32位hex) / `expire_in` / `session_version`；`PHPSESSID` 可有可无，签到接口均可用 |
| 登录页形态 | HTML 为 base64 混淆（前端 `SlowerDecodeBase64` 解出真实 DOM），登录逻辑在内联第 8 个 `<script>` 里 |

## 退出码

- `0`：全部成功或部分失败（任务绿色）
- `1`：全部失败 / 主流程异常（任务红色，便于在青龙识别）

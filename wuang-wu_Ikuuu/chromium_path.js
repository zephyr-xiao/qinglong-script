/**
 * 解析可用的 Chromium 可执行文件路径。
 *
 * Playwright 默认把浏览器路径绑定到自身 revision（例如 chromium-1234），
 * 版本号对不上就报 "Executable doesn't exist" —— 即使机器上已经装了别的版本。
 * 显式传 executablePath 时 Playwright 不校验 revision，所以统一在这里解析：
 *   1. 环境变量 IKUUU_CHROMIUM_PATH（可指向系统 chromium）
 *   2. 青龙持久卷的 .cache/ms-playwright 下任意 revision 的 chrome
 *      （/ql/data 是容器唯一持久挂载，放这里的浏览器能跨容器重建保留）
 *   3. 用户默认缓存目录 ~/.cache/ms-playwright（Playwright 默认位置）
 * 都找不到返回空串，调用方回落到 Playwright 自身逻辑。
 */
const fs = require('fs');
const os = require('os');
const path = require('path');

// 按 revision 从大到小列出目录里的 chromium-<数字>
function listChromium(root) {
  let names;
  try {
    names = fs.readdirSync(root);
  } catch (e) {
    return [];
  }
  return names
    .map((name) => ({ name, rev: Number((/^chromium-(\d+)$/.exec(name) || [])[1]) }))
    .filter((item) => Number.isFinite(item.rev) && item.rev > 0)
    .sort((a, b) => b.rev - a.rev);
}

function findInRoot(root) {
  for (const { name } of listChromium(root)) {
    // 新版 Playwright 解包到 chrome-linux64，旧版是 chrome-linux
    for (const dir of ['chrome-linux64', 'chrome-linux']) {
      const exe = path.join(root, name, dir, 'chrome');
      if (fs.existsSync(exe)) return exe;
    }
  }
  return '';
}

function resolveChromiumPath() {
  const explicit = (process.env.IKUUU_CHROMIUM_PATH || '').trim();
  if (explicit) return explicit;
  // 脚本位于 <QL_DATA>/scripts/<目录>/，往上两级即青龙数据根
  const persisted = path.resolve(__dirname, '..', '..', '.cache', 'ms-playwright');
  return findInRoot(persisted) || findInRoot(path.join(os.homedir(), '.cache', 'ms-playwright'));
}

module.exports = { resolveChromiumPath, findInRoot };

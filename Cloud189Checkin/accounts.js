// 账号加载：支持三种格式的环境变量配置（青龙面板推荐用 TY_ACCOUNTS）
//   1) TY_ACCOUNTS: JSON 数组，如 [{"userName":"138xxxx","password":"xxx"}, ...]
//   2) TY_ACCOUNTS: JSON 单对象，如 {"userName":"138xxxx","password":"xxx"}
//   3) TY_USERNAME_n / TY_PASSWORD_n 系列（逐对读取，兼容保留）
function loadAccountsFromEnv() {
  let accounts = [];

  // 读取 TY_ACCOUNTS（JSON 数组或单对象）
  if (process.env.TY_ACCOUNTS) {
    const parsed = JSON.parse(process.env.TY_ACCOUNTS);
    if (Array.isArray(parsed)) {
      accounts = parsed;
    } else if (
      parsed &&
      typeof parsed === "object" &&
      parsed.userName &&
      parsed.password
    ) {
      // 单对象自动包成数组
      accounts = [parsed];
    }
  } else {
    // 逐对格式：从 TY_USERNAME_n / TY_PASSWORD_n 读取账号，支持任意数量
    let index = 1;
    while (true) {
      const userName = process.env[`TY_USERNAME_${index}`];
      const password = process.env[`TY_PASSWORD_${index}`];
      if (!userName || !password) {
        break;
      }
      accounts.push({
        userName,
        password,
      });
      index++;
    }
  }

  return accounts;
}

const accounts = loadAccountsFromEnv();
module.exports = accounts;

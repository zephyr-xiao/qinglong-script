/**
 * @name 天翼云盘自动签到
 * @description 天翼云盘(189)自动签到，多账号，推送个人/家庭容量变化
 * @cron 8 8 * * *
 *
 * 环境变量：
 *   TY_ACCOUNTS                【必填】账号列表，支持 JSON 数组 / 单对象（详见 accounts.js）
 *   TY_USERNAME_n/TY_PASSWORD_n  旧版账号格式，仍兼容
 *   CLOUD189_VERBOSE           可选，1 = 开启 cloud189-sdk 调试日志
 *
 * 推送：复用同目录 sendNotify.js（青龙官方 Notify），在青龙变量里配 DD_BOT_TOKEN 等即可。
 */
// quiet: 青龙环境无 .env 文件时抑制"injected env"噪音提示
require("dotenv").config({ quiet: true });
const path = require("path");
const {
  CloudClient,
  FileTokenStore,
  logger: sdkLogger,
} = require("cloud189-sdk");
const accounts = require("../accounts");
const { mask, delay } = require("./utils");
const { sendNotify } = require(path.join(__dirname, "../sendNotify"));
const { log4js, cleanLogs, catLogs } = require("./logger");

// token 目录固定在脚本目录下，避免青龙任务 cwd 变化导致凭证写到错误位置
const tokenDir = path.join(__dirname, "../.token");

sdkLogger.configure({
  isDebugEnabled: process.env.CLOUD189_VERBOSE === "1",
});

// 个人任务签到
const doUserTask = async (cloudClient, logger) => {
  const result = await cloudClient.userSign();
  if (result.isSign) {
    logger.info("个人签到任务: 今日已签到过");
  } else {
    logger.info(`个人签到任务: 获得 ${result.netdiskBonus}M 空间`);
  }
};

// 单账号执行，返回是否成功；错误统一在此吞掉，保证多账号串行跑完
const run = async (userName, password, userSizeInfoMap, logger) => {
  if (!userName || !password) {
    return false;
  }
  const before = Date.now();
  let success = false;
  try {
    logger.log("开始执行");
    const cloudClient = new CloudClient({
      username: userName,
      password,
      token: new FileTokenStore(`${tokenDir}/${userName}.json`),
    });
    const beforeUserSizeInfo = await cloudClient.getUserSizeInfo();
    userSizeInfoMap.set(userName, {
      cloudClient,
      userSizeInfo: beforeUserSizeInfo,
      logger,
    });
    await doUserTask(cloudClient, logger);
    success = true;
  } catch (e) {
    if (e.response) {
      logger.log(`请求失败: ${e.response.statusCode}, ${e.response.body}`);
    } else {
      logger.error(e);
    }
  } finally {
    logger.log(
      `执行完毕, 耗时 ${((Date.now() - before) / 1000).toFixed(2)} 秒`
    );
  }
  return success;
};

// 开始执行程序
async function main() {
  //  用于统计实际容量变化
  const userSizeInfoMap = new Map();
  let successCount = 0;
  const total = accounts.length;
  if (total === 0) {
    console.warn("未配置账号环境变量 TY_ACCOUNTS（或旧版 TY_USERNAME_n/TY_PASSWORD_n）");
  }

  for (let index = 0; index < total; index++) {
    const account = accounts[index];
    const { userName, password } = account;
    const userNameInfo = mask(userName, 3, 7);
    const logger = log4js.getLogger(userName);
    logger.addContext("user", userNameInfo);
    const ok = await run(userName, password, userSizeInfoMap, logger);
    if (ok) {
      successCount++;
    }
  }

  // 数据汇总
  for (const [
    userName,
    { cloudClient, userSizeInfo, logger },
  ] of userSizeInfoMap) {
    const afterUserSizeInfo = await cloudClient.getUserSizeInfo();
    logger.log(
      `个人容量：⬆️  ${(
        (afterUserSizeInfo.cloudCapacityInfo.totalSize -
          userSizeInfo.cloudCapacityInfo.totalSize) /
        1024 /
        1024
      ).toFixed(2)}M/${(
        afterUserSizeInfo.cloudCapacityInfo.totalSize /
        1024 /
        1024 /
        1024
      ).toFixed(2)}G`,
      `家庭容量：⬆️  ${(
        (afterUserSizeInfo.familyCapacityInfo.totalSize -
          userSizeInfo.familyCapacityInfo.totalSize) /
        1024 /
        1024
      ).toFixed(2)}M/${(
        afterUserSizeInfo.familyCapacityInfo.totalSize /
        1024 /
        1024 /
        1024
      ).toFixed(2)}G`
    );
  }

  return { successCount, total };
}

(async () => {
  try {
    const { successCount, total } = await main();
    // 等待日志文件写入
    await delay(1000);
    const logs = catLogs();
    await sendNotify("天翼云盘自动签到任务", logs);
    cleanLogs();
    // 全部失败才非 0 退出，便于青龙识别失败任务红色标记
    if (total > 0 && successCount === 0) {
      process.exit(1);
    }
  } catch (e) {
    console.error("主流程异常:", e);
    process.exit(1);
  }
})();

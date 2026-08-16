const log4js = require("log4js");
const fs = require("fs");
const path = require("path");

// 日志目录固定在脚本目录下，避免青龙任务 cwd 变化导致日志写到错误位置
const logDir = path.join(__dirname, "../.logs");

log4js.configure({
  appenders: {
    out: {
      type: "console",
      layout: {
        type: "pattern",
        pattern: "[%d] [%p] %X{user}: %m",
      },
    },
    file: {
      type: "multiFile",
      base: `${logDir}/`,
      property: "categoryName",
      extension: ".log",
      maxLogSize: 10485760,
      backups: 3,
      compress: true,
      layout: {
        type: "pattern",
        pattern: "[账号：%X{user}] %m",
      },
    },
  },
  categories: {
    default: { appenders: ["out", "file"], level: "info" },
  },
});

const cleanLogs = () => {
  if (!fs.existsSync(logDir)) {
    return;
  }
  const logs = fs.readdirSync(logDir);
  logs.forEach((log) => {
    if (log.endsWith(".log")) {
      fs.unlinkSync(`${logDir}/${log}`);
    }
  });
};

const catLogs = () => {
  if (!fs.existsSync(logDir)) {
    return "";
  }
  const logs = fs.readdirSync(logDir);
  const content = logs
    .map((file) => fs.readFileSync(`${logDir}/${file}`, { encoding: "utf-8" }))
    .join("\r");
  return content;
};

module.exports = { log4js, cleanLogs, catLogs };

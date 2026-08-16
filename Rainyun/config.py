import os
import json
import logging

logger = logging.getLogger(__name__)


class Config:
    """统一配置管理类"""
    
    # 默认配置
    DEFAULT_CONFIG = {
        # 基础配置
        "timeout": 20,
        "max_delay": 5,
        "similarity_threshold": 0.4,
        
        # 验证码配置
        "captcha_retry_limit": 10,  # -1表示无限重试
        "captcha_total_timeout": 1800,  # 无限重试模式的全局时间上限（秒），防止验证码结构变化导致死循环
        
        # 下载配置
        "download_max_retries": 3,
        "download_retry_delay": 2,
        "download_timeout": 10,
        
        # API配置
        "api_base_url": "https://api.v2.rainyun.com",
        "api_request_timeout": 10,
        "api_max_retries": 3,
        "api_retry_delay": 2,
        
        # 续费配置（全局默认值）
        "renew_days": 7,
        "renew_threshold_days": 3,
        "min_points_reserve": 5000,
        
        # 其他配置
        "points_to_cny_rate": 2000,
        "cookie_cache": True,   # 会话 Cookie 落盘复用：命中则跳过登录 + 验证码（默认开启）
        
        # 路径配置（相对于主脚本的路径）
        "stealth_js_path": "./stealth.min.js",  # 默认在当前目录

        # chromedriver 路径（空字符串=自动探测：环境变量 CHROMEDRIVER_PATH → PATH → 青龙固定路径）
        "chrome_driver_path": ""
    }
    
    def __init__(self):
        self.config = self._load_config()
    
    def _load_config(self) -> dict:
        """从环境变量加载配置"""
        config_str = os.getenv("RAINYUN_CONFIG", "{}")
        try:
            user_config = json.loads(config_str)
            # 合并用户配置和默认配置
            merged_config = self.DEFAULT_CONFIG.copy()
            merged_config.update(user_config)
            
            logger.info("✅ 配置加载成功")
            # 打印关键配置
            logger.info(f"⚙️  页面超时: {merged_config['timeout']}秒")
            logger.info(f"⚙️  最大延时: {merged_config['max_delay']}分钟")
            logger.info(f"⚙️  验证码重试: {merged_config['captcha_retry_limit']} {'(无限重试)' if merged_config['captcha_retry_limit'] == -1 else '次'}")
            logger.info(f"⚙️  相似度阈值: {merged_config['similarity_threshold']}")
            logger.info(f"⚙️  续费天数: {merged_config['renew_days']}天")
            logger.info(f"⚙️  续费阈值: 剩余{merged_config['renew_threshold_days']}天时触发")
            logger.info(f"⚙️  保留积分: {merged_config['min_points_reserve']}分")
            logger.info(f"⚙️  Cookie 缓存: {'✅ 开启' if merged_config.get('cookie_cache', True) else '⛔ 关闭'}")
            logger.info(f"⚙️  反检测脚本路径: {merged_config['stealth_js_path']}")
            
            return merged_config
        except json.JSONDecodeError as e:
            logger.error(f"❌ RAINYUN_CONFIG 格式错误: {e}")
            logger.warning("⚠️  使用默认配置")
            return self.DEFAULT_CONFIG.copy()
    
    def get(self, key: str, default=None):
        """获取配置项"""
        return self.config.get(key, default)
    
    def __getitem__(self, key):
        """支持字典式访问"""
        return self.config[key]


# 全局配置实例
CONFIG = Config()

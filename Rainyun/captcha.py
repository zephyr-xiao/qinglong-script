import logging
import os
import random
import re
import time
from typing import Tuple

import cv2
import requests
from selenium.common import TimeoutException, NoSuchElementException
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC

logger = logging.getLogger(__name__)


class CaptchaRetryableError(Exception):
    """可重试的验证码错误"""
    pass


def process_captcha(ctx, config: dict) -> bool:
    """处理验证码（循环模式）"""
    retry_limit = config["captcha_retry_limit"]
    is_unlimited = (retry_limit == -1)

    if is_unlimited:
        logger.info("⚠️  验证码无限重试模式已启用")

    # 无限重试时仍受全局时间上限约束，防止验证码结构变化导致脚本死循环
    total_timeout = config.get("captcha_total_timeout", 1800)
    start_time = time.time()

    retry_count = 0

    while True:
        # 检查重试次数
        if not is_unlimited and retry_count >= retry_limit:
            logger.error(f"❌ 验证码重试 {retry_limit} 次仍失败，放弃")
            return False

        # 检查全局时间上限（对无限重试模式兜底）
        if is_unlimited and time.time() - start_time > total_timeout:
            logger.error(f"❌ 验证码无限重试超过全局时限 {total_timeout} 秒，放弃")
            return False

        retry_count += 1
        
        if is_unlimited:
            logger.info(f"🔄 验证码处理第 {retry_count} 次尝试（无限重试模式）")
        else:
            logger.info(f"🔄 验证码处理第 {retry_count}/{retry_limit} 次尝试")
        
        try:
            # 下载验证码图片
            logger.info("📥 开始下载验证码图片...")
            if not download_captcha_img(ctx, config):
                raise CaptchaRetryableError("验证码图片下载失败")
            logger.info("✅ 验证码图片下载成功")
            
            # 校验验证码有效性
            logger.info("🔍 校验验证码碎片有效性...")
            if not check_captcha(ctx):
                raise CaptchaRetryableError("验证码碎片无效")
            logger.info("✅ 验证码碎片有效")
            
            # 识别验证码
            logger.info("🤖 开始识别验证码...")
            captcha = cv2.imread(ctx.temp_path("captcha.jpg"))
            if captcha is None:
                raise CaptchaRetryableError("验证码背景图读取失败")
            
            with open(ctx.temp_path("captcha.jpg"), "rb") as f:
                bboxes = ctx.det.detection(f.read())
            
            if not bboxes:
                raise CaptchaRetryableError("未检测到验证码图案")
            
            logger.info(f"   检测到 {len(bboxes)} 个图案区域")
            
            # 匹配碎片与背景图
            result = {}
            for i, (x1, y1, x2, y2) in enumerate(bboxes):
                cv2.imwrite(ctx.temp_path(f"spec_{i+1}.jpg"), captcha[y1:y2, x1:x2])
                
                for j in range(3):
                    sim, matched = compute_similarity(
                        ctx.temp_path(f"sprite_{j+1}.jpg"),
                        ctx.temp_path(f"spec_{i+1}.jpg")
                    )
                    key_sim = f"sprite_{j+1}.similarity"
                    key_pos = f"sprite_{j+1}.position"
                    
                    if sim > float(result.get(key_sim, 0)):
                        result[key_sim] = sim
                        result[key_pos] = f"{int((x1+x2)/2)},{int((y1+y2)/2)}"
            
            # 校验答案
            if not check_answer(result, config["similarity_threshold"]):
                # 输出匹配率信息
                for i in range(3):
                    sim = result.get(f"sprite_{i+1}.similarity", 0)
                    pos = result.get(f"sprite_{i+1}.position", "N/A")
                    logger.warning(f"   图案 {i+1}: 位置={pos}, 匹配率={sim:.4f}")
                raise CaptchaRetryableError("验证码答案无效")
            
            logger.info("✅ 验证码识别成功")
            
            # 点击验证码
            click_captcha(ctx, result, captcha)
            
            # 提交验证码
            logger.info("📤 提交验证码")
            confirm = ctx.wait.until(
                EC.element_to_be_clickable((By.XPATH, "//div[@id='tcStatus']/div[2]/div[2]/div/div"))
            )
            confirm.click()
            logger.info("⏳ 等待验证结果...")
            time.sleep(5)
            
            # 校验结果
            result_el = ctx.wait.until(
                EC.visibility_of_element_located((By.ID, "tcOperation"))
            )
            if "show-success" in result_el.get_attribute("class"):
                logger.info("✅ 验证码验证通过")
                return True
            else:
                logger.error("❌ 验证码验证失败")
                raise CaptchaRetryableError("验证码验证失败")
        
        except (TimeoutException, ValueError, CaptchaRetryableError) as e:
            logger.error(f"❌ 验证码处理失败: {e}")
            
            # 刷新验证码
            logger.info("🔄 刷新验证码中，稍后重试...")
            if not refresh_captcha(ctx):
                return False
            
            # 指数退避（上限30秒）
            delay = min(3 * (2 ** (retry_count - 1)), 30)
            logger.info(f"⏳ 等待 {delay} 秒后重试...")
            time.sleep(delay)


def download_captcha_img(ctx, config: dict) -> bool:
    """下载验证码图片"""
    try:
        # 清空旧文件
        clear_temp_dir(ctx.temp_dir)
        
        # 下载背景图
        slide_bg = ctx.wait.until(
            EC.visibility_of_element_located((By.ID, "slideBg"))
        )
        img1_style = slide_bg.get_attribute("style")
        img1_url = get_url_from_style(img1_style)
        
        logger.info(f"   验证码背景图URL: {img1_url}")
        if not download_image(img1_url, ctx.temp_path("captcha.jpg"), config):
            logger.error("   背景图下载失败")
            return False
        logger.info("   ✓ 背景图下载成功")
        
        # 下载碎片图
        sprite = ctx.wait.until(
            EC.visibility_of_element_located((By.XPATH, "//div[@id='instruction']//img"))
        )
        img2_url = sprite.get_attribute("src")
        
        logger.info(f"   验证码碎片图URL: {img2_url}")
        if not download_image(img2_url, ctx.temp_path("sprite.jpg"), config):
            logger.error("   碎片图下载失败")
            return False
        logger.info("   ✓ 碎片图下载成功")
        
        return True
        
    except TimeoutException:
        logger.error("❌ 验证码图片加载超时")
        return False
    except Exception as e:
        logger.error(f"❌ 验证码图片下载失败: {e}")
        return False


def download_image(url: str, output_path: str, config: dict) -> bool:
    """下载图片（带重试）"""
    max_retries = config.get("download_max_retries", 3)
    retry_delay = config.get("download_retry_delay", 2)
    timeout = config.get("download_timeout", 10)
    
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        "Referer": "https://app.rainyun.com/"
    }
    
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            
            with open(output_path, "wb") as f:
                f.write(response.content)
            return True
            
        except Exception as e:
            if attempt < max_retries:
                logger.warning(f"   下载失败 (第 {attempt} 次): {e}，{retry_delay}秒后重试...")
                time.sleep(retry_delay)
            else:
                logger.error(f"   下载失败 (已重试 {max_retries} 次): {e}")
                return False


def check_captcha(ctx) -> bool:
    """校验验证码碎片有效性"""
    try:
        raw = cv2.imread(ctx.temp_path("sprite.jpg"))
        if raw is None:
            logger.error("   验证码碎片图读取失败")
            return False
        
        # 分割碎片
        w = raw.shape[1]
        for i in range(3):
            temp = raw[:, w // 3 * i: w // 3 * (i + 1)]
            cv2.imwrite(ctx.temp_path(f"sprite_{i+1}.jpg"), temp)
            
            # 检查是否为无效图片
            with open(ctx.temp_path(f"sprite_{i+1}.jpg"), "rb") as f:
                ocr_result = ctx.ocr.classification(f.read())
                if ocr_result in ["0", "1"]:
                    logger.warning(f"   碎片 {i+1} 无效（OCR结果: {ocr_result}）")
                    return False
        
        logger.info("   ✓ 所有碎片有效")
        return True
        
    except Exception as e:
        logger.error(f"   验证码碎片校验失败: {e}")
        return False


def check_answer(result: dict, threshold: float) -> bool:
    """检查验证码答案有效性"""
    if not result or len(result) < 6:
        logger.warning(f"   验证码识别结果不完整（仅有 {len(result) if result else 0} 个键，预期 6 个）")
        return False
    
    # 检查相似度
    for i in range(3):
        sim = float(result.get(f"sprite_{i+1}.similarity", 0))
        if sim < threshold:
            logger.error(f"   图案 {i+1} 识别率 {sim:.4f} 低于阈值 {threshold}")
            return False
    
    # 检查坐标唯一性
    positions = [result.get(f"sprite_{i+1}.position") for i in range(3)]
    if len(set(positions)) != 3:
        logger.error(f"   验证码坐标重复: {positions}")
        return False
    
    logger.info("   ✓ 验证码答案有效")
    return True


def click_captcha(ctx, result: dict, captcha_img):
    """点击验证码图案"""
    slide_bg = ctx.wait.until(
        EC.visibility_of_element_located((By.ID, "slideBg"))
    )
    style = slide_bg.get_attribute("style")
    
    # 获取显示尺寸
    try:
        width = get_width_from_style(style)
        height = get_height_from_style(style)
        logger.info(f"   验证码显示尺寸: {width}x{height} px")
    except ValueError:
        size = slide_bg.size
        width = float(size.get("width", 300))
        height = float(size.get("height", 150))
        logger.info(f"   验证码显示尺寸（元素获取）: {width}x{height} px")
    
    # 原始图片尺寸
    width_raw, height_raw = captcha_img.shape[1], captcha_img.shape[0]
    logger.info(f"   验证码原始尺寸: {width_raw}x{height_raw} px")
    
    # 依次点击三个图案
    for i in range(3):
        pos = result[f"sprite_{i+1}.position"]
        sim = result[f"sprite_{i+1}.similarity"]
        x, y = map(int, pos.split(","))
        
        logger.info(f"🎯 图案 {i+1} 坐标({x},{y})，匹配率：{sim:.4f}")
        
        # 计算实际点击坐标（适配缩放）
        x_offset = -width / 2
        y_offset = -height / 2
        final_x = int(x_offset + x / width_raw * width) + random.randint(-1, 1)
        final_y = int(y_offset + y / height_raw * height) + random.randint(-1, 1)
        
        logger.info(f"   实际点击坐标: ({final_x}, {final_y})")
        
        # 点击
        ActionChains(ctx.driver).move_to_element_with_offset(
            slide_bg, final_x, final_y
        ).click().perform()
        
        time.sleep(random.uniform(0.5, 1))


def compute_similarity(img1_path: str, img2_path: str) -> Tuple[float, int]:
    """计算两张图片的相似度"""
    try:
        img1 = cv2.imread(img1_path, cv2.IMREAD_GRAYSCALE)
        img2 = cv2.imread(img2_path, cv2.IMREAD_GRAYSCALE)
        
        if img1 is None or img2 is None:
            return 0.0, 0
        
        # 优先使用 SIFT，降级 ORB
        try:
            detector = cv2.SIFT_create()
            norm = cv2.NORM_L2
        except AttributeError:
            detector = cv2.ORB_create()
            norm = cv2.NORM_HAMMING
        
        kp1, des1 = detector.detectAndCompute(img1, None)
        kp2, des2 = detector.detectAndCompute(img2, None)
        
        if des1 is None or des2 is None:
            return 0.0, 0
        
        bf = cv2.BFMatcher(norm, crossCheck=False)
        matches = bf.knnMatch(des1, des2, k=2)
        
        good = []
        for match in matches:
            if len(match) == 2:
                m, n = match
                if m.distance < 0.8 * n.distance:
                    good.append(m)
        
        if not matches:
            return 0.0, 0
        
        similarity = len(good) / len(matches)
        return similarity, len(good)
        
    except Exception as e:
        logger.error(f"相似度计算失败: {e}")
        return 0.0, 0


def refresh_captcha(ctx) -> bool:
    """刷新验证码"""
    try:
        reload_btn = ctx.driver.find_element(By.ID, "reload")
        time.sleep(2)
        reload_btn.click()
        time.sleep(2)
        logger.info("✅ 验证码已刷新")
        return True
    except NoSuchElementException:
        logger.error("❌ 验证码刷新按钮未找到")
        return False
    except Exception as e:
        logger.error(f"❌ 刷新验证码失败: {e}")
        return False


def clear_temp_dir(temp_dir: str):
    """清空临时目录"""
    if not os.path.exists(temp_dir):
        os.makedirs(temp_dir, exist_ok=True)
        return
    
    for filename in os.listdir(temp_dir):
        file_path = os.path.join(temp_dir, filename)
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
        except Exception:
            pass


def get_url_from_style(style: str) -> str:
    """从 style 属性提取 URL"""
    if not style:
        raise ValueError("style 属性为空")
    
    match = re.search(r'url\(["\']?(.*?)["\']?\)', style)
    if not match:
        raise ValueError(f"无法从 style 中解析 URL: {style}")
    
    return match.group(1)


def get_width_from_style(style: str) -> float:
    """从 style 属性提取宽度"""
    if not style:
        raise ValueError("style 属性为空")
    
    match = re.search(r'width:\s*([\d.]+)px', style)
    if not match:
        raise ValueError(f"无法从 style 中解析宽度: {style}")
    
    return float(match.group(1))


def get_height_from_style(style: str) -> float:
    """从 style 属性提取高度"""
    if not style:
        raise ValueError("style 属性为空")
    
    match = re.search(r'height:\s*([\d.]+)px', style)
    if not match:
        raise ValueError(f"无法从 style 中解析高度: {style}")
    
    return float(match.group(1))

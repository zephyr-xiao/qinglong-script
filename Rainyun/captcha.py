import json
import logging
import os
import random
import re
import shutil
import time
from itertools import permutations
from typing import List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np
import requests
from selenium.common import TimeoutException, NoSuchElementException
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC

logger = logging.getLogger(__name__)

# 点选验证码固定要求依次点击 3 个图案
SPRITE_COUNT = 3

# 碎片有效性检查阈值：只拦空白/损坏图，不再用 OCR 猜数字
MIN_ICON_SIZE = 20          # 单个碎片最小边长（像素）
MIN_SEGMENT_STD = 5.0       # 灰度标准差下限，低于该值视为纯色
MIN_SEGMENT_LAPLACIAN = 5.0  # Laplacian 方差下限，低于该值视为无边缘

# 候选框数量上限：控制分配枚举规模（P(10,3)=720）
MAX_CANDIDATES = 10
UPSCALE_FACTOR = 1.5
IOU_DEDUPE = 0.5

# 失败留图目录（脚本同目录，青龙数据卷持久化）
DEBUG_DIR_NAME = "captcha_debug"


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
    # 记录最近一次尝试的现场，重试耗尽时用于留图
    last_state = {"reason": "未知", "boxes": None, "assignment": None, "sim_matrix": None}

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

        # 每次尝试重置现场，避免留图时把上一轮的框和这一轮的图片混在一起
        last_state.update({"boxes": None, "assignment": None, "sim_matrix": None})

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

            # 校验验证码图片是否可用
            logger.info("🔍 校验验证码碎片有效性...")
            if not check_captcha(ctx):
                raise CaptchaRetryableError("验证码碎片无效")
            logger.info("✅ 验证码碎片有效")

            # 识别验证码
            logger.info("🤖 开始识别验证码...")
            captcha = cv2.imread(ctx.temp_path("captcha.jpg"))
            if captcha is None:
                raise CaptchaRetryableError("验证码背景图读取失败")

            det_boxes = detect_candidates(ctx)
            logger.info(f"   检测到 {len(det_boxes)} 个候选区域")

            templates = []
            for sprite_idx in range(SPRITE_COUNT):
                template_path = ctx.temp_path(f"sprite_{sprite_idx+1}.jpg")
                templates.append(cv2.imread(template_path, cv2.IMREAD_GRAYSCALE))

            # 检测框可能漏掉细笔画图标，用 SIFT 定位/模板匹配补候选
            candidates = build_candidates(captcha, det_boxes, templates, ctx.config)
            logger.info(f"   候选框合计 {len(candidates)} 个")
            boxes = [candidate["box"] for candidate in candidates]
            last_state["boxes"] = boxes
            if len(boxes) < SPRITE_COUNT:
                raise CaptchaRetryableError(f"候选区域不足（{len(boxes)} < {SPRITE_COUNT}）")

            # 裁出候选框小图，供相似度计算使用
            crop_paths = []
            for idx, box in enumerate(boxes):
                crop = _crop_box(captcha, box)
                crop_path = ctx.temp_path(f"spec_{idx+1}.jpg")
                cv2.imwrite(crop_path, crop)
                crop_paths.append(crop_path)

            # 3 个碎片 × N 个候选框的相似度矩阵；定位框可用定位置信度补足低分
            sim_matrix = []
            for sprite_idx in range(SPRITE_COUNT):
                sprite_path = ctx.temp_path(f"sprite_{sprite_idx+1}.jpg")
                row = []
                for cand_idx, crop_path in enumerate(crop_paths):
                    base = compute_similarity(sprite_path, crop_path)[0]
                    bonus = candidates[cand_idx]["scores"].get(sprite_idx, 0.0)
                    row.append(max(base, bonus))
                sim_matrix.append(row)

            # 候选过多时按列最高分截断，控制分配枚举规模
            if len(boxes) > MAX_CANDIDATES:
                keep = sorted(
                    range(len(boxes)),
                    key=lambda j: max(row[j] for row in sim_matrix),
                    reverse=True,
                )[:MAX_CANDIDATES]
                boxes = [boxes[j] for j in keep]
                sim_matrix = [[row[j] for j in keep] for row in sim_matrix]
                last_state["boxes"] = boxes

            assignment, _ = assign_sprites(sim_matrix, config["similarity_threshold"])
            last_state["assignment"] = assignment
            last_state["sim_matrix"] = sim_matrix
            if assignment is None:
                raise CaptchaRetryableError("候选区域不足，无法完成分配")
            if not validate_assignment(sim_matrix, assignment, boxes, config["similarity_threshold"]):
                raise CaptchaRetryableError("验证码答案无效")

            # 还原成 click_captcha 需要的结构（顺序即点击顺序）
            result = {}
            for sprite_idx, box_idx in enumerate(assignment):
                x1, y1, x2, y2 = boxes[box_idx]
                result[f"sprite_{sprite_idx+1}.position"] = f"{int((x1+x2)/2)},{int((y1+y2)/2)}"
                result[f"sprite_{sprite_idx+1}.similarity"] = sim_matrix[sprite_idx][box_idx]

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

        except Exception as e:
            # 包含超时、图片解析、cv2/ddddocr 意外异常等，统一走刷新重试
            last_state["reason"] = _sanitize_reason(e)
            logger.error(f"❌ 验证码处理失败: {last_state['reason']}")
            # 每次失败都留现场，便于区分「检测框缺失」和「匹配分数偏低」
            dump_captcha_debug(ctx, last_state["reason"], last_state["boxes"],
                               last_state["assignment"], last_state["sim_matrix"])

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

        logger.info(f"   验证码背景图: {_brief_url(img1_url)}")
        if not download_image(img1_url, ctx.temp_path("captcha.jpg"), config):
            logger.error("   背景图下载失败")
            return False
        logger.info("   ✓ 背景图下载成功")

        # 下载碎片图
        sprite = ctx.wait.until(
            EC.visibility_of_element_located((By.XPATH, "//div[@id='instruction']//img"))
        )
        img2_url = sprite.get_attribute("src")

        logger.info(f"   验证码碎片图: {_brief_url(img2_url)}")
        if not download_image(img2_url, ctx.temp_path("sprite.jpg"), config):
            logger.error("   碎片图下载失败")
            return False
        logger.info("   ✓ 碎片图下载成功")

        return True

    except TimeoutException:
        logger.error("❌ 验证码图片加载超时")
        return False
    except Exception as e:
        logger.error(f"❌ 验证码图片下载失败: {_sanitize_reason(e)}")
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
            # 不直接记录异常原文：requests 的 HTTPError 会带上含 sess 的完整 URL
            brief = _brief_url(url)
            if attempt < max_retries:
                logger.warning(
                    f"   下载失败 (第 {attempt} 次): {type(e).__name__} {brief}，{retry_delay}秒后重试..."
                )
                time.sleep(retry_delay)
            else:
                logger.error(f"   下载失败 (已重试 {max_retries} 次): {type(e).__name__} {brief}")
                return False


def check_captcha(ctx) -> bool:
    """校验验证码图片是否可用（只拦空白/损坏图，不再用 OCR 猜数字）"""
    try:
        raw = cv2.imread(ctx.temp_path("sprite.jpg"))
        if raw is None:
            logger.error("   验证码碎片图读取失败")
            return False

        height, width = raw.shape[:2]
        if width < SPRITE_COUNT * MIN_ICON_SIZE or height < MIN_ICON_SIZE:
            logger.error(f"   验证码碎片图尺寸异常: {width}x{height}")
            return False

        # 分割碎片并逐段检查是否有实际内容
        segment_width = width // SPRITE_COUNT
        for i in range(SPRITE_COUNT):
            start = segment_width * i
            end = width if i == SPRITE_COUNT - 1 else segment_width * (i + 1)
            segment = raw[:, start:end]
            cv2.imwrite(ctx.temp_path(f"sprite_{i+1}.jpg"), segment)
            if not _image_has_content(cv2.cvtColor(segment, cv2.COLOR_BGR2GRAY)):
                logger.warning(f"   碎片 {i+1} 内容异常（疑似空白/损坏）")
                return False

        background = cv2.imread(ctx.temp_path("captcha.jpg"))
        if background is None:
            logger.error("   验证码背景图读取失败")
            return False
        bg_height, bg_width = background.shape[:2]
        if bg_width < 100 or bg_height < 100:
            logger.error(f"   验证码背景图尺寸异常: {bg_width}x{bg_height}")
            return False
        # 背景图只校验尺寸：真实背景可能是低纹理的纯色/渐变照片，
        # 用碎片的内容阈值去判会误杀；背景是否可用交给后续检测环节判断

        logger.info("   ✓ 所有碎片有效")
        return True

    except Exception as e:
        logger.error(f"   验证码碎片校验失败: {e}")
        return False


def _image_has_content(gray) -> bool:
    """判断图像是否有实际内容：既不能是纯色，也不能几乎没有边缘。"""
    _, std_dev = cv2.meanStdDev(gray)
    if float(std_dev[0][0]) < MIN_SEGMENT_STD:
        return False
    laplacian_var = cv2.Laplacian(gray, cv2.CV_64F).var()
    return laplacian_var >= MIN_SEGMENT_LAPLACIAN


def detect_candidates(ctx) -> List[Tuple[int, int, int, int]]:
    """检测背景图中的图标候选框；不足 3 个时放大重试并合并去重。"""
    with open(ctx.temp_path("captcha.jpg"), "rb") as f:
        image_bytes = f.read()

    boxes = _normalize_boxes(ctx.det.detection(image_bytes))
    boxes = _dedupe_boxes(boxes)

    if len(boxes) >= SPRITE_COUNT or not ctx.config.get("captcha_upscale_fallback", True):
        return boxes

    logger.info(f"   候选框不足（{len(boxes)} 个），尝试放大 {UPSCALE_FACTOR}× 重新检测")
    image = cv2.imread(ctx.temp_path("captcha.jpg"))
    if image is None:
        return boxes

    enlarged = cv2.resize(
        image, None, fx=UPSCALE_FACTOR, fy=UPSCALE_FACTOR, interpolation=cv2.INTER_CUBIC
    )
    ok, buffer = cv2.imencode(".jpg", enlarged)
    if not ok:
        return boxes

    extra = []
    for x1, y1, x2, y2 in _normalize_boxes(ctx.det.detection(buffer.tobytes())):
        extra.append((
            int(x1 / UPSCALE_FACTOR),
            int(y1 / UPSCALE_FACTOR),
            int(x2 / UPSCALE_FACTOR),
            int(y2 / UPSCALE_FACTOR),
        ))

    merged = _dedupe_boxes(boxes + extra)
    logger.info(f"   放大重试后候选框: {len(merged)} 个")
    return merged


def _normalize_boxes(boxes) -> List[Tuple[int, int, int, int]]:
    """把检测结果转成合法整数框，并丢掉宽高非正的框。"""
    result = []
    for box in boxes:
        x1, y1, x2, y2 = (int(v) for v in box)
        if x2 > x1 and y2 > y1:
            result.append((x1, y1, x2, y2))
    return result


def _dedupe_boxes(
    boxes: List[Tuple[int, int, int, int]], iou_threshold: float = IOU_DEDUPE
) -> List[Tuple[int, int, int, int]]:
    """按 IoU 去重，保留先出现的框。"""
    kept = []
    for box in boxes:
        if all(_box_iou(box, existing) <= iou_threshold for existing in kept):
            kept.append(box)
    return kept


def _box_iou(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
    """计算两个框的交并比。"""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    inter_width = max(0, min(ax2, bx2) - max(ax1, bx1))
    inter_height = max(0, min(ay2, by2) - max(ay1, by1))
    inter_area = inter_width * inter_height
    union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter_area
    return inter_area / union if union > 0 else 0.0


def _crop_box(image, box: Tuple[int, int, int, int], padding: int = 2):
    """按框裁剪图像，带少量内边距并做边界裁剪。"""
    height, width = image.shape[:2]
    x1, y1, x2, y2 = box
    x1 = max(0, x1 - padding)
    y1 = max(0, y1 - padding)
    x2 = min(width, x2 + padding)
    y2 = min(height, y2 + padding)
    return image[y1:y2, x1:x2]


def sift_localize(
    background_gray,
    template_gray,
    cluster_radius: int = 50,
    min_cluster: int = 3,
) -> Optional[dict]:
    """用 SIFT 特征把碎片匹配到整张背景，再对匹配点聚类定位漏检图标。

    ddddocr 的检测框只覆盖「像文字的区域」，细笔画数字画在复杂背景上时经常漏检；
    这里直接用碎片自身的特征在整张背景上找位置，作为候选框补充。
    """
    try:
        sift = cv2.SIFT_create()
    except Exception:
        return None

    keypoints_bg, descriptors_bg = sift.detectAndCompute(background_gray, None)
    keypoints_tpl, descriptors_tpl = sift.detectAndCompute(template_gray, None)
    if (
        descriptors_bg is None
        or descriptors_tpl is None
        or len(keypoints_bg) == 0
        or len(keypoints_tpl) == 0
    ):
        return None

    matches = cv2.BFMatcher(cv2.NORM_L2).knnMatch(descriptors_tpl, descriptors_bg, k=2)
    good = []
    for pair in matches:
        if len(pair) != 2:
            continue
        best, second = pair
        if best.distance < 0.75 * second.distance:
            good.append(best)
    if len(good) < min_cluster:
        return None

    points = np.array([keypoints_bg[m.trainIdx].pt for m in good], dtype=np.float32)

    # 以每个匹配点为种子统计邻域点数，取最大簇
    best_center = None
    best_count = 0
    for point in points:
        count = int((np.linalg.norm(points - point, axis=1) < cluster_radius).sum())
        if count > best_count:
            best_count = count
            best_center = point
    if best_count < min_cluster:
        return None

    height, width = template_gray.shape[:2]
    center_x, center_y = float(best_center[0]), float(best_center[1])
    box = (
        int(center_x - width / 2),
        int(center_y - height / 2),
        int(center_x + width / 2),
        int(center_y + height / 2),
    )
    return {
        "box": box,
        # 簇内匹配点占模板关键点的比例，作为该碎片在此位置的置信度
        "score": min(1.0, best_count / max(1, len(keypoints_tpl))),
        "center": (int(center_x), int(center_y)),
        "cluster": best_count,
        "keypoints": len(keypoints_tpl),
    }


def template_localize(
    background_gray,
    template_gray,
    scales: Tuple[float, ...] = (0.7, 0.85, 1.0, 1.15, 1.3, 1.5),
    min_score: float = 0.35,
) -> Optional[dict]:
    """SIFT 定位失败时的兜底：Canny 边缘多尺度模板匹配。"""
    background_edges = cv2.Canny(background_gray, 50, 150)
    best = None
    for scale in scales:
        template = cv2.resize(
            template_gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA
        )
        if (
            template.shape[0] >= background_edges.shape[0]
            or template.shape[1] >= background_edges.shape[1]
        ):
            continue
        result = cv2.matchTemplate(
            background_edges, cv2.Canny(template, 50, 150), cv2.TM_CCOEFF_NORMED
        )
        _, max_value, _, max_loc = cv2.minMaxLoc(result)
        if best is None or max_value > best["score"]:
            best = {
                "box": (
                    int(max_loc[0]),
                    int(max_loc[1]),
                    int(max_loc[0] + template.shape[1]),
                    int(max_loc[1] + template.shape[0]),
                ),
                "score": float(max_value),
                "scale": scale,
            }
    if best is None or best["score"] < min_score:
        return None
    return best


def _box_area(box: Tuple[int, int, int, int]) -> int:
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def _box_intersection(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> int:
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0, min(a[3], b[3]) - max(a[1], b[1])
    )


def _same_region(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> bool:
    """两个框是否指向同一区域：IoU > 0.5，或小框 80% 以上落在大框内。"""
    intersection = _box_intersection(a, b)
    if intersection <= 0:
        return False
    area_a, area_b = _box_area(a), _box_area(b)
    union = area_a + area_b - intersection
    if union > 0 and intersection / union > 0.5:
        return True
    smaller = min(area_a, area_b)
    return smaller > 0 and intersection / smaller > 0.8


def _clamp_box(
    box: Tuple[int, int, int, int], width: int, height: int
) -> Tuple[int, int, int, int]:
    x1, y1, x2, y2 = box
    return (
        max(0, min(width, x1)),
        max(0, min(height, y1)),
        max(0, min(width, x2)),
        max(0, min(height, y2)),
    )


def build_candidates(
    background,
    det_boxes: List[Tuple[int, int, int, int]],
    templates: List,
    config: dict,
) -> List[dict]:
    """合并 ddddocr 检测框与定位补充框。

    返回的每个候选是 {"box": (...), "scores": {碎片下标: 定位置信度}}；
    定位置信度只在「该碎片确实由这个框定位到」时存在，用于补足 SIFT
    在低关键点图标上偏低的相似度。
    """
    height, width = background.shape[:2]
    candidates = []
    for box in det_boxes:
        clamped = _clamp_box(box, width, height)
        if _box_area(clamped) > 0:
            candidates.append({"box": clamped, "scores": {}})

    background_gray = cv2.cvtColor(background, cv2.COLOR_BGR2GRAY)
    use_sift = config.get("captcha_sift_localize", True)
    use_template = config.get("captcha_template_fallback", True)

    for sprite_idx, template in enumerate(templates):
        if template is None or template.size == 0:
            continue
        located = sift_localize(background_gray, template) if use_sift else None
        if located is None and use_template:
            located = template_localize(background_gray, template)
        if located is None:
            continue

        box = _clamp_box(located["box"], width, height)
        if _box_area(box) <= 0:
            continue
        _add_candidate(candidates, box, sprite_idx, located["score"])
        logger.info(
            f"   图案 {sprite_idx+1} 定位补充: 位置={box}, "
            f"置信度={located['score']:.4f}"
        )

    _drop_contained(candidates)
    return candidates


def _add_candidate(candidates: List[dict], box, sprite_idx: int, score: float) -> None:
    """把定位框并入已有候选（同一区域则合并分数），否则新增。"""
    for candidate in candidates:
        if _same_region(candidate["box"], box):
            candidate["scores"][sprite_idx] = max(
                candidate["scores"].get(sprite_idx, 0.0), score
            )
            return
    candidates.append({"box": box, "scores": {sprite_idx: score}})


def _drop_contained(candidates: List[dict]) -> None:
    """去掉被更大候选框包含的小框（检测框里常见的嵌套子区域），分数转移给大框。"""
    keep = []
    for candidate in sorted(
        candidates, key=lambda item: _box_area(item["box"]), reverse=True
    ):
        contained = False
        for existing in keep:
            intersection = _box_intersection(candidate["box"], existing["box"])
            smaller = _box_area(candidate["box"])
            if smaller > 0 and intersection / smaller > 0.8:
                for sprite_idx, score in candidate["scores"].items():
                    existing["scores"][sprite_idx] = max(
                        existing["scores"].get(sprite_idx, 0.0), score
                    )
                contained = True
                break
        if not contained:
            keep.append(candidate)
    candidates[:] = keep


def assign_sprites(
    sim_matrix: List[List[float]],
    threshold: Optional[float] = None,
) -> Tuple[Optional[Tuple[int, ...]], float]:
    """在「每个碎片对应不同候选框」的约束下，取总相似度最大的分配。

    暴力枚举所有排列（候选框上限 10 时最多 720 种），保证不会出现
    三个碎片抢同一个框的情况。传入 threshold 时，优先选择「每条边都达标」
    的分配，避免总分最高但含一条低于阈值的边、白白浪费一次尝试。
    """
    sprite_count = len(sim_matrix)
    box_count = len(sim_matrix[0]) if sprite_count else 0
    if box_count < sprite_count:
        return None, 0.0

    best_assignment = None
    best_total = -1.0
    best_valid_assignment = None
    best_valid_total = -1.0

    for columns in permutations(range(box_count), sprite_count):
        total = sum(sim_matrix[i][columns[i]] for i in range(sprite_count))
        if total > best_total:
            best_assignment = columns
            best_total = total

        if threshold is not None and all(
            sim_matrix[i][columns[i]] >= threshold for i in range(sprite_count)
        ):
            if total > best_valid_total:
                best_valid_assignment = columns
                best_valid_total = total

    if best_valid_assignment is not None:
        return best_valid_assignment, best_valid_total
    return best_assignment, best_total


def validate_assignment(
    sim_matrix: List[List[float]],
    assignment: Tuple[int, ...],
    boxes: List[Tuple[int, int, int, int]],
    threshold: float,
) -> bool:
    """校验分配结果：每个碎片匹配率达标，并输出次优候选便于排查。"""
    valid = True
    for sprite_idx, box_idx in enumerate(assignment):
        similarity = sim_matrix[sprite_idx][box_idx]
        x1, y1, x2, y2 = boxes[box_idx]
        position = f"{int((x1+x2)/2)},{int((y1+y2)/2)}"

        alternatives = sorted(
            ((sim_matrix[sprite_idx][j], j) for j in range(len(boxes)) if j != box_idx),
            reverse=True,
        )
        alt_text = ""
        if alternatives:
            alt_similarity, alt_idx = alternatives[0]
            ax1, ay1, ax2, ay2 = boxes[alt_idx]
            alt_text = f"，次优 {int((ax1+ax2)/2)},{int((ay1+ay2)/2)} ({alt_similarity:.4f})"

        if similarity >= threshold:
            logger.info(f"   图案 {sprite_idx+1}: 位置={position}, 匹配率={similarity:.4f}{alt_text}")
        else:
            logger.error(
                f"   图案 {sprite_idx+1}: 位置={position}, 匹配率={similarity:.4f} "
                f"低于阈值 {threshold}{alt_text}"
            )
            valid = False

    if valid:
        logger.info("   ✓ 验证码答案有效")
    return valid


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
    for i in range(SPRITE_COUNT):
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
    """计算两张图片的相似度（SIFT 优先，关键点过少时整对改用 ORB）"""
    try:
        img1 = cv2.imread(img1_path, cv2.IMREAD_GRAYSCALE)
        img2 = cv2.imread(img2_path, cv2.IMREAD_GRAYSCALE)

        if img1 is None or img2 is None:
            return 0.0, 0

        detectors = []
        if hasattr(cv2, "SIFT_create"):
            detectors.append((cv2.SIFT_create, cv2.NORM_L2))
        detectors.append((cv2.ORB_create, cv2.NORM_HAMMING))

        for factory, norm in detectors:
            try:
                detector = factory()
            except Exception:
                continue

            keypoints1, descriptors1 = detector.detectAndCompute(img1, None)
            keypoints2, descriptors2 = detector.detectAndCompute(img2, None)

            # 只要有描述子就尝试匹配：旧实现用 1-3 个关键点也能正确匹配简单图标；
            # 1 vs N 时 Lowe ratio 仍可区分，1 vs 1 会因凑不出第二邻居自然得 0
            if (
                descriptors1 is None
                or descriptors2 is None
                or len(keypoints1) == 0
                or len(keypoints2) == 0
            ):
                continue

            similarity, good = _match_ratio(descriptors1, descriptors2, norm)
            if similarity > 0:
                return similarity, good

        return 0.0, 0

    except Exception as e:
        logger.error(f"相似度计算失败: {e}")
        return 0.0, 0


def _match_ratio(descriptors1, descriptors2, norm) -> Tuple[float, int]:
    """按 Lowe ratio 统计有效匹配占比。"""
    matcher = cv2.BFMatcher(norm, crossCheck=False)
    matches = matcher.knnMatch(descriptors1, descriptors2, k=2)

    good = 0
    total = 0
    for match in matches:
        if len(match) != 2:
            continue
        total += 1
        best, second = match
        if best.distance < 0.8 * second.distance:
            good += 1

    if total == 0:
        return 0.0, 0
    return good / total, good


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
        # 不把 style 原文写进异常：里面就是带 sess 的图片 URL
        raise ValueError("无法从 style 中解析 URL（原始内容已省略）")

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


def _brief_url(url: str) -> str:
    """日志只保留 img_index/image 参数，避免整段 sess（或 data URI）进日志。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"<{parsed.scheme or 'unknown'} 图片>"
    query = parse_qs(parsed.query)
    return (
        f"{parsed.path}?img_index={query.get('img_index', [''])[0]}"
        f"&image={query.get('image', [''])[0]}"
    )


def _sanitize_reason(error: Exception) -> str:
    """异常摘要用于日志/落盘：去掉 URL 查询串等可能含会话凭据的内容。"""
    text = f"{type(error).__name__}: {error}"
    text = re.sub(r"https?://\S+", "<url>", text)
    return text[:200]


def _debug_dir() -> str:
    """验证码调试图目录（脚本同目录）。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), DEBUG_DIR_NAME)


def dump_captcha_debug(
    ctx,
    reason: str,
    boxes=None,
    assignment=None,
    sim_matrix=None,
) -> None:
    """验证码最终失败时落盘现场，便于后续调参。"""
    if not ctx.config.get("captcha_debug", True):
        return
    try:
        # 目录名带毫秒，避免同一秒内多次失败互相覆盖
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
        target = os.path.join(_debug_dir(), stamp)
        os.makedirs(target, exist_ok=True)

        for name in ("captcha.jpg", "sprite.jpg"):
            source = ctx.temp_path(name)
            if os.path.exists(source):
                shutil.copy2(source, os.path.join(target, name))

        # 标注候选框（绿）与最终分配（红，数字为点击顺序）
        background = cv2.imread(ctx.temp_path("captcha.jpg"))
        if background is not None and boxes:
            for x1, y1, x2, y2 in boxes:
                cv2.rectangle(background, (x1, y1), (x2, y2), (0, 255, 0), 1)
            if assignment:
                for sprite_idx, box_idx in enumerate(assignment):
                    x1, y1, x2, y2 = boxes[box_idx]
                    cv2.rectangle(background, (x1, y1), (x2, y2), (0, 0, 255), 2)
                    cv2.putText(
                        background, str(sprite_idx + 1), (x1 + 2, y1 + 14),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1,
                    )
            cv2.imwrite(os.path.join(target, "annotated.jpg"), background)

        meta = {
            "reason": reason,
            "threshold": ctx.config.get("similarity_threshold"),
            "boxes": boxes,
            "assignment": assignment,
            "similarity": sim_matrix,
        }
        with open(os.path.join(target, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        _prune_debug_dirs(ctx.config.get("captcha_debug_keep", 10))
        logger.info(f"🧪 验证码调试图已保存: {target}")
    except Exception as e:
        logger.warning(f"⚠️ 保存验证码调试图失败: {e}")


def _prune_debug_dirs(keep: int) -> None:
    """只保留最近 keep 次调试图，避免长期运行占满磁盘。"""
    try:
        keep = max(1, int(keep))
    except (TypeError, ValueError):
        keep = 10
    try:
        root = _debug_dir()
        if not os.path.isdir(root):
            return
        directories = sorted(
            (os.path.join(root, name) for name in os.listdir(root)
             if os.path.isdir(os.path.join(root, name))),
            reverse=True,
        )
        for old in directories[keep:]:
            shutil.rmtree(old, ignore_errors=True)
    except Exception:
        pass

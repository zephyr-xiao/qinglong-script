"""验证码算法离线单测：不联网、不启动浏览器。

运行环境需要 cv2/numpy（青龙容器内已具备）：
    python3 test_captcha.py
"""
import os
import sys
import tempfile
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import captcha


class FakeCtx:
    def __init__(self, temp_dir, config=None, det=None):
        self.temp_dir = temp_dir
        self.config = config or {"captcha_upscale_fallback": True, "captcha_debug": False}
        self.det = det

    def temp_path(self, name):
        return os.path.join(self.temp_dir, name)


def make_icon(seed, size=60):
    """生成带纹理的合成图标，保证 SIFT/ORB 有足够关键点。"""
    rng = np.random.default_rng(seed)
    img = np.full((size, size, 3), 255, np.uint8)
    for _ in range(8):
        pts = rng.integers(4, size - 4, size=(3, 2)).astype(np.int32)
        color = tuple(int(c) for c in rng.integers(0, 255, size=3))
        cv2.fillPoly(img, [pts], color)
    noise = rng.integers(0, 80, size=img.shape, dtype=np.uint8)
    return cv2.addWeighted(img, 0.85, noise, 0.15, 0)


def test_assignment():
    # 三个碎片各自的最优都是 0 号框（旧贪心必然撞车），全局分配存在唯一解
    sim = [
        [0.90, 0.85, 0.10],
        [0.88, 0.20, 0.15],
        [0.86, 0.30, 0.70],
    ]
    greedy = [max(range(3), key=lambda j: row[j]) for row in sim]
    assert greedy == [0, 0, 0], greedy

    assignment, total = captcha.assign_sprites(sim)
    assert assignment == (1, 0, 2), assignment
    assert len(set(assignment)) == 3
    assert abs(total - 2.43) < 1e-9, total

    boxes = [(0, 0, 10, 10), (20, 20, 30, 30), (40, 40, 50, 50)]
    assert captcha.validate_assignment(sim, assignment, boxes, 0.4)
    assert not captcha.validate_assignment(sim, assignment, boxes, 0.9)


def test_assignment_prefers_all_passing():
    # 总分最高的 (1,2,0) 含一条 0.10 的边；阈值优先应选全部达标的 (1,3,2)
    sim = [
        [0.41, 0.90, 0.00, 0.00],
        [0.00, 0.00, 0.90, 0.42],
        [0.10, 0.00, 0.41, 0.00],
    ]
    best_total, _ = captcha.assign_sprites(sim)
    assert best_total == (1, 2, 0), best_total

    assignment, _ = captcha.assign_sprites(sim, threshold=0.4)
    assert assignment == (1, 3, 2), assignment
    assert all(sim[i][assignment[i]] >= 0.4 for i in range(3))


class FakeDetector:
    """按调用顺序返回指定关键点数量的假检测器，用于验证低关键点保护。"""

    def __init__(self, counts):
        self.counts = list(counts)
        self.calls = 0

    def detectAndCompute(self, image, mask):
        count = self.counts[min(self.calls, len(self.counts) - 1)]
        self.calls += 1
        if count == 0:
            return [], None
        base = np.arange(8, dtype=np.float32)
        rows = [base + i * 100 for i in range(count)]
        return [object()] * count, np.vstack(rows)


def test_low_keypoint_guard(tmp):
    # 旧实现要求关键点 >=4；这里覆盖 3 vs 3 与 1 vs 3 两种简单图标场景
    original_sift = cv2.SIFT_create
    original_orb = cv2.ORB_create
    try:
        cv2.ORB_create = lambda: FakeDetector([0])

        path1 = os.path.join(tmp, "low1.png")
        path2 = os.path.join(tmp, "low2.png")
        cv2.imwrite(path1, np.zeros((20, 20), np.uint8))
        cv2.imwrite(path2, np.zeros((20, 20), np.uint8))

        for counts in ([3, 3], [1, 3]):
            cv2.SIFT_create = lambda counts=counts: FakeDetector(counts)
            similarity, _ = captcha.compute_similarity(path1, path2)
            assert similarity > 0.4, (counts, similarity)
    finally:
        cv2.SIFT_create = original_sift
        cv2.ORB_create = original_orb


def test_similarity(tmp):
    icon_a = make_icon(1)
    icon_b = make_icon(2)
    path_a = os.path.join(tmp, "icon_a.png")
    path_b = os.path.join(tmp, "icon_b.png")
    cv2.imwrite(path_a, icon_a)
    cv2.imwrite(path_b, icon_b)

    rng = np.random.default_rng(3)
    background = rng.integers(40, 200, size=(120, 120, 3), dtype=np.uint8)
    background[30:90, 30:90] = icon_a
    background_path = os.path.join(tmp, "bg.png")
    cv2.imwrite(background_path, background)

    sim_a, _ = captcha.compute_similarity(path_a, background_path)
    sim_b, _ = captcha.compute_similarity(path_b, background_path)
    assert sim_a > 0.4, sim_a
    assert sim_a > sim_b + 0.1, (sim_a, sim_b)


def test_quality_check(tmp):
    sprite = np.hstack([make_icon(10), make_icon(11), make_icon(12)])
    cv2.imwrite(os.path.join(tmp, "sprite.jpg"), sprite)
    rng = np.random.default_rng(4)
    cv2.imwrite(
        os.path.join(tmp, "captcha.jpg"),
        rng.integers(0, 255, size=(240, 320, 3), dtype=np.uint8),
    )
    ctx = FakeCtx(tmp)
    assert captcha.check_captcha(ctx), "正常碎片被误判"

    cv2.imwrite(os.path.join(tmp, "sprite.jpg"), np.full((60, 180, 3), 255, np.uint8))
    assert not captcha.check_captcha(ctx), "空白碎片未被拦截"

    broken = np.hstack([make_icon(13), make_icon(14), np.full((60, 60, 3), 255, np.uint8)])
    cv2.imwrite(os.path.join(tmp, "sprite.jpg"), broken)
    assert not captcha.check_captcha(ctx), "残缺碎片未被拦截"


class FakeDet:
    def __init__(self):
        self.calls = 0

    def detection(self, image_bytes):
        self.calls += 1
        if self.calls == 1:
            return [[10, 10, 50, 50], [60, 60, 100, 100]]
        return [[15, 15, 75, 75], [90, 90, 150, 150], [180, 180, 240, 240]]


def test_upscale_fallback(tmp):
    rng = np.random.default_rng(5)
    cv2.imwrite(
        os.path.join(tmp, "captcha.jpg"),
        rng.integers(0, 255, size=(200, 200, 3), dtype=np.uint8),
    )

    detector = FakeDet()
    ctx = FakeCtx(tmp, {"captcha_upscale_fallback": True, "captcha_debug": False}, detector)
    boxes = captcha.detect_candidates(ctx)
    assert detector.calls == 2, detector.calls
    assert len(boxes) == 3, boxes
    assert (120, 120, 160, 160) in boxes, boxes

    detector2 = FakeDet()
    ctx2 = FakeCtx(tmp, {"captcha_upscale_fallback": False, "captcha_debug": False}, detector2)
    assert captcha.detect_candidates(ctx2) == [(10, 10, 50, 50), (60, 60, 100, 100)]


def test_helpers():
    assert len(captcha._dedupe_boxes([(0, 0, 10, 10), (1, 1, 11, 11)])) == 1

    url = "https://turing.captcha.qcloud.com/x?img_index=1&image=abc123&sess=SECRET&tkid=9"
    brief = captcha._brief_url(url)
    assert "SECRET" not in brief and "sess" not in brief
    assert "img_index=1" in brief and "image=abc123" in brief

    data_uri = "data:image/png;base64," + "A" * 500
    assert "base64" not in captcha._brief_url(data_uri)

    reason = captcha._sanitize_reason(RuntimeError("HTTP 403 https://x/y?sess=SECRET"))
    assert "SECRET" not in reason and "<url>" in reason

    assert captcha._image_has_content(
        np.random.default_rng(6).integers(0, 255, size=(60, 60), dtype=np.uint8)
    )
    assert not captcha._image_has_content(np.zeros((60, 60), np.uint8))


def test_debug_dump(tmp):
    ctx = FakeCtx(tmp, {"captcha_debug": True, "captcha_debug_keep": 2, "similarity_threshold": 0.4})
    rng = np.random.default_rng(7)
    cv2.imwrite(os.path.join(tmp, "captcha.jpg"), rng.integers(0, 255, size=(120, 160, 3), dtype=np.uint8))
    cv2.imwrite(os.path.join(tmp, "sprite.jpg"), np.hstack([make_icon(20), make_icon(21), make_icon(22)]))

    # 指向临时目录，绝不能碰生产环境的 captcha_debug/
    debug_root = os.path.join(tmp, "captcha_debug")
    original_debug_dir = captcha._debug_dir
    captcha._debug_dir = lambda: debug_root
    try:
        for _ in range(3):
            captcha.dump_captcha_debug(
                ctx, "test-reason", boxes=[(10, 10, 40, 40)], assignment=(0,), sim_matrix=[[0.5]]
            )
            time.sleep(1.1)

        directories = sorted(os.listdir(debug_root))
        assert len(directories) == 2, directories
        latest = os.path.join(debug_root, directories[-1])
        assert os.path.exists(os.path.join(latest, "annotated.jpg"))
        assert os.path.exists(os.path.join(latest, "meta.json"))

        # keep=0 应归一化为 1，不能把刚写的目录也删掉
        captcha._prune_debug_dirs(0)
        assert len(os.listdir(debug_root)) == 1
    finally:
        captcha._debug_dir = original_debug_dir


def main():
    with tempfile.TemporaryDirectory(prefix="captcha-test-") as tmp:
        test_assignment()
        print("assignment: ok")
        test_assignment_prefers_all_passing()
        print("assignment threshold: ok")
        test_low_keypoint_guard(tmp)
        print("low keypoint guard: ok")
        test_similarity(tmp)
        print("similarity: ok")
        test_quality_check(tmp)
        print("quality check: ok")
        test_upscale_fallback(tmp)
        print("upscale fallback: ok")
        test_helpers()
        print("helpers: ok")
        test_debug_dump(tmp)
        print("debug dump/prune: ok")
    print("ALL_TESTS_PASSED")


if __name__ == "__main__":
    main()

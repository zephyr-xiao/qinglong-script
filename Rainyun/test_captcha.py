"""验证码算法离线单测：不联网、不启动浏览器。

运行环境需要 cv2/numpy（青龙容器内已具备）：
    python3 test_captcha.py
"""
import os
import sys
import tempfile
import time
import types

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _install_playwright_stub():
    """离线单测环境可能没装 playwright/ddddocr，注入最小 stub 让模块能导入。

    captcha.py 与 main.py 顶层都 import playwright，缺依赖会在 import 阶段直接失败。
    这里只补齐导入所需的名字，不参与任何实际逻辑。

    仅在真实依赖确实缺失时注入：已安装 playwright 的环境不应被假模块遮蔽，
    否则将来想测真实依赖的用例会拿到 stub。
    """
    import importlib.util

    class _Dummy:
        def __init__(self, *args, **kwargs):
            pass

    class _StubTimeoutError(Exception):
        """模拟 playwright.sync_api.TimeoutError。"""

    def _stub(name, attrs=None):
        module = types.ModuleType(name)
        for attr, value in (attrs or {}).items():
            setattr(module, attr, value)
        sys.modules.setdefault(name, module)

    # ddddocr 与 playwright 独立判断：装了 playwright 但没装 ddddocr 的环境
    # （全新开发机常见）同样需要 ddddocr stub，不能被前面的提前 return 跳过
    if importlib.util.find_spec("ddddocr") is None:
        _stub("ddddocr", {"DdddOcr": _Dummy})

    if importlib.util.find_spec("playwright") is not None:
        return
    _stub("playwright")
    _stub("playwright.sync_api", {
        "TimeoutError": _StubTimeoutError,
        "sync_playwright": _Dummy,
    })


_install_playwright_stub()
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


def test_candidate_merge():
    candidates = [
        {"box": (10, 10, 100, 100), "scores": {}},
        {"box": (20, 20, 60, 60), "scores": {}},
    ]
    captcha._drop_contained(candidates)
    assert len(candidates) == 1, candidates
    assert candidates[0]["box"] == (10, 10, 100, 100)

    candidates = [{"box": (10, 10, 100, 100), "scores": {}}]
    captcha._add_candidate(candidates, (12, 12, 98, 98), 0, 0.8)
    assert len(candidates) == 1
    assert candidates[0]["scores"][0] == 0.8


def _paste(background, icon, x, y, scale=1.0):
    height, width = icon.shape[:2]
    resized = cv2.resize(icon, (int(width * scale), int(height * scale)))
    background[y:y + resized.shape[0], x:x + resized.shape[1]] = resized
    return (x, y, x + resized.shape[1], y + resized.shape[0])


def _textured_background(seed, size=(480, 672)):
    rng = np.random.default_rng(seed)
    background = np.zeros((size[0], size[1], 3), np.uint8)
    background[:] = (40, 55, 70)
    for _ in range(30):
        x, y = int(rng.integers(0, size[1] - 80)), int(rng.integers(0, size[0] - 80))
        w, h = int(rng.integers(40, 160)), int(rng.integers(40, 160))
        color = tuple(int(c) for c in rng.integers(20, 160, size=3))
        cv2.rectangle(background, (x, y), (x + w, y + h), color, -1)
    return background


def test_sift_localize(tmp):
    icons = [make_icon(30), make_icon(31), make_icon(32)]
    background = _textured_background(33)
    positions = [(80, 80, 1.0), (300, 200, 1.2), (500, 350, 0.9)]
    for icon, (x, y, scale) in zip(icons, positions):
        _paste(background, icon, x, y, scale)

    gray = cv2.cvtColor(background, cv2.COLOR_BGR2GRAY)
    for icon, (x, y, scale) in zip(icons, positions):
        template = cv2.cvtColor(icon, cv2.COLOR_BGR2GRAY)
        result = captcha.sift_localize(gray, template)
        assert result is not None, (x, y, scale)
        expected_x = x + icon.shape[1] * scale / 2
        expected_y = y + icon.shape[0] * scale / 2
        assert abs(result["center"][0] - expected_x) < 45, result
        assert abs(result["center"][1] - expected_y) < 45, result
        assert result["score"] > 0.3, result


def test_hybrid_recovery(tmp):
    icons = [make_icon(40), make_icon(41), make_icon(42)]
    background = _textured_background(43)
    positions = [(80, 80), (300, 200), (500, 350)]
    boxes = [_paste(background, icon, x, y) for icon, (x, y) in zip(icons, positions)]

    # 只给前两个图标检测框，第三个模拟 ddddocr 漏检
    det_boxes = boxes[:2]
    templates = [cv2.cvtColor(icon, cv2.COLOR_BGR2GRAY) for icon in icons]
    candidates = captcha.build_candidates(
        background,
        det_boxes,
        templates,
        {"captcha_sift_localize": True, "captcha_template_fallback": False},
    )
    assert len(candidates) >= 3, candidates

    crop_paths = []
    for idx, candidate in enumerate(candidates):
        crop = captcha._crop_box(background, candidate["box"])
        path = os.path.join(tmp, f"hybrid_{idx}.jpg")
        cv2.imwrite(path, crop)
        crop_paths.append(path)

    matrix = []
    for index in range(3):
        template_path = os.path.join(tmp, f"hybrid_tpl_{index}.jpg")
        cv2.imwrite(template_path, templates[index])
        row = []
        for cand_idx, crop_path in enumerate(crop_paths):
            base = captcha.compute_similarity(template_path, crop_path)[0]
            bonus = candidates[cand_idx]["scores"].get(index, 0.0)
            row.append(max(base, bonus))
        matrix.append(row)

    assignment, _ = captcha.assign_sprites(matrix, 0.4)
    assert assignment is not None
    assert all(matrix[i][assignment[i]] >= 0.4 for i in range(3)), (assignment, matrix)


def test_template_localize(tmp):
    icon = make_icon(50)
    background = _textured_background(51)
    # 按 1.5 倍粘贴，验证多尺度搜索
    box = _paste(background, icon, 200, 150, scale=1.5)
    gray = cv2.cvtColor(background, cv2.COLOR_BGR2GRAY)
    template = cv2.cvtColor(icon, cv2.COLOR_BGR2GRAY)
    result = captcha.template_localize(gray, template)
    assert result is not None, "模板匹配未找到图标"
    x1, y1, x2, y2 = result["box"]
    center = ((x1 + x2) // 2, (y1 + y2) // 2)
    expected = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
    assert abs(center[0] - expected[0]) < 30, (center, expected)
    assert abs(center[1] - expected[1]) < 30, (center, expected)
    assert result["score"] > 0.6, result


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


def _load_main_module():
    """导入 main.py 用于纯函数测试（依赖 stub 已由模块顶部注入）。"""
    import main
    return main


def test_chromium_version_pick():
    """Chromium 目录必须按版本号数字排序取最新，不能被字典序骗到旧版。

    字典序下 "chromium-998" > "chromium-1248"，Playwright 升级后旧目录不删，
    按字符串排序会静默选到旧版浏览器。
    """
    import browser as browser_layer

    original_candidates = browser_layer.CHROMIUM_BINARY_CANDIDATES
    try:
        with tempfile.TemporaryDirectory(prefix="pw-ver-") as base:
            # 造两个版本目录，只有 998 和 1248；必须选 1248
            for version in ("998", "1248"):
                exe_dir = os.path.join(base, f"chromium-{version}", "chrome-linux64")
                os.makedirs(exe_dir, exist_ok=True)
                exe = os.path.join(exe_dir, "chrome")
                with open(exe, "w", encoding="utf-8") as f:
                    f.write("#!/bin/sh\n")
                os.chmod(exe, 0o755)

            browser_layer.CHROMIUM_BINARY_CANDIDATES = [base]
            picked = browser_layer._detect_playwright_chromium()
            assert picked, "未找到任何 Chromium"
            assert "chromium-1248" in picked, f"选错了版本: {picked}"
    finally:
        browser_layer.CHROMIUM_BINARY_CANDIDATES = original_candidates


def test_classify_sign_state():
    """签到状态文案归一：已完成/去签到/未知三态判定。"""
    main = _load_main_module()

    # 卡片 badge 已完成
    assert main.classify_sign_state(badge_text="每日签到 已完成 积分或抽奖券") == main.SIGN_STATE_DONE
    # 模态框标题「今日已签到」
    assert main.classify_sign_state(modal_title="今日已签到") == main.SIGN_STATE_DONE
    # 状态行「已领取 500 积分，明天再来」
    assert main.classify_sign_state(status_text="已领取 500 积分，明天再来") == main.SIGN_STATE_DONE
    # 可领取：卡片 badge 去签到
    assert main.classify_sign_state(badge_text="每日签到 去签到 积分或抽奖券") == main.SIGN_STATE_CLAIMABLE
    # 可领取：模态框标题选择今日奖励
    assert main.classify_sign_state(modal_title="选择今日奖励") == main.SIGN_STATE_CLAIMABLE
    # 未知文案（页面改版时的兜底，不能误判为可领取）
    assert main.classify_sign_state(badge_text="积分或抽奖券") == main.SIGN_STATE_UNKNOWN
    assert main.classify_sign_state() == main.SIGN_STATE_UNKNOWN
    # 多来源合并：任一出现已完成文案即 done
    assert main.classify_sign_state(badge_text="去签到", status_text="已领取 500 积分") == main.SIGN_STATE_DONE
    # 近似反例：「今日可领取」「未领取」都含「领取」字样，不能被误判成 done
    assert main.classify_sign_state(status_text="今日可领取") == main.SIGN_STATE_UNKNOWN
    assert main.classify_sign_state(status_text="未领取") == main.SIGN_STATE_UNKNOWN


class FakeLocator:
    """模拟 Playwright Locator 的最小接口，供被测函数使用。"""

    def __init__(self, text="", children=None, count=1, raises=False):
        self._text = text
        self._children = children or {}
        self._count = count
        self._raises = raises

    @property
    def first(self):
        return self

    def count(self):
        if self._raises:
            raise RuntimeError("locator dead")
        return self._count

    def nth(self, index):
        return self

    def locator(self, selector):
        if self._raises:
            raise RuntimeError("locator dead")
        if selector in self._children:
            return self._children[selector]
        raise LookupError(f"no child for {selector}")

    def inner_text(self, timeout=None):
        if self._raises:
            raise RuntimeError("locator dead")
        return self._text


class FakePage:
    """按选择器返回预置 locator，模拟签到弹窗的 DOM。"""

    def __init__(self, elements=None, raises=False):
        self._elements = elements or {}
        self._raises = raises

    def locator(self, selector):
        if self._raises:
            raise RuntimeError("page dead")
        if selector in self._elements:
            return self._elements[selector]
        # 未预置的选择器视为不存在（count 为 0）
        return FakeLocator(count=0, raises=False)


def test_find_reward_button():
    """奖励按钮匹配：应容忍空白/全角空格差异，且不能把 1500 误配成 500。"""
    main = _load_main_module()

    def rewards(amount_texts):
        buttons = []
        for text in amount_texts:
            btn = FakeLocator(children={main.SIGN_REWARD_AMOUNT_CSS: FakeLocator(text)})
            buttons.append(btn)
        return FakeLocator(count=len(buttons), children={})

    class MultiLocator(FakeLocator):
        """支持 nth() 返回不同按钮的列表 locator。"""

        def __init__(self, items):
            super().__init__(count=len(items))
            self._items = items

        def nth(self, index):
            return self._items[index]

    def page_with(amount_texts):
        items = [
            FakeLocator(children={main.SIGN_REWARD_AMOUNT_CSS: FakeLocator(t)})
            for t in amount_texts
        ]
        return FakePage({main.SIGN_REWARD_BTN_CSS: MultiLocator(items)})

    # 普通空格
    ctx = type("Ctx", (), {"page": page_with(["500 积分", "抽奖券 × 1"])})()
    assert main.find_reward_button(ctx, "500 积分") is not None

    # 全角空格 + 不换行空格 + 多空格，都应命中
    for variant in ("500　积分", "500\u00a0积分", "500   积分", " 500 积分 "):
        ctx = type("Ctx", (), {"page": page_with([variant])})()
        assert main.find_reward_button(ctx, "500 积分") is not None, variant

    # 前缀相同的 1500 积分不能被误配成 500 积分
    ctx = type("Ctx", (), {"page": page_with(["1500 积分"])})()
    assert main.find_reward_button(ctx, "500 积分") is None

    # 页面异常时安全返回 None
    ctx = type("Ctx", (), {"page": FakePage(raises=True)})()
    assert main.find_reward_button(ctx, "500 积分") is None


def test_wait_sign_result_timeout():
    """所有成功信号都缺失时必须按时超时返回 False，不能死循环。"""
    main = _load_main_module()

    ctx = type("Ctx", (), {
        "page": FakePage(raises=True),
        "config": {"points_to_cny_rate": 2000},
    })()
    result = main.AccountResult(username="demo@example.com", points_before=0)

    started = time.time()
    # quiet_seconds=0 跳过静默期，只验证轮询本身会按时超时
    ok = main.wait_sign_result(ctx, result, timeout=1, quiet_seconds=0)
    elapsed = time.time() - started
    assert ok is False
    assert elapsed < 8, elapsed  # 必须在超时附近返回，不能无限轮询


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
        test_candidate_merge()
        print("candidate merge: ok")
        test_sift_localize(tmp)
        print("sift localize: ok")
        test_hybrid_recovery(tmp)
        print("hybrid recovery: ok")
        test_template_localize(tmp)
        print("template localize: ok")
        test_helpers()
        print("helpers: ok")
        test_debug_dump(tmp)
        print("debug dump/prune: ok")
        test_classify_sign_state()
        print("classify sign state: ok")
        test_chromium_version_pick()
        print("chromium version pick: ok")
        test_find_reward_button()
        print("find reward button: ok")
        test_wait_sign_result_timeout()
        print("wait sign result timeout: ok")
    print("ALL_TESTS_PASSED")


if __name__ == "__main__":
    main()

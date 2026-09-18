"""
ddddocr 验证码识别辅助（供 pt_checkin.js 通过子进程调用）
用法: python ddddocr_ocr.py <图片路径>
输出: 识别结果 JSON {"code": "XXXXXX"} 或 {"error": "..."}（单行，供 Node 解析）

识别管线（实验验证 30 样本 80.0%）：
  1. 预处理（HSV 颜色过滤留黑字符 + 连通域去噪 + 3x 放大）→ 标准模型
  2. beta 模型识别原图
  3. 两结果一致 → 高置信直接用；不一致 → 取预处理结果（实验 80% 优于 beta 原图 63%），
     仅当预处理结果长度≠6（漏字符）时才取 beta 结果
"""
import sys
import json

import numpy as np
import cv2
import ddddocr


def preprocess(png_bytes):
    """HSV 过滤只留黑色字符，连通域去黑色散点，3 倍最近邻放大"""
    arr = np.frombuffer(png_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError('图片解码失败')
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    # 黑色字符与散点: 明度低；彩色噪声: 明度高/饱和度高 → 全白
    binary = (hsv[:, :, 2] < 120).astype(np.uint8) * 255
    # 连通域过滤：字符笔画是大连通域，噪声散点是孤立小块
    num, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    cleaned = np.zeros_like(binary)
    for i in range(1, num):
        if stats[i, cv2.CC_STAT_AREA] >= 12:
            cleaned[labels == i] = 255
    cleaned = cv2.resize(cleaned, None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST)
    ok, buf = cv2.imencode('.png', cleaned)
    if not ok:
        raise ValueError('预处理图编码失败')
    return buf.tobytes()


def clean_code(text):
    """与主脚本 cleanOcrText 一致：只留字母数字"""
    return ''.join(c for c in str(text) if c.isascii() and c.isalnum()).upper()


def main():
    if len(sys.argv) < 2:
        print(json.dumps({'error': '用法: python ddddocr_ocr.py <图片路径>'}))
        sys.exit(1)
    png = open(sys.argv[1], 'rb').read()

    ocr_std = ddddocr.DdddOcr(show_ad=False)
    ocr_beta = ddddocr.DdddOcr(show_ad=False, beta=True)

    code_pre = clean_code(ocr_std.classification(preprocess(png)))
    code_beta = clean_code(ocr_beta.classification(png))

    if code_pre == code_beta:
        code = code_pre  # 双模型一致，高置信
    elif len(code_pre) == 6:
        code = code_pre  # 分歧时预处理结果准确率更高（80% vs 63%）
    else:
        code = code_beta  # 预处理漏字符时才用 beta 兜底

    print(json.dumps({'code': code, 'detail': {'pre': code_pre, 'beta': code_beta}}))


if __name__ == '__main__':
    main()

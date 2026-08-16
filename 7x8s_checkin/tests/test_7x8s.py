# -*- coding: utf-8 -*-
"""
7x8s_checkin 单元测试（unittest 标准库，零新依赖）。

运行方式（在 7x8s_checkin 目录下）：
  python -m unittest discover -s tests -v
"""
import importlib.util
import os
import sys
import unittest

_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 模块名以数字开头，无法直接 import，用 importlib 按路径加载
_spec = importlib.util.spec_from_file_location(
    "checkin_7x8s", os.path.join(_SCRIPT_DIR, "7x8s_checkin.py")
)
_checkin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_checkin)


class TestParseAccounts(unittest.TestCase):
    """parse_accounts：必须保留空位，USER_ID 与账号索引对齐（bug 回归）。"""

    def test_empty_slot_preserved_for_index_alignment(self):
        # README 官方推荐写法：缺省位置留空 → 必须解析出 ["", "8411", ""]
        self.assertEqual(_checkin.parse_accounts("&8411&"), ["", "8411", ""])

    def test_index_alignment_second_account_gets_its_uid(self):
        # bug 复现：过滤空项时 ["", "8411", ""] 会退化成 ["8411"]，
        # 第 1 个账号错误拿到第 2 个账号的 user_id
        user_ids = _checkin.parse_accounts("&8411&")
        accounts = ["s1", "s2", "s3"]
        uid_of_account = {i + 1: user_ids[i] if i < len(user_ids) else ""
                          for i in range(len(accounts))}
        self.assertEqual(uid_of_account[1], "")     # 账号1 留空 → 走 session 自动解码
        self.assertEqual(uid_of_account[2], "8411")  # 账号2 拿到自己的 8411
        self.assertEqual(uid_of_account[3], "")     # 账号3 留空

    def test_single_account(self):
        self.assertEqual(_checkin.parse_accounts("8411"), ["8411"])

    def test_multiple_accounts(self):
        self.assertEqual(_checkin.parse_accounts("111&222"), ["111", "222"])

    def test_all_empty_slots(self):
        self.assertEqual(_checkin.parse_accounts("&&"), ["", "", ""])

    def test_whitespace_trimmed(self):
        self.assertEqual(_checkin.parse_accounts("  a  & b "), ["a", "b"])


class TestFmtQuota(unittest.TestCase):
    """额度缩写展示。"""

    def test_none(self):
        self.assertEqual(_checkin.fmt_quota(None), "0")

    def test_small(self):
        self.assertEqual(_checkin.fmt_quota(500), "500")

    def test_thousands(self):
        self.assertEqual(_checkin.fmt_quota(1500), "1.5K")

    def test_millions(self):
        self.assertEqual(_checkin.fmt_quota(2_500_000), "2.5M")

    def test_invalid(self):
        self.assertEqual(_checkin.fmt_quota("abc"), "0")


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""
抖音私信自动回复 — 核心逻辑单元测试
=====================================
TDD 方式：先写测试，验证通过后再推代码。
测试不依赖浏览器/PyQt5，只测核心逻辑。
"""

import sys, os, json, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ==== 从主代码提取纯逻辑函数（不 import PyQt5 避免依赖） ====

def match_reply(text, rules, phone_reply, default_reply):
    """规则匹配：手机号 > 关键词 > 默认"""
    import re
    if phone_reply and re.search(r'1[3-9]\d{9}', text):
        return phone_reply, "手机号"
    for rule in rules:
        kw = rule.get("keyword", "")
        if kw and kw in text:
            return rule["reply"], f"关键词[{kw}]"
    if default_reply:
        return default_reply, "默认"
    return None, None


def is_group_chat(item_text):
    """判断对话列表项是否为群聊"""
    return '群聊' in item_text or '群' in item_text.split('\n')[0][:5]


def parse_red_dot_count(badge_element_text):
    """从badge元素文本中解析未读数量"""
    text = badge_element_text.strip().replace('+', '')
    if text and text.isdigit():
        return int(text)
    return 1  # 非数字红点（如"新"或只显示红点）= 1条


def should_reply(cid, current_count, seen_counts):
    """判断是否应该回复：未读数增加才回复"""
    if cid not in seen_counts:
        return True  # 首次出现
    return current_count > seen_counts[cid]  # 数字增长


def filter_message_text(raw_text):
    """过滤掉 UI 文字，只保留真实消息内容"""
    junk = ['发送', '输入', '表情', '图片', '语音', '视频通话',
            '可在【', '隐私设置', '举报', '屏蔽', '加好友']
    for j in junk:
        if j in raw_text:
            return ''
    if len(raw_text) < 1 or len(raw_text) > 500:
        return ''
    return raw_text


# ==== 单元测试 ====

class TestRuleMatching(unittest.TestCase):
    """测试规则匹配优先级"""

    def setUp(self):
        self.rules = [
            {"keyword": "利率", "reply": "请咨询96688"},
            {"keyword": "贷款", "reply": "请到网点咨询"},
        ]
        self.phone = "已收到手机号"
        self.default = "感谢关注！"

    def test_keyword_match(self):
        text, rule = match_reply("利率是多少？", self.rules, self.phone, self.default)
        self.assertEqual(text, "请咨询96688")
        self.assertIn("利率", rule)

    def test_phone_match_priority(self):
        """手机号匹配优先级高于关键词"""
        text, rule = match_reply("利率13812345678", self.rules, self.phone, self.default)
        self.assertEqual(text, "已收到手机号")
        self.assertEqual(rule, "手机号")

    def test_default_fallback(self):
        text, rule = match_reply("你好", self.rules, self.phone, self.default)
        self.assertEqual(text, "感谢关注！")

    def test_no_match_without_default(self):
        text, rule = match_reply("你好", self.rules, self.phone, "")
        self.assertIsNone(text)

    def test_empty_text(self):
        text, rule = match_reply("", self.rules, self.phone, self.default)
        self.assertEqual(text, "感谢关注！")


class TestGroupDetection(unittest.TestCase):
    """测试群聊识别"""

    def test_group_chat(self):
        self.assertTrue(is_group_chat("群聊名称"))
        self.assertTrue(is_group_chat("工作群"))

    def test_private_chat(self):
        self.assertFalse(is_group_chat("张三"))
        self.assertFalse(is_group_chat("客户李四"))


class TestRedDotParsing(unittest.TestCase):
    """测试未读数解析"""

    def test_numeric(self):
        self.assertEqual(parse_red_dot_count("3"), 3)

    def test_plus(self):
        self.assertEqual(parse_red_dot_count("99+"), 99)

    def test_new_indicator(self):
        self.assertEqual(parse_red_dot_count("new"), 1)


class TestShouldReply(unittest.TestCase):
    """测试回复判定逻辑"""

    def test_first_seen(self):
        self.assertTrue(should_reply("cid1", 1, {}))

    def test_count_increased(self):
        self.assertTrue(should_reply("cid1", 3, {"cid1": 1}))

    def test_count_same(self):
        self.assertFalse(should_reply("cid1", 1, {"cid1": 1}))

    def test_count_decreased(self):
        self.assertFalse(should_reply("cid1", 1, {"cid1": 3}))


class TestMessageFilter(unittest.TestCase):
    """测试消息过滤"""

    def test_normal_message(self):
        self.assertEqual(filter_message_text("你好啊"), "你好啊")

    def test_ui_text(self):
        self.assertEqual(filter_message_text("可在【设置】中修改"), "")

    def test_short_text(self):
        self.assertEqual(filter_message_text("好"), "好")


class TestReplyTracking(unittest.TestCase):
    """测试回复后重新检测：回完应允许后续新消息"""

    def test_clear_after_reply_allows_new_message(self):
        """回复后清除计数，新消息应被识别"""
        seen = {"cid1": 3}
        # 模拟：检测到 3 条未读，回复完成，红点消失
        # 下次循环红点再出现 1 → 应该是新消息
        cid = "cid1"
        count = 1
        # 回完后清掉
        del seen[cid]
        # 现在 count=1 应该被识别为新消息
        self.assertTrue(should_reply(cid, count, seen))

    def test_new_conversation_still_works(self):
        """新对话不受旧计数影响"""
        seen = {"cid1": 3}
        self.assertTrue(should_reply("cid2", 1, seen))

    def test_same_count_after_clear_not_double_reply(self):
        """回完清掉后，同一轮不要再回两次"""
        seen = {"cid1": 3}
        seen["cid1"] = 3  # set to 3
        # 判断已存在且数量相同 → 不回复
        self.assertFalse(should_reply("cid1", 3, seen))


if __name__ == '__main__':
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestSuite([
        unittest.TestLoader().loadTestsFromTestCase(tc)
        for tc in [TestRuleMatching, TestGroupDetection, TestRedDotParsing,
                    TestShouldReply, TestMessageFilter, TestReplyTracking]
    ]))
    print(f"\n{'='*50}")
    if result.wasSuccessful():
        print("✅ 全部测试通过！")
    else:
        print(f"❌ {len(result.failures) + len(result.errors)} 个测试失败")
        sys.exit(1)

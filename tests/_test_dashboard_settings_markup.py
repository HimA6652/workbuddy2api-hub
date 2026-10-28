"""UI-01：设置页「上游 Header 超时」只保留一组控件。

纯静态回归：用 HTMLParser 检查真实 ID，不渲染整页、不启动账号调度。
同一 ID 若再出现，HTMLParser 会把它当成普通属性收下，不会自动去重；
因此这里能抓住浏览器 getElementById 只操作第一组时漏掉的问题。

    python tests/_test_dashboard_settings_markup.py
"""
import os
import unittest
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class IdCollector(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self)
        self.ids = []
        self.header_inputs = []
        self.retry_inputs = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        element_id = attrs.get("id")
        if element_id:
            self.ids.append(element_id)
        if element_id == "setHeaderTimeout":
            self.header_inputs.append((tag, attrs))
        if element_id in ("setModelQueueRetryAttempts", "setModelQueueRetryBudget"):
            self.retry_inputs[element_id] = (tag, attrs)


class DashboardSettingsMarkupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = os.path.join(ROOT, "dashboard.html")
        with open(path, encoding="utf-8") as fh:
            cls.source = fh.read()
        cls.parser = IdCollector()
        cls.parser.feed(cls.source)
        cls.parser.close()

    def test_header_timeout_control_is_unique(self):
        self.assertEqual(len(self.parser.header_inputs), 1)
        tag, attrs = self.parser.header_inputs[0]
        self.assertEqual(tag, "input")
        self.assertEqual(attrs.get("type"), "number")
        self.assertEqual(attrs.get("min"), "1")

    def test_header_timeout_markup_still_uses_existing_functions(self):
        self.assertIn("const headerTimeoutInput = document.getElementById('setHeaderTimeout');",
                      self.source)
        self.assertEqual(self.source.count("saveHeaderTimeout(this)"), 1)
        self.assertIn("await postJSON('/settings/save', "
                      "{upstream: {header_timeout_seconds: value}});", self.source)

    def test_removed_duplicate_has_no_orphaned_references(self):
        # 删掉的是整组卡片，而不是把第二组的 save 按钮/输入框遗留下来。
        self.assertNotIn("调整上游读取响应头（首字节）的超时时间。\n", self.source)
        self.assertEqual(self.parser.ids.count("setHeaderTimeout"), 1)

    def test_model_queue_retry_controls_have_bounded_inputs_and_real_save(self):
        self.assertEqual(len(self.parser.retry_inputs), 2)
        for name, upper in (("setModelQueueRetryAttempts", "8"),
                            ("setModelQueueRetryBudget", "120")):
            tag, attrs = self.parser.retry_inputs[name]
            self.assertEqual(tag, "input")
            self.assertEqual(attrs.get("type"), "number")
            self.assertEqual(attrs.get("min"), "0")
            self.assertEqual(attrs.get("max"), upper)
            self.assertEqual(self.parser.ids.count(name), 1)
        self.assertEqual(self.source.count("saveModelQueueRetry(this)"), 1)
        self.assertIn("data.model_queue_retry || {}", self.source)
        self.assertIn("Number.isInteger(attempts)", self.source)
        self.assertIn("Number.isInteger(waitBudget)", self.source)
        self.assertIn('postJSON("/settings/save", {model_queue_retry: '
                      '{model_queue_retry_attempts: attempts, '
                      'model_queue_wait_budget_seconds: waitBudget}})',
                      self.source)


if __name__ == "__main__":
    unittest.main(verbosity=2)

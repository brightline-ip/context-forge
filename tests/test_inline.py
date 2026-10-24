import unittest

from context_forge.inline import estimate_tokens, normalize_inline


class InlineNormalizationTests(unittest.TestCase):
    def test_inline_cleanup_is_lossless_except_transport_whitespace(self):
        source = "  Keep leading intent.  \r\n\r\n\r\n\r\nValue: 42  \r\nhttps://example.com/a?q=1"
        result = normalize_inline(source)
        self.assertIn("Keep leading intent.", result.text)
        self.assertIn("Value: 42", result.text)
        self.assertIn("https://example.com/a?q=1", result.text)
        self.assertNotIn("\r", result.text)
        self.assertNotIn("\n\n\n", result.text)
        self.assertNotEqual(result.original_sha256, result.normalized_sha256)

    def test_token_estimate_is_monotonic_for_appended_text(self):
        self.assertGreaterEqual(estimate_tokens("alpha beta gamma delta"), estimate_tokens("alpha beta"))

    def test_empty_text_has_zero_estimated_tokens(self):
        result = normalize_inline("")
        self.assertEqual(result.original_tokens_estimate, 0)
        self.assertEqual(result.normalized_tokens_estimate, 0)

    def test_result_declares_lossless_mode(self):
        self.assertEqual(normalize_inline("unaltered facts: 42").mode, "lossless")


if __name__ == "__main__":
    unittest.main()

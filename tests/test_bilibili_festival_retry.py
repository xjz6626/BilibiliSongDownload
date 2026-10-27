import unittest

from bilibili_festival_retry import (
    extract_failed_bvids,
    festival_url_from_payload,
    resolve_festival_retries,
)


class BilibiliFestivalRetryTests(unittest.TestCase):
    def test_extracts_unique_initial_state_failures_and_counts_other_errors(self):
        log_text = """
ERROR: [BiliBili] 1G4346fEFU: Unable to extract initial state; details
ERROR: [BiliBili] 1G4346fEFU: Unable to extract initial state; duplicate
ERROR: unrelated failure
"""
        self.assertEqual(extract_failed_bvids(log_text), (["BV1G4346fEFU"], 1))

    def test_accepts_matching_official_festival_url(self):
        payload = {
            "code": 0,
            "data": {
                "festival_jump_url": (
                    "http://www.bilibili.com/festival/test-event?foo=1&bvid=BV1G4346fEFU"
                )
            },
        }
        self.assertEqual(
            festival_url_from_payload(payload, "BV1G4346fEFU"),
            "https://www.bilibili.com/festival/test-event?foo=1&bvid=BV1G4346fEFU",
        )

    def test_rejects_wrong_domain_or_bvid(self):
        wrong_domain = {
            "code": 0,
            "data": {"festival_jump_url": "https://example.com/festival/x?bvid=BV1G4346fEFU"},
        }
        wrong_bvid = {
            "code": 0,
            "data": {
                "festival_jump_url": "https://www.bilibili.com/festival/x?bvid=BV1AAAAAAAAA"
            },
        }
        self.assertIsNone(festival_url_from_payload(wrong_domain, "BV1G4346fEFU"))
        self.assertIsNone(festival_url_from_payload(wrong_bvid, "BV1G4346fEFU"))

    def test_unrelated_errors_remain_unresolved(self):
        log_text = """
ERROR: [BiliBili] 1G4346fEFU: Unable to extract initial state
ERROR: network timeout
"""

        def lookup(_bvid):
            return {
                "code": 0,
                "data": {
                    "festival_jump_url": (
                        "https://www.bilibili.com/festival/event?bvid=BV1G4346fEFU"
                    )
                },
            }

        urls, unresolved = resolve_festival_retries(log_text, lookup)
        self.assertEqual(
            urls,
            ["https://www.bilibili.com/festival/event?bvid=BV1G4346fEFU"],
        )
        self.assertEqual(unresolved, ["另有 1 条非活动页下载错误"])


if __name__ == "__main__":
    unittest.main()

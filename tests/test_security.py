import time
import unittest

from app.security import (
    RateLimiter, hash_admin_password, hash_email_for_erasure, hash_secret,
    hash_token, is_erased_email, new_token, sanitize_csv_field, tokens_match,
    verify_admin_password, verify_secret,
)


class TokenTest(unittest.TestCase):
    def test_new_token_is_random_and_long(self):
        a, b = new_token(), new_token()
        self.assertNotEqual(a, b)
        self.assertGreater(len(a), 30)

    def test_hash_and_match(self):
        t = new_token()
        h = hash_token(t)
        self.assertTrue(tokens_match(t, h))
        self.assertFalse(tokens_match("wrong", h))


class SecretHashTest(unittest.TestCase):
    def test_roundtrip(self):
        h, s = hash_secret("123456")
        self.assertTrue(verify_secret("123456", h, s))
        self.assertFalse(verify_secret("000000", h, s))

    def test_admin_password_roundtrip(self):
        stored = hash_admin_password("correct horse battery staple")
        self.assertTrue(verify_admin_password("correct horse battery staple", stored))
        self.assertFalse(verify_admin_password("wrong", stored))
        self.assertFalse(verify_admin_password("wrong", "not-even-formatted-right"))


class ErasureHashTest(unittest.TestCase):
    def test_keyed_hash_differs_per_pepper(self):
        h1 = hash_email_for_erasure("guest@example.com", b"\x01" * 32)
        h2 = hash_email_for_erasure("guest@example.com", b"\x02" * 32)
        self.assertNotEqual(h1, h2)
        self.assertTrue(is_erased_email(h1))

    def test_is_case_insensitive_and_trimmed(self):
        h1 = hash_email_for_erasure("Guest@Example.com ", b"\x01" * 32)
        h2 = hash_email_for_erasure(" guest@example.com", b"\x01" * 32)
        self.assertEqual(h1, h2)


class CsvSanitizeTest(unittest.TestCase):
    def test_prefixes_dangerous_leading_chars(self):
        for bad in ["=cmd()", "+1", "-1", "@SUM(A1)"]:
            self.assertTrue(sanitize_csv_field(bad).startswith("'"))

    def test_leaves_normal_text_alone(self):
        self.assertEqual(sanitize_csv_field("Jane Doe"), "Jane Doe")


class RateLimiterTest(unittest.TestCase):
    def test_blocks_after_max_attempts(self):
        rl = RateLimiter(max_attempts=3, window_seconds=60)
        now = time.time()
        for _ in range(3):
            self.assertTrue(rl.allow("x", now=now))
        self.assertFalse(rl.allow("x", now=now))

    def test_window_expires(self):
        rl = RateLimiter(max_attempts=1, window_seconds=1)
        now = time.time()
        self.assertTrue(rl.allow("x", now=now))
        self.assertFalse(rl.allow("x", now=now))
        self.assertTrue(rl.allow("x", now=now + 2))

    def test_keys_are_independent(self):
        rl = RateLimiter(max_attempts=1, window_seconds=60)
        now = time.time()
        self.assertTrue(rl.allow("a", now=now))
        self.assertTrue(rl.allow("b", now=now))

    # -- retry_after: for the login form's visible countdown (2026-07-05) --

    def test_retry_after_is_zero_when_not_blocked(self):
        rl = RateLimiter(max_attempts=3, window_seconds=60)
        now = time.time()
        self.assertEqual(rl.retry_after("x", now=now), 0.0)
        rl.allow("x", now=now)
        self.assertEqual(rl.retry_after("x", now=now), 0.0)

    def test_retry_after_counts_down_from_the_oldest_hit_aging_out(self):
        rl = RateLimiter(max_attempts=2, window_seconds=60)
        now = time.time()
        rl.allow("x", now=now)
        rl.allow("x", now=now + 10)
        self.assertFalse(rl.allow("x", now=now + 20))
        # oldest hit was at `now`, window is 60s -- ages out at now+60
        self.assertAlmostEqual(rl.retry_after("x", now=now + 20), 40.0)
        self.assertAlmostEqual(rl.retry_after("x", now=now + 55), 5.0)

    def test_retry_after_does_not_consume_an_attempt(self):
        rl = RateLimiter(max_attempts=1, window_seconds=60)
        now = time.time()
        rl.allow("x", now=now)
        rl.retry_after("x", now=now)
        rl.retry_after("x", now=now)
        # Still exactly one hit recorded -- checking retry_after repeatedly
        # must not itself count as more attempts.
        self.assertTrue(rl.allow("x", now=now + 61))


if __name__ == "__main__":
    unittest.main()

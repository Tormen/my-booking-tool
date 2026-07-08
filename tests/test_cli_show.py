import unittest

from app.cli_list import SHORT_ID_LENGTH, assign_short_ids
from app.cli_show import (
    classify_show_query, find_course_by_shortname, find_registrations_on_date,
    find_users_by_name_or_email, looks_like_date, looks_like_registration_id,
)
from app.config import Course


def _course(shortname: str) -> Course:
    return Course(
        shortname=shortname, title=shortname, location="Studio", weekday="mon",
        start_time="09:00", duration_minutes=60, capacity=10,
    )


def _user(user_id: str, name: str, email: str) -> dict:
    return {"user_id": user_id, "name": name, "email": email}


def _reg(reg_id: str, course: str, occurrence_date: str, status: str = "confirmed") -> dict:
    return {
        "registration_id": reg_id, "course_shortname": course,
        "occurrence_date": occurrence_date, "status": status,
    }


class LooksLikeRegistrationIdTest(unittest.TestCase):
    def test_hex_only_at_min_length_is_id_like(self):
        self.assertTrue(looks_like_registration_id("a1b2c3", 6))

    def test_below_min_length_is_not_id_like(self):
        self.assertFalse(looks_like_registration_id("a1b2c", 6))

    def test_non_hex_letters_are_never_id_like(self):
        # "yoga-class-1" is well past min_length but contains non-hex
        # letters -- must never be tried as an id.
        self.assertFalse(looks_like_registration_id("yoga-class-1", 6))

    def test_dashes_are_ignored_for_length(self):
        self.assertTrue(looks_like_registration_id("a1-b2-c3", 6))

    def test_all_hex_short_name_below_min_length_is_not_id_like(self):
        # "Ada" is technically valid hex (a, d, a) but far too short to
        # ever be mistaken for a real id fragment.
        self.assertFalse(looks_like_registration_id("ada", 6))


class LooksLikeDateTest(unittest.TestCase):
    def test_exact_iso_date_matches(self):
        self.assertTrue(looks_like_date("2026-07-10"))

    def test_partial_date_does_not_match(self):
        self.assertFalse(looks_like_date("2026-07"))
        self.assertFalse(looks_like_date("2026"))

    def test_non_date_does_not_match(self):
        self.assertFalse(looks_like_date("lux-fri-yoga"))


class FindCourseByShortnameTest(unittest.TestCase):
    def test_exact_case_insensitive_match(self):
        courses = [_course("lux-fri-yoga"), _course("trier-sat-yoga")]
        result = find_course_by_shortname("LUX-FRI-YOGA", courses)
        self.assertEqual(result, [courses[0]])

    def test_substring_does_not_match(self):
        courses = [_course("lux-fri-yoga")]
        self.assertEqual(find_course_by_shortname("lux-fri", courses), [])

    def test_no_match_returns_empty(self):
        self.assertEqual(find_course_by_shortname("nope", [_course("lux-fri-yoga")]), [])


class FindUsersByNameOrEmailTest(unittest.TestCase):
    def setUp(self):
        self.users = [
            _user("u1", "Ada Lovelace", "ada@example.com"),
            _user("u2", "Fred", "fred@example.org"),
        ]

    def test_matches_email_substring(self):
        self.assertEqual(find_users_by_name_or_email("ada@", self.users), [self.users[0]])

    def test_matches_name_substring_case_insensitive(self):
        self.assertEqual(find_users_by_name_or_email("lovelace", self.users), [self.users[0]])

    def test_no_match_returns_empty(self):
        self.assertEqual(find_users_by_name_or_email("zzz", self.users), [])

    def test_multiple_matches_returned(self):
        users = [_user("u1", "Ada", "ada@example.org"), _user("u2", "Adam", "adam@example.org")]
        self.assertEqual(find_users_by_name_or_email("ada", users), users)


class FindRegistrationsOnDateTest(unittest.TestCase):
    def test_matches_any_course_on_that_date(self):
        rows = [
            _reg("r1", "lux-fri-yoga", "2026-07-10"),
            _reg("r2", "trier-sat-yoga", "2026-07-11"),
            _reg("r3", "other-course", "2026-07-10"),
        ]
        result = find_registrations_on_date("2026-07-10", rows)
        self.assertEqual([r["registration_id"] for r in result], ["r1", "r3"])

    def test_no_match_returns_empty(self):
        self.assertEqual(find_registrations_on_date("2026-01-01", [_reg("r1", "c", "2026-07-10")]), [])


class ClassifyShowQueryTest(unittest.TestCase):
    def setUp(self):
        self.courses = [_course("lux-fri-yoga"), _course("trier-sat-yoga")]
        self.users = [_user("u1", "Ada Lovelace", "ada@example.com"), _user("u2", "Fred", "fred@example.org")]
        self.rows = [
            _reg("11111111-0000-0000-0000-000000000001", "lux-fri-yoga", "2026-07-10"),
            _reg("22222222-0000-0000-0000-000000000002", "trier-sat-yoga", "2026-07-11"),
        ]
        self.full_ids = [r["registration_id"] for r in self.rows]

    def test_exact_full_id_wins(self):
        kind, data = classify_show_query(
            self.full_ids[0], self.full_ids, self.courses, self.users, self.rows,
            min_id_length=SHORT_ID_LENGTH,
        )
        self.assertEqual((kind, data), ("registration", self.full_ids[0]))

    def test_short_id_resolves_to_registration(self):
        short_ids = assign_short_ids(self.full_ids)
        query = short_ids[self.full_ids[0]]
        kind, data = classify_show_query(
            query, self.full_ids, self.courses, self.users, self.rows, min_id_length=SHORT_ID_LENGTH,
        )
        self.assertEqual((kind, data), ("registration", self.full_ids[0]))

    def test_course_shortname_resolves_to_course(self):
        kind, data = classify_show_query(
            "lux-fri-yoga", self.full_ids, self.courses, self.users, self.rows,
            min_id_length=SHORT_ID_LENGTH,
        )
        self.assertEqual(kind, "course")
        self.assertEqual(data.shortname, "lux-fri-yoga")

    def test_date_resolves_to_occurrence_rows(self):
        kind, data = classify_show_query(
            "2026-07-10", self.full_ids, self.courses, self.users, self.rows,
            min_id_length=SHORT_ID_LENGTH,
        )
        self.assertEqual(kind, "occurrence")
        self.assertEqual([r["registration_id"] for r in data], [self.full_ids[0]])

    def test_email_resolves_to_user(self):
        kind, data = classify_show_query(
            "ada@example.com", self.full_ids, self.courses, self.users, self.rows,
            min_id_length=SHORT_ID_LENGTH,
        )
        self.assertEqual((kind, data), ("user", self.users[0]))

    def test_name_substring_resolves_to_user(self):
        kind, data = classify_show_query(
            "lovelace", self.full_ids, self.courses, self.users, self.rows,
            min_id_length=SHORT_ID_LENGTH,
        )
        self.assertEqual((kind, data), ("user", self.users[0]))

    def test_ambiguous_user_match(self):
        users = [_user("u1", "Ada", "ada@example.org"), _user("u2", "Adam", "adam@example.org")]
        kind, data = classify_show_query(
            "ada", self.full_ids, self.courses, users, self.rows, min_id_length=SHORT_ID_LENGTH,
        )
        self.assertEqual(kind, "ambiguous_user")
        self.assertCountEqual(data, users)

    def test_nothing_matches_returns_none(self):
        kind, data = classify_show_query(
            "totally-unmatched-query", self.full_ids, self.courses, self.users, self.rows,
            min_id_length=SHORT_ID_LENGTH,
        )
        self.assertEqual((kind, data), ("none", None))

    def test_course_shortname_checked_before_date_and_user(self):
        # A course shortname that also happens to be hex-charset-free but
        # could otherwise collide conceptually -- exact course match
        # should win outright, no ambiguity.
        kind, data = classify_show_query(
            "trier-sat-yoga", self.full_ids, self.courses, self.users, self.rows,
            min_id_length=SHORT_ID_LENGTH,
        )
        self.assertEqual(kind, "course")

    def test_all_hex_short_name_is_treated_as_search_not_id(self):
        # "ada" is valid hex but far below min_id_length -- must resolve
        # via the user-search path, not silently attempt id resolution.
        kind, data = classify_show_query(
            "ada@example.com", self.full_ids, self.courses, self.users, self.rows,
            min_id_length=SHORT_ID_LENGTH,
        )
        self.assertEqual(kind, "user")


if __name__ == "__main__":
    unittest.main()

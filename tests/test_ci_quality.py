import unittest

from scripts.ci_quality import find_secret_matches


class CIQualityTests(unittest.TestCase):
    def test_secret_patterns_are_rejected_without_exposing_values(self) -> None:
        private_key = "-----BEGIN " + "PRIVATE KEY-----"
        aws_key = "AKIA" + "1234567890123456"
        github_token = "ghp_" + "a" * 24
        openai_key = "sk-" + "b" * 24
        generic_value = "abcdefghijklmnopqrst"
        generic = "api_key = " + repr(generic_value)

        matches = find_secret_matches(
            "\n".join((private_key, aws_key, github_token, openai_key, generic))
        )

        self.assertEqual(len(matches), 5)
        self.assertTrue(all("at line" in match for match in matches))
        self.assertNotIn(generic_value, " ".join(matches))

    def test_short_values_and_variable_names_are_allowed(self) -> None:
        safe = "\n".join(
            (
                'api_key = "short"',
                'token = os.environ.get("FLAYR_TOKEN")',
                'password = "placeholder"',
            )
        )

        self.assertEqual(find_secret_matches(safe), [])


if __name__ == "__main__":
    unittest.main()

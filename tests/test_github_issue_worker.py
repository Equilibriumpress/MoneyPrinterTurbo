import unittest

from scripts.github_issue_worker import (
    absolute_result_links,
    extract_job,
    replace_status_label,
)


class GitHubIssueWorkerTests(unittest.TestCase):
    def test_extracts_json_payload_and_aliases(self):
        payload = extract_job(
            """
            ```json
            {
              "subject": "Test video",
              "language": "nl",
              "aspect": "9:16",
              "subtitles": true,
              "unknown": "ignored"
            }
            ```
            """
        )
        self.assertEqual(payload["video_subject"], "Test video")
        self.assertEqual(payload["video_language"], "nl")
        self.assertEqual(payload["video_aspect"], "9:16")
        self.assertTrue(payload["subtitle_enabled"])
        self.assertNotIn("unknown", payload)

    def test_requires_subject(self):
        with self.assertRaisesRegex(RuntimeError, "video_subject is required"):
            extract_job('```json\n{"video_count": 1}\n```')

    def test_replaces_queue_status_label(self):
        labels = replace_status_label(["video-job", "priority"], "video-processing")
        self.assertEqual(labels, ["priority", "video-processing"])

    def test_builds_public_result_url(self):
        links = absolute_result_links(
            ["/tasks/demo/final.mp4"],
            "https://example.ngrok-free.app",
        )
        self.assertEqual(
            links,
            ["https://example.ngrok-free.app/tasks/demo/final.mp4"],
        )


if __name__ == "__main__":
    unittest.main()

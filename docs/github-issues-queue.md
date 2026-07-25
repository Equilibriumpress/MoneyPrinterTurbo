# GitHub Issues video queue

This queue lets one running Google Colab session process video requests from GitHub Issues.

## Flow

1. Start the queue cell in `docs/MoneyPrinterTurbo.ipynb`.
2. The cell starts the local MoneyPrinterTurbo API on port 8080.
3. It creates a temporary ngrok URL for result downloads.
4. The worker polls open issues with the `video-job` label.
5. The worker submits the issue JSON to `POST /api/v1/videos`.
6. It updates one issue comment with progress.
7. On success, it closes the issue and adds `video-completed`.
8. On failure, it keeps the issue open and adds `video-failed`.

## GitHub token

Create a fine-grained personal access token for this repository with:

- Metadata: read
- Issues: read and write

Do not place the token in the notebook source or in an issue. The Colab cell requests it with `getpass`.

The worker only accepts issues created by the token owner by default. Set
`MPT_ALLOWED_GITHUB_USERS` to a comma-separated allowlist before starting the
worker when another trusted account needs access.

## Job format

Create an issue with the **Generate a MoneyPrinterTurbo video** template. Its
textarea contains a JSON object:

```json
{
  "video_subject": "Vijf tips voor een betere nachtrust",
  "video_language": "nl",
  "video_aspect": "9:16",
  "video_source": "pexels",
  "video_count": 1,
  "voice_name": "nl-NL-ColetteNeural",
  "subtitle_enabled": true,
  "bgm_type": "random"
}
```

`video_subject` is required. The worker ignores unknown fields. It accepts one
video per issue by default. Set `MPT_MAX_VIDEO_COUNT` before starting the worker
to raise this limit.

## Status labels

The worker creates these labels when it starts:

- `video-job`
- `video-processing`
- `video-completed`
- `video-failed`

To retry a failed issue, correct its JSON, remove `video-failed`, and add
`video-job`.

## Runtime behavior

Colab runtimes stop after inactivity or platform limits. Open jobs remain in
GitHub and are processed after the queue cell starts again.

Result links use the temporary ngrok API tunnel. Download completed files before
the Colab session or tunnel ends.

## Local worker command

```bash
export MPT_GITHUB_TOKEN="..."
export MPT_GITHUB_REPOSITORY="Equilibriumpress/MoneyPrinterTurbo"
export MPT_API_BASE="http://127.0.0.1:8080/api/v1"
export MPT_PUBLIC_BASE="https://example.ngrok-free.app"

uv run python scripts/github_issue_worker.py --poll-seconds 20
```

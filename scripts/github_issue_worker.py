#!/usr/bin/env python3
"""Process MoneyPrinterTurbo video jobs submitted as GitHub Issues.

The worker is intended for a single trusted Colab runtime. It polls issues with
the ``video-job`` label, submits their JSON payload to the local
MoneyPrinterTurbo API, and writes progress and result links back to the issue.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen


LABELS = {
    "video-job": ("1d76db", "Queued MoneyPrinterTurbo video job"),
    "video-processing": ("fbca04", "MoneyPrinterTurbo is processing this job"),
    "video-completed": ("0e8a16", "MoneyPrinterTurbo completed this job"),
    "video-failed": ("d93f0b", "MoneyPrinterTurbo could not complete this job"),
}

ALLOWED_FIELDS = {
    "video_subject",
    "video_script",
    "video_terms",
    "video_aspect",
    "video_concat_mode",
    "video_transition_mode",
    "video_clip_duration",
    "video_clip_speed",
    "match_materials_to_script",
    "video_count",
    "video_source",
    "video_materials",
    "custom_audio_file",
    "video_language",
    "voice_name",
    "voice_volume",
    "voice_rate",
    "bgm_type",
    "bgm_file",
    "bgm_volume",
    "video_music_prompt",
    "sonilo_bgm_prompt",
    "subtitle_enabled",
    "subtitle_position",
    "custom_position",
    "font_name",
    "text_fore_color",
    "text_background_color",
    "rounded_subtitle_background",
    "font_size",
    "stroke_color",
    "stroke_width",
    "n_threads",
    "paragraph_number",
    "video_script_prompt",
    "custom_system_prompt",
}

ALIASES = {
    "subject": "video_subject",
    "topic": "video_subject",
    "language": "video_language",
    "aspect": "video_aspect",
    "voice": "voice_name",
    "subtitles": "subtitle_enabled",
}


class WorkerError(RuntimeError):
    pass


@dataclass
class Settings:
    repository: str
    github_token: str
    api_base: str
    public_base: str
    poll_seconds: int
    status_seconds: int
    allowed_users: set[str]
    max_video_count: int
    once: bool


class GitHubClient:
    def __init__(self, repository: str, token: str):
        self.repository = repository
        self.token = token
        self.api_root = "https://api.github.com"

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        url = path if path.startswith("http") else f"{self.api_root}{path}"
        data = None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "MoneyPrinterTurbo-Colab-Worker",
        }
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise WorkerError(
                f"GitHub {method} {path} failed with {exc.code}: {detail}"
            ) from exc
        except URLError as exc:
            raise WorkerError(f"GitHub request failed: {exc}") from exc

    def authenticated_login(self) -> str:
        return str(self.request("GET", "/user")["login"])

    def ensure_labels(self) -> None:
        owner, repo = self.repository.split("/", 1)
        for name, (color, description) in LABELS.items():
            encoded = quote(name, safe="")
            try:
                self.request("GET", f"/repos/{owner}/{repo}/labels/{encoded}")
            except WorkerError as exc:
                if "failed with 404" not in str(exc):
                    raise
                self.request(
                    "POST",
                    f"/repos/{owner}/{repo}/labels",
                    {"name": name, "color": color, "description": description},
                )

    def list_queued_issues(self) -> list[dict[str, Any]]:
        owner, repo = self.repository.split("/", 1)
        params = urlencode(
            {
                "state": "open",
                "labels": "video-job",
                "sort": "created",
                "direction": "asc",
                "per_page": 30,
            }
        )
        issues = self.request("GET", f"/repos/{owner}/{repo}/issues?{params}")
        return [issue for issue in issues if "pull_request" not in issue]

    def update_issue(
        self,
        number: int,
        *,
        labels: list[str] | None = None,
        title: str | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        owner, repo = self.repository.split("/", 1)
        payload: dict[str, Any] = {}
        if labels is not None:
            payload["labels"] = labels
        if title is not None:
            payload["title"] = title
        if state is not None:
            payload["state"] = state
        return self.request("PATCH", f"/repos/{owner}/{repo}/issues/{number}", payload)

    def create_comment(self, number: int, body: str) -> int:
        owner, repo = self.repository.split("/", 1)
        comment = self.request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            {"body": body},
        )
        return int(comment["id"])

    def update_comment(self, comment_id: int, body: str) -> None:
        owner, repo = self.repository.split("/", 1)
        self.request(
            "PATCH",
            f"/repos/{owner}/{repo}/issues/comments/{comment_id}",
            {"body": body},
        )


def api_request(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise WorkerError(
            f"MoneyPrinterTurbo API {method} {url} failed with {exc.code}: {detail}"
        ) from exc
    except URLError as exc:
        raise WorkerError(f"MoneyPrinterTurbo API request failed: {exc}") from exc


def parse_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    lower = value.lower()
    if lower in {"true", "yes", "on"}:
        return True
    if lower in {"false", "no", "off"}:
        return False
    if lower in {"null", "none", "~"}:
        return None
    if value.startswith(("[", "{", '"')) or value in {"[]", "{}"}:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value.strip("'\"")


def parse_simple_yaml(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = parse_scalar(value)
    return result


def extract_job(body: str) -> dict[str, Any]:
    body = body or ""
    json_match = re.search(r"```json\s*(\{.*?\})\s*```", body, re.I | re.S)
    if json_match:
        try:
            raw = json.loads(json_match.group(1))
        except json.JSONDecodeError as exc:
            raise WorkerError(f"Invalid JSON job payload: {exc}") from exc
    else:
        yaml_match = re.search(r"```ya?ml\s*(.*?)```", body, re.I | re.S)
        raw = parse_simple_yaml(yaml_match.group(1) if yaml_match else body)

    if not isinstance(raw, dict):
        raise WorkerError("The issue payload must be a JSON object")

    normalized: dict[str, Any] = {}
    for key, value in raw.items():
        target = ALIASES.get(str(key), str(key))
        if target in ALLOWED_FIELDS:
            normalized[target] = value

    subject = str(normalized.get("video_subject", "")).strip()
    if not subject:
        raise WorkerError("video_subject is required")
    normalized["video_subject"] = subject
    return normalized


def label_names(issue: dict[str, Any]) -> list[str]:
    return [str(label["name"]) for label in issue.get("labels", [])]


def replace_status_label(labels: list[str], new_status: str) -> list[str]:
    status_labels = set(LABELS)
    retained = [label for label in labels if label not in status_labels]
    return [*retained, new_status]


def status_body(
    *,
    issue_number: int,
    state: str,
    task_id: str | None = None,
    progress: int | None = None,
    detail: str | None = None,
    links: list[str] | None = None,
) -> str:
    lines = [
        "<!-- moneyprinterturbo-worker-status -->",
        f"MoneyPrinterTurbo job #{issue_number}",
        "",
        f"Status: **{state}**",
    ]
    if task_id:
        lines.append(f"Task ID: `{task_id}`")
    if progress is not None:
        lines.append(f"Progress: **{progress}%**")
    if detail:
        lines.extend(["", detail])
    if links:
        lines.extend(["", "Resultaten:"])
        lines.extend(f"- {link}" for link in links)
    return "\n".join(lines)


def absolute_result_links(paths: list[str], public_base: str) -> list[str]:
    links: list[str] = []
    for path in paths:
        if not path:
            continue
        parsed = urlparse(path)
        if parsed.scheme in {"http", "https"}:
            if parsed.hostname in {"127.0.0.1", "localhost"} and public_base:
                links.append(urljoin(public_base.rstrip("/") + "/", parsed.path.lstrip("/")))
            else:
                links.append(path)
        elif public_base:
            links.append(urljoin(public_base.rstrip("/") + "/", path.lstrip("/")))
        else:
            links.append(path)
    return links


def submit_video(settings: Settings, payload: dict[str, Any]) -> str:
    max_count = settings.max_video_count
    video_count = int(payload.get("video_count", 1) or 1)
    if video_count < 1 or video_count > max_count:
        raise WorkerError(f"video_count must be between 1 and {max_count}")
    payload["video_count"] = video_count

    response = api_request(
        "POST",
        f"{settings.api_base.rstrip('/')}/videos",
        payload,
    )
    if int(response.get("status", 500)) != 200:
        raise WorkerError(f"Video submission failed: {response}")
    task_id = str(response.get("data", {}).get("task_id", "")).strip()
    if not task_id:
        raise WorkerError(f"Video submission returned no task_id: {response}")
    return task_id


def wait_for_video(
    settings: Settings,
    github: GitHubClient,
    issue_number: int,
    comment_id: int,
    task_id: str,
) -> dict[str, Any]:
    last_progress = -1
    last_update = 0.0

    while True:
        response = api_request(
            "GET",
            f"{settings.api_base.rstrip('/')}/tasks/{quote(task_id, safe='')}",
        )
        data = response.get("data") or {}
        state = int(data.get("state", 4))
        progress = int(data.get("progress", 0) or 0)
        now = time.time()

        if (
            progress != last_progress
            and (progress >= last_progress + 5 or now - last_update >= settings.status_seconds)
        ):
            github.update_comment(
                comment_id,
                status_body(
                    issue_number=issue_number,
                    state="processing",
                    task_id=task_id,
                    progress=progress,
                ),
            )
            last_progress = progress
            last_update = now

        if state == 1:
            return data
        if state == -1:
            detail = data.get("error") or data.get("failed_stage") or "Unknown generation error"
            raise WorkerError(str(detail))
        time.sleep(settings.poll_seconds)


def process_issue(settings: Settings, github: GitHubClient, issue: dict[str, Any]) -> None:
    number = int(issue["number"])
    author = str(issue.get("user", {}).get("login", "")).lower()
    if author not in settings.allowed_users:
        print(f"Skipping issue #{number} from unauthorized user {author!r}", flush=True)
        return

    current_labels = label_names(issue)
    processing_labels = replace_status_label(current_labels, "video-processing")
    title = str(issue.get("title", f"Video job #{number}"))
    github.update_issue(number, labels=processing_labels)

    comment_id = github.create_comment(
        number,
        status_body(issue_number=number, state="accepted", progress=0),
    )

    try:
        payload = extract_job(str(issue.get("body") or ""))
        task_id = submit_video(settings, payload)
        github.update_comment(
            comment_id,
            status_body(
                issue_number=number,
                state="processing",
                task_id=task_id,
                progress=0,
            ),
        )
        result = wait_for_video(settings, github, number, comment_id, task_id)
        raw_paths = result.get("combined_videos") or result.get("videos") or []
        links = absolute_result_links(list(raw_paths), settings.public_base)
        github.update_comment(
            comment_id,
            status_body(
                issue_number=number,
                state="completed",
                task_id=task_id,
                progress=100,
                links=links,
                detail=(
                    "De openbare links werken zolang de Colab-runtime en ngrok-tunnel actief zijn."
                    if links
                    else "De taak is voltooid, maar de API gaf geen videolink terug."
                ),
            ),
        )
        github.update_issue(
            number,
            labels=replace_status_label(processing_labels, "video-completed"),
            state="closed",
        )
        print(f"Completed issue #{number}: {title}", flush=True)
    except Exception as exc:
        message = str(exc)
        github.update_comment(
            comment_id,
            status_body(
                issue_number=number,
                state="failed",
                detail=f"Fout: `{message[:1500]}`",
            ),
        )
        github.update_issue(
            number,
            labels=replace_status_label(processing_labels, "video-failed"),
        )
        print(f"Failed issue #{number}: {message}", file=sys.stderr, flush=True)


def load_settings(args: argparse.Namespace) -> Settings:
    token = os.environ.get("MPT_GITHUB_TOKEN", "").strip()
    if not token:
        raise WorkerError("MPT_GITHUB_TOKEN is required")

    repository = (
        args.repository
        or os.environ.get("MPT_GITHUB_REPOSITORY", "Equilibriumpress/MoneyPrinterTurbo")
    ).strip()
    if repository.count("/") != 1:
        raise WorkerError("Repository must use owner/name format")

    api_base = (
        args.api_base
        or os.environ.get("MPT_API_BASE", "http://127.0.0.1:8080/api/v1")
    ).strip()
    public_base = (
        args.public_base or os.environ.get("MPT_PUBLIC_BASE", "")
    ).strip()

    explicit_users = {
        item.strip().lower()
        for item in os.environ.get("MPT_ALLOWED_GITHUB_USERS", "").split(",")
        if item.strip()
    }

    settings = Settings(
        repository=repository,
        github_token=token,
        api_base=api_base,
        public_base=public_base,
        poll_seconds=max(5, int(args.poll_seconds)),
        status_seconds=max(15, int(args.status_seconds)),
        allowed_users=explicit_users,
        max_video_count=max(1, int(os.environ.get("MPT_MAX_VIDEO_COUNT", "1"))),
        once=bool(args.once),
    )
    return settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository")
    parser.add_argument("--api-base")
    parser.add_argument("--public-base")
    parser.add_argument("--poll-seconds", type=int, default=20)
    parser.add_argument("--status-seconds", type=int, default=30)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = load_settings(args)
    github = GitHubClient(settings.repository, settings.github_token)
    github.ensure_labels()

    if not settings.allowed_users:
        settings.allowed_users = {github.authenticated_login().lower()}

    worker_id = str(uuid.uuid4())[:8]
    print(
        f"Worker {worker_id} watches {settings.repository} for video-job issues. "
        f"Allowed users: {', '.join(sorted(settings.allowed_users))}",
        flush=True,
    )

    while True:
        try:
            issues = github.list_queued_issues()
            for issue in issues:
                process_issue(settings, github, issue)
        except Exception as exc:
            print(f"Queue poll failed: {exc}", file=sys.stderr, flush=True)

        if settings.once:
            return 0
        time.sleep(settings.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())

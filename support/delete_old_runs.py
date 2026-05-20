# 这个脚本专门负责清理旧的 GitHub Actions 运行记录。
# 它只依赖运行时环境变量和 GitHub REST API，不依赖主签到主程序。
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_HOUR_COUNT = 168
REQUEST_TIMEOUT_SECONDS = 30
RUNS_PER_PAGE = 100


@dataclass(slots=True)
class CleanupConfig:
    # 所有运行参数都集中放在这里，方便统一校验和后续扩展。
    repository_name: str
    token: str
    current_run_id: int
    hour_count: int
    api_base_url: str
    summary_path: str


class WorkflowRunCleaner:
    # 清理逻辑集中在这个类里，避免工作流文件里堆叠复杂脚本。
    def __init__(self, config: CleanupConfig) -> None:
        self.config = config
        self.current_time = datetime.now(timezone.utc)
        self.deleted_count = 0
        self.scanned_count = 0
        self.runs_url = (
            f"{self.config.api_base_url.rstrip('/')}"
            f"/repos/{self.config.repository_name}/actions/runs?per_page={RUNS_PER_PAGE}"
        )

    def run(self) -> int:
        # 这里通过多轮遍历规避删除后的分页位移，避免旧记录被漏掉。
        self.log("Delete old workflow runs started.")
        self.log(
            f"Repository: {self.config.repository_name} | Current Run ID: {self.config.current_run_id} | Hour Count: {self.config.hour_count}"
        )
        any_deleted = True

        while any_deleted:
            any_deleted = False
            next_url = self.runs_url

            while next_url:
                payload, next_url = self.fetch_runs_page(next_url)
                runs = (
                    payload.get("workflow_runs", [])
                    if isinstance(payload, dict)
                    else []
                )

                for run in runs:
                    if not isinstance(run, dict):
                        continue

                    self.scanned_count += 1
                    if not self.should_delete_run(run):
                        continue

                    run_id = int(run.get("id", 0) or 0)
                    if run_id <= 0:
                        continue

                    self.delete_run(run_id)
                    any_deleted = True

        self.log(f"Scanned workflow runs: {self.scanned_count}")
        self.log(f"Deleted workflow runs: {self.deleted_count}")
        self.write_summary()
        return 0

    def fetch_runs_page(self, url: str) -> tuple[dict[str, Any], str]:
        # 这个函数负责拉取一页运行记录，并同时解析下一页链接。
        payload, headers = self.request_json("GET", url)
        next_url = self.extract_next_url(headers.get("Link", ""))
        return payload if isinstance(payload, dict) else {}, next_url

    def should_delete_run(self, run: dict[str, Any]) -> bool:
        # hour_count 为 0 时，表示删除当前运行之外的全部记录。
        run_id = int(run.get("id", 0) or 0)
        if run_id == self.config.current_run_id:
            return False

        if self.config.hour_count == 0:
            return True

        created_at_text = str(run.get("created_at", "")).strip()
        if not created_at_text:
            return False

        try:
            created_at = datetime.strptime(
                created_at_text, "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            return False

        return self.current_time - created_at > timedelta(hours=self.config.hour_count)

    def delete_run(self, run_id: int) -> None:
        # 这里删除单条运行记录，并把结果明确写入日志。
        delete_url = f"{self.config.api_base_url.rstrip('/')}/repos/{self.config.repository_name}/actions/runs/{run_id}"
        try:
            _, _, status_code = self.request("DELETE", delete_url)
        except RuntimeError as exc:
            self.log(f"Failed to delete run {run_id}: {exc}")
            return

        if status_code == 204:
            self.deleted_count += 1
            self.log(f"Deleted run with ID {run_id}")
            return

        self.log(f"Failed to delete run with ID {run_id}. Status code: {status_code}")

    def request_json(self, method: str, url: str) -> tuple[Any, dict[str, str]]:
        # JSON 接口统一从这里发出，解析失败时抛出明确异常。
        raw_text, headers, _ = self.request(method, url)
        if not raw_text:
            return {}, headers
        try:
            return json.loads(raw_text), headers
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Response is not valid JSON: {exc}") from exc

    def request(self, method: str, url: str) -> tuple[str, dict[str, str], int]:
        # HTTP 请求统一从这里发出，避免多处重复拼接请求头。
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.config.token}",
            "User-Agent": "XiXunYunAuto-DeleteOldRuns",
        }
        request = Request(url=url, method=method, headers=headers)

        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                body = response.read().decode("utf-8", errors="replace")
                return body, dict(response.headers.items()), response.status
        except HTTPError as exc:
            error_text = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {error_text}") from exc
        except URLError as exc:
            raise RuntimeError(f"Network error: {exc}") from exc

    def write_summary(self) -> None:
        # 当前环境提供 Summary 文件时，这里会补一份简洁摘要。
        if not self.config.summary_path:
            return

        workflow_info_lines = build_workflow_info_lines(self.config, self.current_time)
        summary_lines = [
            "# DeleteOldRuns",
            "",
            "Repository Name：" + self.config.repository_name,
            "",
            "Current Run ID：" + str(self.config.current_run_id),
            "",
            "Hour Count：" + str(self.config.hour_count),
            "",
            "Scanned Workflow Runs：" + str(self.scanned_count),
            "",
            "Deleted Workflow Runs：" + str(self.deleted_count),
            "",
            "## 工作流信息",
            "",
        ]
        for line in workflow_info_lines:
            summary_lines.extend([line, ""])
        with open(
            self.config.summary_path, "a", encoding="utf-8", newline="\n"
        ) as summary_file:
            summary_file.write("\n".join(summary_lines).strip() + "\n")

    def log(self, message: str) -> None:
        # 日志统一输出北京时间，方便和当前仓库其他工作流的排查信息对齐。
        beijing_time = datetime.now(timezone.utc).astimezone(
            timezone(timedelta(hours=8))
        )
        timestamp_text = beijing_time.strftime("%Y-%m-%d %H:%M:%S:%f")[:-3]
        print(f"{timestamp_text} {message}")

    @staticmethod
    def extract_next_url(link_header: str) -> str:
        # 从 Link 头里提取下一页地址，拿不到时返回空字符串。
        for chunk in link_header.split(","):
            text = chunk.strip()
            if 'rel="next"' not in text:
                continue
            if "<" in text and ">" in text:
                return text.split("<", 1)[1].split(">", 1)[0]
        return ""


def load_config() -> CleanupConfig:
    # 环境变量读取和默认值处理统一放在这里。
    repository_name = os.getenv("REPOSITORY_NAME", "").strip()
    if not repository_name:
        repository_name = os.getenv("GITHUB_REPOSITORY", "").strip()
    token = os.getenv("GITHUB_TOKEN", "").strip()
    current_run_id = parse_positive_int(
        os.getenv("GITHUB_RUN_ID", "").strip(), default=0
    )
    hour_count = parse_hour_count(os.getenv("HOUR_COUNT", "").strip())
    api_base_url = os.getenv("GITHUB_API_URL", "https://api.github.com").strip()
    if not api_base_url:
        api_base_url = "https://api.github.com"
    summary_path = os.getenv("GITHUB_STEP_SUMMARY", "").strip()

    if not repository_name:
        raise RuntimeError(
            "Missing repository name in REPOSITORY_NAME or GITHUB_REPOSITORY"
        )

    return CleanupConfig(
        repository_name=repository_name,
        token=token,
        current_run_id=current_run_id,
        hour_count=hour_count,
        api_base_url=api_base_url,
        summary_path=summary_path,
    )


def parse_positive_int(raw_value: str, default: int) -> int:
    # 正整数解析失败时会回退到明确默认值。
    if not raw_value:
        return default
    if not raw_value.isdigit():
        return default
    return int(raw_value)


def parse_hour_count(raw_value: str) -> int:
    # hour_count 解析沿用当前项目规则，空值、非法值和负数都会回退到 168。
    if raw_value == "":
        return DEFAULT_HOUR_COUNT
    if raw_value == "0":
        return 0
    if not raw_value.isdigit():
        return DEFAULT_HOUR_COUNT

    hour_count = int(raw_value)
    if hour_count < 0:
        return DEFAULT_HOUR_COUNT
    return hour_count


def main() -> int:
    # 缺少 token 时按当前项目规则直接跳过，避免工作流因为清理功能报错。
    config = load_config()
    if not config.token:
        WorkflowRunCleaner(config).log(
            "GITHUB_TOKEN is empty. Skip deleting workflow runs."
        )
        return 0

    cleaner = WorkflowRunCleaner(config)
    return cleaner.run()


def build_workflow_info_lines(
    config: CleanupConfig, current_time: datetime
) -> list[str]:
    # 工作流信息区块会在这里补齐 delete_old_runs 需要展示的完整字段集合。
    beijing_time = current_time.astimezone(timezone(timedelta(hours=8))).strftime(
        "%Y-%m-%d %H:%M:%S:%f"
    )[:-3]
    triggering_actor = os.getenv("GITHUB_TRIGGERING_ACTOR", "").strip()
    actor_name = os.getenv("GITHUB_ACTOR", "").strip()
    initiated_run_by = triggering_actor or actor_name
    return [
        "Branch Name：" + describe_runtime_value(
            os.getenv("GITHUB_REF_NAME", "").strip(), "当前环境未提供 GITHUB_REF_NAME"
        ),
        "Triggered By：" + describe_runtime_value(
            os.getenv("GITHUB_EVENT_NAME", "").strip(),
            "当前环境未提供 GITHUB_EVENT_NAME",
        ),
        "Initial Run By：" + describe_runtime_value(
            actor_name, "当前环境未提供 GITHUB_ACTOR"
        ),
        "Initial Run By ID：" + describe_runtime_value(
            os.getenv("GITHUB_ACTOR_ID", "").strip(), "当前环境未提供 GITHUB_ACTOR_ID"
        ),
        "Initiated Run By：" + describe_runtime_value(
            initiated_run_by, "当前环境未提供 GITHUB_TRIGGERING_ACTOR"
        ),
        "Repository Name：" + describe_runtime_value(
            config.repository_name, "当前环境未提供 GITHUB_REPOSITORY"
        ),
        "Commit SHA：" + describe_runtime_value(
            os.getenv("GITHUB_SHA", "").strip(), "当前环境未提供 GITHUB_SHA"
        ),
        "Workflow Name：" + describe_runtime_value(
            os.getenv("GITHUB_WORKFLOW", "").strip(), "当前环境未提供 GITHUB_WORKFLOW"
        ),
        "Workflow Number：" + describe_runtime_value(
            os.getenv("GITHUB_RUN_NUMBER", "").strip(),
            "当前环境未提供 GITHUB_RUN_NUMBER",
        ),
        "Workflow ID：" + describe_runtime_value(
            str(config.current_run_id) if config.current_run_id else "",
            "当前环境未提供 GITHUB_RUN_ID",
        ),
        f"Beijing Time：{beijing_time}",
        "Copyright © 2026 NianBroken. All rights reserved.",
    ]


def describe_runtime_value(value: str, fallback_message: str) -> str:
    # 工作流字段会优先显示真实值，缺失时给出明确说明。
    text = value.strip()
    if text:
        return text
    return fallback_message


if __name__ == "__main__":
    raise SystemExit(main())

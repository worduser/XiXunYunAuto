# 这个模块统一管理接口原始响应的格式化、预览和落盘输出。
# 控制台展示和 TXT 文件写入都由运行配置控制，业务流程只负责传入记录内容。
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from support.time_utils import format_datetime, now_in_timezone


class ResponseRecorder:
    # 这个记录器负责保存整个运行过程中每次接口调用的原始请求摘要和响应内容。
    def __init__(
        self,
        timezone_name: str,
        time_format: str,
        console_enabled: bool,
        file_enabled: bool,
        console_truncate: int,
    ) -> None:
        self.timezone_name = timezone_name
        self.time_format = time_format
        self.console_enabled = console_enabled
        self.file_enabled = file_enabled
        # 控制台截断长度完全遵循配置值，具体合法性由配置加载阶段负责校验。
        self.console_truncate = console_truncate
        self.entries: list[str] = []
        self.output_path = ""

    def _pretty_text(self, payload: Any, fallback_text: str) -> str:
        # 响应正文如果能够按 JSON 格式化，就优先输出格式化后的结构化文本。
        if payload is not None:
            try:
                return json.dumps(payload, ensure_ascii=False, indent=2)
            except Exception:
                pass
        return fallback_text

    def record(
        self,
        stage: str,
        endpoint_name: str,
        request_summary: dict[str, Any],
        response_payload: Any,
        raw_text: str,
        error_text: str,
    ) -> None:
        # 每次接口调用都会记录阶段名、接口名、请求摘要、原始响应和异常信息。
        timestamp_text = format_datetime(
            now_in_timezone(self.timezone_name), self.time_format
        )
        formatted_payload = self._pretty_text(response_payload, raw_text)
        block = [
            "=" * 88,
            f"记录时间: {timestamp_text}",
            f"阶段: {stage}",
            f"接口: {endpoint_name}",
            "请求摘要:",
            json.dumps(request_summary, ensure_ascii=False, indent=2),
            "原始响应:",
            formatted_payload or "无响应正文",
        ]
        if error_text:
            block.extend(["异常信息:", error_text])
        block_text = "\n".join(block)
        self.entries.append(block_text)

        if self.console_enabled:
            # 控制台预览在内容过长时只展示前半段，完整文本仍然保留在 entries 中。
            console_text = block_text
            if len(console_text) > self.console_truncate:
                console_text = (
                    f"{console_text[: self.console_truncate]}\n"
                    f"... 已截断，完整内容可通过 txt 原始响应输出查看 ..."
                )
            print(console_text)

    def finalize(self) -> str:
        # 当前运行过程只会写出一个 TXT 文件，文件名由时间戳和五位随机数组成。
        if not self.file_enabled or not self.entries:
            return ""

        output_dir = Path("output") / "raw_responses"
        output_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{now_in_timezone(self.timezone_name).strftime('%Y%m%d%H%M%S')}_{random.randint(10000, 99999)}.txt"
        output_file = output_dir / filename
        output_file.write_text("\n\n".join(self.entries), encoding="utf-8")
        self.output_path = str(output_file.resolve())
        return self.output_path

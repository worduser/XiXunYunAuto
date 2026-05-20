# 这个文件负责把整个自动签到流程串成一条完整执行链。
# 配置加载、接口调用、结果判定、消息推送、Summary 写入和收尾清理都会在这里统一编排。
from __future__ import annotations

import os
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from api.client import ApiClient, format_business_error
from api.endpoints import (
    LOGIN_ENDPOINT,
    SCHOOL_MAP_ENDPOINT,
    SIGN_HOME_ENDPOINT,
    SIGN_SUBMIT_ENDPOINT,
    build_login_version,
)
from config.loader import (
    ConfigError,
    load_config,
    resolve_force_push_bootstrap,
    resolve_force_push_with_source,
)
from core.cleanup import cleanup_runtime_artifacts
from core.errors import WorkflowError
from core.models import (
    AccountConfig,
    ApiCallResult,
    ExecutionSnapshot,
    ResolvedConfig,
    RunEnvironment,
    StageResult,
)
from core.retry import RetryManager
from messaging.message_builder import DEFAULT_TITLES, build_message
from messaging.notifier import send_notification
from messaging.summary_builder import build_summary
from security.location_encryptor import encrypt_coordinate
from support.logging_utils import RunLogger
from support.response_recorder import ResponseRecorder
from support.time_utils import (
    format_datetime,
    now_in_timezone,
    parse_sign_history_items,
    today_text,
)


CONFIG_PATH = os.getenv("XIXUNYUN_CONFIG_PATH", "config/config.json")


def main() -> int:
    # 主入口统一负责申请和回收本次运行用到的资源，避免异常路径留下未处理状态。
    environment = build_environment()
    config: ResolvedConfig | None = None
    logger: RunLogger | None = None
    response_recorder: ResponseRecorder | None = None
    api_client: ApiClient | None = None
    snapshot = ExecutionSnapshot(
        environment_label=environment.source_label,
        force_push_active=resolve_force_push_bootstrap(
            CONFIG_PATH, environment.is_github_actions
        ),
    )

    try:
        # 只有配置加载成功后，程序才继续初始化日志器、原始响应记录器、HTTP 客户端和重试管理器。
        config = load_config(CONFIG_PATH, environment.is_github_actions)
        logger = RunLogger(config.runtime.timezone_name, config.runtime.time_format)
        response_recorder = ResponseRecorder(
            timezone_name=config.runtime.timezone_name,
            time_format=config.runtime.time_format,
            console_enabled=config.runtime.console_raw_response_enabled,
            file_enabled=config.runtime.txt_raw_response_enabled,
            console_truncate=config.runtime.response_console_truncate,
        )
        api_client = ApiClient(
            config.runtime.request_timeout_seconds, response_recorder
        )
        retry_manager = RetryManager(
            config.runtime.retry_attempts, config.runtime.retry_interval_seconds, logger
        )

        # 这些快照字段会被消息推送和 Summary 反复复用，所以会在流程最前面统一补齐。
        snapshot.current_time_text = format_datetime(
            now_in_timezone(config.runtime.timezone_name), config.runtime.time_format
        )
        snapshot.today_text = today_text(config.runtime.timezone_name)
        snapshot.force_push_active, snapshot.force_push_source = (
            resolve_force_push_with_source(config, environment.is_github_actions)
        )
        snapshot.config_sources_text = build_config_source_lines(
            config, environment, snapshot.force_push_source
        )
        snapshot.workflow_info_text = build_workflow_info_lines(
            environment, snapshot.force_push_active
        )
        snapshot.school_name = config.account.school_name
        snapshot.school_id = config.account.school_id
        snapshot.target_sign_type = config.sign.target_type

        logger.info("配置校验", f"当前运行环境为 {environment.source_label}")
        logger.info("配置校验", f"配置文件路径为 {config.config_path}")

        # 学校 ID 缺失时访问学校查询接口，已有值时直接沿用配置结果。
        school_id = resolve_school_id(
            config, api_client, retry_manager, logger, snapshot
        )
        snapshot.school_id = school_id

        # 登录阶段负责拿到 token、用户资料和积分信息，后续接口调用和推送内容都依赖这些结果。
        login_result = login_account(
            config, school_id, api_client, retry_manager, logger, snapshot
        )
        apply_login_snapshot(login_result, snapshot)

        # 第一次签到查询用来确认当天是否已签，并收集发起签到需要的资源字段。
        initial_sign_home = query_sign_home(
            api_client,
            retry_manager,
            logger,
            snapshot,
            stage="首次签到查询",
            stage_key="initial_query",
            token=snapshot.token,
        )
        initial_query_details = parse_sign_home(
            initial_sign_home,
            config,
            snapshot,
            stage="首次签到查询",
            require_remark=True,
            target_sign_type=config.sign.target_type,
        )
        snapshot.initial_query_message = initial_sign_home.message
        snapshot.initial_query_signed_today = initial_query_details["signed_today"]
        snapshot.recent_sign_times = initial_query_details["recent_sign_times"]
        snapshot.sign_in_month_count = str(initial_query_details["sign_in_month_count"])
        snapshot.continuous_sign_in = str(initial_query_details["continuous_sign_in"])

        # 发起签到前会先整理最终提交参数，包括地址、经纬度和签到类型。
        sign_payload = prepare_sign_payload(
            config.account,
            config.runtime.default_address_name,
            initial_query_details,
            config.sign.target_type,
        )
        apply_sign_payload_snapshot(sign_payload, snapshot)
        wait_before_sign(config, logger)

        # 发起签到后会先根据接口结果更新快照，最终状态仍要等待再次查询确认。
        sign_submit_result = submit_sign(
            api_client=api_client,
            retry_manager=retry_manager,
            logger=logger,
            snapshot=snapshot,
            token=snapshot.token,
            sign_payload=sign_payload,
        )
        apply_sign_submit_snapshot(sign_submit_result, snapshot)

        # 发起签到接口只有成功和重复签到两种业务码会继续进入验证阶段，其他情况直接按失败处理。
        if sign_submit_result.code not in {20000, 64032}:
            snapshot.final_status = "failure"
            snapshot.final_title = DEFAULT_TITLES["failure"]
            snapshot.reason = format_business_error(sign_submit_result)
            raise WorkflowError(
                "发起签到", snapshot.reason, sign_submit_result.endpoint_name
            )

        # 第二次签到查询专门用来确认今天最终是否已经签到成功。
        verify_sign_home = query_sign_home(
            api_client,
            retry_manager,
            logger,
            snapshot,
            stage="签到后验证",
            stage_key="verify_query",
            token=snapshot.token,
        )
        verify_query_details = parse_sign_home(
            verify_sign_home,
            config,
            snapshot,
            stage="签到后验证",
            require_remark=False,
            target_sign_type=config.sign.target_type,
        )
        snapshot.verify_query_signed_today = verify_query_details["signed_today"]
        snapshot.verify_query_message = verify_sign_home.message
        snapshot.recent_sign_times = verify_query_details["recent_sign_times"]
        snapshot.sign_in_month_count = str(verify_query_details["sign_in_month_count"])
        snapshot.continuous_sign_in = str(verify_query_details["continuous_sign_in"])

        # 最终状态必须同时结合发起签到接口结果和再次查询结果判定。
        finalize_sign_result(snapshot)
        logger.success("签到判定", snapshot.reason)

        # 默认规则下重复签到不推送，其他状态再按配置决定是否发送消息。
        emit_notification_if_needed(
            config, environment, snapshot, response_recorder, logger
        )
        return 0 if snapshot.final_status in {"success", "repeated"} else 1

    except ConfigError as exc:
        # 配置错误发生时，日志器还未必已经初始化，所以这里需要兼容直接打印。
        snapshot.final_status = "exception"
        snapshot.final_title = DEFAULT_TITLES["exception"]
        snapshot.reason = str(exc)
        snapshot.error_stage = "配置校验"
        if logger:
            logger.error("配置校验", snapshot.reason)
        else:
            print(f"[配置校验] {snapshot.reason}")
        write_minimal_summary_if_possible(environment, snapshot)
        return 1
    except WorkflowError as exc:
        # 业务阶段主动抛出的结构化异常会直接携带阶段名、接口名和最终状态。
        if not snapshot.final_status:
            snapshot.final_status = exc.final_status
            snapshot.final_title = DEFAULT_TITLES.get(
                exc.final_status, DEFAULT_TITLES["exception"]
            )
            snapshot.reason = exc.detail
        snapshot.error_stage = exc.stage
        snapshot.error_endpoint = exc.endpoint_name
        if logger:
            logger.error(exc.stage, exc.detail)
        emit_notification_if_possible(
            config, environment, snapshot, response_recorder, logger
        )
        return 1
    except Exception as exc:
        # 兜底异常路径负责补足堆栈、文件和行号，保证错误定位信息完整。
        snapshot.final_status = "exception"
        snapshot.final_title = DEFAULT_TITLES["exception"]
        snapshot.reason = f"程序出现未处理异常: {exc}"
        snapshot.error_stage = snapshot.error_stage or "主流程"
        snapshot.error_traceback = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
        snapshot.error_file, snapshot.error_line = extract_exception_location(exc)
        if logger:
            logger.error(snapshot.error_stage, snapshot.reason)
            logger.error(snapshot.error_stage, snapshot.error_traceback.strip())
        else:
            print(f"[{snapshot.error_stage}] {snapshot.reason}")
            print(snapshot.error_traceback.strip())
        write_minimal_summary_if_possible(environment, snapshot)
        emit_notification_if_possible(
            config, environment, snapshot, response_recorder, logger
        )
        return 1
    finally:
        # 原始响应落盘、缓存清理、Summary 写入和客户端关闭，无论成功还是失败都必须执行。
        if logger and config:
            snapshot.current_time_text = snapshot.current_time_text or format_datetime(
                now_in_timezone(config.runtime.timezone_name),
                config.runtime.time_format,
            )
            if response_recorder:
                # 原始响应输出路径要先写回快照，后面的 Summary 和日志才能直接引用。
                snapshot.raw_response_output_path = response_recorder.finalize()
                if snapshot.raw_response_output_path:
                    logger.info(
                        "原始响应输出",
                        f"原始响应已写入 {snapshot.raw_response_output_path}",
                    )
        cleanup_logger = logger or RunLogger("Asia/Shanghai", "%Y-%m-%d %H:%M:%S")
        cleanup_runtime_artifacts(str(Path.cwd()), cleanup_logger)
        if logger and config:
            snapshot.timeline = logger.timeline
            write_summary_if_needed(config, environment, snapshot, logger)
        if api_client:
            api_client.close()


def build_environment() -> RunEnvironment:
    # 运行环境上下文全部从当前进程环境变量提取。
    # 当前是否运行在 GitHub Actions 中，只由 GITHUB_ACTIONS 的值决定。
    is_github_actions = os.getenv("GITHUB_ACTIONS", "").strip().lower() == "true"
    source_label = "GitHub Actions" if is_github_actions else "本地运行"
    workflow_ref = os.getenv("GITHUB_WORKFLOW_REF", "").strip()
    workflow_sha = os.getenv("GITHUB_WORKFLOW_SHA", "").strip()
    branch_name = os.getenv("GITHUB_REF_NAME", "").strip()
    if not branch_name:
        raw_ref = os.getenv("GITHUB_REF", "").strip()
        if raw_ref.startswith("refs/heads/"):
            # GITHUB_REF_NAME 缺失时，会从完整 ref 中提取分支名作为回退值。
            branch_name = raw_ref.removeprefix("refs/heads/")
    return RunEnvironment(
        is_github_actions=is_github_actions,
        source_label=source_label,
        started_at=datetime.now(timezone.utc),
        event_name=os.getenv("GITHUB_EVENT_NAME", "").strip(),
        workflow_name=os.getenv("GITHUB_WORKFLOW", "").strip(),
        workflow_ref=workflow_ref,
        workflow_sha=workflow_sha,
        repository_name=os.getenv("GITHUB_REPOSITORY", "").strip(),
        branch_name=branch_name,
        commit_sha=os.getenv("GITHUB_SHA", "").strip(),
        actor_name=os.getenv("GITHUB_ACTOR", "").strip(),
        actor_id=os.getenv("GITHUB_ACTOR_ID", "").strip(),
        triggering_actor=os.getenv("GITHUB_TRIGGERING_ACTOR", "").strip(),
        run_id=os.getenv("GITHUB_RUN_ID", "").strip(),
        run_number=os.getenv("GITHUB_RUN_NUMBER", "").strip(),
    )


def build_config_source_lines(
    config: ResolvedConfig, environment: RunEnvironment, force_push_source: str
) -> list[str]:
    # 这里把 source_map 中的来源标识翻译成可直接写进 Summary 的中文说明。
    ordered_keys = [
        "runtime.timezone",
        "runtime.time_format",
        "runtime.console_raw_response_enabled",
        "runtime.txt_raw_response_enabled",
        "runtime.response_console_truncate",
        "runtime.default_address_name",
        "runtime.request_timeout_seconds",
        "runtime.retry_attempts",
        "runtime.retry_interval_seconds",
        "runtime.sign_submit_delay_seconds",
        "account.school_name",
        "account.school_id",
        "account.account",
        "account.password",
        "account.address",
        "account.address_name",
        "account.longitude",
        "account.latitude",
        "sign.target_type",
        "notification.enabled",
        "notification.force_push",
        "notification.request.url",
        "notification.request.method",
        "notification.request.body_type",
    ]
    remaining_keys = sorted(key for key in config.source_map if key not in ordered_keys)
    lines: list[str] = []

    for key in ordered_keys + remaining_keys:
        # notification.force_push 的来源存在被工作流输入覆盖的情况，所以会单独使用外部传入的来源值。
        source_id = (
            force_push_source
            if key == "notification.force_push"
            else config.source_map.get(key, "")
        )
        if not source_id:
            continue
        lines.append(
            f"{describe_source_key(key)}：{describe_source_value(source_id, environment, config)}"
        )
    return lines


def describe_source_key(source_key: str) -> str:
    # 配置字段标识统一在这里转成适合展示的中文名称。
    # 动态请求模板字段保留原始键名，避免丢失真实请求结构。
    static_labels = {
        "runtime.timezone": "运行时区",
        "runtime.time_format": "时间格式",
        "runtime.console_raw_response_enabled": "控制台原始响应输出开关",
        "runtime.txt_raw_response_enabled": "TXT 原始响应输出开关",
        "runtime.response_console_truncate": "控制台原始响应截断长度",
        "runtime.default_address_name": "默认地址名称",
        "runtime.request_timeout_seconds": "接口超时时间",
        "runtime.retry_attempts": "重试次数",
        "runtime.retry_interval_seconds": "重试间隔秒数",
        "runtime.sign_submit_delay_seconds": "发起签到前等待秒数",
        "account.school_name": "学校名称",
        "account.school_id": "学校 ID",
        "account.account": "账号",
        "account.password": "密码",
        "account.address": "详细地址",
        "account.address_name": "地址名称",
        "account.longitude": "经度",
        "account.latitude": "纬度",
        "sign.target_type": "目标签到类型",
        "notification.enabled": "消息推送开关",
        "notification.force_push": "强制推送开关",
        "notification.request.url": "消息推送地址",
        "notification.request.method": "消息推送方法",
        "notification.request.body_type": "消息推送请求体类型",
        "github_actions.secret_overrides_env": "整包 Secret 环境变量名",
        "github_actions.manual_force_push_env": "强制推送环境变量名",
    }
    if source_key in static_labels:
        return static_labels[source_key]
    if source_key.startswith("notification.request.headers."):
        return "消息推送请求头 " + source_key.removeprefix(
            "notification.request.headers."
        )
    if source_key.startswith("notification.request.query_params."):
        return "消息推送查询参数 " + source_key.removeprefix(
            "notification.request.query_params."
        )
    if source_key.startswith("notification.request.body_fields."):
        return "消息推送请求体字段 " + source_key.removeprefix(
            "notification.request.body_fields."
        )
    return source_key


def describe_source_value(
    source_id: str, environment: RunEnvironment, config: ResolvedConfig
) -> str:
    # 来源说明只描述值来自哪里，不泄露任何真实敏感值。
    if source_id == "json":
        return "已从 JSON 配置文件读取"
    if source_id == "default":
        return "已使用程序默认值"
    if source_id == "empty":
        return "当前没有提供有效值"
    if source_id == "message_variable":
        return "已从运行时消息变量读取"
    if source_id.startswith("secret_json:"):
        return (
            f"已从 GitHub Repository Secret {config.github_secret_overrides_env} 读取"
        )
    if source_id.startswith("workflow_input:"):
        env_name = source_id.removeprefix("workflow_input:")
        return f"已从 GitHub Actions 手动输入项映射到环境变量 {env_name} 读取"
    if source_id.startswith("env:"):
        env_name = source_id.removeprefix("env:")
        if environment.is_github_actions:
            if env_name.startswith("XIXUNYUN_"):
                return f"已从 GitHub Repository Secret 映射到环境变量 {env_name} 读取"
            return f"已从 GitHub Actions 环境变量 {env_name} 读取"
        return f"已从本地环境变量 {env_name} 读取"
    return f"已从 {source_id} 读取"


def build_workflow_info_lines(
    environment: RunEnvironment, force_push_active: bool
) -> list[str]:
    # 工作流信息区块只展示当前运行环境里能够实际拿到的上下文字段。
    beijing_time_text = format_datetime(
        now_in_timezone("Asia/Shanghai"), "%Y-%m-%d %H:%M:%S:%f"
    )[:-3]
    initiated_run_by = environment.triggering_actor or environment.actor_name
    return [
        f"Force Push Message：{str(force_push_active)}",
        f"Branch Name：{describe_runtime_value(environment.branch_name, '当前环境未提供 GITHUB_REF_NAME 或 GITHUB_REF')}",
        f"Triggered By：{describe_runtime_value(environment.event_name, '当前环境未提供 GITHUB_EVENT_NAME')}",
        f"Initial Run By：{describe_runtime_value(environment.actor_name, '当前环境未提供 GITHUB_ACTOR')}",
        f"Initial Run By ID：{describe_runtime_value(environment.actor_id, '当前环境未提供 GITHUB_ACTOR_ID')}",
        f"Initiated Run By：{describe_runtime_value(initiated_run_by, '当前环境未提供 GITHUB_TRIGGERING_ACTOR')}",
        f"Repository Name：{describe_runtime_value(environment.repository_name, '当前环境未提供 GITHUB_REPOSITORY')}",
        f"Commit SHA：{describe_runtime_value(environment.commit_sha, '当前环境未提供 GITHUB_SHA')}",
        f"Workflow Name：{describe_runtime_value(resolve_workflow_name(environment), '当前环境未提供 GITHUB_WORKFLOW')}",
        f"Workflow Number：{describe_runtime_value(environment.run_number, '当前环境未提供 GITHUB_RUN_NUMBER')}",
        f"Workflow ID：{describe_runtime_value(environment.run_id, '当前环境未提供 GITHUB_RUN_ID')}",
        f"Beijing Time：{beijing_time_text}",
        "Copyright © 2026 NianBroken. All rights reserved.",
    ]


def resolve_workflow_name(environment: RunEnvironment) -> str:
    # 工作流名称优先使用环境变量里的直接值，缺失时再从 workflow_ref 中提取。
    workflow_name = environment.workflow_name.strip()
    if workflow_name:
        return workflow_name
    workflow_ref = environment.workflow_ref.strip()
    if workflow_ref:
        return workflow_ref.rsplit("@", 1)[0].split("/")[-1]
    return ""


def describe_runtime_value(value: str, missing_message: str) -> str:
    # 工作流信息里缺少值时会直接返回明确提示文本，而不是保留空字符串。
    text = value.strip()
    if text:
        return text
    return missing_message


def set_stage_result(
    snapshot: ExecutionSnapshot,
    stage_key: str,
    code: object,
    status: str,
    detail: str = "",
) -> None:
    # 所有阶段结果都只写入这一份字典，Summary 和消息逻辑统一从这里读取。
    snapshot.stage_results[stage_key] = StageResult(
        code=normalize_text(code), status=status, detail=detail.strip()
    )


def normalize_text(value: object) -> str:
    # 任意对象转展示文本时，都会先在这里做空值和首尾空白处理。
    if value is None:
        return ""
    return str(value).strip()


def get_result_status(result: ApiCallResult) -> str:
    # 接口结果统一归类为 success、failure 或 exception，供阶段结果记录复用。
    if result.error_text or result.http_status is None:
        return "exception"
    if not isinstance(result.json_body, dict):
        return "exception"
    if result.code == 20000:
        return "success"
    return "failure"


def resolve_school_id(
    config: ResolvedConfig,
    api_client: ApiClient,
    retry_manager: RetryManager,
    logger: RunLogger,
    snapshot: ExecutionSnapshot,
) -> str:
    # 学校 ID 已存在时会直接跳过学校查询接口，严格遵守配置优先规则。
    if config.account.school_id:
        logger.success("学校 ID 获取", "已直接使用配置中的学校 ID，不访问学校查询接口")
        set_stage_result(
            snapshot, "school_id", "未执行", "skipped", "已直接使用配置中的学校 ID"
        )
        return config.account.school_id

    target_school_name = config.account.school_name.strip()

    def action(_: int) -> ApiCallResult:
        # 学校查询接口不依赖额外参数，会直接请求完整学校映射列表。
        return api_client.request(SCHOOL_MAP_ENDPOINT, stage="学校 ID 获取")

    def should_retry(result: ApiCallResult) -> tuple[bool, str]:
        # 只有拿到成功业务码并且能精确匹配学校名称时，才算本阶段成功。
        if result.error_text or result.http_status is None:
            return True, format_business_error(result)
        if result.code != 20000:
            return True, format_business_error(result)
        matched_school_id = find_school_id(result.json_body, target_school_name)
        if not matched_school_id:
            return True, "学校名称精确匹配失败，未找到对应学校"
        return False, ""

    result = retry_manager.execute("学校 ID 获取", action, should_retry)
    matched_school_id = find_school_id(result.json_body, target_school_name)
    if not matched_school_id:
        # 重试结束后仍然找不到学校时，会直接视为当前阶段失败。
        set_stage_result(
            snapshot,
            "school_id",
            result.code,
            get_result_status(result),
            "学校名称精确匹配失败，未找到对应学校",
        )
        raise WorkflowError(
            "学校 ID 获取",
            "学校名称精确匹配失败，未找到对应学校",
            SCHOOL_MAP_ENDPOINT.name,
        )
    logger.success("学校 ID 获取", "已成功解析学校 ID")
    set_stage_result(snapshot, "school_id", result.code, "success", "已成功解析学校 ID")
    snapshot.school_name = target_school_name
    return matched_school_id


def find_school_id(json_body: dict | None, target_school_name: str) -> str:
    # 学校列表按省份分组返回，所以要遍历各省份列表中的学校项做精确名称匹配。
    if not isinstance(json_body, dict):
        return ""
    data = json_body.get("data", [])
    if not isinstance(data, list):
        return ""
    for province in data:
        if not isinstance(province, dict):
            continue
        for school in province.get("list", []):
            if not isinstance(school, dict):
                continue
            if str(school.get("school_name", "")).strip() == target_school_name:
                return str(school.get("school_id", "")).strip()
    return ""


def login_account(
    config: ResolvedConfig,
    school_id: str,
    api_client: ApiClient,
    retry_manager: RetryManager,
    logger: RunLogger,
    snapshot: ExecutionSnapshot,
) -> ApiCallResult:
    # 登录接口的 version 参数会根据当前年份自动生成。
    current_year = None
    try:
        current_year = now_in_timezone(config.runtime.timezone_name).year
    except Exception:
        current_year = None
    version_value = build_login_version(current_year)

    def action(_: int) -> ApiCallResult:
        # 登录请求会同时提交账号、密码、学校 ID 和固定的 request_source。
        return api_client.request(
            LOGIN_ENDPOINT,
            stage="账号登录",
            params={"version": version_value},
            data={
                "account": config.account.account,
                "password": config.account.password,
                "school_id": school_id,
                "request_source": "3",
            },
        )

    def should_retry(result: ApiCallResult) -> tuple[bool, str]:
        # 登录阶段只有业务码 20000 才算成功。
        if result.error_text or result.http_status is None:
            return True, format_business_error(result)
        if result.code != 20000:
            return True, format_business_error(result)
        return False, ""

    result = retry_manager.execute("账号登录", action, should_retry)
    set_stage_result(
        snapshot,
        "login",
        result.code,
        get_result_status(result),
        format_business_error(result)
        if result.code != 20000
        else "登录成功，已获取用户资料和 token",
    )
    if result.code != 20000:
        raise WorkflowError(
            "账号登录", format_business_error(result), LOGIN_ENDPOINT.name
        )
    logger.success("账号登录", "登录成功，已获取用户资料和 token")
    return result


def apply_login_snapshot(
    login_result: ApiCallResult, snapshot: ExecutionSnapshot
) -> None:
    # 登录成功后，会把消息正文和 Summary 需要的用户资料统一回填到快照。
    data = (
        login_result.json_body.get("data", {})
        if isinstance(login_result.json_body, dict)
        else {}
    )
    snapshot.token = str(data.get("token", "")).strip()
    snapshot.user_id = str(data.get("user_id", "")).strip()
    snapshot.user_number = str(data.get("user_number", "")).strip()
    snapshot.user_name = str(data.get("user_name", "")).strip()
    snapshot.class_name = str(data.get("class_name", "")).strip()
    snapshot.point = str(data.get("point", "")).strip()
    snapshot.point_rank = str(data.get("point_rank", "")).strip()
    snapshot.entrance_year = str(data.get("entrance_year", "")).strip()
    snapshot.graduation_year = str(data.get("graduation_year", "")).strip()
    if not snapshot.token:
        # 登录接口业务码成功但缺少 token 时，整个签到流程无法继续。
        set_stage_result(
            snapshot,
            "login",
            login_result.code,
            "exception",
            "登录接口返回成功，但响应中缺少 token",
        )
        raise WorkflowError(
            "账号登录",
            "登录接口返回成功，但响应中缺少 token",
            LOGIN_ENDPOINT.name,
            final_status="exception",
        )


def query_sign_home(
    api_client: ApiClient,
    retry_manager: RetryManager,
    logger: RunLogger,
    snapshot: ExecutionSnapshot,
    stage: str,
    stage_key: str,
    token: str,
) -> ApiCallResult:
    # 首次签到查询和签到后验证复用同一套查询逻辑，差异只体现在阶段名和阶段键上。
    def action(_: int) -> ApiCallResult:
        return api_client.request(
            SIGN_HOME_ENDPOINT,
            stage=stage,
            params={"token": token},
        )

    def should_retry(result: ApiCallResult) -> tuple[bool, str]:
        # 查询接口只有业务码 20000 才表示本阶段可以继续向下处理。
        if result.error_text or result.http_status is None:
            return True, format_business_error(result)
        if result.code != 20000:
            return True, format_business_error(result)
        return False, ""

    result = retry_manager.execute(stage, action, should_retry)
    set_stage_result(
        snapshot,
        stage_key,
        result.code,
        get_result_status(result),
        format_business_error(result) if result.code != 20000 else result.message,
    )
    if result.code != 20000:
        # 请求异常、HTTP 状态缺失或 JSON 不合法时归为 exception，其余非 20000 归为 failure。
        if result.error_text:
            final_status = "exception"
        elif result.http_status is None:
            final_status = "exception"
        elif not isinstance(result.json_body, dict):
            final_status = "exception"
        else:
            final_status = "failure"
        raise WorkflowError(
            stage,
            format_business_error(result),
            SIGN_HOME_ENDPOINT.name,
            final_status=final_status,
        )
    logger.success(stage, "签到查询接口返回成功")
    return result


def parse_sign_home(
    sign_home_result: ApiCallResult,
    config: ResolvedConfig,
    snapshot: ExecutionSnapshot,
    stage: str,
    require_remark: bool,
    target_sign_type: str,
) -> dict[str, object]:
    # 签到查询接口里可供后续流程使用的数据，主要位于 data 和 sign_resources_info 中。
    data = (
        sign_home_result.json_body.get("data", {})
        if isinstance(sign_home_result.json_body, dict)
        else {}
    )
    if not isinstance(data, dict):
        raise WorkflowError(
            stage, "签到查询接口的 data 字段不是对象结构", SIGN_HOME_ENDPOINT.name
        )

    # 首次签到查询必须先从 mark_list 中找到配置要求的签到类型键值。
    remark_value = ""
    mark_list = data.get("mark_list")
    if require_remark:
        if not isinstance(mark_list, list) or not mark_list:
            raise WorkflowError(
                stage,
                "签到查询接口缺少可选签到类型列表 mark_list",
                SIGN_HOME_ENDPOINT.name,
                final_status="exception",
            )
        remark_value = find_sign_type_remark(mark_list, target_sign_type)
        if not remark_value:
            available_sign_types = collect_sign_type_values(mark_list)
            available_types_text = (
                "，".join(available_sign_types)
                if available_sign_types
                else "mark_list 中没有可用的签到类型值"
            )
            raise WorkflowError(
                stage,
                (
                    "签到查询接口存在 mark_list，但找不到值为 "
                    f"{target_sign_type}"
                    " 的签到类型。当前可选签到类型为 "
                    f"{available_types_text}"
                ),
                SIGN_HOME_ENDPOINT.name,
                final_status="exception",
            )

    # sign_resources_info 提供签到地址和经纬度兜底值，sign_in_month 提供最近签到历史。
    sign_resources_info = (
        data.get("sign_resources_info")
        if isinstance(data.get("sign_resources_info"), dict)
        else {}
    )
    sign_in_month = (
        data.get("sign_in_month") if isinstance(data.get("sign_in_month"), list) else []
    )
    signed_today = has_signed_today(
        data, sign_in_month, snapshot.today_text, config.runtime.timezone_name
    )
    recent_sign_times = parse_sign_history_items(
        sign_in_month, config.runtime.timezone_name, config.runtime.time_format, limit=5
    )

    return {
        "remark": remark_value,
        "signed_today": signed_today,
        "sign_in_month_count": len(sign_in_month),
        "continuous_sign_in": data.get("continuous_sign_in", ""),
        "recent_sign_times": recent_sign_times,
        "sign_resources_info": sign_resources_info,
    }


def has_signed_today(
    data: dict, sign_in_month: list[dict], today_value: str, timezone_name: str
) -> bool:
    # 当天签到判定优先检查 sign_in_month 列表，再回退到根节点 sign_time 文本。
    for item in sign_in_month:
        if not isinstance(item, dict):
            continue
        if str(item.get("sign_time_text", "")).strip() == today_value:
            return True
        sign_timestamp = item.get("sign_time")
        if sign_timestamp not in (None, ""):
            try:
                # 时间戳换算后命中当天日期时，同样视为今天已经签到。
                local_date = datetime.fromtimestamp(
                    float(sign_timestamp), tz=timezone.utc
                ).astimezone(now_in_timezone(timezone_name).tzinfo)
                if local_date.strftime("%Y-%m-%d") == today_value:
                    return True
            except Exception:
                continue

    root_sign_time = str(data.get("sign_time", "")).strip()
    if root_sign_time and root_sign_time.startswith(today_value):
        return True
    return False


def prepare_sign_payload(
    account: AccountConfig,
    default_address_name: str,
    initial_query_details: dict[str, object],
    target_sign_type: str,
) -> dict[str, str]:
    # 发起签到前的最终提交参数会在这里统一整理。
    # 用户已经填写的字段始终优先，只有空缺项才从首次签到查询结果中补值。
    sign_resources_info = initial_query_details.get("sign_resources_info", {})
    if not isinstance(sign_resources_info, dict):
        sign_resources_info = {}

    address_value = (
        account.address or str(sign_resources_info.get("mid_sign_address", "")).strip()
    )
    address_name_value = account.address_name or default_address_name
    longitude_value = account.longitude or stringify_number(
        sign_resources_info.get("mid_sign_longitude")
    )
    latitude_value = account.latitude or stringify_number(
        sign_resources_info.get("mid_sign_latitude")
    )
    remark_value = str(initial_query_details.get("remark", "")).strip()

    # 这些字段任何一个缺失都会导致发起签到接口无法满足要求，所以会统一在这里提前拦截。
    if not address_value:
        raise WorkflowError(
            "发起签到前的数据准备",
            "详细地址缺失，用户配置和签到查询接口都没有可用值",
            final_status="failure",
        )
    if not address_name_value:
        raise WorkflowError(
            "发起签到前的数据准备",
            "地址名称缺失，且默认地址名称也为空",
            final_status="failure",
        )
    if not longitude_value:
        raise WorkflowError(
            "发起签到前的数据准备",
            "经度缺失，用户配置和签到查询接口都没有可用值",
            final_status="failure",
        )
    if not latitude_value:
        raise WorkflowError(
            "发起签到前的数据准备",
            "纬度缺失，用户配置和签到查询接口都没有可用值",
            final_status="failure",
        )
    if not remark_value:
        raise WorkflowError(
            "发起签到前的数据准备",
            "签到类型缺失，无法确定 "
            f"{target_sign_type}"
            " 对应的 remark",
            final_status="failure",
        )

    return {
        "address": address_value,
        "address_name": address_name_value,
        "longitude": longitude_value,
        "latitude": latitude_value,
        "remark": remark_value,
    }


def apply_sign_payload_snapshot(
    sign_payload: dict[str, str], snapshot: ExecutionSnapshot
) -> None:
    # 快照里保留的是加密前的明文字段，便于日志、消息和 Summary 直接展示。
    snapshot.sign_address = sign_payload["address"]
    snapshot.sign_address_name = sign_payload["address_name"]
    snapshot.sign_longitude = sign_payload["longitude"]
    snapshot.sign_latitude = sign_payload["latitude"]
    snapshot.sign_type_remark = sign_payload["remark"]


def stringify_number(value: object) -> str:
    # 接口返回的经纬度可能是数字或字符串，这里统一转成去空白后的字符串。
    if value in (None, ""):
        return ""
    return str(value).strip()


def find_sign_type_remark(mark_list: list[object], target_sign_type: str) -> str:
    # 这个函数专门负责从 mark_list 中找出配置指定的签到类型对应的 key。
    normalized_target = target_sign_type.strip()
    for item in mark_list:
        if not isinstance(item, dict):
            continue
        current_value = str(item.get("value", "")).strip()
        if current_value != normalized_target:
            continue
        return str(item.get("key", "")).strip()
    return ""


def collect_sign_type_values(mark_list: list[object]) -> list[str]:
    # 这里会把 mark_list 中全部可读的签到类型值整理出来，供错误提示直接复用。
    values: list[str] = []
    for item in mark_list:
        if not isinstance(item, dict):
            continue
        current_value = str(item.get("value", "")).strip()
        if current_value and current_value not in values:
            values.append(current_value)
    return values


def wait_before_sign(config: ResolvedConfig, logger: RunLogger) -> None:
    # 发起签到前是否等待完全由运行配置决定，等待逻辑只执行一次。
    delay_seconds = config.runtime.sign_submit_delay_seconds
    if delay_seconds <= 0:
        return
    logger.info("发起签到前等待", f"按配置等待 {delay_seconds} 秒后发起签到")
    time.sleep(delay_seconds)
    logger.info("发起签到前等待", "等待结束，开始发起签到")


def submit_sign(
    api_client: ApiClient,
    retry_manager: RetryManager,
    logger: RunLogger,
    snapshot: ExecutionSnapshot,
    token: str,
    sign_payload: dict[str, str],
) -> ApiCallResult:
    # 发起签到前会在每次尝试内部重新加密经纬度，避免把上一次尝试生成的密文复用于下一次请求。
    def action(_: int) -> ApiCallResult:
        encrypted_longitude = encrypt_coordinate(sign_payload["longitude"])
        encrypted_latitude = encrypt_coordinate(sign_payload["latitude"])
        return api_client.request(
            SIGN_SUBMIT_ENDPOINT,
            stage="发起签到",
            data={
                "token": token,
                "address": sign_payload["address"],
                "address_name": sign_payload["address_name"],
                "longitude": encrypted_longitude,
                "latitude": encrypted_latitude,
                "remark": sign_payload["remark"],
            },
        )

    def should_retry(result: ApiCallResult) -> tuple[bool, str]:
        # 发起签到接口把重复签到业务码 64032 视为可接受结果，所以不会继续重试。
        if result.error_text or result.http_status is None:
            return True, format_business_error(result)
        if result.code in {20000, 64032}:
            return False, ""
        return True, format_business_error(result)

    result = retry_manager.execute("发起签到", action, should_retry)
    # 阶段结果里会单独把 64032 归类为 repeated，方便最终状态和消息文案区分。
    submit_status = "repeated" if result.code == 64032 else get_result_status(result)
    submit_detail = result.message if result.message else format_business_error(result)
    set_stage_result(snapshot, "sign_submit", result.code, submit_status, submit_detail)
    if result.code in {20000, 64032}:
        logger.success("发起签到", f"发起签到接口返回业务码 {result.code}")
    return result


def apply_sign_submit_snapshot(
    sign_submit_result: ApiCallResult, snapshot: ExecutionSnapshot
) -> None:
    # 发起签到接口返回的业务码、消息和积分信息会统一写回快照。
    snapshot.sign_api_code = str(sign_submit_result.code or "")
    snapshot.sign_api_message = sign_submit_result.message
    data = (
        sign_submit_result.json_body.get("data", {})
        if isinstance(sign_submit_result.json_body, dict)
        else {}
    )
    if isinstance(data, dict):
        snapshot.sign_point = str(data.get("point", "")).strip()
        if not snapshot.continuous_sign_in:
            snapshot.continuous_sign_in = str(data.get("continuous", "")).strip()


def finalize_sign_result(snapshot: ExecutionSnapshot) -> None:
    # 最终状态判定同时依赖发起签到接口业务码和签到后验证结果。
    if snapshot.sign_api_code == "20000" and snapshot.verify_query_signed_today:
        snapshot.final_status = "success"
        snapshot.final_title = DEFAULT_TITLES["success"]
        snapshot.reason = "发起签到接口返回成功，签到查询接口确认今天已签到"
        return

    if snapshot.sign_api_code == "64032" and snapshot.verify_query_signed_today:
        snapshot.final_status = "repeated"
        snapshot.final_title = DEFAULT_TITLES["repeated"]
        snapshot.reason = (
            "发起签到接口返回今日签到次数已满，签到查询接口确认今天已经签过到"
        )
        return

    # 其余所有组合都视为未能确认签到成功。
    snapshot.final_status = "failure"
    snapshot.final_title = DEFAULT_TITLES["failure"]
    snapshot.reason = "发起签到接口和签到查询接口未能同时确认今天签到成功"


def emit_notification_if_needed(
    config: ResolvedConfig,
    environment: RunEnvironment,
    snapshot: ExecutionSnapshot,
    response_recorder: ResponseRecorder,
    logger: RunLogger,
) -> None:
    # 默认情况下成功、失败和异常都会推送，重复签到只有在强制推送开启时才发送消息。
    if not config.notification.enabled:
        logger.info("消息推送", "推送功能未启用，跳过发送消息")
        set_stage_result(
            snapshot, "notification", "未执行", "skipped", "推送功能未启用"
        )
        return

    if snapshot.final_status == "repeated" and not snapshot.force_push_active:
        logger.info("消息推送", "当前结果为重复签到，且未启用强制推送，默认不发送消息")
        set_stage_result(
            snapshot, "notification", "未执行", "skipped", "重复签到且未启用强制推送"
        )
        return

    emit_notification(config, environment, snapshot, response_recorder, logger)


def emit_notification_if_possible(
    config: ResolvedConfig | None,
    environment: RunEnvironment,
    snapshot: ExecutionSnapshot,
    response_recorder: ResponseRecorder | None,
    logger: RunLogger | None,
) -> None:
    # 异常路径下也会尽量尝试推送通知，但前提是配置、日志器和响应记录器都已经准备完成。
    if not config or not response_recorder or not logger:
        return
    if not config.notification.enabled:
        logger.info("消息推送", "推送功能未启用，异常消息不发送")
        return
    emit_notification(config, environment, snapshot, response_recorder, logger)


def emit_notification(
    config: ResolvedConfig,
    environment: RunEnvironment,
    snapshot: ExecutionSnapshot,
    response_recorder: ResponseRecorder,
    logger: RunLogger,
) -> None:
    # 真正的推送动作集中在这里执行，主流程不需要了解具体请求模板结构。
    snapshot.environment_label = environment.source_label
    try:
        # 先根据快照生成消息变量，再交给动态请求模板完成发送。
        message_payload = build_message(snapshot, config.notification.message_layouts)
        notification_response = send_notification(
            config.notification.request_template,
            message_payload,
            config.runtime.request_timeout_seconds,
        )
        notification_json = (
            notification_response["json_body"]
            if isinstance(notification_response["json_body"], dict)
            else {}
        )
        notification_code = (
            notification_json.get("code", notification_response["http_status"])
            if isinstance(notification_json, dict)
            else notification_response["http_status"]
        )
        # 推送请求也会纳入原始响应记录，便于和业务接口一起排查。
        response_recorder.record(
            stage="消息推送",
            endpoint_name="消息推送",
            request_summary=notification_response["resolved_request"],
            response_payload=notification_response["json_body"],
            raw_text=notification_response["raw_text"],
            error_text="",
        )
        set_stage_result(
            snapshot,
            "notification",
            notification_code,
            "success",
            f"HTTP 状态码 {notification_response['http_status']}",
        )
        logger.success(
            "消息推送",
            f"消息已发送，HTTP 状态码为 {notification_response['http_status']}",
        )
    except Exception as exc:
        # 推送失败不会覆盖主流程最终状态，但会把错误上下文补进快照和日志。
        error_text = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
        snapshot.context_lines.append("消息推送发送失败")
        snapshot.context_lines.append(error_text.strip())
        set_stage_result(
            snapshot, "notification", "异常", "exception", f"消息发送失败: {exc}"
        )
        logger.error("消息推送", f"消息发送失败: {exc}")
        logger.error("消息推送", error_text.strip())


def write_summary_if_needed(
    config: ResolvedConfig,
    environment: RunEnvironment,
    snapshot: ExecutionSnapshot,
    logger: RunLogger,
) -> None:
    # 只有 GitHub Actions 环境才存在可写入的 Step Summary 文件。
    if not environment.is_github_actions:
        return

    summary_path = os.getenv("GITHUB_STEP_SUMMARY", "").strip()
    if not summary_path:
        logger.info("Summary", "当前环境未提供 GITHUB_STEP_SUMMARY，跳过写入")
        return

    # 写入前会再次同步时间线和工作流信息，确保 Summary 使用的是最终快照。
    snapshot.timeline = logger.timeline
    snapshot.environment_label = environment.source_label
    snapshot.workflow_info_text = build_workflow_info_lines(
        environment, snapshot.force_push_active
    )
    summary_text = build_summary(snapshot, config.notification.summary_layouts)
    Path(summary_path).write_text(summary_text, encoding="utf-8")


def write_minimal_summary_if_possible(
    environment: RunEnvironment, snapshot: ExecutionSnapshot
) -> None:
    # 配置尚未完整加载前如果已经失败，仍然会尽量在 GitHub Actions 中留下最小化摘要。
    if not environment.is_github_actions:
        return
    summary_path = os.getenv("GITHUB_STEP_SUMMARY", "").strip()
    if not summary_path:
        return
    lines = [
        "# 习讯云自动签到",
        "## 运行结果",
        f"最终状态：{snapshot.final_status or '签到异常'}",
        f"结果说明：{snapshot.reason or '无'}",
        f"运行来源：{environment.source_label}",
        f"出错阶段：{snapshot.error_stage or '主流程'}",
    ]
    workflow_info_lines = build_workflow_info_lines(
        environment, snapshot.force_push_active
    )
    if workflow_info_lines:
        lines.append("## 工作流信息")
        lines.extend(workflow_info_lines)
    Path(summary_path).write_text(join_summary_lines(lines) + "\n", encoding="utf-8")


def join_summary_lines(lines: list[str]) -> str:
    # 最小化 Summary 统一使用一行正文加一个空行的拼接格式。
    normalized_lines = [line.strip() for line in lines if line and line.strip()]
    return "\n\n".join(normalized_lines).strip()


def extract_exception_location(exc: Exception) -> tuple[str, str]:
    # 错误定位会直接取回溯最后一帧，作为当前异常最直接的出错位置。
    traceback_frames = traceback.extract_tb(exc.__traceback__)
    if not traceback_frames:
        return "", ""
    last_frame = traceback_frames[-1]
    return str(last_frame.filename), str(last_frame.lineno)


def run_bootstrap() -> int:
    # 这个引导函数只负责执行主流程，并把返回值交给底部入口转换为进程退出码。
    return main()


if __name__ == "__main__":
    raise SystemExit(run_bootstrap())

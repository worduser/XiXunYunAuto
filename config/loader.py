# 这个模块统一负责配置文件读取、补值、来源标记和运行前校验。
# 主流程只接收解析完成后的配置对象，不直接操作原始 JSON 结构。
from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from core.models import (
    AccountConfig,
    NotificationConfig,
    ResolvedConfig,
    SignConfig,
    RuntimeOptions,
)


# 这个环境变量名用于在 GitHub Actions 中直接接收消息推送开关。
# 当整包 Secret JSON 没有给出 notification.enabled 时，会回退到这里继续解析。
NOTIFICATION_ENABLED_ENV_NAME = "XIXUNYUN_NOTIFICATION_ENABLED"
DEFAULT_TIMEZONE_NAME = "Asia/Shanghai"
DEFAULT_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_ADDRESS_NAME = "中国"
DEFAULT_RESPONSE_CONSOLE_TRUNCATE = 1200
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_INTERVAL_SECONDS = 5
DEFAULT_SIGN_SUBMIT_DELAY_SECONDS = 3
DEFAULT_TARGET_SIGN_TYPE = "上班"
DEFAULT_SECRET_OVERRIDES_ENV_NAME = "XIXUNYUN_SECRET_OVERRIDES_JSON"
DEFAULT_FORCE_PUSH_ENV_NAME = "XIXUNYUN_FORCE_PUSH"


class ConfigError(Exception):
    # 这个异常用于表示配置内容本身存在问题，错误信息需要能直接指导用户定位。
    pass


def load_config(config_path: str, is_github_actions: bool) -> ResolvedConfig:
    # 配置读取流程固定为先加载 JSON，再按字段规则补 Secret JSON 和环境变量。
    config_file = Path(config_path)
    if not config_file.exists():
        raise ConfigError(f"配置文件不存在: {config_file.resolve()}")

    # 先取出顶层区块，整套解析逻辑都围绕这些固定区块展开。
    raw_config = _load_json_config(config_file)
    runtime_section = _ensure_mapping_value(raw_config.get("runtime", {}), "runtime")
    github_actions_section = _ensure_mapping_value(
        raw_config.get("github_actions", {}), "github_actions"
    )
    account_section = _ensure_mapping_value(raw_config.get("account", {}), "account")
    sign_section = _ensure_mapping_value(raw_config.get("sign", {}), "sign")
    notification_section = _ensure_mapping_value(
        raw_config.get("notification", {}), "notification"
    )

    # 这两个环境变量名本身也允许通过配置文件改写。
    secret_overrides_env = str(
        github_actions_section.get(
            "secret_overrides_env", DEFAULT_SECRET_OVERRIDES_ENV_NAME
        )
    ).strip()
    manual_force_push_env = str(
        github_actions_section.get("manual_force_push_env", DEFAULT_FORCE_PUSH_ENV_NAME)
    ).strip()
    secret_overrides = _load_secret_overrides(secret_overrides_env, is_github_actions)

    # source_map 负责记录每个字段最终命中的来源，供 Summary 直接复用。
    source_map: dict[str, str] = {}
    _mark_plain_section_sources(
        runtime_section,
        [
            "timezone",
            "time_format",
            "console_raw_response_enabled",
            "txt_raw_response_enabled",
            "response_console_truncate",
            "default_address_name",
            "request_timeout_seconds",
            "retry_attempts",
            "retry_interval_seconds",
            "sign_submit_delay_seconds",
        ],
        "runtime",
        source_map,
    )
    _mark_plain_section_sources(
        notification_section, ["force_push"], "notification", source_map
    )
    _mark_plain_section_sources(
        github_actions_section,
        ["secret_overrides_env", "manual_force_push_env"],
        "github_actions",
        source_map,
    )

    # runtime 区块字段不支持动态补值，缺失时只按默认值回退。
    timezone_name = _read_text_setting(
        runtime_section, "timezone", DEFAULT_TIMEZONE_NAME
    )
    time_format = _read_text_setting(
        runtime_section, "time_format", DEFAULT_TIME_FORMAT
    )
    default_address_name = _read_text_setting(
        runtime_section, "default_address_name", DEFAULT_ADDRESS_NAME
    )

    # runtime 对象承载的是主流程运行期直接消费的基础参数。
    # 这里会把 JSON 中的值统一转成主流程预期的数据类型。
    runtime = RuntimeOptions(
        timezone_name=timezone_name,
        time_format=time_format,
        console_raw_response_enabled=_read_bool_setting(
            runtime_section,
            "console_raw_response_enabled",
            default=False,
        ),
        txt_raw_response_enabled=_read_bool_setting(
            runtime_section,
            "txt_raw_response_enabled",
            default=False,
        ),
        response_console_truncate=_read_int_setting(
            runtime_section,
            "response_console_truncate",
            default=DEFAULT_RESPONSE_CONSOLE_TRUNCATE,
            minimum=1,
        ),
        default_address_name=default_address_name,
        request_timeout_seconds=_read_int_setting(
            runtime_section,
            "request_timeout_seconds",
            default=DEFAULT_REQUEST_TIMEOUT_SECONDS,
            minimum=1,
        ),
        retry_attempts=_read_int_setting(
            runtime_section,
            "retry_attempts",
            default=DEFAULT_RETRY_ATTEMPTS,
            minimum=1,
        ),
        retry_interval_seconds=_read_int_setting(
            runtime_section,
            "retry_interval_seconds",
            default=DEFAULT_RETRY_INTERVAL_SECONDS,
            minimum=0,
        ),
        sign_submit_delay_seconds=_read_int_setting(
            runtime_section,
            "sign_submit_delay_seconds",
            default=DEFAULT_SIGN_SUBMIT_DELAY_SECONDS,
            minimum=0,
        ),
    )

    # account 区块按字段级规则解析，支持 JSON、整包 Secret JSON 和字段级环境变量补值。
    # 这里返回的每个字段都已经是最终生效的字符串值，业务层不再关心来源细节。
    account = AccountConfig(
        school_name=_resolve_value(
            account_section,
            secret_overrides,
            ["account", "school_name"],
            is_github_actions,
            source_map,
        ),
        school_id=_resolve_value(
            account_section,
            secret_overrides,
            ["account", "school_id"],
            is_github_actions,
            source_map,
        ),
        account=_resolve_value(
            account_section,
            secret_overrides,
            ["account", "account"],
            is_github_actions,
            source_map,
        ),
        password=_resolve_value(
            account_section,
            secret_overrides,
            ["account", "password"],
            is_github_actions,
            source_map,
        ),
        address=_resolve_value(
            account_section,
            secret_overrides,
            ["account", "address"],
            is_github_actions,
            source_map,
        ),
        address_name=_resolve_value(
            account_section,
            secret_overrides,
            ["account", "address_name"],
            is_github_actions,
            source_map,
        ),
        longitude=_resolve_value(
            account_section,
            secret_overrides,
            ["account", "longitude"],
            is_github_actions,
            source_map,
        ),
        latitude=_resolve_value(
            account_section,
            secret_overrides,
            ["account", "latitude"],
            is_github_actions,
            source_map,
        ),
    )
    sign_target_type = _resolve_value(
        sign_section,
        secret_overrides,
        ["sign", "target_type"],
        is_github_actions,
        source_map,
    )
    if not sign_target_type:
        sign_target_type = DEFAULT_TARGET_SIGN_TYPE
        source_map["sign.target_type"] = "default"
    sign = SignConfig(target_type=sign_target_type)

    # 通知请求模板允许保留动态结构，因此会单独走模板合并逻辑。
    # 模板中的动态值来源和层级对象会在这里完成规整，发送阶段只读取最终模板。
    notification_request_template = _merge_request_template(
        _ensure_mapping_value(
            notification_section.get("request", {}), "notification.request"
        ),
        _ensure_mapping_value(
            _ensure_mapping_value(
                secret_overrides.get("notification", {}), "notification"
            ).get("request", {}),
            "notification.request",
        ),
        is_github_actions,
        source_map,
    )
    # enabled 和 force_push 分别承担是否发送通知以及重复签到是否继续发送的职责。
    notification_enabled = _resolve_notification_enabled(
        notification_section, secret_overrides, is_github_actions, source_map
    )
    notification = NotificationConfig(
        enabled=notification_enabled,
        force_push=_read_bool_setting(
            notification_section, "force_push", default=False
        ),
        request_template=notification_request_template,
        message_layouts=deepcopy(
            _ensure_mapping_value(
                notification_section.get("message_layouts", {}),
                "notification.message_layouts",
            )
        ),
        summary_layouts=deepcopy(
            _ensure_mapping_value(
                notification_section.get("summary_layouts", {}),
                "notification.summary_layouts",
            )
        ),
    )

    # 所有字段解析完成后再统一做运行前校验，避免主流程进入半配置状态。
    _validate_config(account, sign, runtime, notification)

    # 这里返回的配置对象就是主流程唯一使用的配置入口。
    # 原始 JSON、Secret JSON 和环境变量差异都已经折叠成统一结构。
    return ResolvedConfig(
        config_path=str(config_file.resolve()),
        runtime=runtime,
        github_secret_overrides_env=secret_overrides_env,
        github_force_push_env=manual_force_push_env,
        account=account,
        sign=sign,
        notification=notification,
        source_map=source_map,
    )


def resolve_force_push_bootstrap(config_path: str, is_github_actions: bool) -> bool:
    # 主流程在配置对象完全建立前，会先通过这里拿到一份尽量准确的强制推送状态。
    raw_config = _load_json_config_or_empty(config_path)
    github_actions_section = _ensure_mapping_value(
        raw_config.get("github_actions", {}), "github_actions"
    )
    notification_section = _ensure_mapping_value(
        raw_config.get("notification", {}), "notification"
    )
    force_push_env_name = str(
        github_actions_section.get("manual_force_push_env", DEFAULT_FORCE_PUSH_ENV_NAME)
    ).strip() or DEFAULT_FORCE_PUSH_ENV_NAME

    if is_github_actions:
        raw_env_value = os.getenv(force_push_env_name, "").strip()
        if raw_env_value:
            parsed_env_value = _parse_bool_value(raw_env_value)
            if parsed_env_value is not None:
                return parsed_env_value

    parsed_json_value = _parse_bool_value(notification_section.get("force_push"))
    if parsed_json_value is not None:
        return parsed_json_value
    return False


def resolve_force_push(config: ResolvedConfig, is_github_actions: bool) -> bool:
    # 这个兼容入口只返回布尔结果，底层仍然复用同时返回来源信息的实现。
    enabled, _ = resolve_force_push_with_source(config, is_github_actions)
    return enabled


def resolve_force_push_with_source(
    config: ResolvedConfig, is_github_actions: bool
) -> tuple[bool, str]:
    # 强制推送开关在 GitHub Actions 中优先读取手动输入，其次才回退到配置文件里的 force_push。
    if is_github_actions:
        # 工作流输入进入进程后统一按小写文本解析，避免大小写差异影响判断结果。
        raw_value = os.getenv(config.github_force_push_env, "").strip()
        if raw_value:
            parsed_value = _parse_bool_value(raw_value)
            if parsed_value is None:
                raise ConfigError(
                    f"环境变量 {config.github_force_push_env} 必须是 true 或 false"
                )
            return parsed_value, f"workflow_input:{config.github_force_push_env}"
    # 本地运行和未提供工作流输入时，统一沿用配置对象里的 force_push 结果。
    return config.notification.force_push, config.source_map.get(
        "notification.force_push", "json"
    )


def _resolve_notification_enabled(
    notification_section: dict[str, Any],
    secret_overrides: dict[str, Any],
    is_github_actions: bool,
    source_map: dict[str, str],
) -> bool:
    # 消息推送开关在 GitHub Actions 中优先读取 Repository Secret，再回退到 JSON。
    source_key = "notification.enabled"
    if is_github_actions:
        # 整包 Secret JSON 允许在不改动仓库配置文件的情况下直接覆盖通知开关。
        override_value = _deep_get(secret_overrides, ["notification", "enabled"])
        if not _is_blank(override_value):
            parsed_override_value = _parse_bool_value(override_value)
            if parsed_override_value is None:
                raise ConfigError(
                    "notification.enabled 在整包 Secret JSON 中必须是 true 或 false"
                )
            source_map[source_key] = "secret_json:notification"
            return parsed_override_value

        # 这个环境变量是通知开关的固定兜底入口，不依赖字段级 env_name 配置。
        raw_env_value = os.getenv(NOTIFICATION_ENABLED_ENV_NAME, "").strip()
        if raw_env_value:
            parsed_env_value = _parse_bool_value(raw_env_value)
            if parsed_env_value is None:
                raise ConfigError(
                    f"环境变量 {NOTIFICATION_ENABLED_ENV_NAME} 必须是 true 或 false"
                )
            source_map[source_key] = f"env:{NOTIFICATION_ENABLED_ENV_NAME}"
            return parsed_env_value

    if "enabled" in notification_section:
        # JSON 中字段存在时，会按布尔值规则严格解析。
        parsed_json_value = _parse_bool_value(notification_section.get("enabled"))
        if parsed_json_value is None:
            raise ConfigError("notification.enabled 必须是 true 或 false")
        source_map[source_key] = "json"
        return parsed_json_value

    # 三层来源都没有给出有效值时，通知开关默认关闭。
    source_map[source_key] = "default"
    return False


def _load_secret_overrides(
    secret_overrides_env: str, is_github_actions: bool
) -> dict[str, Any]:
    # 整包 Secret JSON 只在 GitHub Actions 中生效，用来提供任意层级的敏感字段。
    if not is_github_actions:
        return {}
    if not secret_overrides_env:
        return {}
    # 环境变量未提供时直接返回空对象，表示当前运行没有整包覆盖层。
    raw_value = os.getenv(secret_overrides_env, "").strip()
    if not raw_value:
        return {}
    try:
        # Secret JSON 的读取结果会沿用配置文件的注释清洗规则，保证两类来源结构一致。
        parsed = json.loads(raw_value)
        if not isinstance(parsed, dict):
            raise ConfigError(f"环境变量 {secret_overrides_env} 的顶层必须是对象")
        return _strip_comment_entries(parsed)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"环境变量 {secret_overrides_env} 不是合法 JSON: {exc}"
        ) from exc


def _read_text_setting(
    section: dict[str, Any], field_name: str, default: str
) -> str:
    # 纯文本配置为空时会回退到默认值，非空时直接使用去空白后的结果。
    if field_name not in section:
        return default
    text = str(section.get(field_name, "")).strip()
    return text or default


def _read_bool_setting(
    section: dict[str, Any], field_name: str, default: bool
) -> bool:
    # 布尔开关既支持标准布尔值，也支持 true 和 false 这类常见文本写法。
    if field_name not in section:
        return default
    parsed_value = _parse_bool_value(section.get(field_name))
    if parsed_value is None:
        raise ConfigError(f"{field_name} 必须是 true 或 false")
    return parsed_value


def _read_int_setting(
    section: dict[str, Any],
    field_name: str,
    default: int,
    minimum: int,
) -> int:
    # 整数型配置会在这里统一做类型转换和最小值校验。
    if field_name not in section:
        return default
    raw_value = section.get(field_name)
    try:
        parsed_value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field_name} 必须是整数") from exc
    if parsed_value < minimum:
        raise ConfigError(f"{field_name} 不能小于 {minimum}")
    return parsed_value


def _resolve_value(
    section: dict[str, Any],
    secret_overrides: dict[str, Any],
    path_parts: list[str],
    is_github_actions: bool,
    source_map: dict[str, str],
) -> str:
    # 单个字段严格执行先 JSON、再整包 Secret JSON、最后字段级环境变量的读取顺序。
    field_name = path_parts[-1]
    config_spec = deepcopy(section.get(field_name, {}))
    json_value = ""
    env_name = ""

    # 字段既支持完整 spec 对象，也兼容直接写成纯字符串的简化形式。
    if isinstance(config_spec, dict):
        json_value = str(config_spec.get("value", "")).strip()
        env_name = str(config_spec.get("env_name", "")).strip()
    else:
        json_value = str(config_spec).strip()

    source_key = ".".join(path_parts)
    if not _is_blank(json_value):
        # JSON 中只要已经给出非空值，就直接作为最终结果，不再继续向后补值。
        source_map[source_key] = "json"
        return json_value

    override_value = _deep_get(secret_overrides, path_parts)
    if is_github_actions and not _is_blank(override_value):
        # 整包 Secret JSON 只在 JSON 留空时参与补值。
        source_map[source_key] = f"secret_json:{path_parts[0]}"
        return str(override_value).strip()

    if is_github_actions and env_name:
        env_value = os.getenv(env_name, "").strip()
        if not _is_blank(env_value):
            # 字段级环境变量是最后一级补值来源。
            source_map[source_key] = f"env:{env_name}"
            return env_value

    # 三层来源都没有给出有效值时，统一落成空字符串，并在来源表中标成 empty。
    source_map[source_key] = "empty"
    return ""


def _merge_request_template(
    base_request_template: dict[str, Any],
    secret_request_overrides: dict[str, Any],
    is_github_actions: bool,
    source_map: dict[str, str],
) -> dict[str, Any]:
    # 推送模板允许包含任意结构，这里只做通用补值和来源标记，不假设固定字段集合。
    merged = deepcopy(base_request_template)

    if is_github_actions:
        # 只有在 GitHub Actions 中才会用整包 Secret JSON 去填补模板里的空值。
        merged = _deep_fill_blank_values(merged, secret_request_overrides)

    # method 和 body_type 会在模板层统一归一化，发送逻辑直接使用归一化后的结果。
    method_value = str(merged.get("method", "POST")).strip() or "POST"
    body_type_value = str(merged.get("body_type", "form")).strip() or "form"
    merged["method"] = method_value.upper()
    merged["body_type"] = body_type_value.lower()
    # 这三个区块在发送阶段会被当作键值映射直接消费，所以在这里先做对象校验。
    merged["headers"] = _ensure_mapping_value(
        merged.get("headers", {}), "notification.request.headers"
    )
    merged["query_params"] = _ensure_mapping_value(
        merged.get("query_params", {}), "notification.request.query_params"
    )
    merged["body_fields"] = _ensure_mapping_value(
        merged.get("body_fields", {}), "notification.request.body_fields"
    )

    source_map["notification.request.method"] = "json"
    source_map["notification.request.body_type"] = "json"
    # URL 的来源既可能是固定值，也可能来自环境变量或消息变量声明。
    source_map["notification.request.url"] = _resolve_spec_source(
        merged.get("url", {}), "notification.request.url", is_github_actions, source_map
    )

    # 头、查询参数和请求体字段的来源信息会逐项展开，供 Summary 单独展示。
    _mark_mapping_sources(
        merged.get("headers", {}),
        "notification.request.headers",
        is_github_actions,
        source_map,
    )
    _mark_mapping_sources(
        merged.get("query_params", {}),
        "notification.request.query_params",
        is_github_actions,
        source_map,
    )
    _mark_mapping_sources(
        merged.get("body_fields", {}),
        "notification.request.body_fields",
        is_github_actions,
        source_map,
    )
    return merged


def _mark_mapping_sources(
    mapping: dict[str, Any],
    prefix: str,
    is_github_actions: bool,
    source_map: dict[str, str],
) -> None:
    # 这里不展开 message 变量的实际值，只记录每个模板字段最终的来源类型。
    # 调用方负责保证 mapping 已经是对象结构，这里只逐项登记来源。
    for key, spec in mapping.items():
        source_map[f"{prefix}.{key}"] = _resolve_spec_source(
            spec, f"{prefix}.{key}", is_github_actions, source_map
        )


def _mark_plain_section_sources(
    section: dict[str, Any],
    field_names: list[str],
    prefix: str,
    source_map: dict[str, str],
) -> None:
    # 这类字段不支持额外补值，因此来源只区分为 JSON 或程序默认值。
    for field_name in field_names:
        source_map[f"{prefix}.{field_name}"] = (
            "json" if field_name in section else "default"
        )


def _resolve_spec_source(
    spec: Any, source_key: str, is_github_actions: bool, source_map: dict[str, str]
) -> str:
    # 这个函数只判断推送模板字段的来源类型，不返回字段本身的实际值。
    if isinstance(spec, dict):
        # source 为 message 时表示字段值来自运行期消息变量，而不是配置中的静态值。
        source_type = str(spec.get("source", "")).strip()
        if source_type == "message":
            return "message_variable"

        # 这里的判断顺序和真正取值顺序保持一致，先看 value，再看环境变量。
        json_value = spec.get("value", "")
        env_name = str(spec.get("env_name", "")).strip()
        if not _is_blank(json_value):
            return "json"
        if is_github_actions and env_name and not _is_blank(os.getenv(env_name, "")):
            return f"env:{env_name}"
    # 没有显式命中其他来源时，Summary 统一把模板字段视作来自 JSON 配置结构。
    return "json"


def _deep_fill_blank_values(base_value: Any, override_value: Any) -> Any:
    # 这个深度合并函数只会在原值为空时填入覆盖值，保证 JSON 优先级始终高于 Secret。
    if isinstance(base_value, dict) and "value" in base_value:
        # 叶子节点采用 spec 结构时，只允许覆盖其中的 value，不改变其他描述字段。
        if not isinstance(override_value, dict):
            merged_spec = deepcopy(base_value)
            if _is_blank(merged_spec.get("value")) and not _is_blank(override_value):
                merged_spec["value"] = override_value
            return merged_spec

    if isinstance(base_value, dict) and isinstance(override_value, dict):
        # 两边都是对象时递归下钻，保证嵌套模板仍然沿用同一套留空补值规则。
        merged: dict[str, Any] = deepcopy(base_value)
        for key, override_child in override_value.items():
            base_child = merged.get(key)
            if key not in merged:
                merged[key] = deepcopy(override_child)
                continue
            merged[key] = _deep_fill_blank_values(base_child, override_child)
        return merged

    # 标量和列表场景只在原值为空时用覆盖值替换，否则保持原值不动。
    if _is_blank(base_value) and not _is_blank(override_value):
        return deepcopy(override_value)
    return deepcopy(base_value)


def _deep_get(data: dict[str, Any], path_parts: list[str]) -> Any:
    # 按路径安全读取嵌套字典中的值，任何一级不存在都返回空字符串。
    current: Any = data
    for part in path_parts:
        if not isinstance(current, dict) or part not in current:
            return ""
        current = current[part]
    return current


def _is_blank(value: Any) -> bool:
    # None、空白字符串以及空集合都视为未填写。
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def _parse_bool_value(value: Any) -> bool | None:
    # 布尔型配置统一在这里解析，非法值返回 None 供上层继续回退。
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return None


def _load_json_config(config_file: Path) -> dict[str, Any]:
    # 配置文件统一按 UTF-8 读取，并在进入字段解析前剔除说明性注释键。
    try:
        parsed = json.loads(config_file.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON: {exc}") from exc
    # 顶层必须是对象结构，后续所有固定区块都是从这个对象里按键取值。
    if not isinstance(parsed, dict):
        raise ConfigError("配置文件顶层必须是对象")
    return _strip_comment_entries(parsed)


def _load_json_config_or_empty(config_path: str) -> dict[str, Any]:
    # 预读取配置时只要拿不到合法 JSON，就直接回退到空对象，不阻断主流程后续统一报错。
    config_file = Path(config_path)
    if not config_file.exists():
        return {}
    try:
        return _load_json_config(config_file)
    except ConfigError:
        return {}


def _strip_comment_entries(value: Any) -> Any:
    # 说明性注释键只服务于人工阅读，不允许参与程序实际配置解析和映射。
    if isinstance(value, dict):
        # 字典节点会逐项递归清洗，非注释键保持原始层级结构不变。
        cleaned: dict[str, Any] = {}
        for key, child in value.items():
            if _is_comment_key(key):
                continue
            cleaned[key] = _strip_comment_entries(child)
        return cleaned
    if isinstance(value, list):
        # 列表节点不会删元素，只会继续清洗列表里的嵌套对象。
        return [_strip_comment_entries(item) for item in value]
    return value


def _is_comment_key(key: Any) -> bool:
    # 当前配置文件中以 _comment 开头的键都被视为说明文本。
    return isinstance(key, str) and key.startswith("_comment")


def _ensure_mapping_value(value: Any, field_name: str) -> dict[str, Any]:
    # 需要对象结构的配置区块都会先经过这里校验，避免错误类型流入业务链路。
    if value is None:
        # 缺失区块按空对象处理，调用方可以继续套用默认值逻辑。
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{field_name} 必须是对象")
    return value


def _validate_config(
    account: AccountConfig,
    sign: SignConfig,
    runtime: RuntimeOptions,
    notification: NotificationConfig,
) -> None:
    # 这里负责最基础的运行前校验，确保主流程拿到的配置至少满足执行前提。
    if not account.school_name and not account.school_id:
        raise ConfigError("学校名称和学校 ID 至少需要填写一项")
    if account.school_id and not account.school_id.isdigit():
        raise ConfigError("学校 ID 必须是纯数字字符串")
    if not account.account:
        raise ConfigError("账号不能为空")
    if not account.password:
        raise ConfigError("密码不能为空")
    if not sign.target_type.strip():
        raise ConfigError("目标签到类型不能为空")

    for field_name, value in {
        "longitude": account.longitude,
        "latitude": account.latitude,
    }.items():
        if value:
            try:
                # 经纬度只要填写，就必须能转成浮点数。
                float(value)
            except ValueError as exc:
                raise ConfigError(f"{field_name} 必须是度数加小数点格式") from exc

    if runtime.request_timeout_seconds < 1:
        raise ConfigError("request_timeout_seconds 不能小于 1")
    if runtime.response_console_truncate < 1:
        raise ConfigError("response_console_truncate 不能小于 1")
    if runtime.retry_attempts < 1:
        raise ConfigError("retry_attempts 不能小于 1")
    if runtime.retry_interval_seconds < 0:
        raise ConfigError("retry_interval_seconds 不能小于 0")
    if runtime.sign_submit_delay_seconds < 0:
        raise ConfigError("sign_submit_delay_seconds 不能小于 0")

    # 这三个字段会直接传给通知发送模块作为映射对象使用。
    # 如果这里不是对象，后续请求组装阶段就无法按键展开字段。
    for field_name in ["headers", "query_params", "body_fields"]:
        if not isinstance(notification.request_template.get(field_name, {}), dict):
            raise ConfigError(f"notification.request.{field_name} 必须是对象")

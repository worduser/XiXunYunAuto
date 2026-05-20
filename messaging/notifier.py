# 这个模块统一执行消息推送请求。
# 推送地址、请求方法、字段键名和字段值都从配置模板动态解析，业务层只负责提供消息变量。
from __future__ import annotations

import os
from typing import Any

from curl_cffi import requests as curl_requests


def send_notification(
    request_template: dict[str, Any],
    message_variables: dict[str, str],
    timeout_seconds: int,
) -> dict[str, Any]:
    # 推送请求的 URL、请求头、查询参数和请求体字段都会在发送前统一解析。
    url = _resolve_value(request_template.get("url", ""), message_variables)
    method = str(request_template.get("method", "POST")).upper()
    body_type = str(request_template.get("body_type", "form")).lower()
    headers = _resolve_mapping(request_template.get("headers", {}), message_variables)
    query_params = _resolve_mapping(
        request_template.get("query_params", {}), message_variables
    )
    body_fields = _resolve_mapping(
        request_template.get("body_fields", {}), message_variables
    )

    request_kwargs: dict[str, Any] = {
        # 推送请求的超时时间会和主业务请求复用同一份运行配置。
        # impersonate 保持固定，避免通知模板和网络指纹配置彼此混杂。
        "method": method,
        "url": url,
        "headers": headers,
        "params": query_params,
        "timeout": timeout_seconds,
        "impersonate": "chrome124",
    }

    if body_type == "json":
        # JSON 请求体通过 json 参数发送。
        request_kwargs["json"] = body_fields
    elif body_type == "form":
        # 表单请求体通过 data 参数发送。
        request_kwargs["data"] = body_fields
    else:
        # 未识别的请求体类型统一按 data 发送，保持当前行为稳定。
        request_kwargs["data"] = body_fields

    response = curl_requests.request(**request_kwargs)
    response_text = response.text
    try:
        response_json = response.json()
    except Exception:
        response_json = None

    return {
        "http_status": response.status_code,
        "raw_text": response_text,
        "json_body": response_json,
        "resolved_request": {
            "method": method,
            "url": url,
            "headers": headers,
            "params": query_params,
            "body_type": body_type,
            "body_fields": body_fields,
            "timeout_seconds": timeout_seconds,
        },
    }


def _resolve_mapping(
    mapping: dict[str, Any], message_variables: dict[str, str]
) -> dict[str, str]:
    # 每个字段都会独立解析，允许同一份模板同时混用运行时变量和固定值。
    resolved: dict[str, str] = {}
    for key, spec in mapping.items():
        value = _resolve_value(spec, message_variables)
        if value == "":
            continue
        resolved[key] = value
    return resolved


def _resolve_value(spec: Any, message_variables: dict[str, str]) -> str:
    # source 为 message 时从消息变量中取值，其他情况按 value 优先、env_name 次之的顺序解析。
    if isinstance(spec, dict):
        source_type = str(spec.get("source", "value")).strip()
        if source_type == "message":
            variable_name = str(spec.get("variable", "")).strip()
            return str(message_variables.get(variable_name, ""))

        value = str(spec.get("value", "")).strip()
        env_name = str(spec.get("env_name", "")).strip()
        if value:
            return value
        if env_name:
            return os.getenv(env_name, "").strip()
        return ""
    return str(spec)

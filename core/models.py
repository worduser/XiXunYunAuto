# 这个模块集中定义主流程、推送模块和 Summary 模块共享的数据结构。
# 所有运行态数据都会通过这些数据类在模块之间传递，避免字段定义分散在多个文件中。
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class ResolvedValue:
    # 这个结构用于描述单个配置项的解析结果。
    # value 保存最终生效的值。
    value: Any
    # source 记录当前值来自 JSON、环境变量还是其他来源。
    source: str


@dataclass(slots=True)
class RuntimeOptions:
    # 这个对象保存程序运行期直接使用的基础参数。
    # timezone_name 控制日志、消息和 Summary 使用的时区。
    timezone_name: str
    # time_format 控制运行期所有日期时间文本的输出格式。
    time_format: str
    # console_raw_response_enabled 决定是否在控制台预览原始响应。
    console_raw_response_enabled: bool
    # txt_raw_response_enabled 决定是否把原始响应写入 TXT 文件。
    txt_raw_response_enabled: bool
    # response_console_truncate 表示控制台原始响应预览的最大长度。
    response_console_truncate: int
    # default_address_name 用于地址名称缺失时的默认值。
    default_address_name: str
    # request_timeout_seconds 表示单次 HTTP 请求超时时间。
    request_timeout_seconds: int
    # retry_attempts 表示阶段动作允许尝试的总次数。
    retry_attempts: int
    # retry_interval_seconds 表示两次重试之间的等待秒数。
    retry_interval_seconds: int
    # sign_submit_delay_seconds 表示发起签到前的额外等待秒数。
    sign_submit_delay_seconds: int


@dataclass(slots=True)
class AccountConfig:
    # 这个对象保存签到所需的账号和位置信息。
    # school_name 用于在缺少学校 ID 时查询学校列表。
    school_name: str
    # school_id 允许直接跳过学校查询接口。
    school_id: str
    # account 表示登录习讯云接口使用的账号。
    account: str
    # password 表示和账号配套使用的登录密码。
    password: str
    # address 表示签到提交时使用的详细地址。
    address: str
    # address_name 表示签到提交时使用的地点名称。
    address_name: str
    # longitude 表示签到提交时使用的经度明文。
    longitude: str
    # latitude 表示签到提交时使用的纬度明文。
    latitude: str


@dataclass(slots=True)
class SignConfig:
    # 这个对象保存签到类型匹配规则。
    # target_type 表示要在签到查询接口 mark_list.value 中匹配的签到类型文本。
    target_type: str


@dataclass(slots=True)
class NotificationConfig:
    # 这个对象保存消息推送相关配置。
    # enabled 表示是否启用消息推送。
    enabled: bool
    # force_push 表示重复签到结果是否仍然发送通知。
    force_push: bool
    # request_template 保存动态请求模板的完整结构。
    request_template: dict[str, Any]
    # message_layouts 保存不同状态下的消息段落编排配置。
    message_layouts: dict[str, list[str]]
    # summary_layouts 保存 GitHub Actions Summary 的区块顺序配置。
    summary_layouts: dict[str, list[str]]


@dataclass(slots=True)
class ResolvedConfig:
    # 这是主流程实际依赖的完整配置对象。
    # config_path 记录当前生效配置文件的绝对路径。
    config_path: str
    # runtime 保存运行参数。
    runtime: RuntimeOptions
    # github_secret_overrides_env 保存整包 Secret JSON 的环境变量名。
    github_secret_overrides_env: str
    # github_force_push_env 保存强制推送输入映射的环境变量名。
    github_force_push_env: str
    # account 保存账号和位置信息。
    account: AccountConfig
    # sign 保存签到类型匹配规则。
    sign: SignConfig
    # notification 保存消息推送配置。
    notification: NotificationConfig
    # source_map 记录每个配置字段最终来自哪里。
    source_map: dict[str, str]


@dataclass(slots=True)
class RunEnvironment:
    # 这个对象保存当前运行环境的上下文信息。
    # is_github_actions 表示当前是否运行在 GitHub Actions 中。
    is_github_actions: bool
    # source_label 用于向日志、消息和 Summary 描述运行来源。
    source_label: str
    # started_at 记录程序启动时的 UTC 时间。
    started_at: datetime
    # event_name 表示触发工作流的事件名。
    event_name: str = ""
    # workflow_name 表示 GitHub Actions 当前工作流名。
    workflow_name: str = ""
    # workflow_ref 表示工作流引用路径。
    workflow_ref: str = ""
    # workflow_sha 表示工作流定义对应的提交号。
    workflow_sha: str = ""
    # repository_name 表示仓库全名。
    repository_name: str = ""
    # branch_name 表示当前运行所在分支名。
    branch_name: str = ""
    # commit_sha 表示当前代码提交号。
    commit_sha: str = ""
    # actor_name 表示最初触发这次运行的账号名。
    actor_name: str = ""
    # actor_id 表示最初触发这次运行的账号 ID。
    actor_id: str = ""
    # triggering_actor 表示重新运行时的操作者账号名。
    triggering_actor: str = ""
    # run_id 表示当前工作流运行 ID。
    run_id: str = ""
    # run_number 表示当前工作流运行序号。
    run_number: str = ""


@dataclass(slots=True)
class ApiCallResult:
    # 这个结构表示一次 HTTP 调用的统一结果。
    # endpoint_name 表示这次请求对应的接口名称。
    endpoint_name: str
    # http_status 表示 HTTP 状态码，拿不到时为 None。
    http_status: int | None
    # raw_text 保存原始响应正文。
    raw_text: str
    # json_body 保存 JSON 解析成功后的结果。
    json_body: Any | None
    # error_text 保存请求异常或回退失败时的错误说明。
    error_text: str

    @property
    def code(self) -> Any:
        # 只有 JSON 响应是字典结构时，才尝试读取业务码字段。
        if isinstance(self.json_body, dict):
            return self.json_body.get("code")
        return None

    @property
    def message(self) -> str:
        # 业务消息统一从这里读取，外层不需要重复写字典取值逻辑。
        if isinstance(self.json_body, dict):
            return str(self.json_body.get("message", ""))
        return ""


@dataclass(slots=True)
class StageLog:
    # 这个结构保存一条阶段日志的完整信息。
    # level 表示日志级别。
    level: str
    # stage 表示日志所属的业务阶段。
    stage: str
    # message 表示实际日志内容。
    message: str
    # timestamp_text 表示已经格式化好的时间文本。
    timestamp_text: str
    # raw_line 表示打印到控制台的完整原始行。
    raw_line: str


@dataclass(slots=True)
class StageResult:
    # 这个结构保存单个业务阶段的最终结构化结果。
    # code 表示该阶段对应接口或动作的结果码。
    code: str = ""
    # status 表示该阶段的归类状态。
    status: str = ""
    # detail 保存该阶段的具体说明。
    detail: str = ""


@dataclass(slots=True)
class ExecutionSnapshot:
    # 这个对象承载主流程执行后的完整快照。
    # final_status 和 final_title 表示最终结果状态及其标题文案。
    final_status: str = ""
    final_title: str = ""
    # environment_label、reason 和强制推送相关字段用于消息和 Summary 展示。
    environment_label: str = ""
    reason: str = ""
    force_push_active: bool = False
    force_push_source: str = ""
    # 学校和账号基础信息用于追踪当前签到主体。
    school_name: str = ""
    school_id: str = ""
    token: str = ""
    user_id: str = ""
    user_number: str = ""
    user_name: str = ""
    class_name: str = ""
    # 积分与签到统计信息来自登录接口和签到接口响应。
    point: str = ""
    point_rank: str = ""
    entrance_year: str = ""
    graduation_year: str = ""
    sign_point: str = ""
    sign_in_month_count: str = ""
    continuous_sign_in: str = ""
    # target_sign_type 保存当前配置要求匹配的签到类型文本。
    target_sign_type: str = ""
    # 签到提交参数相关字段保存最终参与提交或展示的地点信息。
    sign_type_remark: str = ""
    sign_address: str = ""
    sign_address_name: str = ""
    sign_longitude: str = ""
    sign_latitude: str = ""
    # 当前时间和日期文本用于判断当天签到状态并生成对外展示文本。
    current_time_text: str = ""
    today_text: str = ""
    # 首次查询、验证查询和发起签到的结果会在这里分别保留。
    initial_query_signed_today: bool = False
    verify_query_signed_today: bool = False
    sign_api_code: str = ""
    sign_api_message: str = ""
    initial_query_message: str = ""
    verify_query_message: str = ""
    # 以下集合字段用于承载 Summary、日志和排查信息。
    config_sources_text: list[str] = field(default_factory=list)
    workflow_info_text: list[str] = field(default_factory=list)
    recent_sign_times: list[str] = field(default_factory=list)
    timeline: list[StageLog] = field(default_factory=list)
    stage_results: dict[str, StageResult] = field(default_factory=dict)
    # 错误上下文用于异常通知和 GitHub Actions Summary 错误详情区块。
    error_stage: str = ""
    error_endpoint: str = ""
    error_file: str = ""
    error_line: str = ""
    error_traceback: str = ""
    context_lines: list[str] = field(default_factory=list)
    # raw_response_output_path 保存这次运行生成的原始响应文件路径。
    raw_response_output_path: str = ""

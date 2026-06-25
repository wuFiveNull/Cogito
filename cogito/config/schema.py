# cogito/config/schema.py

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class AppMetaConfig(BaseModel):
    name: str = "cogito"
    environment: str = "development"


class AgentConfig(BaseModel):
    system_prompt_file: Path = Path("prompts/system.md")
    show_thinking: bool = False


class LoopConfig(BaseModel):
    inbound_queue_size: int = 100
    session_mailbox_size: int = 20
    max_concurrent_sessions: int = 4

    turn_timeout_s: float = 300.0
    provider_timeout_s: float = 180.0
    tool_timeout_s: float = 120.0


class StorageConfig(BaseModel):
    sqlite_path: Path = Path(".workspace/cogito.db")


class DeliveryConfig(BaseModel):
    channel_queue_size: int = 100
    send_timeout_s: float = 30.0

    retry_max_attempts: int = 5
    retry_base_delay_s: float = 2.0
    retry_max_delay_s: float = 300.0

    web_host: str = "0.0.0.0"
    web_port: int = 8888


class ProviderConfig(BaseModel):
    adapter: Literal[
        "openai",
        "openai_compatible",
        "deepseek",
        "dashscope",
    ]

    base_url: str
    api_key_env: str = ""

    # 开发阶段可明文存放 API Key（须启用 allow_plaintext_key）
    api_key: str | None = None

    request_timeout_s: float = 180.0
    stream_idle_timeout_s: float = 90.0

    max_retries: int = 2
    retry_base_delay_s: float = 1.0
    retry_max_delay_s: float = 30.0

    default_headers: dict[str, str] = Field(default_factory=dict)


class ModelConfig(BaseModel):
    provider: str
    model: str

    max_output_tokens: int = 4096

    capabilities: set[str] = Field(
        default_factory=lambda: {"text", "streaming"}
    )

    extra_body: dict[str, Any] = Field(default_factory=dict)

    # Embedding-only fields
    dimensions: int | None = None
    max_batch_size: int = 10


class LLMConfig(BaseModel):
    providers: dict[str, ProviderConfig]
    models: dict[str, ModelConfig]
    routes: dict[str, str]


class AppConfig(BaseModel):
    app: AppMetaConfig = Field(default_factory=AppMetaConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    loop: LoopConfig = Field(default_factory=LoopConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)

    llm: LLMConfig

    config_path: Path | None = None
    project_dir: Path | None = None

    def resolve_path(self, value: Path) -> Path:
        if value.is_absolute():
            return value.resolve()

        base = self.project_dir or Path.cwd()
        return (base / value).resolve()

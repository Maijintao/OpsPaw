from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    chat_model: str
    chat_base_url: str
    chat_api_key: str
    chat_thinking: str = ""
    chat_reasoning_effort: str = ""
    chat_reasoning_split: str = ""
    token_budget: int = 2000
    database_url: str = "mysql+asyncmy://root:root@localhost:3306/opspaw"
    test_database_url: str = "mysql+asyncmy://root:root@localhost:3306/opspaw_test"
    max_agent_steps: int = 6
    checkpointer_db_path: str = "data/ch05_checkpoints.sqlite"
    intent_model: str = ""
    summary_trigger_messages: int = 30
    context_window_turns: int = 8
    context_window_max_tokens: int = 3000
    summary_model: str = ""
    mcp_monitor_url: str = "http://127.0.0.1:8101/mcp"
    mcp_logs_url: str = "http://127.0.0.1:8102/mcp"
    tool_default_timeout: float = 5.0
    mcp_tool_timeout: float = 10.0
    tool_max_retries: int = 2
    demo_ticket_delay_seconds: float = 0.0
    repair_public_base_url: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str = ""


settings = Settings()

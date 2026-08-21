"""全局配置。值来自环境变量/.env，测试可覆盖。"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "extra": "ignore"}

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    fred_api_key: str = ""  # FRED 宏观数据（2026-08-12 起，CPI/利率/原油库存基线）
    eia_api_key: str = ""  # EIA OpenData（原油库存周度基线，2026-08-12 接入）

    db_path: str = "data/foresight.db"
    llm_model: str = "deepseek-chat"  # 当前主模型名；测试覆盖用
    llm_temperature: float = 0.0
    max_retries: int = 2
    timeout_seconds: float = 60.0

    @property
    def llm_client_kwargs(self) -> dict:
        return {
            "base_url": self.deepseek_base_url,
            "api_key": self.deepseek_api_key,
            "model": self.deepseek_model,
            "max_retries": self.max_retries,
            "timeout": self.timeout_seconds,
        }

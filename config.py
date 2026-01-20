import os

from azure.cosmos import CosmosClient
from azure.storage.blob.aio import BlobServiceClient
from i_style.llm import ModelRegistry, ModelConfig

# 共通設定
COSMOS_CONNECTION_STRING = os.environ.get("COSMOS_CONNECTION_STRING")
COSMOS_CLIENT = CosmosClient.from_connection_string(conn_str=COSMOS_CONNECTION_STRING)


LLM_REGISTRY = ModelRegistry(enable_models=["aoai", "gemini", "claude"])

NON_CHAT_REGISTRY = ModelRegistry(enable_models=["aoai"])

_models_data = {
    "gpt4.1": {
        "endpoint": os.environ.get("NON_CHAT_GPT4_1_API_ENDPOINT"),
        "key": os.environ.get("NON_CHAT_GPT4_1_API_KEY"),
        "deployment_name": os.environ.get("NON_CHAT_GPT4_1_DEPLOYMENT_NAME"),
        "max_tokens": {"input": 1_047_576, "output": 32_768},
        "service": "aoai",
    },
    "gpt4.1-mini": {
        "endpoint": os.environ.get("NON_CHAT_GPT4_1_MINI_API_ENDPOINT"),
        "key": os.environ.get("NON_CHAT_GPT4_1_MINI_API_KEY"),
        "deployment_name": os.environ.get("NON_CHAT_GPT4_1_MINI_DEPLOYMENT_NAME"),
        "max_tokens": {"input": 1_047_576, "output": 32_768},
        "service": "aoai",
    },
    "gpt4.1-nano": {
        "endpoint": os.environ.get("NON_CHAT_GPT4_1_NANO_API_ENDPOINT"),
        "key": os.environ.get("NON_CHAT_GPT4_1_NANO_API_KEY"),
        "deployment_name": os.environ.get("NON_CHAT_GPT4_1_NANO_DEPLOYMENT_NAME"),
        "max_tokens": {"input": 1_047_576, "output": 32_768},
        "service": "aoai",
    },
    "o4-mini": {
        "endpoint": os.environ.get("NON_CHAT_O4_MINI_API_ENDPOINT"),
        "key": os.environ.get("NON_CHAT_O4_MINI_API_KEY"),
        "deployment_name": os.environ.get("NON_CHAT_O4_MINI_DEPLOYMENT_NAME"),
        "max_tokens": {"input": 200_000, "output": 100_000},
        "service": "aoai",
    },
    "gpt5": {
        "endpoint": os.environ.get("NON_CHAT_GPT5_API_ENDPOINT"),
        "key": os.environ.get("NON_CHAT_GPT5_API_KEY"),
        "deployment_name": os.environ.get("NON_CHAT_GPT5_DEPLOYMENT_NAME"),
        "max_tokens": {"input": 272_000, "output": 128_000},
        "service": "aoai",
    },
    "gpt5-mini": {
        "endpoint": os.environ.get("NON_CHAT_GPT5_MINI_API_ENDPOINT"),
        "key": os.environ.get("NON_CHAT_GPT5_MINI_API_KEY"),
        "deployment_name": os.environ.get("NON_CHAT_GPT5_MINI_DEPLOYMENT_NAME"),
        "max_tokens": {"input": 272_000, "output": 128_000},
        "service": "aoai",
    },
    "gpt5-nano": {
        "endpoint": os.environ.get("NON_CHAT_GPT5_NANO_API_ENDPOINT"),
        "key": os.environ.get("NON_CHAT_GPT5_NANO_API_KEY"),
        "deployment_name": os.environ.get("NON_CHAT_GPT5_NANO_DEPLOYMENT_NAME"),
        "max_tokens": {"input": 272_000, "output": 128_000},
        "service": "aoai",
    },
}

for model_name, config in _models_data.items():
    if config["endpoint"] and config["deployment_name"]:
        if config.get("key"):
            NON_CHAT_REGISTRY.models[model_name] = ModelConfig(**config)

GPT_API_VERSION = os.environ.get("GPT_API_VERSION")
GPT4O_TRANSCRIBE_API_ENDPOINT = os.environ.get("GPT4O_TRANSCRIBE_API_ENDPOINT")
GPT4O_TRANSCRIBE_API_KEY = os.environ.get("GPT4O_TRANSCRIBE_API_KEY")
GPT4O_TRANSCRIBE_DEPLOYMENT_NAME = os.environ.get("GPT4O_TRANSCRIBE_DEPLOYMENT_NAME")

# blob
BLOB_CONNECTION_STRING = os.environ.get("BLOB_CONNECTION_STRING")
BLOB_SERVICE_CLIENT = BlobServiceClient.from_connection_string(BLOB_CONNECTION_STRING)

AUDIO_CONTAINER_NAME = "audio-data"

FILE_CONTAINER_NAME = "file-data"


# Durableの環境変数
MCP_AGENT_URL = os.environ.get("MCP_AGENT_URL")
MCP_AGENT_API_KEY = os.environ.get("MCP_AGENT_API_KEY")

# Gemini labels設定
GEMINI_DEFAULT_LABELS = {"caller-service": "i-colleague"}

ENVIRONMENT_SELECTED = os.environ.get("ENVIRONMENT_SELECTED", "dv")

VARIABLE_LIST = {
    "dv": {
        # 以前はハードコードされていた認証コードを環境変数に退避
        "box_auth_url": os.environ.get("BOX_AUTH_URL_DV"),
        "data_size": 200,
        "llm_docs_overrides": {
            "temperature": 0.0,
            "top": 7,
            "semanticRanker": True,
            "vectorSearch": True,
        },
        "log_mail_address": "takeuchi-yuki@itochu.co.jp",
        "send_mail_address": "i-colleague@itochuapp.com",
    },
    "te": {
        "box_auth_url": os.environ.get("BOX_AUTH_URL_TE"),
        "data_size": 130,
        "llm_docs_overrides": {"semanticRanker": True, "temperature": 0.0, "top": 7},
        "log_mail_address": "takeuchi-yuki@itochu.co.jp",
        "send_mail_address": "i-colleague@itochu.co.jp",
    },
    "pr": {
        "box_auth_url": os.environ.get("BOX_AUTH_URL_PR"),
        "data_size": 130,
        "llm_docs_overrides": {"semanticRanker": True, "temperature": 0.0, "top": 7},
        "log_mail_address": "tokgv-pp@itochu.co.jp",
        "send_mail_address": "i-colleague@itochu.co.jp",
    },
}

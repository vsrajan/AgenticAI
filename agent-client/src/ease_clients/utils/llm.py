"""LLM provider for the agent client.

Centralises Azure OpenAI configuration so every module can simply call
``get_llm()`` to obtain a ready-to-use LangChain chat model.
"""

import os

from langchain_openai import AzureChatOpenAI


def get_llm() -> AzureChatOpenAI:
    """Return a configured AzureChatOpenAI instance.

    Required environment variables (set in .env):
        AZURE_OPENAI_API_KEY      – API key for the Azure OpenAI resource
        AZURE_OPENAI_ENDPOINT     – e.g. https://<resource>.openai.azure.com/
        AZURE_OPENAI_DEPLOYMENT   – deployment name (e.g. "gpt-4o")
        AZURE_OPENAI_API_VERSION  – API version (default "2024-12-01-preview")
    """
    return AzureChatOpenAI(
        azure_deployment=os.environ["AZURE_OPENAI_DEPLOYMENT"],
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview"),
        temperature=0,
    )

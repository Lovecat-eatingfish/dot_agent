import os
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI


def create_model() -> ChatOpenAI:
    load_dotenv()

    api_key = os.getenv("API_KEY")
    model = os.getenv("MODEL")
    base_url = os.getenv("BASE_URL")

    missing = [name for name, value in {"API_KEY": api_key, "MODEL": model, "BASE_URL": base_url}.items() if not value]
    if missing:
        raise RuntimeError(f"missing required .env setting(s): {', '.join(missing)}")
    return ChatOpenAI(
        api_key=api_key,
        model=model,
        base_url=base_url,
        temperature=0,
    )

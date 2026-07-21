from langchain.agents import create_agent
from model.openai_provider import create_model



agent = create_agent(
    model=create_model(),
    system_prompt="",
    tools=
)

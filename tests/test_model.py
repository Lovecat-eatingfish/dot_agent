from src.mokioclaw.providers import openai_provider
from dotenv import load_dotenv


def test_model():
    model = openai_provider.create_model()
    print(model.invoke("你是那个模型"))

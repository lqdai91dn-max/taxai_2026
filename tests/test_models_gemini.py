# test_models.py
from google import genai

client = genai.Client(api_key="AIzaSyCTcwo50Fg-Rn7i0AsAku3jrEpq-XVom_A")
for model in client.models.list():
    if "gemini" in model.name.lower():
        print(model.name)
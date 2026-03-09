# test_gemini.py
from google import genai

client = genai.Client(api_key="AIzaSyCTcwo50Fg-Rn7i0AsAku3jrEpq-XVom_A")
response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents="Xin chào, bạn là ai?"
)
print(response.text)
import os
import requests
import json
from dotenv import load_dotenv

load_dotenv()

class LocalLLM:
    def __init__(self):
        self.api_url = os.getenv("OLLAMA_URL")
        self.model = os.getenv("LLM_MODEL")

    def generate_response(self, system_prompt: str, user_query: str):
        # ✨ 거짓말 방지 설정
        payload = {
            "model": self.model,
            "prompt": f"{system_prompt}\n\nUser Question: {user_query}",
            "stream": False,
            "options": {
                "temperature": 0.1,  # ✨ 낮춰서 환각 방지
                "num_ctx": 16384,     # ✨ 컨텍스트 늘림
                "top_p": 0.9,        # ✨ 확률 높은 것만
                "repeat_penalty": 1.15,# ✨ 반복 방지
                "num_predict": 8192,  
            }
        }
        try:
            response = requests.post(self.api_url, json=payload, timeout=500)
            response.raise_for_status()
            return json.loads(response.text)['response']
        except Exception as e:
            return f"❌ Error: {str(e)}"
        
        

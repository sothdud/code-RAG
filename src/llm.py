# llm.py
import os
import requests
import json
from dotenv import load_dotenv

load_dotenv()

class LocalLLM:
    def __init__(self):
        self.api_url = os.getenv("OLLAMA_URL")
        self.model = os.getenv("LLM_MODEL")
        self.fast_model = os.getenv("FAST_LLM_MODEL") # 🌟 빠른 모델 추가

    # 🌟 use_fast=False 파라미터 추가 (기본값은 무거운 30b 모델)
    def generate_response(self, system_prompt: str, user_query: str, use_fast: bool = False):
        
        # 스위치에 따라 사용할 모델 결정
        target_model = self.fast_model if use_fast else self.model
        
        payload = {
            "model": target_model, # 🌟 결정된 모델 주입
            "prompt": f"{system_prompt}\n\nUser Question: {user_query}",
            "stream": True,
            "options": {
                "temperature": 0.1,
                "num_ctx": 16384, 
                "top_p": 0.9,
                "repeat_penalty": 1.15,
                "num_predict": 8192,
            }
        }
        
        try:
            response = requests.post(self.api_url, json=payload, stream=True, timeout=500)
            response.raise_for_status()
            
            buffer = ""
            for line in response.iter_lines():
                if line:
                    chunk = json.loads(line)
                    token = chunk.get("response", "")
                    buffer += token
                    
                    if "<thinking>" in buffer:
                        buffer = buffer.replace("<thinking>", "\n> 🧠 **[코드 분석 중...]**\n> ")
                    if "</thinking>" in buffer:
                        buffer = buffer.replace("</thinking>", "\n\n**[분석 완료]**\n\n")

                    is_done = chunk.get("done", False)

                    if not is_done and "<" in buffer:
                        last_bracket_idx = buffer.rfind("<")
                        possible_tag = buffer[last_bracket_idx:]
                        if "<thinking>".startswith(possible_tag) or "</thinking>".startswith(possible_tag):
                            continue 
                            
                    yield buffer
                    buffer = ""

                    if is_done:
                        break

        except Exception as e:
            yield f"\n\n[LLM 통신 오류]: {str(e)}"
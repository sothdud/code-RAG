# app.py
import streamlit as st
import os
import json
import re
from dotenv import load_dotenv

# 기존 모듈
from src.database import VectorStore
from src.llm import LocalLLM
from src.graph_store import GraphStore
from src.search_engine import SmartSearchEngine
from src import prompts 

# 환경 변수 로드
load_dotenv()

# =====================================================================
# [디자인 CSS] 
# =====================================================================
st.markdown("""
    <style>
        /* 드롭다운 꾸미기 */
        div[data-baseweb="select"] > div {
            background-color: #f8f9fa;
            border: 2px solid #4a90e2;
            border-radius: 8px;
        }
        /* 채팅 말풍선 여백 조정 */
        .stChatMessage { padding: 1rem; }
    </style>
""", unsafe_allow_html=True)

# =====================================================================
# [질문 분석 및 프롬프트 생성 로직]
# =====================================================================

def detect_query_type(query: str, llm) -> dict:
    """LLM을 사용하여 질문의 의도를 동적으로 파악하고 검색 키워드를 확장"""
    
    # 7b 모델을 위한 날렵한 프롬프트 (오버피팅 방지)
    router_prompt = """
    당신은 C# WPF(MVVM 패턴)와 Python으로 구성된 프로젝트의 RAG 검색 라우터입니다.
    사용자의 짧고 모호한 질문을 분석하여, 실제 소스 코드(C#, XAML, Python)에 존재할 법한 
    '가상의 핵심 기술 키워드'를 추론하세요.

    [지침]
    1. 질문의 정답이 될 코드에 어떤 클래스, 메서드, 속성, XAML 태그가 있을지 상상하세요.
    2. 질문이 한국어여도, 검색용 키워드는 반드시 관련된 영문 프로그래밍 용어(예: Button, Command, ViewModel, Event 등)로 확장하세요.

    [출력 형식 (JSON)]
    {
        "type": "bug|flow|search|mvvm|general",
        "filenames": ["예상되는_파일명.py", "파일명.cs"],
        "target_name": "예상되는_함수_또는_클래스명(없으면 null)",
        "language": "python|csharp|xaml|mixed",
        "search_keywords": "검색엔진에 넣을 구체적이고 확장된 영문 키워드 모음 (예: Train Stop Button Command Execute ViewModel XAML Binding)"
    }
    
    반드시 JSON 형식만 출력하세요. 설명은 필요 없습니다.
    """

    try:
        # 🌟 핵심: 라우터는 7b 모델(fast)을 사용하도록 지시!
        response_gen = llm.generate_response(router_prompt, query, use_fast=True)
        response_text = "".join(list(response_gen))
        
        # JSON 파싱 (마크다운 백틱 등 제거)
        clean_json = response_text.replace("```json", "").replace("```", "").strip()
        result = json.loads(clean_json)
        
        # 확장된 키워드가 비어있으면 원본 질문으로 폴백
        if 'search_keywords' not in result or not result['search_keywords']:
            result['search_keywords'] = query
            
        return result

    except Exception as e:
        # 실패 시 안전하게 기본값 반환
        return {
            "type": "general",
            "filenames": [],
            "target_name": None,
            "language": "mixed",
            "search_keywords": query # 실패해도 원본 질문으로 검색 진행
        }

def build_optimized_prompt(query: str, results: list, query_info: dict) -> str:
    """검색 결과 데이터 구조에 맞춰 안전하게 코드를 추출하고 프롬프트 생성"""
    context_texts = []
    
    # 🌟 어떤 형태의 데이터(객체/딕셔너리)가 들어오든 안전하게 파싱하도록 전면 수정
    for r in results:
        filepath = "Unknown"
        content = ""
        
        if hasattr(r, 'payload') and isinstance(r.payload, dict):
            filepath = r.payload.get('filepath') or r.payload.get('chunk', {}).get('filepath') or "Unknown"
            content = r.payload.get('content') or r.payload.get('chunk', {}).get('content') or ""
        elif isinstance(r, dict):
            filepath = r.get('filepath') or r.get('chunk', {}).get('filepath') or "Unknown"
            content = r.get('content') or r.get('chunk', {}).get('content') or ""
        else:
            content = str(r)
            
        context_texts.append(f"File: {filepath}\nCode:\n{content}")
        
    context_str = "\n\n---\n\n".join(context_texts)
    
    # 질문 유형별 적절한 프롬프트 반환
    qtype = query_info.get('type', 'general')
    
    if qtype == 'existence' and hasattr(prompts, 'get_existence_check_prompt'):
        return prompts.get_existence_check_prompt(query, context_str, query_info.get('target_name') or "unknown")
    elif qtype == 'flow' and hasattr(prompts, 'get_flow_analysis_prompt'):
        return prompts.get_flow_analysis_prompt(query, context_str)
    elif qtype == 'bug' and hasattr(prompts, 'get_bug_analysis_prompt'):
        return prompts.get_bug_analysis_prompt(query, context_str)
    elif qtype == 'mvvm' and hasattr(prompts, 'get_mvvm_analysis_prompt'):
        return prompts.get_mvvm_analysis_prompt(query, context_str)
    elif qtype == 'file_summary' and hasattr(prompts, 'get_file_summary_prompt'):
        return prompts.get_file_summary_prompt(query, context_str, query_info.get('filename') or "Multiple Files")
    elif qtype == 'error' and hasattr(prompts, 'get_error_diagnostic_prompt'):
        traceback_match = re.search(r'(Traceback.*?)(?:\n\n|\Z)', query, re.DOTALL)
        traceback = traceback_match.group(1) if traceback_match else query
        return prompts.get_error_diagnostic_prompt(query, context_str, traceback, language=query_info.get('language', 'python'))
    
    # 해당 프롬프트가 없거나 general일 경우 기본 요약/분석 프롬프트 사용
    if hasattr(prompts, 'get_general_prompt'):
        return prompts.get_general_prompt(query, context_str)
    
    return prompts.get_file_summary_prompt(query, context_str, "Multiple Files")


# =====================================================================
# 앱 기본 설정 및 세션 초기화
# =====================================================================
PROJECT_MAP = {
    "🌐 TIDAL codebase": "semiconductor_codebase_tidal",
    "Dreamer codebase": "semiconductor_codebase",
}

st.title("GraphRAG")

with st.sidebar:
    st.header("설정")
    selected_project = st.selectbox(
        "조회할 프로젝트를 선택하세요:", 
        options=list(PROJECT_MAP.keys())
    )
    target_collection = PROJECT_MAP[selected_project]
    
    st.caption(f"현재 연결된 Qdrant 컬렉션:\n`{target_collection}`")
    
    if st.button("대화 기록 초기화"):
        st.session_state.messages = []
        st.rerun()

# 엔진 로드
if "current_collection" not in st.session_state or st.session_state.current_collection != target_collection:
    st.session_state.current_collection = target_collection
    st.session_state.messages = [] 
    
    with st.spinner(f"'{selected_project}' 엔진을 로딩 중입니다... ⏳"):
        st.session_state.vector_store = VectorStore(collection_name=target_collection)
        st.session_state.graph_store = GraphStore()
        st.session_state.search_engine = SmartSearchEngine(
            st.session_state.vector_store, 
            st.session_state.graph_store
        )
        st.session_state.llm = LocalLLM()
        
# =====================================================================
# 메인 채팅 UI
# =====================================================================
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

query = st.chat_input("코드에 대해 무엇이든 물어보세요! (예: 이 XAML 버튼 클릭하면 C#의 어떤 메서드가 실행돼?)")

if query:
    st.session_state.messages.append({"role": "user", "content": query})
    with st.chat_message("user"):
        st.markdown(query)

    with st.chat_message("assistant"):
        engine = st.session_state.search_engine
        llm = st.session_state.llm
        
        # 1. 의도 파악
        with st.spinner("질문 의도를 파악하는 중입니다... 🤔"):
            query_info = detect_query_type(query, llm)
        
        # 2. 코드 검색
        with st.spinner(f"[{query_info.get('type', 'general').upper()}] 관련 코드를 찾는 중입니다... 🧐"):
            results = engine.search(query, top_k=5)
            
            if not results:
                answer = "❌ 관련 코드를 찾을 수 없습니다."
                st.error(answer)
                st.session_state.messages.append({"role": "assistant", "content": answer})
            else:
                # 3. 프롬프트 생성 및 답변 출력
                prompt = build_optimized_prompt(query, results, query_info)
                
                # write_stream은 생성된 전체 텍스트를 반환합니다.
                answer = st.write_stream(llm.generate_response(prompt, query))
                
                # 4. 참조 파일 목록 UI (안전하게 파싱)
                with st.expander("📚 참조된 파일 목록 보기"):
                    st.write(f"💡 감지된 질문 유형: `{query_info.get('type', 'Unknown').upper()}`")
                    
                    ref_files = set()
                    for r in results:
                        if hasattr(r, 'payload') and isinstance(r.payload, dict):
                            file_path = r.payload.get('filepath') or r.payload.get('chunk', {}).get('filepath')
                            if file_path: ref_files.add(file_path)
                        elif isinstance(r, dict):
                            file_path = r.get('filepath') or r.get('chunk', {}).get('filepath')
                            if file_path: ref_files.add(file_path)
                            
                    for f in sorted(filter(None, ref_files)):
                        st.write(f"- `{f}`")

                # 대화 기록 저장
                st.session_state.messages.append({"role": "assistant", "content": answer})
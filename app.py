
import streamlit as st
import json
import os
from dotenv import load_dotenv

from src.database import VectorStore
from src.llm import LocalLLM
from src.graph_store import GraphStore
from src.search_engine import SmartSearchEngine
from src.agent import CodebaseAgent

load_dotenv()

st.markdown("""
<style>
    .tool-step {
        font-size: 0.82rem; color: #333;
        border-left: 3px solid #4a90e2;
        padding: 3px 12px; margin: 2px 0;
        background: #f5f8ff; border-radius: 0 4px 4px 0;
    }
    .model-badge {
        font-size: 0.7rem; padding: 2px 6px;
        border-radius: 10px; margin-left: 6px;
    }
    .agent-badge  { background: #e3f2fd; color: #1565c0; }
    .answer-badge { background: #e8f5e9; color: #2e7d32; }
</style>
""", unsafe_allow_html=True)

PROJECT_MAP = {
    "🌐 TIDAL codebase": "semiconductor_codebase_tidal",
    "Dreamer codebase":  "semiconductor_codebase",
}

st.title("🤖 GraphRAG Agent")


with st.sidebar:
    st.header("설정")
    selected_project  = st.selectbox("프로젝트 선택:", list(PROJECT_MAP.keys()))
    target_collection = PROJECT_MAP[selected_project]
    st.caption(f"Qdrant: `{target_collection}`")
    if st.button("대화 초기화"):
        st.session_state.messages = []
        st.rerun()

# ── 엔진 초기화 ───────────────────────────────────────────────────────────────
if (
    "current_collection" not in st.session_state
    or st.session_state.current_collection != target_collection
):
    st.session_state.current_collection = target_collection
    st.session_state.messages = []

    with st.spinner("엔진 로딩 중... ⏳"):
        vector_store  = VectorStore(collection_name=target_collection)
        graph_store   = GraphStore()
        search_engine = SmartSearchEngine(vector_store, graph_store)
        llm           = LocalLLM()
        agent         = CodebaseAgent(search_engine, vector_store, graph_store)

        st.session_state.vector_store  = vector_store
        st.session_state.graph_store   = graph_store
        st.session_state.search_engine = search_engine
        st.session_state.llm           = llm
        st.session_state.agent         = agent

# ── 대화 기록 ────────────────────────────────────────────────────────────────
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# ── 입력 처리 ─────────────────────────────────────────────────────────────────
query = st.chat_input("코드에 대해 무엇이든 물어보세요!")

if query:
    st.session_state.messages.append({"role": "user", "content": query})
    with st.chat_message("user"):
        st.markdown(query)

    with st.chat_message("assistant"):
        agent: CodebaseAgent = st.session_state.agent
        llm:   LocalLLM      = st.session_state.llm

        tool_placeholder     = st.empty()
        response_placeholder = st.empty()
        full_answer          = ""
        tool_log             = []

        for chunk in agent.run(query, llm):
            if isinstance(chunk, str) and chunk.startswith("__TOOL_LOG__"):
                tool_log = json.loads(chunk[len("__TOOL_LOG__"):])
                with tool_placeholder.container():
                    for step in tool_log:
                        st.markdown(
                            f"<div class='tool-step'>{step}</div>",
                            unsafe_allow_html=True
                        )
            else:
                full_answer += chunk
                response_placeholder.markdown(full_answer + "▌")

        response_placeholder.markdown(full_answer)

        with st.expander("📚 Agent 실행 로그"):
            for step in tool_log:
                st.markdown(
                    f"<div class='tool-step'>{step}</div>",
                    unsafe_allow_html=True
                )

        st.session_state.messages.append({"role": "assistant", "content": full_answer})
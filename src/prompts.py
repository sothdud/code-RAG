from pathlib import Path
from .source_extraction import extract_source_lines

# ===================================================================
# 🏭 System Context & Graph Schema
# ===================================================================

# 30B 모델은 이 스키마 정보를 이해하고 "코드 간 연결성"을 추론할 수 있습니다.
GRAPH_SCHEMA_INFO = """
<knowledge_graph_schema>
The Knowledge Graph currently stores **Function Call Relationships** only:
- **Nodes**: `Function` (Attributes: name, filepath, qualified_name)
- **Edges**: `(:Function)-[:CALLS]->(:Function)`

Use this to trace logic flow (e.g., "Which function calls `login`?").
Note: Class inheritance or module imports are NOT explicitly stored in the graph.
</knowledge_graph_schema>
"""

SYSTEM_PROMPT = f"""You are an expert Senior Python Code Analyst.
Your goal is to analyze the provided code context and answer the user's query accurately.

{GRAPH_SCHEMA_INFO}

### 🧠 CRITICAL THINKING PROCESS
Before answering, you must "think" inside <thinking> tags:
1. **Context Verification**: Check provided <file> tags. Do not assume code not present in context.
2. **Logic Tracing**: Trace the execution flow (Caller -> Callee).
3. **Graph Reasoning**: Use the graph schema to infer relationships between components.
4. **Answer Formulation**: Provide a structured, evidence-based answer.

### 🚫 STRICT RULES
- **No Hallucination**: If the code is not in <context>, say "Code not provided".
- **Citations**: Always cite file paths and line numbers (e.g., `main.py:10`).
- **Language**: Answer in Korean (한국어).
"""

# ===================================================================
# 🧱 Context Builders (XML Style)
# ===================================================================

def format_context_xml(results: list) -> str:
    """
    검색 결과를 XML 형식으로 변환 (모델이 파일 경계를 명확히 인식함)
    """
    xml_context = "<context>\n"
    
    for idx, item in enumerate(results, 1):
        if isinstance(item, dict) and 'chunk' in item:
            data = item['chunk']
            content = data.get('content', '')
            filepath = data.get('filepath', 'unknown')
            start_line = data.get('start_line', 1)
        elif isinstance(item, dict) and 'filepath' in item:
            data = item
            content = item.get('content', '') or item.get('fullContent', '')
            filepath = item.get('filepath', 'unknown')
            start_line = item.get('start_line', 1)
        else:
            continue

        xml_context += f"""
    <file index="{idx}" path="{filepath}" start_line="{start_line}">
<![CDATA[
{content}
]]>
    </file>
"""
    xml_context += "</context>"
    return xml_context

# 호환성 유지
def build_file_context(results: list) -> str:
    return format_context_xml(results)

def build_smart_search_context(results: list) -> str:
    return format_context_xml(results)


# ===================================================================
# 🎯 Task-Specific Prompts
# ===================================================================

def get_existence_check_prompt(query: str, context: str, target_name: str) -> str:
    return f"""
{SYSTEM_PROMPT}

<task>
Verify if the function or class `{target_name}` exists and explain its role.
</task>

{context}

<user_query>
{query}
</user_query>

Let's think step by step in <thinking> tags. Check if `{target_name}` is defined or just imported.
"""

def get_flow_analysis_prompt(query: str, context: str) -> str:
    return f"""
{SYSTEM_PROMPT}

<task>
Trace the execution flow based on the user's query.
Focus on data transformation, arguments passing, and return values.
</task>

{context}

<user_query>
{query}
</user_query>

<output_format>
### 🚀 실행 흐름 분석

1. **[Step 1] Function Name** (`filepath:line`)
   - **Input**: ...
   - **Logic**: ...
   - **Call**: `func()` → ...

2. **[Step 2] ...**
</output_format>

Let's think step by step in <thinking> tags. Trace variables across functions.
"""

def get_bug_analysis_prompt(query: str, context: str) -> str:
    return f"""
{SYSTEM_PROMPT}

<task>
Analyze the code for potential bugs, logical errors, or edge cases mentioned in the query.
</task>

{context}

<user_query>
{query}
</user_query>

<output_format>
### 🎯 버그 분석 결과

**[결론]**: (발견됨 / 없음 / 정보부족)

#### 🔴 발견된 문제 (if any)
1. **문제점**: ...
   - 📍 **위치**: `filepath:line`
   - 📝 **원인**: ...
   - ✅ **수정 제안**:
     ```python
     # Corrected Code
     ```
</output_format>

Let's think step by step in <thinking> tags.
"""

def get_file_summary_prompt(query: str, context: str, filename: str) -> str:
    return f"""
{SYSTEM_PROMPT}

<task>
Summarize the structure, responsibility, and key dependencies of `{filename}`.
</task>

{context}

<user_query>
{query}
</user_query>

Let's think step by step in <thinking> tags.
"""

def get_error_diagnostic_prompt(query: str, context: str, error_traceback: str) -> str:
    return f"""
{SYSTEM_PROMPT}

<task>
Diagnose the error based on the traceback and the provided code.
Match the traceback line numbers with the code context to find the root cause.
</task>

<traceback>
{error_traceback}
</traceback>

{context}

<user_query>
{query}
</user_query>

Let's think step by step in <thinking> tags.
"""

def get_general_prompt(query: str, context: str) -> str:
    return f"""
{SYSTEM_PROMPT}

<task>
Answer the user's general coding question based on the context.
Utilize the Knowledge Graph Schema to explain how components interact.
</task>

{context}

<user_query>
{query}
</user_query>

Let's think step by step in <thinking> tags.
"""
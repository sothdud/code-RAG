from pathlib import Path
from .source_extraction import extract_source_lines

# ===================================================================
# 🏭 System Context & Graph Schema
# ===================================================================

# 🆕 Multi-language 지원 명시
GRAPH_SCHEMA_INFO = """
<knowledge_graph_schema>
The Knowledge Graph stores **Function/Method Call Relationships** for multiple languages:

**Supported Languages:**
- Python (.py)
- C# (.cs)
- XAML (.xaml) - WPF View definitions

**Nodes:**
- `Function` (Python functions)
- `Method` (C# methods, Python class methods)
- `Property` (C# properties, for MVVM binding)
- `View` (XAML views)

**Edges:**
- `(:Function)-[:CALLS]->(:Function)` - Function calls (Python)
- `(:Method)-[:CALLS]->(:Method)` - Method calls (C#, Python)
- `(:View)-[:BINDS]->(:Property)` - XAML data binding (e.g., UI Elements binding to ViewModel Properties/Commands)
- `(:Property)-[:CALLS]->(:Method)` - Command Properties invoking actual Methods via Delegates (e.g., RelayCommand in C#)

**Naming Conventions:**
- Python: `module.path.ClassName.method_name`
- C#: `Namespace.ClassName.MethodName`
- XAML: `Namespace.ViewName`

Use this to trace logic flow across languages (e.g., "Which C# method does this XAML button call?").
</knowledge_graph_schema>
"""

# 👇 수정된 부분: 응답 스타일에 코드 원본 출력 강제 지시 추가
SYSTEM_PROMPT = f"""You're a helpful coding assistant analyzing a codebase that contains Python, C#, and XAML code.

{GRAPH_SCHEMA_INFO}

**Response style:**
- Use Korean polite form (존댓말): 합니다/습니다/해요/네요 (NOT 해/야/어)
- Talk like explaining to a colleague - conversational but professional
- Cite code locations: "ObjectTestViewModel.cs 269번 줄에서..."
- If info is missing from context: acknowledge what you found + guide next steps (don't just say "없어요" and stop)
- **CRITICAL: ALWAYS include the actual code snippets in your response using markdown code blocks (```csharp, ```python, ```xml).**
- **CRITICAL: Do NOT just summarize the logic. You MUST show the exact code proving your explanation.**

**Process:**
Use <thinking> to analyze, then answer clearly in polite Korean.
"""

# ===================================================================
# 🧱 Context Builders (XML Style)
# ===================================================================

def format_context_xml(results: list) -> str:
    """
    검색 결과를 XML 형식으로 변환 (언어 구분 명시)
    """
    xml_context = "<context>\n"
    
    for idx, item in enumerate(results, 1):
        if isinstance(item, dict) and 'chunk' in item:
            data = item['chunk']
            content = data.get('content', '')
            filepath = data.get('filepath', 'unknown')
            start_line = data.get('start_line', 1)
            language = data.get('language', 'unknown')
            chunk_type = data.get('type', 'code')
        elif isinstance(item, dict) and 'filepath' in item:
            data = item
            content = item.get('content', '') or item.get('fullContent', '')
            filepath = item.get('filepath', 'unknown')
            start_line = item.get('start_line', 1)
            language = item.get('language', 'unknown')
            chunk_type = item.get('type', 'code')
        else:
            continue

        xml_context += f"""
    <file index="{idx}" path="{filepath}" start_line="{start_line}" language="{language}" type="{chunk_type}">
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
# 🎯 Task-Specific Prompts (Multi-language)
# ===================================================================

def get_existence_check_prompt(query: str, context: str, target_name: str) -> str:
    return f"""
{SYSTEM_PROMPT}

Here's the code:
{context}

Question: {query}

Check if `{target_name}` exists and what it does. Use <thinking> to look through the code, then answer naturally.
"""

def get_flow_analysis_prompt(query: str, context: str) -> str:
    return f"""
{SYSTEM_PROMPT}

Here's the code:
{context}

Question: {query}

Trace through the execution flow and explain how things work. Use <thinking> to analyze, then explain it naturally like you're walking someone through the code.
"""

def get_bug_analysis_prompt(query: str, context: str) -> str:
    return f"""
{SYSTEM_PROMPT}

Here's the code:
{context}

Question: {query}

Look for bugs, edge cases, or potential issues. Use <thinking> to analyze, then explain what you found conversationally.
"""

def get_file_summary_prompt(query: str, context: str, filename: str) -> str:
    return f"""
{SYSTEM_PROMPT}

Here's the code from `{filename}`:
{context}

Question: {query}

Summarize what this file does. Use <thinking> to analyze it, then explain like you're giving a quick overview to a teammate.
"""

def get_error_diagnostic_prompt(query: str, context: str, error_traceback: str, language: str = "python") -> str:
    return f"""
{SYSTEM_PROMPT}

Here's the error:
{error_traceback}

And here's the relevant code:
{context}

Question: {query}

Debug this {language} error. Use <thinking> to match the traceback to the code, then explain what's wrong and how to fix it.
"""

def get_mvvm_analysis_prompt(query: str, context: str) -> str:
    return f"""
{SYSTEM_PROMPT}

Here's the code:
{context}

Question: {query}

Analyze the MVVM architecture - how the View, ViewModel, and Model connect. Use <thinking> to trace the bindings and data flow, then explain it naturally.
"""

# 👇 수정된 부분: LLM이 최종 답변을 생성할 때 가장 많이 쓰이는 프롬프트에 재차 강조
def get_general_prompt(query: str, context: str) -> str:
    return f"""
{SYSTEM_PROMPT}

Here's the relevant code:
{context}

Question: {query}

Analyze the code in <thinking> tags, then answer naturally like you're chatting with a teammate.
**IMPORTANT RULE**: You MUST include the exact code blocks (using ``` markdown format) from the Context above to support your explanation. Do not just summarize the code.
"""
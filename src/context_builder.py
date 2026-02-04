"""
LLM용 컨텍스트 생성 (Graph-Code의 RAG 방식)
"""

from .models import CodeChunk


class ContextBuilder:
    def build_context(self, chunks: list[CodeChunk], max_tokens: int = 30000) -> str:
        """
        LLM에 전달할 컨텍스트 생성

        Graph-Code 방식: 관계 정보 포함
        """
        context_parts = []
        current_tokens = 0

        for chunk in chunks:
            doc_section = f"\n**Docstring**: {chunk.docstring}" if chunk.docstring else ""
            # 함수 정보
            func_info = f"""
## {chunk.qualified_name}
**File**: `{chunk.filepath}:{chunk.start_line}`
**Type**: {chunk.type}

### Code:
```{chunk.language}
{chunk.content}
```
 
### Relationships:
        - **Calls**: {', '.join(chunk.calls) if chunk.calls else 'None'}
        - **Called by**: {', '.join(chunk.called_by) if chunk.called_by else 'None'}
        - **Imports**: {', '.join(chunk.imports) if chunk.imports else 'None'}
        
        ---
        """

            # 토큰 수 추정 (1 token ≈ 4 chars)
            estimated_tokens = len(func_info) // 4

            if current_tokens + estimated_tokens > max_tokens:
                break

            context_parts.append(func_info)
            current_tokens += estimated_tokens

        return "\n".join(context_parts)

    def build_call_chain_context(self, call_chain: list[str],
                                 call_graph) -> str:
        """호출 체인 시각화"""
        lines = ["## Call Chain:\n"]

        for i, func_qn in enumerate(call_chain):
            chunk = call_graph.nodes.get(func_qn)
            if chunk:
                indent = "  " * i
                lines.append(f"{indent}↓ {chunk.name} ({chunk.filepath})")

        return "\n".join(lines)
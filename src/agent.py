from __future__ import annotations
import json
import os
from typing import List, Dict, Generator, Callable
from dataclasses import dataclass
from ollama import Client  # 💡 공식 SDK 추가
from . import prompts

@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    function: Callable

    def to_ollama_spec(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            }
        }


AGENT_SYSTEM_PROMPT = """You are a senior developer assistant analyzing a codebase.
The target project uses the following tech stack: C# WPF with MVVM architecture, and Python.
## Tool usage strategy
1. For any question about code: ALWAYS use tools first, NEVER answer from memory
2. Start with semantic_search to find candidate functions by meaning
3. If you get node_ids back, use get_code_snippet to retrieve actual source code
4. For call flow questions: use query_knowledge_graph with natural language
5. Use find_by_name when you know the exact function/class name

## Response
Answer in Korean (존댓말). Always cite file path and line number.
Stop calling tools when you have enough code to answer confidently.
"""


class CodebaseAgent:

    def __init__(self, search_engine, vector_store, graph_store, max_steps: int = 6):
        self.search   = search_engine
        self.db       = vector_store
        self.graph    = graph_store
        self.max_steps = max_steps

        host_url = os.getenv("OLLAMA_URL", "http://localhost:11434").replace("/api/generate", "")
        self.client = Client(host=host_url)
        self.agent_model = os.getenv("AGENT_MODEL", "qwen3:30b")

        self.tools = self._register_tools()
        self.tool_map = {t.name: t for t in self.tools}

    def _register_tools(self) -> List[Tool]:
        return [
            Tool(
                name="semantic_search",
                description="Semantic search for functions/classes by meaning. Use this FIRST for any question. Translates Korean queries automatically.",
                parameters={
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string", "description": "Natural language query (English preferred)"},
                        "top_k": {"type": "integer", "description": "Number of results (default: 5)"}
                    }
                },
                function=self._tool_semantic_search,
            ),
            Tool(
                name="get_code_snippet",
                description="Retrieve actual source code by exact qualified_name. Use after semantic_search.",
                parameters={
                    "type": "object",
                    "required": ["qualified_name"],
                    "properties": {"qualified_name": {"type": "string", "description": "Exact qualified name"}}
                },
                function=self._tool_get_code_snippet,
            ),
            Tool(
                name="find_by_name",
                description="Find function/class/method by exact name. Use when you already know the exact identifier.",
                parameters={
                    "type": "object",
                    "required": ["names"],
                    "properties": {"names": {"type": "array", "items": {"type": "string"}, "description": "List of exact identifiers"}}
                },
                function=self._tool_find_by_name,
            ),
            Tool(
                name="query_knowledge_graph",
                description="Query the call graph to trace execution flow. Ask in natural language.",
                parameters={
                    "type": "object",
                    "required": ["question"],
                    "properties": {
                        "question": {"type": "string", "description": "Natural language question about call relationships"},
                        "seed_name": {"type": "string", "description": "Starting function/method qualified_name"}
                    }
                },
                function=self._tool_query_knowledge_graph,
            ),
            Tool(
                name="read_file",
                description="Get all functions and classes from a specific file.",
                parameters={
                    "type": "object",
                    "required": ["filename"],
                    "properties": {"filename": {"type": "string", "description": "Filename or partial path"}}
                },
                function=self._tool_read_file,
            ),
        ]

    #ReAct 루프
    def run(self, query: str, llm) -> Generator[str, None, None]:
        messages = [
            {"role": "system", "content": AGENT_SYSTEM_PROMPT},
            {"role": "user",   "content": query},
        ]
        tool_specs   = [t.to_ollama_spec() for t in self.tools]
        collected    = {}   
        tool_log     = []
        steps        = 0

        while steps < self.max_steps:
            steps += 1

            try:
                response = self.client.chat(
                    model=self.agent_model,
                    messages=messages,
                    tools=tool_specs,
                    stream=False # JSON만
                )
                msg = response.message
                tool_calls = getattr(msg, 'tool_calls', [])
            except Exception as e:
                yield f"❌ Ollama 통신 에러: {e}"
                return

            # 도구 호출이 발생한 경우
            if tool_calls:
                call = tool_calls[0] # 첫 번째 도구만 실행
                tool_name = call.function.name
                tool_args = call.function.arguments

                tool = self.tool_map.get(tool_name)
                if not tool:
                    break

                # 도구 실행 
                result_text, summary, chunks = tool.function(**tool_args)
                for c in chunks:
                    qn = c.get("qualified_name", c.get("name", ""))
                    if qn:
                        collected[qn] = c

                # Streamlit UI용 로그 생성
                tool_log.append(f"{_icon(tool_name)} **{tool_name}**({_args_str(tool_args)}): {summary}")

                messages.append(msg)
                messages.append({
                    "role": "tool",
                    "content": str(result_text),
                })

  
            else:
                yield "__TOOL_LOG__" + json.dumps(tool_log)

                final_chunks = list(collected.values())[:14]
                wrapped      = [{"chunk": c} for c in final_chunks]
                context      = prompts.format_context_xml(wrapped)
                final_prompt = prompts.get_general_prompt(query, context)

                yield from llm.generate_response(
                    final_prompt, query,
                    model_override=llm.model  # qwen3-coder:30b
                )
                return

        # max_steps 초과 시 강제 답변
        yield "__TOOL_LOG__" + json.dumps(tool_log)
        final_chunks = list(collected.values())[:14]
        wrapped      = [{"chunk": c} for c in final_chunks]
        context      = prompts.format_context_xml(wrapped)
        yield from llm.generate_response(prompts.get_general_prompt(query, context), query)

    #도구 구현

    def _tool_semantic_search(self, query: str, top_k: int = 5):
        try:
            results = self.search.search(query, top_k=top_k)
            chunks  = []
            lines   = []
            for r in results:
                c = r.get("chunk", r) if isinstance(r, dict) else r
                if not isinstance(c, dict): continue
                chunks.append(c)
                qn = c.get("qualified_name", c.get("name", "unknown"))
                fp = c.get("filepath", "")
                ln = c.get("start_line", "")
                lines.append(f"- {qn} ({fp}:{ln})")

            result_text = f"Found {len(chunks)} results:\n" + "\n".join(lines)
            return result_text, f"Found {len(chunks)} matches", chunks
        except Exception as e:
            return f"Search error: {e}", "error", []

    def _tool_get_code_snippet(self, qualified_name: str):
        try:
            results = self.db.client.scroll(
                collection_name=self.db.collection,
                scroll_filter={"must": [{"key": "qualified_name", "match": {"value": qualified_name}}]},
                limit=1, with_payload=True, with_vectors=False,
            )
            points = results[0] if results else []
            if points:
                chunk = points[0].payload
                content = chunk.get("content", "")
                fp = chunk.get("filepath", "")
                ln = chunk.get("start_line", "")
                return f"[{qualified_name}] ({fp}:{ln})\n{content}", f"Retrieved ({len(content)} chars)", [chunk]
            return f"Not found: {qualified_name}", "not found", []
        except Exception as e:
            return f"Error: {e}", "error", []

    def _tool_find_by_name(self, names: List[str]):
        found, seen = [], set()
        for chunk in self.search.all_chunks:
            cname = chunk.get("name", "")
            qn = chunk.get("qualified_name", "")
            for t in names:
                if (cname == t or qn.endswith(f".{t}") or qn == t or cname.lower() == t.lower()):
                    if qn not in seen:
                        found.append(chunk)
                        seen.add(qn)
                    break
        lines = [f"- {c.get('qualified_name', c.get('name', ''))} ({c.get('filepath', '')}:{c.get('start_line', '')})" for c in found]
        return f"Found {len(found)} exact matches:\n" + "\n".join(lines), f"{len(found)} exact matches", found

    def _tool_query_knowledge_graph(self, question: str, seed_name: str = ""):
        results, lines = [], []
        if seed_name:
            lines.extend(self.graph.get_execution_flow(seed_name, depth=2))
            callees = self.graph.get_callees(seed_name)
            if callees:
                results.extend(self.db.retrieve_by_filenames(callees[:5]))
        import re
        identifiers = re.findall(r'\b[A-Z][a-zA-Z0-9]+|[a-z][a-z0-9_]+[a-z0-9]\b', question)
        for ident in identifiers[:3]:
            lines.extend(self.graph.get_execution_flow(ident, depth=1))
        result_text = "\n".join(lines) if lines else "No graph relationships found."
        return result_text, f"{len(lines)} relationships found", results

    def _tool_read_file(self, filename: str):
        try:
            chunks, seen = [], set()
            for chunk in self.search.all_chunks:
                fp = chunk.get("filepath", "")
                if filename.lower() in fp.lower():
                    qn = chunk.get("qualified_name", "")
                    if qn not in seen:
                        chunks.append(chunk)
                        seen.add(qn)
            lines = [f"- {c.get('name', '')} ({c.get('type', '')})" for c in chunks]
            return f"File '{filename}' contains {len(chunks)} items:\n" + "\n".join(lines), f"{len(chunks)} items", chunks
        except Exception as e:
            return f"Error: {e}", "error", []


def _icon(name: str) -> str:
    return {"semantic_search": "🔍", "get_code_snippet": "📄", "find_by_name": "🎯", "query_knowledge_graph": "🕸️", "read_file": "📂"}.get(name, "⚙️")

def _args_str(args: dict) -> str:
    v = list(args.values())
    first = v[0] if v else ""
    if isinstance(first, list): return ", ".join(str(x) for x in first[:3])
    return str(first)[:40]


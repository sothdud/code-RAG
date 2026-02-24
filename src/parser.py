import os
import re
import requests
from pathlib import Path
from typing import List, Optional

from tree_sitter_languages import get_language, get_parser
from .models import CodeChunk
from .call_extractor import CallExtractor
from .fqn_resolver import resolve_fqn_from_ast
from .language_specs import PYTHON_SPEC, CSHARP_SPEC, XAML_SPEC

class ASTParser:
    def __init__(self, context_lines: int = 5):
        self.context_lines = context_lines
        
        self.specs = {
            ".py": PYTHON_SPEC,
            ".cs": CSHARP_SPEC,
            ".xaml": XAML_SPEC
        }
        
        self.parsers = {}
        self.queries = {}
        self.call_extractors = {}
        self.csharp_call_query = None

        for ext, spec in self.specs.items():
            try:
                lang = get_language(spec.name)
                self.parsers[ext] = get_parser(spec.name)
                self.queries[ext] = lang.query(spec.structure_query)
                
                if ext == ".py":
                    self.call_extractors[ext] = CallExtractor(spec.name)
                elif ext == ".cs":
                    self.csharp_call_query = lang.query(spec.call_query)
            except Exception as e:
                print(f"⚠️ Failed to load parser for {spec.name}: {e}")

    # 💡 [핵심] 오직 LLM(Ollama)만을 사용하여 코드 요약 생성
    def _generate_llm_summary(self, code_snippet: str) -> str:
        ollama_url = os.getenv("OLLAMA_URL")
        llm_model = os.getenv("LLM_MODEL", "qwen3-coder:30b")
        if not ollama_url: return ""
            
        prompt = (
            "문장 끝은 반드시 '~니다.' 형태의 정중한 평어체로 통일해 (ex. 합니다. or 입니다.)"
            "다음 코드의 역할과 기능을 2~3줄로 간단히 요약해. "
            "마크다운이나 부가 설명 없이 핵심 요약 텍스트만 대답해.\n\n"
            f"코드:\n{code_snippet}"
        )
        
        payload = {"model": llm_model, "prompt": prompt, "stream": False}
        
        try:
            #타임아웃을 60초에서 120초로 넉넉하게 늘립니다.
            response = requests.post(ollama_url, json=payload, timeout=120)
            if response.status_code == 200:
                return response.json().get("response", "").strip()
        except Exception as e:
            print(f"\n⚠️ LLM 요약 실패 (TimeOut 등): {e}")   
            return "⚠️ AI 요약 생성 실패 (답변 시간 초과 또는 모델 오류)"
            
        return "⚠️ 요약 생성 실패 (알 수 없는 오류)"

    def parse(self, filepath: str) -> List[CodeChunk]:
        ext = os.path.splitext(filepath)[1].lower()
        if ext == ".xaml":
            return self._parse_xaml(filepath)
        if ext in self.specs:
            return self._parse_code(filepath, ext)
        return []

    def _normalize_filepath(self, filepath: str) -> str:
        path = Path(filepath)
        parts = path.parts
        new_parts = []
        for i, part in enumerate(parts):
            if i > 0 and part == parts[i-1]: continue
            new_parts.append(part)
        return str(Path(*new_parts))

    def _read_file_as_utf8_bytes(self, filepath: str) -> bytes:
        encodings = ['utf-8-sig', 'utf-8', 'cp949', 'euc-kr', 'latin-1']
        for enc in encodings:
            try:
                with open(filepath, 'r', encoding=enc) as f:
                    return f.read().encode('utf-8')
            except (UnicodeDecodeError, UnicodeEncodeError):
                continue
        return b""
    
    def _read_file_safe(self, filepath: str) -> str:
        encodings = ['utf-8-sig', 'utf-8', 'cp949', 'euc-kr', 'latin-1']
        for enc in encodings:
            try:
                with open(filepath, 'r', encoding=enc) as f:
                    return f.read()
            except UnicodeDecodeError:
                continue
        return ""

    def _parse_xaml(self, filepath: str) -> List[CodeChunk]:
        try:
            content = self._read_file_safe(filepath)
            if not content: return []
            
            normalized_filepath = self._normalize_filepath(filepath)
            chunks = []

            x_class_match = re.search(r'x:Class="([^"]+)"', content)
            if x_class_match:
                qualified_name = x_class_match.group(1)
                name = qualified_name.split('.')[-1]
            else:
                name = Path(filepath).stem
                qualified_name = name

            bindings = re.findall(r'\{Binding\s+(?:Path=)?([a-zA-Z0-9_.]+)', content)
            valid_bindings = sorted(list(set([b for b in bindings if b])))

            chunks.append(CodeChunk(
                name=name,
                type="view",
                content=content,
                filepath=normalized_filepath,
                start_line=1,
                language="xaml",
                qualified_name=qualified_name,
                module_path=str(Path(filepath).parent),
                calls=valid_bindings,
                docstring="WPF XAML View" # XAML은 화면 구성이므로 일단 고정
            ))
            return chunks
        except Exception as e:
            return []

    def _parse_code(self, filepath: str, ext: str) -> List[CodeChunk]:
        try:
            code_bytes = self._read_file_as_utf8_bytes(filepath)
            if not code_bytes: return []
            
            try:
                code = code_bytes.decode('utf-8')
            except UnicodeDecodeError:
                return []
            
            parser = self.parsers.get(ext)
            query = self.queries.get(ext)
            
            if not parser or not query: return []
            
            tree = parser.parse(code_bytes)
            captures = query.captures(tree.root_node)

            chunks = []
            processed_nodes = set()
            normalized_filepath = self._normalize_filepath(filepath)

            for node, tag in captures:
                if node in processed_nodes: continue
                if tag == "namespace_block": continue 

                start_line = node.start_point[0]
                name_node = node.child_by_field_name('name')
                if not name_node: continue
                
                func_name = code_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8")
                chunk_content = code_bytes[node.start_byte:node.end_byte].decode("utf-8")
                
                qualified_name = func_name
                calls = []
                docstring = "" 

                if ext == ".py":
                    qualified_name = resolve_fqn_from_ast(node, Path(filepath), Path("."), code_bytes) or func_name
                    calls = self.call_extractors[ext].extract_calls(chunk_content)
                    docstring = self._generate_llm_summary(chunk_content) # 파이썬도 LLM 요약 적용
                
                elif ext == ".cs":
                    ns_match = re.search(r'namespace\s+([a-zA-Z0-9_.]+)', code)
                    ns = ns_match.group(1) if ns_match else ""
                    qualified_name = f"{ns}.{func_name}" if ns else func_name
                    
                    if self.csharp_call_query:
                        chunk_bytes = chunk_content.encode("utf-8")
                        call_tree = parser.parse(chunk_bytes)
                        call_captures = self.csharp_call_query.captures(call_tree.root_node)
                        for c_node, c_tag in call_captures:
                            call_name = chunk_bytes[c_node.start_byte:c_node.end_byte].decode("utf-8")
                            calls.append(call_name)
                    
                    # 💡 주석 추출 로직을 버리고, 무조건 LLM(Ollama)에게 요약 요청!
                    docstring = self._generate_llm_summary(chunk_content)

                chunks.append(CodeChunk(
                    name=func_name,
                    type=tag,
                    content=chunk_content,
                    filepath=normalized_filepath,
                    start_line=start_line + 1,
                    language=self.specs[ext].db_name,
                    qualified_name=qualified_name,
                    module_path=str(Path(filepath).parent),
                    calls=list(set(calls)),
                    docstring=docstring # 👈 LLM이 작성한 요약이 저장됩니다!
                ))
                processed_nodes.add(node)
            
            return chunks

        except Exception as e:
            return []
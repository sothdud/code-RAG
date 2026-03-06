import os
import re
import requests
from pathlib import Path
from typing import List, Optional
from bs4 import BeautifulSoup
from tree_sitter_languages import get_language, get_parser
from .models import CodeChunk
from .call_extractor import CallExtractor
from .fqn_resolver import resolve_fqn_from_ast
from .language_specs import PYTHON_SPEC, CSHARP_SPEC, XAML_SPEC, LanguageSpec

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
            # 💡 Tree-sitter 쿼리가 없는 언어(예: XAML)는 Tree-sitter 초기화를 건너뜀
            if not spec.structure_query:
                continue

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
        
        if not ollama_url: 
            return "⚠️ OLLAMA_URL이 설정되지 않았습니다."
            
        prompt = (
            "문장 끝은 반드시 '~니다.' 형태의 정중한 평어체로 통일해 (ex. 합니다. or 입니다.)\n"
            "다음 코드의 역할과 기능을 2~3줄로 간단히 요약해. \n"
            "마크다운이나 부가 설명 없이 핵심 요약 텍스트만 대답해.\n\n"
            f"코드:\n{code_snippet}"
        )
        
        payload = {"model": llm_model, "prompt": prompt, "stream": False}
        
        try:
            # 타임아웃 120초로 넉넉하게 설정
            response = requests.post(ollama_url, json=payload, timeout=120)
            if response.status_code == 200:
                return response.json().get("response", "").strip()
            else:
                return f"⚠️ LLM API 오류: 상태 코드 {response.status_code}"
        except Exception as e:
            print(f"\n⚠️ LLM 요약 실패: {e}")   
            return "⚠️ AI 요약 생성 실패"

    def parse(self, filepath: str) -> List[CodeChunk]:
        ext = os.path.splitext(filepath)[1].lower()
        spec = self.specs.get(ext)
        if not spec: 
            return []

        # 💡 Spec에 정규식 패턴이 정의되어 있다면 정규식 파서로 라우팅
        if spec.binding_pattern:
            return self._parse_regex_from_spec(filepath, spec)
        else:
            return self._parse_code(filepath, ext)

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

    # 💡 Spec 기반 정규식 파서 (XAML 처리용)
    def _parse_regex_from_spec(self, filepath: str, spec: LanguageSpec) -> List[CodeChunk]:
        try:
            content = self._read_file_safe(filepath)
            if not content: return []
            
            normalized_filepath = self._normalize_filepath(filepath)

            # 1. BeautifulSoup으로 XML 파싱 (lxml 엔진 사용)
            soup = BeautifulSoup(content, 'xml')

            # 2. x:Class 추출 (View의 Qualified Name 결정)
            # 루트 태그에서 x:Class 속성을 찾습니다. 정규식보다 훨씬 안전합니다.
            root_tag = soup.find()
            if root_tag and root_tag.has_attr('x:Class'):
                qualified_name = root_tag['x:Class']
                name = qualified_name.split('.')[-1]
            else:
                name = Path(filepath).stem
                qualified_name = name

            # 3. Binding 구문 추출 (ViewModel과의 의존성 Edge 생성)
            calls = set()
            if spec.binding_pattern:
                binding_regex = re.compile(spec.binding_pattern)
                
                # 모든 태그를 순회하며 속성값(Attribute)을 검사
                for tag in soup.find_all(True):
                    for attr_name, attr_value in tag.attrs.items():
                        # 속성값이 문자열이고 '{Binding'을 포함하는 경우에만 정규식 검사
                        if isinstance(attr_value, str) and '{Binding' in attr_value:
                            match = binding_regex.search(attr_value)
                            if match:
                                calls.add(match.group(1))

            calls = sorted(list(calls))

            # 💡 XAML 내용 전체를 LLM에 넘겨서 요약 생성
            print(f"🔄 요약 생성 중... [{name} (XAML View)]")
            docstring = self._generate_llm_summary(content)

            return [CodeChunk(
                name=name,
                type="view",
                content=content,
                filepath=normalized_filepath,
                start_line=1,
                language=spec.db_name,
                qualified_name=qualified_name,
                module_path=str(Path(filepath).parent),
                calls=calls, # 추출된 데이터 바인딩이 edges로 사용됨
                docstring=docstring 
            )]
            
        except Exception as e:
            print(f"⚠️ XML 파싱 실패 ({filepath}): {e}")
            import traceback
            traceback.print_exc()
            return []

    # 기존 Tree-sitter 기반 코드 파서 (Python, C# 처리용)
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
            captures = sorted(query.captures(tree.root_node), key=lambda x: x[0].start_byte)  # ← 정렬 추가

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
                    if ext in self.call_extractors:
                        calls = self.call_extractors[ext].extract_calls(chunk_content)
                    
                    print(f"🔄 요약 생성 중... [{func_name}]")
                    docstring = self._generate_llm_summary(chunk_content) 
                
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
                    
                    print(f"🔄 요약 생성 중... [{func_name}]")
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
                    docstring=docstring # LLM이 작성한 요약 저장
                ))
                processed_nodes.add(node)
            
            return chunks

        except Exception as e:
            print(f"⚠️ 코드 파싱 실패 ({filepath}): {e}")
            return []
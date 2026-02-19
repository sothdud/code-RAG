import os
import re
from pathlib import Path
from typing import List, Optional

from tree_sitter_languages import get_language, get_parser
from .models import CodeChunk
from .call_extractor import CallExtractor
from .fqn_resolver import resolve_fqn_from_ast
from .language_specs import PYTHON_SPEC, CSHARP_SPEC,XAML_SPEC

class ASTParser:
    def __init__(self, context_lines: int = 5):
        self.context_lines = context_lines
        
        # 1. 언어별 스펙 로드
        self.specs = {
            ".py": PYTHON_SPEC,
            ".cs": CSHARP_SPEC,
            ".xaml": XAML_SPEC
        }
        
        self.parsers = {}
        self.queries = {}
        self.call_extractors = {}
        self.csharp_call_query = None

        # 2. 파서 및 쿼리 초기화
        for ext, spec in self.specs.items():
            try:
                lang = get_language(spec.name)
                self.parsers[ext] = get_parser(spec.name)
                self.queries[ext] = lang.query(spec.structure_query)
                
                # Python은 기존 CallExtractor 사용
                if ext == ".py":
                    self.call_extractors[ext] = CallExtractor(spec.name)
                # C#은 별도 쿼리 로드
                elif ext == ".cs":
                    self.csharp_call_query = lang.query(spec.call_query)
                    
            except Exception as e:
                print(f"⚠️ Failed to load parser for {spec.name}: {e}")

    def parse(self, filepath: str) -> List[CodeChunk]:
        """ingest.py에서 호출하는 메인 메서드"""
        ext = os.path.splitext(filepath)[1].lower()

        # 1. XAML 처리 (정규식)
        if ext == ".xaml":
            return self._parse_xaml(filepath)

        # 2. 코드 파일 (.py, .cs) 처리 (Tree-sitter)
        if ext in self.specs:
            return self._parse_code(filepath, ext)

        return []

    def _normalize_filepath(self, filepath: str) -> str:
        """경로 정규화"""
        path = Path(filepath)
        parts = path.parts
        new_parts = []
        for i, part in enumerate(parts):
            if i > 0 and part == parts[i-1]:
                continue
            new_parts.append(part)
        return str(Path(*new_parts))

    def _read_file_as_utf8_bytes(self, filepath: str) -> bytes:
        """
        파일을 읽어서 UTF-8 바이트로 변환합니다.
        원본 인코딩을 자동 감지하여 안전하게 변환합니다.
        """
        # 시도할 인코딩 목록 (순서 중요)
        encodings = ['utf-8-sig', 'utf-8', 'cp949', 'euc-kr', 'latin-1']
        
        for enc in encodings:
            try:
                with open(filepath, 'r', encoding=enc) as f:
                    content = f.read()
                # 성공적으로 읽었으면 UTF-8 바이트로 변환하여 반환
                return content.encode('utf-8')
            except (UnicodeDecodeError, UnicodeEncodeError):
                # 이 인코딩이 아니면 다음 것으로 시도
                continue
            except Exception as e:
                print(f"⚠️ Read Error {filepath}: {e}")
                return b""
        
        # 모든 인코딩 시도 실패
        print(f"❌ Failed to decode {filepath} with any standard encoding.")
        return b""
    
    def _read_file_safe(self, filepath: str) -> str:
        """XAML 등 문자열이 필요한 경우를 위한 헬퍼 메서드"""
        encodings = ['utf-8-sig', 'utf-8', 'cp949', 'euc-kr', 'latin-1']
        
        for enc in encodings:
            try:
                with open(filepath, 'r', encoding=enc) as f:
                    return f.read()
            except UnicodeDecodeError:
                continue
            except Exception as e:
                print(f"⚠️ Read Error {filepath}: {e}")
                return ""
        
        print(f"❌ Failed to decode {filepath} with any standard encoding.")
        return ""

    def _parse_xaml(self, filepath: str) -> List[CodeChunk]:
        """XAML 파싱"""
        try:
            content = self._read_file_safe(filepath)
            if not content: return []
            
            normalized_filepath = self._normalize_filepath(filepath)
            chunks = []

            # x:Class 추출
            x_class_match = re.search(r'x:Class="([^"]+)"', content)
            if x_class_match:
                qualified_name = x_class_match.group(1)
                name = qualified_name.split('.')[-1]
            else:
                name = Path(filepath).stem
                qualified_name = name

            # Binding 추출
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
                docstring="WPF XAML View"
            ))
            return chunks
        except Exception as e:
            print(f"⚠️ Error parsing XAML {filepath}: {e}")
            return []

    def _parse_code(self, filepath: str, ext: str) -> List[CodeChunk]:
        """코드 파싱 (Python/C#)"""
        try:
            # 1. ✅ 직접 UTF-8 바이트로 읽기 (재인코딩 문제 방지)
            code_bytes = self._read_file_as_utf8_bytes(filepath)
            if not code_bytes: 
                return []
            
            # 2. 디코딩해서 문자열도 준비 (정규식 검색용)
            try:
                code = code_bytes.decode('utf-8')
            except UnicodeDecodeError:
                print(f"⚠️ Failed to decode UTF-8 bytes for {filepath}")
                return []
            
            parser = self.parsers.get(ext)
            query = self.queries.get(ext)
            
            if not parser or not query:
                return []
            
            tree = parser.parse(code_bytes)
            captures = query.captures(tree.root_node)

            chunks = []
            processed_nodes = set()
            normalized_filepath = self._normalize_filepath(filepath)

            for node, tag in captures:
                if node in processed_nodes:
                    continue
                if tag == "namespace_block": 
                    continue 

                start_line = node.start_point[0]
                
                # 이름 추출
                name_node = node.child_by_field_name('name')
                if not name_node: 
                    continue
                
                func_name = code_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8")
                
                # 내용 추출
                chunk_content = code_bytes[node.start_byte:node.end_byte].decode("utf-8")
                
                # Qualified Name & Calls 처리
                qualified_name = func_name
                calls = []

                if ext == ".py":
                    qualified_name = resolve_fqn_from_ast(node, Path(filepath), Path("."), code_bytes) or func_name
                    calls = self.call_extractors[ext].extract_calls(chunk_content)
                
                elif ext == ".cs":
                    ns_match = re.search(r'namespace\s+([a-zA-Z0-9_.]+)', code)
                    ns = ns_match.group(1) if ns_match else ""
                    qualified_name = f"{ns}.{func_name}" if ns else func_name
                    
                    if self.csharp_call_query:
                        # chunk_content는 이미 UTF-8 문자열이므로 안전하게 인코딩 가능
                        chunk_bytes = chunk_content.encode("utf-8")
                        call_tree = parser.parse(chunk_bytes)
                        call_captures = self.csharp_call_query.captures(call_tree.root_node)
                        for c_node, c_tag in call_captures:
                            call_name = chunk_bytes[c_node.start_byte:c_node.end_byte].decode("utf-8")
                            calls.append(call_name)

                chunks.append(CodeChunk(
                    name=func_name,
                    type=tag,
                    content=chunk_content,
                    filepath=normalized_filepath,
                    start_line=start_line + 1,
                    language=self.specs[ext].name,
                    qualified_name=qualified_name,
                    module_path=str(Path(filepath).parent),
                    calls=list(set(calls)),
                    docstring=""
                ))
                processed_nodes.add(node)
            
            return chunks

        except Exception as e:
            print(f"⚠️ Error parsing code {filepath}: {e}")
            return []
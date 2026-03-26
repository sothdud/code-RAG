import os
import re
import requests
import concurrent.futures
from pathlib import Path
from typing import List, Optional
from bs4 import BeautifulSoup
from tree_sitter_languages import get_language, get_parser
from .models import CodeChunk
from .call_extractor import CallExtractor
from .fqn_resolver import resolve_fqn_from_ast, resolve_csharp_fqn_from_ast, resolve_cpp_fqn_from_ast
from .language_specs import PYTHON_SPEC, CSHARP_SPEC, XAML_SPEC, CPP_SPEC, LanguageSpec

class ASTParser:
    def __init__(self, context_lines: int = 5):
        self.context_lines = context_lines
        
        self.specs = {
            ".py": PYTHON_SPEC,
            ".cs": CSHARP_SPEC,
            ".xaml": XAML_SPEC,
            ".cpp": CPP_SPEC,
            ".h": CPP_SPEC,
            ".hpp": CPP_SPEC,
            ".c": CPP_SPEC
        }
        
        self.parsers = {}
        self.queries = {}
        self.call_extractors = {}
        self.csharp_call_query = None
        self.cpp_call_query = None

        for ext, spec in self.specs.items():
            if not spec.structure_query:
                continue

            try:
                lang = get_language(spec.name)
                if ext not in self.parsers:
                    self.parsers[ext] = get_parser(spec.name)
                    self.queries[ext] = lang.query(spec.structure_query)
                
                if ext == ".py":
                    self.call_extractors[ext] = CallExtractor(spec.name)
                elif ext == ".cs" and not self.csharp_call_query:
                    self.csharp_call_query = lang.query(spec.call_query)
                elif ext in [".cpp", ".h", ".hpp", ".c"] and not self.cpp_call_query:
                    self.cpp_call_query = lang.query(spec.call_query)
            except Exception as e:
                print(f"⚠️ Failed to load parser for {spec.name} ({ext}): {e}")


    def _extract_block(self, content: str, brace_start: int) -> str: #중괄호 처리
        depth = 0
        for i in range(brace_start, len(content)):
            if content[i] == '{':
                depth += 1
            elif content[i] == '}':
                depth -= 1
                if depth == 0:
                    return content[brace_start: i + 1]
        return content[brace_start:]

    def _parse_fallback_regex(self, content: str, filepath: str, spec: LanguageSpec) -> List[CodeChunk]:
        
        #Tree-sitter 실패하면 정규표현식
      
        chunks: List[CodeChunk] = []
        file_stem = Path(filepath).stem
        normalized_path = self._normalize_filepath(filepath)
        module_path = str(Path(filepath).parent)

        ns_match = re.search(r'namespace\s+(\w+)', content)
        namespace = ns_match.group(1) if ns_match else file_stem

        class_pattern = re.compile(
            r'(?:public\s+|private\s+)?(?:ref\s+)?(?:class|struct)\s+(\w+)[^{;]*\{'
        )
        found_any_class = False

        for cls_match in class_pattern.finditer(content):
            cls_name = cls_match.group(1)
            cls_start_line = content[:cls_match.start()].count('\n') + 1

            brace_pos = content.rfind('{', cls_match.start(), cls_match.end())
            if brace_pos == -1:
                continue
            cls_body = self._extract_block(content, brace_pos)
            found_any_class = True

            method_pattern = re.compile(
                r'(?:array<[^\>]+>\^?|void|int|bool|double|float|unsigned\s+\w+|System::\w+\^?|[\w:]+\^?)\s+'
                r'(\w+)\s*\([^)]*\)\s*\{'
            )
            
            skeleton_cls_body = ""
            last_idx = 0
            
            for m_match in method_pattern.finditer(cls_body):
                method_name = m_match.group(1)
                SKIP_KEYWORDS = {'if', 'for', 'while', 'switch', 'return', 'else', 'catch'}
                if method_name in SKIP_KEYWORDS:
                    continue

                method_start_line = cls_start_line + cls_body[:m_match.start()].count('\n')
                brace_pos_m = cls_body.find('{', m_match.start()) # 현재 메서드의 여는 중괄호 찾기
                if brace_pos_m == -1:
                    continue
                
                method_body = self._extract_block(cls_body, brace_pos_m)
      
                skeleton_cls_body += cls_body[last_idx:brace_pos_m] + "{ /* 구현부 생략 */ }"
                last_idx = brace_pos_m + len(method_body)

                full_method = cls_body[m_match.start(): m_match.start() + len(m_match.group(0)) - 1] + method_body
                
                qn = f"{namespace}.{cls_name}.{method_name}"
                print(f"  ↪ 🔄 fallback 요약 중... [{method_name} (method)]")
                summary = self._generate_llm_summary(full_method)
                chunks.append(CodeChunk(
                    name=method_name,
                    type="method",
                    content=full_method,
                    summary=summary,
                    filepath=normalized_path,
                    start_line=method_start_line,
                    language=spec.db_name,
                    qualified_name=qn,
                    docstring=summary,
                    module_path=module_path,
                ))

            # 남은 나머지 텍스트 이어붙이기
            skeleton_cls_body += cls_body[last_idx:]

            qn = f"{namespace}.{cls_name}"
            print(f"  ↪ 🔄 fallback 요약 중... [{cls_name} (class skeleton)]")
            summary = self._generate_llm_summary(skeleton_cls_body)
            chunks.append(CodeChunk(
                name=cls_name,
                type="class",
                content=skeleton_cls_body,
                summary=summary,
                filepath=normalized_path,
                start_line=cls_start_line,
                language=spec.db_name,
                qualified_name=qn,
                docstring=summary,
                module_path=module_path,
            ))

        if not found_any_class:
            print(f"  ⚠️ No class found in {Path(filepath).name}, content will be stored in file chunk")

        return chunks


    MAX_SUMMARY_INPUT_CHARS = 3000

    def _generate_llm_summary(self, content: str, summary_type: str = "short") -> str:
       
        if len(content.strip()) < 80:
            clean_code = " ".join(content.strip().split())
            return f"단순 속성/선언부: `{clean_code}`"

        ollama_url = os.getenv("VLLM_API_BASE")
        llm_model = os.getenv("LLM_MODEL")

        if not ollama_url:
            return "⚠️ OLLAMA_URL이 설정되지 않았습니다."

        if len(content) > self.MAX_SUMMARY_INPUT_CHARS:
            content = content[:self.MAX_SUMMARY_INPUT_CHARS] + "\n... (truncated)"
            
        if summary_type == "ui_detailed":
            prompt = (
                "너는 UI 분석 및 설계 전문가야. 아래 [XAML 코드]를 분석하여 AI 에이전트가 이 화면의 '공간적 배치'와 '기능적 역할'을 모두 파악하도록 문서화해.\n\n"
                f"--- [코드 시작] ---\n{content}\n--- [코드 끝] ---\n\n"
                "분석 시 다음 3가지 관점을 균형 있게 반영해:\n"
                "1. **시각적 레이아웃(Visual Layout)**: 전체적인 화면 분할(Grid, StackPanel 등)과 주요 컨트롤의 공간적 위치.\n"
                "2. **상태 및 데이터(State & Data)**: 어떤 데이터가 화면에 표시되고, 사용자 입력이 어떤 속성과 동기화되는지.\n"
                "3. **상호작용 로직(Interaction)**: 버튼 클릭, 더블 클릭 등 이벤트 발생 시 실행되는 커맨드와 그 목적.\n\n"
                "반드시 다음 목차 구조를 지켜서 한국어로 작성해:\n"
                "1. 🔍 전체 레이아웃 구조 (상하/좌우 배치 중심)\n"
                "2. 🧱 주요 UI 컴포넌트 및 기능 (위치별 상세 설명)\n"
                "3. 🧠 데이터 바인딩 및 커맨드 매핑 (View-ViewModel 연결)\n\n"
                "4.**추측 금지**: '추론됩니다', '보입니다', '예상됩니다', '~할 것입니다' 등 모든 불확실한 표현을 **금지**한다.\n"
                "불필요한 디자인 속성(Margin, Padding)은 생략하고, 에이전트가 UI의 '형태'와 '기능'을 연결할 수 있게 작성해."
            )
        else:
            prompt = (
                "너는 코드 구조 요약기야. 아래 코드의 역할과 기능을 2줄 이내로 극도로 짧게 요약해.\n"
                "규칙 1: 인사말, 서론, 부가 설명 절대 금지.\n"
                "규칙 2: 마크다운 문법 사용 금지.\n"
                "규칙 3: 문장 끝은 '~합니다.' 또는 '~입니다.'로 끝낼 것.\n"
                "규칙 4: 반드시 한국어로만 작성할 것.\n\n"
                f"[코드]\n{content}\n\n"
                "요약:"
            )
            
        payload = {
            "model": llm_model,
            "messages": [
                {"role": "system", "content": "You are a professional code summarizer."},
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 4096,
            "temperature": 0.1,
            "top_p": 0.95,
            "top_k": 20,
            "presence_penalty": 0.5,
            "repetition_penalty": 1.0
        }
        
        try:
            response = requests.post(ollama_url, json=payload, timeout=60)
            if response.status_code == 200:
                raw = response.json()["choices"][0]["message"]["content"].strip()
                clean = re.sub(r'<think>.*?(?:</think>|$)', '', raw, flags=re.DOTALL).strip()
                return clean
            else:
                return f"⚠️ LLM API 오류: 상태 코드 {response.status_code}"
                
        except requests.exceptions.Timeout:
            return "⚠️ 타임아웃 (서버 응답 지연)"
        except requests.exceptions.ConnectionError:
            return "⚠️ 연결 오류 (vLLM 서버 응답 없음)"
        except KeyError as e:
            return "⚠️ 응답 파싱 에러 (JSON 구조 다름)"
        except Exception as e:
            print(f"\n⚠️ LLM 요약 실패: {e}")
            return f"⚠️ 기타 오류 ({type(e).__name__})"
        

    def parse(self, filepath: str) -> List[CodeChunk]:
        ext = os.path.splitext(filepath)[1].lower()
        spec = self.specs.get(ext)
        if not spec: 
            return []

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

    def _read_file_safe(self, filepath: str) -> str:
        encodings = ['utf-8-sig', 'utf-8', 'cp949', 'euc-kr', 'latin-1']
        for enc in encodings:
            try:
                with open(filepath, 'r', encoding=enc) as f:
                    return f.read()
            except UnicodeDecodeError:
                continue
        return ""

    # Spec 기반 정규식 파서 (XAML 등)
    def _parse_regex_from_spec(self, filepath: str, spec: LanguageSpec) -> List[CodeChunk]:
        try:
            content = self._read_file_safe(filepath)
            if not content: return []
            
            normalized_filepath = self._normalize_filepath(filepath)
            soup = BeautifulSoup(content, 'xml')
            root_tag = soup.find()
            if root_tag and root_tag.has_attr('x:Class'):
                qualified_name = root_tag['x:Class']
                name = qualified_name.split('.')[-1]
            else:
                name = Path(filepath).stem
                qualified_name = name

            xmlns_map: dict[str, str] = {}
            if root_tag:
                for attr_name, attr_value in root_tag.attrs.items():
                    if attr_name.startswith('xmlns:') and 'clr-namespace:' in attr_value:
                        prefix = attr_name.split(':', 1)[1]
                        clr_ns = attr_value.split('clr-namespace:', 1)[1].split(';')[0].strip()
                        xmlns_map[prefix] = clr_ns
            datatype_vm_qnames: list[str] = []
            xtype_pattern = re.compile(r'\{x:Type\s+([a-zA-Z0-9_]+):([a-zA-Z0-9_]+)\}')
            for tag in soup.find_all(True):
                dt_val = tag.attrs.get('DataType', '')
                if not isinstance(dt_val, str):
                    continue
                m = xtype_pattern.search(dt_val)
                if m:
                    prefix, class_name = m.group(1), m.group(2)
                    if prefix in xmlns_map:
                        vm_qn = f"{xmlns_map[prefix]}.{class_name}"
                        datatype_vm_qnames.append(vm_qn)

            datacontext_vm_qnames: list[str] = []
            
            if root_tag:
                for attr_name, attr_value in root_tag.attrs.items():
                    attr_name_lower = attr_name.lower()
                    
                    if 'datacontext' in attr_name_lower:
                        match = re.search(r'(?:Type=)?([a-zA-Z0-9_]+):([a-zA-Z0-9_]+)', str(attr_value))
                        if match:
                            prefix, class_name = match.group(1), match.group(2)
                            if prefix in xmlns_map:
                                datacontext_vm_qnames.append(f"{xmlns_map[prefix]}.{class_name}")

                    elif 'autowireviewmodel' in attr_name_lower and str(attr_value).lower() == 'true':
                        if qualified_name:
                            vm_qn = qualified_name.replace(".Views.", ".ViewModels.")
                            if vm_qn.endswith("View"):
                                vm_qn = vm_qn[:-4] + "ViewModel"
                            elif not vm_qn.endswith("ViewModel"):
                                vm_qn += "ViewModel"
                            datacontext_vm_qnames.append(vm_qn)

            calls = set()
            wpf_events = {'Click', 'Loaded', 'Unloaded', 'SelectionChanged', 'TextChanged', 'MouseDoubleClick'}

            linked_vm_qnames = list(dict.fromkeys(datacontext_vm_qnames + datatype_vm_qnames))
            for vm_qn in linked_vm_qnames:
                calls.add(f"__vm_context__:{vm_qn}")  

            if spec.binding_pattern:
                binding_regex = re.compile(spec.binding_pattern)
                for tag in soup.find_all(True):
                    for attr_name, attr_value in tag.attrs.items():
                        if not isinstance(attr_value, str): continue
                        if '{Binding' in attr_value:
                            match = binding_regex.search(attr_value)
                            if match:
                                calls.add(match.group(1))
                        elif attr_name in wpf_events:
                            code_behind_method = f"{qualified_name}.{attr_value}"
                            calls.add(code_behind_method)

            calls = sorted(list(calls))

            if linked_vm_qnames:
                print(f"  🔗 ViewModel 연결 감지: {linked_vm_qnames}")

            codebehind_path = Path(str(filepath) + '.cs')
            codebehind_content = ""
            if codebehind_path.exists():
                codebehind_content = self._read_file_safe(str(codebehind_path))
                print(f"  🔗 코드비하인드 병합: {codebehind_path.name}")

            merged_content = content
            if codebehind_content:
                merged_content = (
                    f"=== XAML (View) ===\n{content}\n\n"
                    f"=== Code-Behind (.xaml.cs) ===\n{codebehind_content}"
                )

            print(f"🔄 요약 생성 중... [{name} (XAML View)]")
            docstring = self._generate_llm_summary(merged_content, summary_type="ui_detailed")

            return [CodeChunk(
                name=name,
                type="view",
                content=merged_content,
                filepath=normalized_filepath,
                start_line=1,
                summary=docstring,
                language=spec.db_name,
                qualified_name=qualified_name,
                module_path=str(Path(filepath).parent),
                calls=calls,
                docstring=docstring
            )]
            
        except Exception as e:
            print(f"⚠️ XML 파싱 실패 ({filepath}): {e}")
            return []
    

    

    # Tree-sitter 기반 코드 파서 (Python, C#, C++)
    def _parse_code(self, filepath: str, ext: str) -> List[CodeChunk]:
        chunks = []
        try:
            content = self._read_file_safe(filepath)
            if not content: return []
            code_bytes = content.encode('utf-8')

            parser = self.parsers.get(ext)
            query = self.queries.get(ext)
            spec = self.specs.get(ext)
            normalized_filepath = self._normalize_filepath(filepath)

            tree = parser.parse(code_bytes)
            captures = query.captures(tree.root_node)

            node_map = {}
            for node, capture_name in captures:
                if capture_name in ("class", "function", "method", "property", "struct"):
                    node_map[id(node)] = (node, capture_name)

            for node, node_type in node_map.values():
                name_node = node.child_by_field_name('name')
                if not name_node: continue
                
                node_name = code_bytes[name_node.start_byte:name_node.end_byte].decode('utf-8')
                raw_code = code_bytes[node.start_byte:node.end_byte].decode('utf-8')
                start_line = node.start_point[0] + 1
                
    
                if node_type in ("class", "struct"):
                    bodies_to_replace = []
                    
                    def find_bodies(n):
                        # C#/C++/Java 형태의 메서드
                        if n.type in ('function_definition', 'method_declaration', 'constructor_declaration'):
                            body_node = n.child_by_field_name('body')
                            if body_node:
                                bodies_to_replace.append((body_node.start_byte, body_node.end_byte))
                        # 자식 노드 순회
                        for child in n.children:
                            find_bodies(child)
                            
                    find_bodies(node)
                    
       
                    bodies_to_replace.sort(key=lambda x: x[0], reverse=True)
                    modified_code_bytes = bytearray(code_bytes[node.start_byte:node.end_byte])
                    
                    for b_start, b_end in bodies_to_replace:
                        rel_start = b_start - node.start_byte
                        rel_end = b_end - node.start_byte
                        
                        if 0 <= rel_start < len(modified_code_bytes) and 0 < rel_end <= len(modified_code_bytes):
                            # Python은 pass로 치환, C 계열은 /* 구현부 생략 */ 으로 치환
                            replacement = " pass # 구현부 생략".encode('utf-8') if ext == ".py" else "{ /* 구현부 생략 */ }".encode('utf-8')
                            modified_code_bytes[rel_start:rel_end] = replacement
                            
                    # 내용이 껍데기(Skeleton)만 남은 상태로 raw_code 갱신
                    raw_code = modified_code_bytes.decode('utf-8', errors='ignore')

                # FQN 추출
                if ext == ".py":
                    fqn = resolve_fqn_from_ast(node, Path(filepath), Path(os.getcwd()), code_bytes) or node_name
                elif ext == ".cs":
                    fqn = resolve_csharp_fqn_from_ast(node, code_bytes) or node_name
                elif ext in [".cpp", ".h", ".hpp", ".c"]:
                    fqn = resolve_cpp_fqn_from_ast(node, code_bytes, filepath) or node_name
                else:
                    fqn = node_name
 
                # module_path 추출
                module_path_value = ""
                if ext == ".py":
                    try:
                        rel_path = Path(filepath).resolve().relative_to(Path(os.getcwd()).resolve())
                        module_path_value = ".".join(rel_path.with_suffix('').parts)
                    except:
                        module_path_value = Path(filepath).stem
                else:
                    module_path_value = str(Path(filepath).parent)

                # 호출 관계 추출
                extracted_calls = []
                if ext == ".py" and ext in self.call_extractors:
                    extracted_calls = self.call_extractors[ext].extract_calls(raw_code)
                elif ext == ".cs" and self.csharp_call_query:
                    call_captures = self.csharp_call_query.captures(node)
                    for call_node, tag in call_captures:
                        func_name = code_bytes[call_node.start_byte:call_node.end_byte].decode('utf-8')
                        extracted_calls.append(func_name)
                    extracted_calls = list(set(extracted_calls))
                elif ext in [".cpp", ".h", ".hpp", ".c"] and self.cpp_call_query:
                    call_captures = self.cpp_call_query.captures(node)
                    for call_node, tag in call_captures:
                        func_name = code_bytes[call_node.start_byte:call_node.end_byte].decode('utf-8')
                        extracted_calls.append(func_name)
                    extracted_calls = list(set(extracted_calls))

                # LLM 개별 요약 생성 (클래스인 경우 위에서 치환된 껍데기 코드가 전달되어 요약 효율이 상승함)
                print(f"  ↪ 🔄 개별 요약 중... [{node_name} ({node_type})]")
                chunk_summary = self._generate_llm_summary(raw_code)

                chunk = CodeChunk(
                    name=node_name,
                    type=node_type,
                    content=raw_code,
                    summary=chunk_summary,  
                    filepath=normalized_filepath,
                    start_line=start_line,
                    language=spec.db_name,
                    qualified_name=fqn,
                    docstring=chunk_summary,
                    calls=extracted_calls,
                    module_path=module_path_value 
                )
                chunks.append(chunk)

         
            file_name = Path(filepath).name
            
            toc_lines = [f"  ㄴ [{c.type}] {c.name} - {c.summary}" for c in chunks] if chunks else []
            toc_content = "\n".join(toc_lines) if toc_lines else "" 
            
            if ext in ['.xaml','.xml']:
                print(f"🔄 UI 상세 분석 중... [{file_name} (View)]")
                file_docstring = self._generate_llm_summary(content, summary_type="ui_detailed")
            else:
                print(f"🔄 요약 생성 중... [{file_name} (File 전체)]") 
                
                if toc_content:
                    file_summary_prompt = f"다음은 {file_name} 파일의 구조입니다:\n{toc_content}\n\n이 파일 전체의 목적과 기술적 역할을 2줄로 요약해."
                else:
                    safe_content = content[:3000] 
                    file_summary_prompt = f"다음은 {file_name} 파일의 원본 코드입니다:\n{safe_content}\n\n이 코드의 목적과 기술적 역할을 2줄로 극도로 짧게 요약해."
                    
                file_docstring = self._generate_llm_summary(file_summary_prompt, summary_type="short")
            
            file_stem = Path(filepath).stem

            if ext == ".py":
                try:
                    rel_path = Path(filepath).resolve().relative_to(Path(os.getcwd()).resolve())
                    file_module_path = ".".join(rel_path.with_suffix('').parts)
                except:
                    file_module_path = file_stem
            else:
                file_module_path = str(Path(filepath).parent)

            file_qualified_name = file_name
            if ext == ".py":
                file_qualified_name = f"{file_module_path}.py"
            elif ext == ".cs":
                namespace_match = re.search(r'namespace\s+([\w\.]+)', content)
                if namespace_match:
                    namespace = namespace_match.group(1)
                    file_qualified_name = f"{namespace}.{file_name}"
                else:
                    file_qualified_name = file_name

            file_imports = []
            if ext in [".cpp", ".h", ".hpp", ".c"]:
                file_imports = re.findall(r'#include\s*[<"]([^>"]+)[>"]', content)
                file_imports = list(set(file_imports))

            has_structural_chunks = any(c.type in ('class', 'method') for c in chunks)
            file_content = (
                f"File: {file_name}\nPath: {normalized_filepath}\nSummary: {file_docstring}"
                if has_structural_chunks
                else content
            )
            chunks.append(CodeChunk(
                name=file_name,
                type="file",
                content=file_content,
                summary=file_docstring,
                filepath=normalized_filepath,
                start_line=1,
                language=spec.db_name,
                qualified_name=file_qualified_name,
                docstring=file_docstring,
                module_path=file_module_path,
                imports=file_imports
            ))

        except Exception as e:
            print(f"⚠️ Code Parse Error in {filepath}: {e}")

        if ext in ('.cpp', '.h', '.hpp', '.c') and not any(
            c.type != 'file' for c in chunks
        ):
            print(f"  ⚠️ tree-sitter yielded 0 structural chunks for {Path(filepath).name}, using regex fallback")
            try:
                content_for_fallback = self._read_file_safe(filepath)
                spec = self.specs.get(ext)
                fallback_chunks = self._parse_fallback_regex(content_for_fallback, filepath, spec)
                if fallback_chunks:
                    non_file = [c for c in fallback_chunks if c.type != 'file']
                    chunks = non_file + [c for c in chunks if c.type == 'file']
                    print(f"  ✅ regex fallback produced {len(non_file)} chunks")
            except Exception as fe:
                print(f"  ⚠️ regex fallback also failed: {fe}")

        if ext == '.cpp':
            try:
                content_for_cpp = self._read_file_safe(filepath)
                extra = self._parse_cpp_out_of_class(content_for_cpp, filepath)
                if extra:
                    existing_qns = {c.qualified_name for c in chunks}
                    new_chunks = [c for c in extra if c.qualified_name not in existing_qns]
                    chunks = [c for c in chunks if c.type == 'file'] + \
                             [c for c in chunks if c.type != 'file'] + new_chunks
                    print(f"  ✅ out-of-class methods: +{len(new_chunks)} chunks")
            except Exception as ce:
                print(f"  ⚠️ cpp out-of-class parse failed: {ce}")

        return chunks

    def _parse_cpp_out_of_class(self, content: str, filepath: str) -> list:
        chunks = []
        normalized_path = self._normalize_filepath(filepath)
        module_path = str(Path(filepath).parent)

        ns_match = re.search(r'namespace\s+(\w+)', content)
        namespace = ns_match.group(1) if ns_match else ""

        pattern = re.compile(
            r'(?:[\w<>\^\*\s]+?)\s+'
            r'(\w+)::(\w+)\s*'
            r'\([^)]*\)\s*\{',
            re.MULTILINE
        )

        for m in pattern.finditer(content):
            cls_name = m.group(1)
            method_name = m.group(2)
            start_line = content[:m.start()].count('\n') + 1

            brace_pos = content.rfind('{', m.start(), m.end())
            if brace_pos == -1:
                continue
            body = self._extract_block(content, brace_pos)
            full_method = content[m.start(): m.start() + len(m.group(0)) - 1] + body

            qn = f"{namespace}.{cls_name}.{method_name}" if namespace else f"{cls_name}.{method_name}"

            extracted_calls = []
            if self.cpp_call_query:
                try:
                    code_bytes = full_method.encode('utf-8')
                    tree = self.parsers['.cpp'].parse(code_bytes)
                    call_captures = self.cpp_call_query.captures(tree.root_node)
                    for call_node, _ in call_captures:
                        extracted_calls.append(
                            code_bytes[call_node.start_byte:call_node.end_byte].decode('utf-8')
                        )
                    extracted_calls = list(set(extracted_calls))
                except Exception:
                    pass

            print(f"  ↪ 🔄 out-of-class 요약 중... [{cls_name}::{method_name}]")
            summary = self._generate_llm_summary(full_method)

            chunks.append(CodeChunk(
                name=method_name,
                type="method",
                content=full_method,
                summary=summary,
                filepath=normalized_path,
                start_line=start_line,
                language="cpp",
                qualified_name=qn,
                docstring=summary,
                module_path=module_path,
                calls=extracted_calls,
            ))

        return chunks
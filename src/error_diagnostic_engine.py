"""
에러 진단 엔진 - 스택 트레이스 자동 분석 및 원인 코드 특정
Production-Ready Error Diagnostic System
"""

import re
import traceback
from pathlib import Path
from typing import Optional, List, Dict
from dataclasses import dataclass
from loguru import logger


@dataclass
class ErrorLocation:
    """에러 발생 위치 정보"""
    filepath: str
    line_number: int
    function_name: str
    code_snippet: str
    error_type: str
    error_message: str
    
    
@dataclass
class ErrorDiagnostic:
    """에러 진단 결과"""
    error_location: ErrorLocation
    root_cause: Optional[ErrorLocation] = None  # 실제 버그 위치 (다를 수 있음)
    call_chain: List[ErrorLocation] = None  # 전체 호출 체인
    related_code: List[Dict] = None  # 연관 코드 청크들
    diagnosis: str = ""  # LLM 진단 결과
    fix_suggestion: str = ""  # 수정 제안


class ErrorTraceParser:
    """
    파이썬 에러 트레이스백 파싱
    실제 현장/사내에서 발생한 에러 메시지를 분석
    """
    
    # 다양한 에러 패턴 매칭
    TRACEBACK_PATTERN = re.compile(
        r'File "([^"]+)", line (\d+), in (.+)'
    )
    
    ERROR_TYPE_PATTERN = re.compile(
        r'^(\w+Error|Exception): (.+)$', 
        re.MULTILINE
    )
    
    # 일반적인 에러 타입들
    COMMON_ERRORS = {
        'AttributeError': 'Level1',  # 속성/메서드 없음
        'TypeError': 'Level1',       # 타입 불일치
        'ValueError': 'Level2',      # 값 검증 실패
        'KeyError': 'Level2',        # 딕셔너리 키 없음
        'IndexError': 'Level2',      # 리스트 인덱스 범위 초과
        'ImportError': 'Level1',     # 임포트 실패
        'NameError': 'Level1',       # 정의되지 않은 변수
        'FileNotFoundError': 'Level2',
        'ConnectionError': 'Level3', # 외부 연동 문제
        'TimeoutError': 'Level3',
    }
    
    def __init__(self, repo_root: Path = None):
        self.repo_root = repo_root or Path.cwd()
        
    def parse_traceback(self, error_text: str) -> List[ErrorLocation]:
        """
        트레이스백 텍스트를 파싱하여 에러 위치 리스트 추출
        
        입력 예시:
        ```
        Traceback (most recent call last):
          File "/app/main.py", line 45, in process_data
            result = calculate(x, y)
          File "/app/utils.py", line 12, in calculate
            return x / y
        ZeroDivisionError: division by zero
        ```
        """
        locations = []
        
        # 1. 스택 프레임 추출
        for match in self.TRACEBACK_PATTERN.finditer(error_text):
            filepath = match.group(1)
            line_num = int(match.group(2))
            func_name = match.group(3).strip()
            
            # 코드 스니펫 추출 시도
            code_snippet = self._extract_code_at_line(filepath, line_num)
            
            locations.append(ErrorLocation(
                filepath=filepath,
                line_number=line_num,
                function_name=func_name,
                code_snippet=code_snippet,
                error_type="",  # 아직 미정
                error_message=""
            ))
        
        # 2. 에러 타입 및 메시지 추출
        error_match = self.ERROR_TYPE_PATTERN.search(error_text)
        if error_match and locations:
            error_type = error_match.group(1)
            error_msg = error_match.group(2)
            
            # 마지막 위치(실제 에러 발생 지점)에 정보 추가
            locations[-1].error_type = error_type
            locations[-1].error_message = error_msg
        
        return locations
    
    def _extract_code_at_line(self, filepath: str, line_num: int, 
                              context_lines: int = 3) -> str:
        """
        실제 파일에서 해당 라인 주변 코드 추출
        """
        try:
            path = Path(filepath)
            
            # 상대 경로 처리
            if not path.is_absolute():
                path = self.repo_root / path
            
            if not path.exists():
                return f"# File not found: {filepath}"
            
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
            
            start = max(0, line_num - context_lines - 1)
            end = min(len(lines), line_num + context_lines)
            
            snippet_lines = []
            for i in range(start, end):
                marker = ">>>" if i == line_num - 1 else "   "
                snippet_lines.append(f"{marker} {i+1:4d} | {lines[i].rstrip()}")
            
            return "\n".join(snippet_lines)
            
        except Exception as e:
            return f"# Error reading file: {e}"
    
    def classify_error_severity(self, error_type: str) -> str:
        """
        에러 타입에 따른 심각도 분류
        """
        return self.COMMON_ERRORS.get(error_type, 'Level2')


class ErrorDiagnosticEngine:
    """
    에러 자동 진단 시스템
    
    사용 시나리오:
    1. 사용자가 에러 메시지 붙여넣기
    2. 자동으로 관련 코드 찾기 (RAG 검색)
    3. LLM이 원인 분석 + 수정 제안
    """
    
    def __init__(self, search_engine, llm_client, repo_root: Path = None):
        """
        Args:
            search_engine: SmartSearchEngine 인스턴스
            llm_client: LocalLLM 인스턴스
            repo_root: 프로젝트 루트 경로
        """
        self.search = search_engine
        self.llm = llm_client
        self.parser = ErrorTraceParser(repo_root)
        
    def diagnose_error(self, error_text: str) -> ErrorDiagnostic:
        """
        에러 메시지를 받아서 자동으로 진단
        
        단계:
        1. 트레이스백 파싱
        2. 에러 발생 코드 위치 특정
        3. RAG로 연관 코드 검색
        4. LLM으로 원인 분석 + 수정 제안
        """
        logger.info("🔍 Starting error diagnosis...")
        
        # Step 1: 트레이스백 파싱
        error_locations = self.parser.parse_traceback(error_text)
        
        if not error_locations:
            logger.warning("⚠️ No traceback found in error text")
            return self._create_fallback_diagnostic(error_text)
        
        error_loc = error_locations[-1]  # 실제 에러 발생 지점
        logger.info(f"📍 Error detected at: {error_loc.filepath}:{error_loc.line_number}")
        
        # Step 2: 에러 위치 기반으로 RAG 검색
        search_query = self._build_search_query(error_loc, error_text)
        logger.info(f"🔎 Searching with query: {search_query}")
        
        related_results = self.search.search(search_query, top_k=5)
        
        # Step 3: 호출 체인 역추적 (가능한 경우)
        call_chain = error_locations if len(error_locations) > 1 else None
        
        # Step 4: LLM에게 진단 요청
        diagnosis_prompt = self._build_diagnostic_prompt(
            error_location=error_loc,
            call_chain=call_chain,
            related_code=related_results,
            original_error=error_text
        )
        
        logger.info("🤖 Requesting LLM diagnosis...")
        llm_response = self.llm.generate_response(
            system_prompt=self._get_diagnostic_system_prompt(),
            user_query=diagnosis_prompt
        )
        
        # Step 5: 결과 구조화
        return ErrorDiagnostic(
            error_location=error_loc,
            root_cause=self._identify_root_cause(error_locations),
            call_chain=call_chain,
            related_code=related_results,
            diagnosis=llm_response,
            fix_suggestion=""  # LLM 응답에서 추출 가능
        )
    
    def _build_search_query(self, error_loc: ErrorLocation, error_text: str) -> str:
        """
        에러 정보를 기반으로 최적의 검색 쿼리 생성
        """
        # 파일명 추출
        filename = Path(error_loc.filepath).name
        
        # 함수명 정리 (lambda, <module> 등 제외)
        func_name = error_loc.function_name
        if func_name in ['<module>', '<lambda>']:
            func_name = ""
        
        # 에러 타입 및 핵심 키워드 추출
        error_type = error_loc.error_type or ""
        
        # 쿼리 조합
        query_parts = [
            filename,
            func_name,
            error_type,
        ]
        
        # 에러 메시지에서 변수명/함수명 추출
        if error_loc.error_message:
            # 작은따옴표 안의 내용 추출 (변수/함수명일 가능성 높음)
            keywords = re.findall(r"'([^']+)'", error_loc.error_message)
            query_parts.extend(keywords[:2])  # 최대 2개만
        
        return " ".join(filter(None, query_parts))
    
    def _build_diagnostic_prompt(self, error_location: ErrorLocation,
                                 call_chain: List[ErrorLocation],
                                 related_code: List[Dict],
                                 original_error: str) -> str:
        """
        LLM에게 전달할 진단 프롬프트 생성
        """
        prompt_parts = [
            "# 🐛 에러 진단 요청\n",
            "## 발생한 에러\n",
            "```",
            original_error,
            "```\n",
            f"## 에러 발생 위치\n",
            f"**파일**: `{error_location.filepath}:{error_location.line_number}`",
            f"**함수**: `{error_location.function_name}`",
            f"**에러 타입**: `{error_location.error_type}`\n",
            "### 해당 코드\n",
            "```python",
            error_location.code_snippet,
            "```\n",
        ]
        
        # 호출 체인 정보 추가
        if call_chain and len(call_chain) > 1:
            prompt_parts.append("## 호출 체인 (Call Stack)\n")
            for i, loc in enumerate(call_chain):
                prompt_parts.append(
                    f"{i+1}. `{loc.filepath}:{loc.line_number}` "
                    f"in `{loc.function_name}`"
                )
            prompt_parts.append("\n")
        
        # 연관 코드 정보 추가
        if related_code:
            prompt_parts.append("## 연관 코드 (RAG 검색 결과)\n")
            for i, result in enumerate(related_code[:3], 1):
                chunk = result.get('chunk', {})
                prompt_parts.extend([
                    f"### {i}. `{chunk.get('qualified_name', 'unknown')}`",
                    f"**위치**: `{chunk.get('filepath')}:{chunk.get('start_line')}`\n",
                    "```python",
                    chunk.get('content', '')[:500],  # 500자로 제한
                    "```\n"
                ])
        
        prompt_parts.append("""
## 요청 사항

다음 형식으로 답변해주세요:

### 🎯 원인 진단
[에러가 발생한 정확한 이유를 코드와 함께 설명]

### 🔍 문제 코드
```python
# 문제가 되는 정확한 라인
```

### ✅ 수정 방법
```python
# 수정된 코드
```

### ⚠️ 주의사항
[이 에러를 피하기 위한 추가 조언]
""")
        
        return "\n".join(prompt_parts)
    
    def _get_diagnostic_system_prompt(self) -> str:
        """
        에러 진단용 시스템 프롬프트
        """
        return """**PRODUCTION ERROR DIAGNOSTIC SYSTEM**

## 역할
당신은 현장/사내에서 발생한 실제 에러를 분석하는 전문 디버거입니다.

## 핵심 원칙

1. **증거 기반 분석**
   - 제공된 트레이스백과 코드만으로 판단
   - 추측 금지 - 확실한 것만 보고

2. **명확한 위치 특정**
   - 파일명:라인번호 필수 표기
   - 문제 코드를 정확히 인용

3. **실용적 해결책**
   - 즉시 적용 가능한 수정안 제시
   - "왜 이렇게 수정하는가" 설명

4. **간결성**
   - 장황한 설명 지양
   - 핵심만 전달

## 분석 체크리스트

✅ 에러 타입이 정확히 무엇을 의미하는가?
✅ 해당 라인에서 왜 이 에러가 발생했는가?
✅ 호출 체인 상 다른 곳에 근본 원인이 있는가?
✅ 어떻게 수정해야 하는가?

**언어**: 한국어로 답변
"""
    
    def _identify_root_cause(self, 
                            locations: List[ErrorLocation]) -> Optional[ErrorLocation]:
        """
        호출 체인에서 실제 버그의 근본 원인 위치 추정
        
        예: ZeroDivisionError가 calculate()에서 발생했지만
            실제 원인은 validate_input()에서 검증 누락
        """
        if not locations or len(locations) == 1:
            return None
        
        # 휴리스틱: 에러 타입에 따라 판단
        error_type = locations[-1].error_type
        
        # TypeError, ValueError 등은 입력 검증 누락이 원인일 가능성 높음
        if error_type in ['TypeError', 'ValueError', 'ZeroDivisionError']:
            # 호출 체인에서 2-3단계 위를 의심
            if len(locations) >= 2:
                return locations[-2]
        
        return None
    
    def _create_fallback_diagnostic(self, error_text: str) -> ErrorDiagnostic:
        """
        트레이스백이 없는 경우 대체 진단
        """
        logger.warning("⚠️ Creating fallback diagnostic")
        
        # 에러 타입만이라도 추출
        error_match = re.search(r'(\w+Error|Exception): (.+)', error_text)
        
        error_type = ""
        error_msg = error_text
        
        if error_match:
            error_type = error_match.group(1)
            error_msg = error_match.group(2)
        
        fallback_location = ErrorLocation(
            filepath="unknown",
            line_number=0,
            function_name="unknown",
            code_snippet="# 코드 위치를 특정할 수 없습니다",
            error_type=error_type,
            error_message=error_msg
        )
        
        return ErrorDiagnostic(
            error_location=fallback_location,
            diagnosis="트레이스백 정보가 부족하여 자동 진단이 어렵습니다. 전체 에러 메시지를 제공해주세요."
        )


# ===================================================================
# 사용 예시
# ===================================================================

def example_usage():
    """
    실제 사용 시나리오
    """
    from .search import SmartSearchEngine
    from .llm_client import LocalLLM
    from .database import VectorStore
    from .graph_store import GraphStore
    
    # 1. 의존성 초기화
    vector_db = VectorStore(...)
    graph_db = GraphStore(...)
    search_engine = SmartSearchEngine(vector_db, graph_db)
    llm = LocalLLM()
    
    # 2. 진단 엔진 생성
    diagnostic = ErrorDiagnosticEngine(
        search_engine=search_engine,
        llm_client=llm,
        repo_root=Path("/app")
    )
    
    # 3. 사용자가 붙여넣은 에러 메시지
    error_message = """
Traceback (most recent call last):
  File "/app/api/endpoints.py", line 45, in process_request
    result = data_processor.transform(input_data)
  File "/app/core/processor.py", line 78, in transform
    return self._apply_rules(data['items'])
KeyError: 'items'
    """
    
    # 4. 자동 진단
    result = diagnostic.diagnose_error(error_message)
    
    # 5. 결과 출력
    print("=" * 60)
    print("🐛 에러 진단 결과")
    print("=" * 60)
    print(f"📍 위치: {result.error_location.filepath}:{result.error_location.line_number}")
    print(f"🔴 에러: {result.error_location.error_type}")
    print(f"💬 메시지: {result.error_location.error_message}")
    print("\n" + "=" * 60)
    print(result.diagnosis)
    print("=" * 60)


if __name__ == "__main__":
    example_usage()
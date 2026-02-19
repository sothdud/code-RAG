"""
에러 진단 엔진 (Python + C# 지원)
트레이스백 파싱 → 코드 검색 → LLM 분석
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List
from .search_engine import SmartSearchEngine
from .llm import LocalLLM


@dataclass
class ErrorLocation:
    """에러 발생 위치 정보"""
    filepath: str
    line_number: int
    function_name: str
    code_snippet: str
    error_type: str
    error_message: str
    language: str = "python"  # 🆕 언어 필드 추가


@dataclass
class ErrorDiagnostic:
    """에러 진단 결과"""
    error_location: ErrorLocation
    root_cause: Optional[ErrorLocation] = None
    diagnosis: str = ""
    call_chain: Optional[List[ErrorLocation]] = None
    related_code: Optional[List] = None
    fix_suggestion: Optional[str] = None


class ErrorTracebackParser:
    """
    다중 언어 트레이스백 파서 (Python + C#)
    """
    
    # Python 에러 타입별 심각도
    COMMON_ERRORS = {
        "AttributeError": "Level2",
        "TypeError": "Level2",
        "ValueError": "Level2",
        "KeyError": "Level2",
        "IndexError": "Level2",
        "NameError": "Level1",
        "ImportError": "Level1",
        "SyntaxError": "Level1",
        "IndentationError": "Level1",
        "ZeroDivisionError": "Level3",
        "FileNotFoundError": "Level2",
        "PermissionError": "Level2",
        "RuntimeError": "Level3",
    }
    
    # C# 에러 타입별 심각도
    CSHARP_ERRORS = {
        "NullReferenceException": "Level1",
        "ArgumentNullException": "Level2",
        "InvalidOperationException": "Level2",
        "ArgumentException": "Level2",
        "IndexOutOfRangeException": "Level2",
        "KeyNotFoundException": "Level2",
        "DivideByZeroException": "Level3",
        "FileNotFoundException": "Level2",
        "UnauthorizedAccessException": "Level2",
        "NotImplementedException": "Level2",
    }
    
    def classify_error_severity(self, error_type: str) -> str:
        """에러 심각도 분류"""
        # Python 에러 체크
        if error_type in self.COMMON_ERRORS:
            return self.COMMON_ERRORS[error_type]
        # C# 에러 체크
        if error_type in self.CSHARP_ERRORS:
            return self.CSHARP_ERRORS[error_type]
        return "Level2"  # 기본값
    
    def detect_language(self, traceback_text: str) -> str:
        """트레이스백에서 언어 감지"""
        # Python 트레이스백 특징
        if "Traceback (most recent call last):" in traceback_text:
            return "python"
        
        # C# 트레이스백 특징
        if any(marker in traceback_text for marker in [
            "at System.", 
            "at Microsoft.",
            "System.Exception:",
            "   at ",  # C# 스택 트레이스 들여쓰기
        ]):
            return "csharp"
        
        # .cs 파일 언급
        if ".cs:line" in traceback_text or ".cs'" in traceback_text:
            return "csharp"
        
        return "python"  # 기본값
    
    def parse_traceback(self, error_text: str) -> List[ErrorLocation]:
        """
        언어 자동 감지 후 적절한 파서 호출
        """
        language = self.detect_language(error_text)
        
        if language == "csharp":
            return self._parse_csharp_traceback(error_text)
        else:
            return self._parse_python_traceback(error_text)
    
    def _parse_python_traceback(self, error_text: str) -> List[ErrorLocation]:
        """
        Python 트레이스백 파싱
        
        예시:
        Traceback (most recent call last):
          File "app.py", line 10, in main
            result = process_data(None)
          File "utils.py", line 5, in process_data
            return data.upper()
        AttributeError: 'NoneType' object has no attribute 'upper'
        """
        locations = []
        
        # 1. 에러 타입 및 메시지 추출
        error_match = re.search(
            r'([A-Z][a-zA-Z]+Error|[A-Z][a-zA-Z]+Exception):\s*(.+?)(?:\n|$)', 
            error_text
        )
        
        error_type = error_match.group(1) if error_match else "UnknownError"
        error_message = error_match.group(2).strip() if error_match else "Unknown error"
        
        # 2. 스택 프레임 추출
        # 패턴: File "파일명", line 번호, in 함수명
        stack_pattern = r'File "([^"]+)",\s*line\s*(\d+),\s*in\s*(\S+)'
        matches = re.finditer(stack_pattern, error_text)
        
        for match in matches:
            filepath = match.group(1)
            line_num = int(match.group(2))
            func_name = match.group(3)
            
            # 코드 스니펫 추출 (다음 줄에 있는 실제 코드)
            code_snippet = ""
            lines = error_text.split('\n')
            for i, line in enumerate(lines):
                if match.group(0) in line and i + 1 < len(lines):
                    code_snippet = lines[i + 1].strip()
                    break
            
            locations.append(ErrorLocation(
                filepath=filepath,
                line_number=line_num,
                function_name=func_name,
                code_snippet=code_snippet,
                error_type=error_type,
                error_message=error_message,
                language="python"
            ))
        
        return locations
    
    def _parse_csharp_traceback(self, error_text: str) -> List[ErrorLocation]:
        """
        C# 트레이스백 파싱
        
        예시:
        System.NullReferenceException: Object reference not set to an instance of an object.
           at TIDAL.ViewModels.ExperimentViewModel.LoadData() in C:\\TIDAL\\ViewModels\\ExperimentViewModel.cs:line 45
           at TIDAL.Views.MainWindow.OnLoaded(Object sender, RoutedEventArgs e) in C:\\TIDAL\\Views\\MainWindow.xaml.cs:line 23
        """
        locations = []
        
        # 1. 에러 타입 및 메시지 추출
        error_match = re.search(
            r'(System\.[A-Z][a-zA-Z]+Exception|[A-Z][a-zA-Z]+Exception):\s*(.+?)(?:\n|$)',
            error_text
        )
        
        if error_match:
            error_type = error_match.group(1).split('.')[-1]  # System.NullReferenceException -> NullReferenceException
            error_message = error_match.group(2).strip()
        else:
            error_type = "UnknownException"
            error_message = "Unknown C# error"
        
        # 2. 스택 프레임 추출
        # 패턴: at Namespace.Class.Method() in 파일경로:line 번호
        stack_pattern = r'at\s+([^\s]+)\s+in\s+([^:]+):line\s+(\d+)'
        matches = re.finditer(stack_pattern, error_text)
        
        for match in matches:
            full_method = match.group(1)  # TIDAL.ViewModels.ExperimentViewModel.LoadData()
            filepath = match.group(2).strip()
            line_num = int(match.group(3))
            
            # 메서드명 추출 (마지막 점 이후)
            func_name = full_method.split('.')[-1].replace('()', '')
            
            # 파일 경로 정규화 (Windows 경로 처리)
            filepath = filepath.replace('\\\\', '/')
            
            locations.append(ErrorLocation(
                filepath=filepath,
                line_number=line_num,
                function_name=func_name,
                code_snippet="",  # C#는 트레이스백에 코드 스니펫이 없음
                error_type=error_type,
                error_message=error_message,
                language="csharp"
            ))
        
        return locations


class ErrorDiagnosticEngine:
    """
    에러 자동 진단 엔진 (Multi-language)
    """
    
    def __init__(
        self,
        search_engine: SmartSearchEngine,
        llm_client: LocalLLM,
        repo_root: Path = None
    ):
        self.search = search_engine
        self.llm = llm_client
        self.repo_root = repo_root or Path.cwd()
        self.parser = ErrorTracebackParser()
    
    def diagnose_error(self, error_text: str) -> ErrorDiagnostic:
        """
        에러 진단 메인 로직
        
        1. 트레이스백 파싱
        2. 에러 위치 코드 검색
        3. 호출 체인 추적 (그래프)
        4. LLM 분석
        """
        # Step 1: 트레이스백 파싱
        locations = self.parser.parse_traceback(error_text)
        
        if not locations:
            raise ValueError("트레이스백을 파싱할 수 없습니다.")
        
        error_location = locations[-1]  # 실제 에러 발생 지점 (마지막)
        
        # Step 2: 에러 발생 위치 코드 검색
        search_query = f"{error_location.filepath} {error_location.function_name}"
        related_code = self.search.search(search_query, top_k=10)
        
        # Step 3: 호출 체인 구성 (역순으로)
        call_chain = locations  # 이미 호출 순서대로 정렬됨
        
        # Step 4: 근본 원인 추적 (첫 번째 프레임)
        root_cause = locations[0] if len(locations) > 1 else None
        
        # Step 5: LLM에게 진단 요청
        diagnosis = self._generate_diagnosis(
            error_location, 
            related_code, 
            call_chain,
            error_text
        )
        
        # Step 6: 수정 제안 생성
        fix_suggestion = self._generate_fix_suggestion(
            error_location,
            related_code,
            diagnosis
        )
        
        return ErrorDiagnostic(
            error_location=error_location,
            root_cause=root_cause,
            diagnosis=diagnosis,
            call_chain=call_chain,
            related_code=related_code,
            fix_suggestion=fix_suggestion
        )
    
    def _generate_diagnosis(
        self,
        error_loc: ErrorLocation,
        related_code: List,
        call_chain: List[ErrorLocation],
        full_traceback: str
    ) -> str:
        """LLM을 사용한 에러 원인 분석"""
        
        # Context 구성
        context_parts = []
        
        # 1. 에러 위치 코드
        for item in related_code[:5]:
            chunk = item.get('chunk', {})
            context_parts.append(f"""
## {chunk.get('qualified_name', 'Unknown')}
**File**: {chunk.get('filepath', '')}:{chunk.get('start_line', '')}
**Language**: {chunk.get('language', 'unknown')}

```{chunk.get('language', '')}
{chunk.get('content', '')}
```
""")
        
        # 2. 호출 체인 시각화
        chain_visual = "\n".join([
            f"{'  ' * i}→ {loc.function_name} ({loc.filepath}:{loc.line_number})"
            for i, loc in enumerate(call_chain)
        ])
        
        # Prompt 구성
        system_prompt = f"""
당신은 {error_loc.language.upper()} 에러 진단 전문가입니다.
주어진 트레이스백과 코드를 분석하여 에러의 근본 원인을 찾으세요.

## 에러 정보
- **타입**: {error_loc.error_type}
- **메시지**: {error_loc.error_message}
- **언어**: {error_loc.language}
- **위치**: {error_loc.filepath}:{error_loc.line_number} in {error_loc.function_name}

## 호출 체인
{chain_visual}

## 관련 코드
{''.join(context_parts)}

## 전체 트레이스백
```
{full_traceback}
```

**분석 결과를 한국어로 작성하세요:**
1. 에러 발생 원인
2. 코드 흐름 분석
3. 왜 이 에러가 발생했는지 설명
"""
        
        return self.llm.generate_response(system_prompt, "위 에러를 분석해주세요.")
    
    def _generate_fix_suggestion(
        self,
        error_loc: ErrorLocation,
        related_code: List,
        diagnosis: str
    ) -> str:
        """수정 방법 제안"""
        
        chunk = related_code[0].get('chunk', {}) if related_code else {}
        
        system_prompt = f"""
당신은 {error_loc.language.upper()} 코드 수정 전문가입니다.

## 에러 정보
- **타입**: {error_loc.error_type}
- **위치**: {error_loc.filepath}:{error_loc.line_number}

## 문제 코드
```{chunk.get('language', '')}
{chunk.get('content', '')}
```

## 진단 결과
{diagnosis}

**수정 방법을 제안하세요 (한국어):**
1. 구체적인 코드 수정안
2. 예방 방법
"""
        
        return self.llm.generate_response(system_prompt, "이 에러를 어떻게 고칠 수 있나요?")
"""
에러 진단 API 엔드포인트
FastAPI로 실제 서비스 제공
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List
from pathlib import Path

from .error_diagnostic_engine import (
    ErrorDiagnosticEngine,
    ErrorLocation,
    ErrorDiagnostic
)
from .search import SmartSearchEngine
from .llm_client import LocalLLM
from .database import VectorStore
from .graph_store import GraphStore

# ===================================================================
# Request/Response 모델
# ===================================================================

class ErrorDiagnosticRequest(BaseModel):
    """에러 진단 요청"""
    error_text: str = Field(
        ..., 
        description="트레이스백 포함 전체 에러 메시지",
        example="""Traceback (most recent call last):
  File "app.py", line 10, in main
    process_data(None)
  File "utils.py", line 5, in process_data
    return data.upper()
AttributeError: 'NoneType' object has no attribute 'upper'"""
    )
    
    include_fix_suggestion: bool = Field(
        default=True,
        description="수정 제안 포함 여부"
    )
    
    search_depth: int = Field(
        default=5,
        ge=1,
        le=10,
        description="연관 코드 검색 깊이"
    )


class ErrorLocationResponse(BaseModel):
    """에러 위치 정보 응답"""
    filepath: str
    line_number: int
    function_name: str
    code_snippet: str
    error_type: str
    error_message: str


class ErrorDiagnosticResponse(BaseModel):
    """에러 진단 결과 응답"""
    success: bool
    error_location: ErrorLocationResponse
    root_cause: Optional[ErrorLocationResponse] = None
    diagnosis: str
    severity: str = Field(description="Level1/Level2/Level3")
    
    # 추가 정보
    call_chain: Optional[List[ErrorLocationResponse]] = None
    related_files: Optional[List[str]] = None
    fix_suggestion: Optional[str] = None


# ===================================================================
# 전역 인스턴스 (서버 시작 시 초기화)
# ===================================================================

_diagnostic_engine: Optional[ErrorDiagnosticEngine] = None


def initialize_diagnostic_engine(
    vector_store: VectorStore,
    graph_store: GraphStore,
    llm_client: LocalLLM,
    repo_root: Path = None
):
    """
    서버 시작 시 호출 (main.py에서)
    """
    global _diagnostic_engine
    
    search_engine = SmartSearchEngine(vector_store, graph_store)
    
    _diagnostic_engine = ErrorDiagnosticEngine(
        search_engine=search_engine,
        llm_client=llm_client,
        repo_root=repo_root or Path.cwd()
    )
    
    print("✅ Error Diagnostic Engine initialized")


def get_diagnostic_engine() -> ErrorDiagnosticEngine:
    """의존성 주입"""
    if _diagnostic_engine is None:
        raise RuntimeError("Diagnostic engine not initialized!")
    return _diagnostic_engine


# ===================================================================
# API 라우터
# ===================================================================

router = APIRouter(prefix="/api/diagnostic", tags=["Error Diagnosis"])


@router.post("/analyze-error", response_model=ErrorDiagnosticResponse)
def analyze_error(request: ErrorDiagnosticRequest):
    """
    🐛 에러 자동 진단 API
    
    **사용법**:
    1. 사용자가 에러 메시지 전체를 붙여넣기
    2. 자동으로 원인 코드 위치 특정
    3. LLM이 원인 분석 + 수정 제안
    
    **입력 예시**:
    ```json
    {
        "error_text": "Traceback (most recent call last):\\n  File ...",
        "include_fix_suggestion": true,
        "search_depth": 5
    }
    ```
    
    **출력**:
    - 에러 발생 위치 (파일:라인)
    - 근본 원인 분석
    - 수정 방법 제안
    - 관련 파일 목록
    """
    try:
        engine = get_diagnostic_engine()
        
        # 진단 실행
        result: ErrorDiagnostic = engine.diagnose_error(request.error_text)
        
        # 에러 심각도 분류
        severity = engine.parser.classify_error_severity(
            result.error_location.error_type
        )
        
        # 관련 파일 목록 추출
        related_files = []
        if result.call_chain:
            related_files = list(set(
                loc.filepath for loc in result.call_chain
            ))
        elif result.related_code:
            related_files = list(set(
                chunk.get('chunk', {}).get('filepath', '')
                for chunk in result.related_code
            ))
        
        # 응답 구성
        return ErrorDiagnosticResponse(
            success=True,
            error_location=ErrorLocationResponse(**result.error_location.__dict__),
            root_cause=(
                ErrorLocationResponse(**result.root_cause.__dict__)
                if result.root_cause else None
            ),
            diagnosis=result.diagnosis,
            severity=severity,
            call_chain=(
                [ErrorLocationResponse(**loc.__dict__) for loc in result.call_chain]
                if result.call_chain else None
            ),
            related_files=related_files if related_files else None,
            fix_suggestion=(
                result.fix_suggestion if request.include_fix_suggestion else None
            )
        )
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error diagnosis failed: {str(e)}"
        )


@router.post("/quick-check")
async def quick_error_check(error_text: str):
    """
    ⚡ 빠른 에러 체크 (LLM 없이 파싱만)
    
    트레이스백만 파싱하여 에러 위치를 즉시 반환
    현장에서 "어디서 났는지만 빨리 보고 싶을 때" 사용
    """
    try:
        engine = get_diagnostic_engine()
        
        # 파싱만 수행 (LLM 호출 없음)
        locations = engine.parser.parse_traceback(error_text)
        
        if not locations:
            return {
                "success": False,
                "message": "트레이스백을 찾을 수 없습니다",
                "locations": []
            }
        
        return {
            "success": True,
            "error_location": locations[-1].__dict__,
            "call_stack": [loc.__dict__ for loc in locations],
            "total_depth": len(locations)
        }
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Quick check failed: {str(e)}"
        )


@router.post("/search-similar-errors")
async def search_similar_errors(
    error_text: str,
    top_k: int = 3
):
    """
    🔍 유사한 과거 에러 검색
    
    같은 타입의 에러가 과거에 발생했는지 찾기
    (향후 에러 로그 DB 구축 시 활용)
    """
    try:
        engine = get_diagnostic_engine()
        
        # 에러 파싱
        locations = engine.parser.parse_traceback(error_text)
        
        if not locations:
            return {"message": "No error found in text"}
        
        error_loc = locations[-1]
        
        # RAG 검색으로 유사 코드 찾기
        search_query = f"{error_loc.error_type} {error_loc.function_name}"
        
        results = engine.search.search(search_query, top_k=top_k)
        
        return {
            "error_type": error_loc.error_type,
            "search_query": search_query,
            "similar_code_locations": [
                {
                    "qualified_name": r.get('chunk', {}).get('qualified_name'),
                    "filepath": r.get('chunk', {}).get('filepath'),
                    "line": r.get('chunk', {}).get('start_line')
                }
                for r in results
            ]
        }
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Search failed: {str(e)}"
        )


# ===================================================================
# 배치 진단 (여러 에러 한 번에)
# ===================================================================

class BatchDiagnosticRequest(BaseModel):
    """여러 에러를 한 번에 진단"""
    errors: List[str] = Field(
        ...,
        description="에러 메시지 리스트",
        max_items=10  # 과부하 방지
    )


@router.post("/batch-analyze")
async def batch_analyze_errors(request: BatchDiagnosticRequest):
    """
    📦 배치 에러 진단
    
    여러 에러를 한 번에 분석 (예: 로그 파일 일괄 처리)
    최대 10개까지 제한
    """
    try:
        engine = get_diagnostic_engine()
        
        results = []
        for idx, error_text in enumerate(request.errors):
            try:
                diagnostic = engine.diagnose_error(error_text)
                results.append({
                    "index": idx,
                    "success": True,
                    "location": diagnostic.error_location.__dict__,
                    "diagnosis_summary": diagnostic.diagnosis[:200] + "..."
                })
            except Exception as e:
                results.append({
                    "index": idx,
                    "success": False,
                    "error": str(e)
                })
        
        return {
            "total": len(request.errors),
            "processed": len(results),
            "results": results
        }
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Batch analysis failed: {str(e)}"
        )


# ===================================================================
# 통계/모니터링 엔드포인트
# ===================================================================

@router.get("/stats")
async def get_diagnostic_stats():
    """
    📊 진단 시스템 통계
    
    - 지원 에러 타입
    - 현재 인덱스 상태
    """
    try:
        engine = get_diagnostic_engine()
        
        return {
            "supported_error_types": list(engine.parser.COMMON_ERRORS.keys()),
            "bm25_index_size": (
                len(engine.search.all_chunks) 
                if engine.search.bm25 else 0
            ),
            "graph_store_ready": engine.search.graph is not None,
            "status": "operational"
        }
        
    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }
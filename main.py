from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.table import Table
from rich import box
from rich.progress import Progress, SpinnerColumn, TextColumn
import re
import json

# 모듈 임포트 (경로는 프로젝트 구조에 맞게 조정 필요)
from src.database import VectorStore
from src.llm import LocalLLM
from src.graph_store import GraphStore
from src.search_engine import SmartSearchEngine
from src import prompts 

console = Console()


# ===================================================================
# 🔍 Query Analysis (질문 유형 자동 감지)
# ===================================================================

def detect_query_type(query: str, llm) -> dict:
    """
    LLM을 사용하여 질문의 의도를 동적으로 파악합니다. (규칙 기반 X -> AI 기반 O)
    """
    
    # 2. 분류를 위한 프롬프트 작성
    router_prompt = """
    당신은 사용자의 질문을 분석하여 JSON 형식으로 분류하는 'Router' AI입니다.
    사용자의 질문을 분석해서 아래 4가지 유형 중 하나로 분류하고 중요 정보를 추출하세요.

    [분류 유형]
    1. bug: 에러 수정, 오류 찾기, 디버깅 요청 (예: "이거 왜 안돼?", "Traceback 에러")
    2. flow: 코드의 실행 흐름, 동작 원리, 순서 설명 요청 (예: "이게 어떻게 돌아가는거야?")
    3. search: 특정 기능/파일 찾기, 존재 여부 확인 (예: "로그인 기능 어디있어?", "User 클래스 찾아줘")
    4. general: 그 외 일반적인 코딩 질문

    [출력 형식 (JSON)]
    {
        "type": "유형(bug, flow, search, general 중 택1)",
        "filenames": ["언급된_파일명.py"],
        "target_name": "언급된_함수_또는_클래스명(없으면 null)",
        "keywords": ["검색용_핵심키워드1", "키워드2"]
    }
    
    반드시 JSON 형식만 출력하세요. 설명은 필요 없습니다.
    """

    # 3. LLM에게 판단 요청
    try:
        # LLM에게 물어보기
        response_text = llm.generate_response(router_prompt, query)
        
        # 4. JSON 파싱 (LLM이 가끔 마크다운 ```json ... ``` 을 붙일 수 있어서 제거)
        clean_json = response_text.replace("```json", "").replace("```", "").strip()
        result = json.loads(clean_json)
        
        # 필수 필드 안전장치 (혹시 LLM이 빼먹었을 경우 대비)
        if 'filenames' not in result: result['filenames'] = []
        if 'target_name' not in result: result['target_name'] = None
        
        # 호환성을 위해 기존 코드에서 쓰던 필드 추가
        result['filename'] = result['filenames'][0] if result['filenames'] else None
        result['has_traceback'] = 'traceback' in query.lower() # 이건 확실하니까 룰 유지해도 됨
        
        return result

    except Exception as e:
        # LLM이 JSON을 잘못 줬거나 에러가 나면 안전하게 기본값 리턴
        console.print(f"[red]⚠️ 라우팅 실패 (기본값 사용): {e}[/red]")
        return {
            "type": "general",
            "filenames": [],
            "filename": None,
            "target_name": None,
            "has_traceback": False
        }
def build_optimized_prompt(query: str, results: list, query_info: dict) -> str:
    """
    질문 유형에 따라 최적화된 프롬프트 생성
    """
    # Context 생성
    if results and isinstance(results[0], dict) and 'filepath' in results[0] and 'chunk' not in results[0]:
        context_str = prompts.build_file_context(results)
    else:
        context_str = prompts.build_smart_search_context(results)
    
    # 질문 유형별 프롬프트 선택
    qtype = query_info['type']
    
    if qtype == 'existence':
        return prompts.get_existence_check_prompt(
            query, context_str, query_info['target_name'] or "unknown"
        )
    elif qtype == 'flow':
        return prompts.get_flow_analysis_prompt(query, context_str)
    elif qtype == 'bug':
        return prompts.get_bug_analysis_prompt(query, context_str)
    elif qtype == 'file_summary':
        return prompts.get_file_summary_prompt(
            query, context_str, query_info['filename']
        )
    elif qtype == 'error':
        traceback_match = re.search(r'(Traceback.*?)(?:\n\n|\Z)', query, re.DOTALL)
        traceback = traceback_match.group(1) if traceback_match else query
        return prompts.get_error_diagnostic_prompt(query, context_str, traceback)
    else:
        return prompts.get_general_prompt(query, context_str)


# ===================================================================
# 📊 Evidence Panel (검색 결과 시각화)
# ===================================================================

def print_evidence_panel(results: list, query_info: dict):
    table = Table(
        title=f"🔍 Analysis Context [{query_info['type'].upper()}]", 
        box=box.ROUNDED, 
        show_lines=True
    )

    table.add_column("File", style="cyan", no_wrap=True, width=30)
    table.add_column("Type", style="magenta", width=10)
    table.add_column("Function/Class", style="green", width=25)
    table.add_column("Line", justify="right", style="yellow", width=10)

    for item in results[:15]:  # 너무 길어지지 않게 15개로 제한
        if isinstance(item, dict) and 'chunk' in item:
            chunk = item['chunk']
            table.add_row(
                chunk.get('filepath', 'Unknown')[-30:], 
                chunk.get('type', 'code'),
                chunk.get('name', 'Unknown'),
                str(chunk.get('start_line', '?'))
            )
        elif isinstance(item, dict) and 'filepath' in item:
            table.add_row(
                item['filepath'][-30:],
                item.get('type', 'file'),
                item.get('name', 'Entire File'),
                str(item.get('start_line', '-'))
            )

    console.print(table)
    console.print(f"[dim]Total: {len(results)} candidates[/dim]\n")


# ===================================================================
# 🎯 Main Application
# ===================================================================

def main():
    console.print(Panel.fit(
        "[bold blue]🤖 Production-Ready Code RAG (AI Router)[/bold blue]\n"
    ))
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    ) as progress:
        task = progress.add_task("[cyan]Initializing system...", total=None)
        
        db = VectorStore()
        llm = LocalLLM()
        graph_store = GraphStore()
        engine = SmartSearchEngine(db, graph_store)
        
        progress.update(task, completed=True)
    
    console.print("[green]✅ System ready[/green]\n")


    try:
        while True:
            query = console.input("\n[bold green]질문 (exit): [/bold green]")
            if query.lower() in ['exit', 'quit', 'q']:
                break

            # ===================================================================
            # Step 1: 질문 유형 자동 감지 (LLM 사용)
            # ===================================================================
            # [수정] 이제 llm 객체를 함께 넘겨줍니다.
            with console.status("[bold blue]🤔 Intent Classification...[/bold blue]"):
                query_info = detect_query_type(query, llm)
                
            console.print(f"[dim]🔎 Detected: {query_info['type'].upper()} (Keywords: {query_info.get('keywords', [])})[/dim]")

            # ===================================================================
            # Step 2: 검색 전략 선택
            # ===================================================================
            with console.status("[bold blue]🔍 Searching code...[/bold blue]"):
                
                # 1. 다중 파일 검색
                if len(query_info['filenames']) > 1:
                    console.print(f"[cyan]📁 Multi-target: {', '.join(query_info['filenames'])}[/cyan]")
                    results = []
                    for fname in query_info['filenames']:
                        f_res = db.search_by_filepath(fname, top_k=20)
                        payloads = [r.payload if hasattr(r, 'payload') else r for r in f_res]
                        results.extend(payloads)

                # 2. 단일 파일 검색
                elif query_info['filename']:
                    console.print(f"[cyan]📁 Target file: {query_info['filename']}[/cyan]")
                    all_results = db.search_by_filepath(query_info['filename'], top_k=1000)
                    
                    if not all_results:
                        console.print("[red]❌ 파일을 찾을 수 없습니다.[/red]")
                        continue
                    
                    results = [r.payload if hasattr(r, 'payload') else r for r in all_results]
                    if query_info['target_name']:
                        target_chunks = [r for r in results if query_info['target_name'] in r.get('name', '')]
                        other_chunks = [r for r in results if query_info['target_name'] not in r.get('name', '')]
                        results = target_chunks + other_chunks
                    results = results[:50]
                    
                # 3. 에러 트레이스백 처리
                elif query_info['has_traceback']:
                    traceback_files = re.findall(r'File "([^"]+)"', query)
                    if traceback_files:
                        console.print(f"[cyan]🚨 Error in files: {', '.join(set(traceback_files))}[/cyan]")
                        results = []
                        for fname in set(traceback_files):
                            file_results = db.search_by_filepath(fname, top_k=50)
                            results.extend([r.payload if hasattr(r, 'payload') else r for r in file_results])
                    else:
                        results = engine.search(query, top_k=5)
                        
                # 4. 일반 Smart Search
                else:
                    # [추가] LLM이 추출한 키워드가 있다면 검색에 활용 (없으면 원본 쿼리)
                    search_query = " ".join(query_info.get('keywords', [])) if query_info.get('keywords') else query
                    # 너무 짧으면 그냥 원본 쿼리 사용
                    if len(search_query) < 5: search_query = query
                    results = engine.search(search_query, top_k=5)
                # [2] ✨ 여기부터 새로 추가되는 "Graph 확장" 로직입니다 ✨
                # (1차 검색이 끝난 후, results를 가지고 2차 탐색을 합니다)
                # ---------------------------------------------------------------
                if results:
                    # console.print(f"[dim]🕸️ Expanding context via Knowledge Graph...[/dim]") # 너무 시끄러우면 주석 처리
                    expanded_results = []
                    existing_names = set()
                    
                    # 이미 찾은 1차 결과들의 이름을 등록 (중복 방지용)
                    for r in results:
                        if isinstance(r, dict) and 'chunk' in r:
                            existing_names.add(r['chunk'].get('qualified_name'))
                        elif isinstance(r, dict):
                            existing_names.add(r.get('qualified_name'))
                    
                    # 상위 5개 결과에 대해서만 "이 함수가 호출하는 친구들"을 찾아봄
                    for r in results[:5]: 
                        current_qn = None
                        if isinstance(r, dict) and 'chunk' in r:
                            current_qn = r['chunk'].get('qualified_name')
                        
                        if current_qn:
                            # 그래프 DB에 물어봄: "얘가 누구 호출해?"
                            callee_names = graph_store.get_callees(current_qn)
                            
                            for callee in callee_names:
                                # 처음 보는 함수라면? -> 벡터 DB에서 코드를 가져옴!
                                if callee not in existing_names:
                                    # 주의: search 메서드는 텍스트 검색이므로 정확도를 위해 qualified_name 필터링 필요
                                    # 여기서는 간단히 키워드 검색 후 이름 비교
                                    callee_hits = db.search(callee, top_k=3)
                                    for hit in callee_hits:
                                        payload = hit.payload if hasattr(hit, 'payload') else hit
                                        
                                        # 이름이 정확히 일치하는지 확인 (검색 정확도 보정)
                                        hit_qn = payload.get('qualified_name') or payload.get('chunk', {}).get('qualified_name')
                                        
                                        if hit_qn == callee:
                                            expanded_results.append(payload)
                                            existing_names.add(callee)
                                            # console.print(f"[dim cyan]  └─ Link found: {callee.split('.')[-1]}[/dim cyan]")
                                            break # 찾았으면 다음 callee로

                    # 확장된 결과가 있으면 원래 결과 뒤에 붙임
                    if expanded_results:
                        console.print(f"[dim cyan]🕸️ Graph Expanded: +{len(expanded_results)} related functions[/dim cyan]")
                        results.extend(expanded_results)

                # ---------------------------------------------------------------

                
                if not results:
                    console.print("[red]❌ 관련 코드를 찾을 수 없습니다.[/red]")
                    continue

            # ===================================================================
            # Step 3: 검색 결과 시각화
            # ===================================================================
            console.print()
            print_evidence_panel(results, query_info)

            # ===================================================================
            # Step 4: LLM 분석
            # ===================================================================
            with console.status("[bold magenta]🧠 Analyzing code...[/bold magenta]"):
                prompt = build_optimized_prompt(query, results, query_info)
                answer = llm.generate_response(prompt, query)

            # ===================================================================
            # Step 5: 답변 출력
            # ===================================================================
            console.print(Panel(
                Markdown(answer), 
                title=f"Answer [{query_info['type'].upper()}]",
                border_style="green"
            ))

            # ===================================================================
            # Step 6: 참조 파일 목록 출력
            # ===================================================================
            referenced_files = set()
            for r in results:
                if isinstance(r, dict) and 'chunk' in r:
                    referenced_files.add(r['chunk']['filepath'])
                elif isinstance(r, dict) and 'filepath' in r:
                    referenced_files.add(r['filepath'])

            console.print(f"\n[dim]📚 Referenced Files ({len(referenced_files)}):[/dim]")
            for f in sorted(referenced_files):
                console.print(f"[dim]  └─ {f}[/dim]")

    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user[/yellow]")
    
    finally:
        graph_store.close()
        console.print("\n[dim]👋 Goodbye![/dim]")


if __name__ == "__main__":
    main()
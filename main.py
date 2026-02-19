from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.table import Table
from rich import box
from rich.progress import Progress, SpinnerColumn, TextColumn
import re
import json

from src.database import VectorStore
from src.llm import LocalLLM
from src.graph_store import GraphStore
from src.search_engine import SmartSearchEngine
from src import prompts 

console = Console()


# ===================================================================
# 🔍 Query Analysis (질문 유형 자동 감지 - Multi-language)
# ===================================================================

def detect_query_type(query: str, llm) -> dict:
    """
    LLM을 사용하여 질문의 의도를 동적으로 파악 (Python + C# + XAML)
    """
    
    router_prompt = """
    당신은 사용자의 질문을 분석하여 JSON 형식으로 분류하는 'Router' AI입니다.
    사용자의 질문을 분석해서 아래 분류 유형 중 하나로 분류하고 중요 정보를 추출하세요.

    [분류 유형]
    1. bug: 에러 수정, 오류 찾기, 디버깅 요청 (예: "이거 왜 안돼?", "NullReferenceException 에러")
    2. flow: 코드의 실행 흐름, 동작 원리, 순서 설명 요청 (예: "이게 어떻게 돌아가는거야?", "버튼 클릭하면 뭐가 실행돼?")
    3. search: 특정 기능/파일 찾기, 존재 여부 확인 (예: "로그인 기능 어디있어?", "User 클래스 찾아줘")
    4. mvvm: MVVM 패턴 관련 질문 (예: "ViewModel이랑 View 어떻게 연결돼?", "바인딩 확인해줘")
    5. general: 그 외 일반적인 코딩 질문

    [언어 감지]
    - Python: .py 파일, snake_case 함수명, import 구문
    - C#: .cs 파일, PascalCase 메서드명, namespace, using 구문
    - XAML: .xaml 파일, Binding, DataContext

    [출력 형식 (JSON)]
    {
        "type": "유형(bug, flow, search, mvvm, general 중 택1)",
        "filenames": ["언급된_파일명.py", "파일명.cs", "파일명.xaml"],
        "target_name": "언급된_함수_또는_클래스명(없으면 null)",
        "keywords": ["검색용_핵심키워드1", "키워드2"],
        "language": "주요_언어(python, csharp, xaml, mixed 중 택1)"
    }
    
    반드시 JSON 형식만 출력하세요. 설명은 필요 없습니다.
    """

    try:
        response_text = llm.generate_response(router_prompt, query)
        
        # JSON 파싱
        clean_json = response_text.replace("```json", "").replace("```", "").strip()
        result = json.loads(clean_json)
        
        # 필수 필드 안전장치
        if 'filenames' not in result: result['filenames'] = []
        if 'target_name' not in result: result['target_name'] = None
        if 'language' not in result: result['language'] = 'python'
        
        # 호환성 필드
        result['filename'] = result['filenames'][0] if result['filenames'] else None
        result['has_traceback'] = any(keyword in query.lower() for keyword in ['traceback', 'exception', 'error at'])
        
        return result

    except Exception as e:
        console.print(f"[red]⚠️ 라우팅 실패 (기본값 사용): {e}[/red]")
        
        # 간단한 룰 기반 fallback
        language = "python"
        if any(ext in query for ext in ['.cs', '.xaml', 'ViewModel', 'Binding']):
            language = "csharp"
        
        return {
            "type": "general",
            "filenames": [],
            "filename": None,
            "target_name": None,
            "has_traceback": False,
            "language": language
        }


def build_optimized_prompt(query: str, results: list, query_info: dict) -> str:
    """
    질문 유형에 따라 최적화된 프롬프트 생성 (언어별 처리)
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
    elif qtype == 'mvvm':
        return prompts.get_mvvm_analysis_prompt(query, context_str)
    elif qtype == 'file_summary':
        return prompts.get_file_summary_prompt(
            query, context_str, query_info['filename']
        )
    elif qtype == 'error':
        traceback_match = re.search(r'(Traceback.*?)(?:\n\n|\Z)', query, re.DOTALL)
        traceback = traceback_match.group(1) if traceback_match else query
        return prompts.get_error_diagnostic_prompt(
            query, context_str, traceback, 
            language=query_info.get('language', 'python')
        )
    else:
        return prompts.get_general_prompt(query, context_str)


# ===================================================================
# 📊 Evidence Panel (검색 결과 시각화 - 언어 표시)
# ===================================================================

def print_evidence_panel(results: list, query_info: dict):
    table = Table(
        title=f"🔍 Analysis Context [{query_info['type'].upper()}] - Lang: {query_info.get('language', 'unknown').upper()}", 
        box=box.ROUNDED, 
        show_lines=True
    )

    table.add_column("File", style="cyan", no_wrap=True, width=30)
    table.add_column("Lang", style="blue", width=6)
    table.add_column("Type", style="magenta", width=10)
    table.add_column("Function/Class", style="green", width=25)
    table.add_column("Line", justify="right", style="yellow", width=10)

    for item in results[:15]:
        if isinstance(item, dict) and 'chunk' in item:
            chunk = item['chunk']
            table.add_row(
                chunk.get('filepath', 'Unknown')[-30:],
                chunk.get('language', '?')[:5],
                chunk.get('type', 'code'),
                chunk.get('name', 'Unknown'),
                str(chunk.get('start_line', '?'))
            )
        elif isinstance(item, dict) and 'filepath' in item:
            table.add_row(
                item['filepath'][-30:],
                item.get('language', '?')[:5],
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
        "[bold blue]🤖 Multi-Language Code RAG[/bold blue]\n"
        "[dim]Supports: Python, C#, XAML[/dim]"
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

            # Step 1: 질문 유형 자동 감지
            with console.status("[bold blue]🤔 Intent Classification...[/bold blue]"):
                query_info = detect_query_type(query, llm)
                
            console.print(f"[dim]🔎 Detected: {query_info['type'].upper()} ({query_info.get('language', 'unknown')}) - Keywords: {query_info.get('keywords', [])}[/dim]")

            # Step 2: 검색 전략 선택
            with console.status("[bold blue]🔍 Searching code...[/bold blue]"):
                
                # 특정 함수/메서드명이 언급된 경우
                if query_info.get('target_name'):
                    console.print(f"[cyan]🎯 Target: {query_info['target_name']}[/cyan]")
                    if query_info.get('filename'):
                        console.print(f"[cyan]📁 In file: {query_info['filename']}[/cyan]")
                    
                    results = engine.search(query, top_k=10)
                
                # 다중 파일 검색
                elif len(query_info['filenames']) > 1:
                    console.print(f"[cyan]📁 Multi-target: {', '.join(query_info['filenames'])}[/cyan]")
                    results = []
                    for fname in query_info['filenames']:
                        f_res = db.search_by_filepath(fname, top_k=20)
                        payloads = [r.payload if hasattr(r, 'payload') else r for r in f_res]
                        results.extend(payloads)

                # 단일 파일 검색
                elif query_info['filename']:
                    console.print(f"[cyan]📁 Target file: {query_info['filename']}[/cyan]")
                    all_results = db.search_by_filepath(query_info['filename'], top_k=1000)
                    
                    if not all_results:
                        console.print("[red]❌ 파일을 찾을 수 없습니다.[/red]")
                        continue
                    
                    results = [r.payload if hasattr(r, 'payload') else r for r in all_results]
                    results = results[:50]
                    
                # 에러 트레이스백 처리
                elif query_info['has_traceback']:
                    # Python 트레이스백
                    traceback_files = re.findall(r'File "([^"]+)"', query)
                    # C# 트레이스백
                    csharp_files = re.findall(r'in ([^:]+\.cs):line', query)
                    
                    all_error_files = list(set(traceback_files + csharp_files))
                    
                    if all_error_files:
                        console.print(f"[cyan]🚨 Error in files: {', '.join(all_error_files)}[/cyan]")
                        results = []
                        for fname in all_error_files:
                            file_results = db.search_by_filepath(fname, top_k=50)
                            results.extend([r.payload if hasattr(r, 'payload') else r for r in file_results])
                    else:
                        results = engine.search(query, top_k=5)
                
                # 언어별 검색 (C# 전용 질문 등)
                elif query_info.get('language') in ['csharp', 'xaml']:
                    lang_map = {'csharp': 'c_sharp', 'xaml': 'xaml'}
                    search_lang = lang_map.get(query_info['language'], 'python')
                    console.print(f"[cyan]🔤 Language filter: {search_lang.upper()}[/cyan]")
                    results = engine.search_by_language(query, search_lang, top_k=10)
                        
                # 일반 Smart Search
                else:
                    search_query = " ".join(query_info.get('keywords', [])) if query_info.get('keywords') else query
                    if len(search_query) < 5: search_query = query
                    results = engine.search(search_query, top_k=5)
                
                # Graph 확장
                if results:
                    expanded_results = []
                    existing_names = set()
                    
                    for r in results:
                        if isinstance(r, dict) and 'chunk' in r:
                            existing_names.add(r['chunk'].get('qualified_name'))
                        elif isinstance(r, dict):
                            existing_names.add(r.get('qualified_name'))
                    
                    for r in results[:5]:
                        current_qn = None
                        if isinstance(r, dict) and 'chunk' in r:
                            current_qn = r['chunk'].get('qualified_name')
                        
                        if current_qn:
                            callee_names = graph_store.get_callees(current_qn)
                            
                            for callee in callee_names:
                                if callee not in existing_names:
                                    callee_hits = db.search(callee, top_k=3)
                                    for hit in callee_hits:
                                        payload = hit.payload if hasattr(hit, 'payload') else hit
                                        hit_qn = payload.get('qualified_name') or payload.get('chunk', {}).get('qualified_name')
                                        
                                        if hit_qn == callee:
                                            expanded_results.append(payload)
                                            existing_names.add(callee)
                                            break

                    if expanded_results:
                        console.print(f"[dim cyan]🕸️ Graph Expanded: +{len(expanded_results)} related[/dim cyan]")
                        results.extend(expanded_results)

                if not results:
                    console.print("[red]❌ 관련 코드를 찾을 수 없습니다.[/red]")
                    continue

            # Step 3: 검색 결과 시각화
            console.print()
            print_evidence_panel(results, query_info)

            # Step 4: LLM 분석
            with console.status("[bold magenta]🧠 Analyzing code...[/bold magenta]"):
                prompt = build_optimized_prompt(query, results, query_info)
                answer = llm.generate_response(prompt, query)

            # Step 5: 답변 출력
            console.print(Panel(
                Markdown(answer), 
                title=f"Answer [{query_info['type'].upper()}] - {query_info.get('language', 'unknown').upper()}",
                border_style="green"
            ))

            # Step 6: 참조 파일 목록 출력
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
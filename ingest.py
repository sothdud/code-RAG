import os
import sys
import json
import hashlib
from pathlib import Path
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import track

# Qdrant Filter 관련 임포트
from qdrant_client.models import Filter, FieldCondition, MatchText, MatchAny

from src.parser import ASTParser
from src.graph_builder import GraphBuilder
from src.database import VectorStore
from src.graph_store import GraphStore
from src.path_utils import should_skip_path

load_dotenv()
console = Console()

STATE_FILE = ".ingest_state.json"
REPO_ROOT = Path(os.getenv("SOURCE_CODE_PATH", os.getcwd())) 

def calculate_file_hash(filepath: Path) -> str:
    """파일 내용의 MD5 해시를 계산합니다."""
    hash_md5 = hashlib.md5()
    try:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    except FileNotFoundError:
        return ""

def load_state() -> dict:
    """이전 인덱싱 상태를 로드합니다."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_state(state: dict):
    """현재 인덱싱 상태를 저장합니다."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=4)

def main():
    # --full 옵션 확인
    FULL_RESET = "--full" in sys.argv

    if FULL_RESET:
        console.print("\n[bold red]🔄 FULL RESET MODE ENABLED[/bold red]")
        console.print("  → Will recreate entire database\n")

    # DB 및 파서 초기화
    db = VectorStore()
    graph_store = GraphStore()
    
    # 파서 초기화 (ASTParser 내부에 UIParser가 포함되어 있음)
    parser = ASTParser() 
    graph_builder = GraphBuilder()

    # Full Reset 시 DB 초기화
    if FULL_RESET:
        db.recreate_collection()
        graph_store.clear_all_data()
        previous_state = {}
    else:
        previous_state = load_state()

    current_state = {}
    
    # 소스 코드 파일 수집 (.py 및 .ui)
    all_files = []
    # glob 패턴을 리스트로 관리하여 확장성 확보
    patterns = ["**/*.py", "**/*.ui"]
    
    for ext in patterns:
        all_files.extend(list(REPO_ROOT.glob(ext)))

    files_to_process = []
    
    console.print("\n[bold blue]🔍 Phase 1: Detecting Changes...[/bold blue]")
    
    for filepath in all_files:
        # [수정] should_skip_path에 REPO_ROOT 인자 전달 (TypeError 방지)
        if should_skip_path(filepath, REPO_ROOT):
            continue

        str_path = str(filepath)
        current_hash = calculate_file_hash(filepath)
        current_state[str_path] = current_hash

        # 변경 감지 로직
        if FULL_RESET:
            files_to_process.append(filepath)
            # UI 파일인지 확인하여 로그 색상 다르게 표시
            if filepath.suffix == '.ui':
                console.print(f"  → Queue: [magenta]{filepath.name}[/magenta] (UI)")
            else:
                console.print(f"  → Queue: [cyan]{filepath.name}[/cyan]")
        else:
            prev_hash = previous_state.get(str_path)
            if prev_hash != current_hash:
                files_to_process.append(filepath)
                console.print(f"  → [green]Modified:[/green] {filepath.name}")

    if not files_to_process:
        console.print("\n[green]✨ No changes detected. System is up to date![/green]")
        save_state(current_state)
        return

    # ---------------------------------------------------------
    # 4. 파싱 및 청크 생성
    # ---------------------------------------------------------
    console.print("\n[bold blue]🧠 Phase 2: Parsing & Building Structure...[/bold blue]")
    
    all_chunks_for_graph = []
    chunks_to_upsert = []

    # 트랙킹바와 함께 처리
    for filepath in track(files_to_process, description="Parsing AST & UI..."):
        try:
            # [수정] parse_file 호출 시 REPO_ROOT 전달 (FQN 생성용)
            # ASTParser 내부에서 .ui 확장자를 확인하여 UIParser로 분기함
            chunks = parser.parse_file(str(filepath), str(REPO_ROOT))
            
            if not chunks:
                continue

            for chunk in chunks:
                # 그래프 빌더에 추가 (전체 구조 파악용)
                graph_builder.add_chunk(chunk)
                all_chunks_for_graph.append(chunk)
                
                # 벡터 DB 업서트 리스트에 추가
                chunks_to_upsert.append(chunk)

        except Exception as e:
            console.print(f"  [red]Error parsing {filepath.name}: {e}[/red]")
            continue # 에러 발생해도 멈추지 않고 다음 파일 진행

    # ---------------------------------------------------------
    # 5. Vector DB 저장
    # ---------------------------------------------------------
    if chunks_to_upsert:
        console.print(f"\n[bold green]💾 Phase 3: Updating Vector DB ({len(chunks_to_upsert)} chunks)...[/bold green]")
        # database.py에서 메모리 관리(batch 처리)가 수행됨
        db.upsert_chunks(chunks_to_upsert)
    else:
        console.print("\n[dim]💾 Phase 3: Vector DB skipped (No new content)[/dim]")

    # ---------------------------------------------------------
    # 6. Graph DB 저장 (Full Sync)
    # ---------------------------------------------------------
    console.print(f"\n[bold magenta]🕸️ Phase 4: Syncing Graph DB...[/bold magenta]")
    
    # 전체 파일 간의 호출 관계 계산
    call_graph = graph_builder.build_call_graph()
    
    # Memgraph 저장
    graph_store.save_graph_data(all_chunks_for_graph, call_graph.edges)

    # ---------------------------------------------------------
    # 7. 상태 저장
    # ---------------------------------------------------------
    save_state(current_state)
    
    if FULL_RESET:
        console.print("\n[bold blue]✨ Full Reset Complete![/bold blue]")
    else:
        console.print("\n[bold blue]✨ Incremental Ingest Complete![/bold blue]")

if __name__ == "__main__":
    main()
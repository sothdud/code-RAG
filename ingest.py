import os
import sys
import json
import hashlib
from pathlib import Path
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import track

# Qdrant Filter 관련 임포트 (삭제용)
from qdrant_client.models import Filter, FieldCondition, MatchText

from src.parser import ASTParser
from src.graph_builder import GraphBuilder
from src.database import VectorStore
from src.graph_store import GraphStore
from src.path_utils import should_skip_path

load_dotenv()
console = Console()

STATE_FILE = ".ingest_state.json"

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
        json.dump(state, f, indent=2)

def main():
    # ============================================
    # 옵션 파싱
    # ============================================
    FULL_RESET = "--full" in sys.argv or "--reset" in sys.argv
    
    if FULL_RESET:
        console.print("\n[bold red]🔄 FULL RESET MODE ENABLED[/bold red]")
        console.print("  → Will recreate entire database\n")
    
    # 1. 설정
    TARGET_DIR = Path(os.getenv("SOURCE_CODE_PATH", "./data"))
    if not TARGET_DIR.exists():
        console.print(f"[red]❌ 경로 없음: {TARGET_DIR}[/red]")
        return

    parser = ASTParser()
    graph_builder = GraphBuilder()
    db = VectorStore()
    graph_store = GraphStore()
    
    # ============================================
    # FULL RESET: 전체 DB 초기화
    # ============================================
    if FULL_RESET:
        console.print("[bold yellow]🗑️ Dropping existing collections...[/bold yellow]")
        db.recreate_collection()
        graph_store.clear_all_data()
        
        # 상태 파일도 삭제
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        
        console.print("[green]✓ Database reset complete[/green]\n")
        previous_state = {}
    else:
        previous_state = load_state()
    
    # 2. 상태 로드 및 변경 감지
    console.print(f"[bold yellow]🔍 Phase 1: Detecting Changes...[/bold yellow]")
    
    current_state = {}
    
    all_files = []
    # 파일 탐색
    for root, dirs, files in os.walk(TARGET_DIR):
        root_path = Path(root)
        
        # 디렉토리 제외 처리 (재귀적 탐색 효율화)
        dirs[:] = [d for d in dirs if not should_skip_path(root_path / d, TARGET_DIR)]
        
        for file in files:
            if not file.endswith('.py'): continue
            file_path = root_path / file
            if should_skip_path(file_path, TARGET_DIR): continue
            
            all_files.append(file_path)

    # 변경 사항 분류
    files_to_embed = []      # 임베딩 새로 해야 할 파일 (신규/수정)
    files_to_delete = []     # DB에서 지워야 할 파일 (수정/삭제)
    unchanged_files = []     # 변경 없는 파일
    
    # 2-1. 신규 및 수정 파일 감지
    for file_path in all_files:
        str_path = str(file_path)
        current_hash = calculate_file_hash(file_path)
        current_state[str_path] = current_hash
        
        prev_hash = previous_state.get(str_path)
        
        # FULL RESET 모드면 모든 파일 재처리
        if FULL_RESET:
            files_to_embed.append(file_path)
            console.print(f"  [cyan]→ Queue:[/cyan] {file_path.name}")
        elif prev_hash != current_hash:
            if prev_hash is None:
                console.print(f"  [green]+ New:[/green] {file_path.name}")
            else:
                console.print(f"  [yellow]* Modified:[/yellow] {file_path.name}")
                files_to_delete.append(str_path) # 수정된 경우 기존 거 삭제 필요
            files_to_embed.append(file_path)
        else:
            unchanged_files.append(file_path)

    # 2-2. 삭제된 파일 감지 (FULL RESET 모드에선 불필요)
    if not FULL_RESET:
        for old_path in previous_state:
            if old_path not in current_state:
                console.print(f"  [red]- Deleted:[/red] {old_path}")
                files_to_delete.append(old_path)

    # 변경사항이 없으면 조기 종료
    if not FULL_RESET and not files_to_embed and not files_to_delete:
        console.print("\n[bold green]✅ No changes detected. System is up to date.[/bold green]")
        return

    # ---------------------------------------------------------
    # 3. 데이터베이스 정리 (Incremental Only)
    # ---------------------------------------------------------
    if not FULL_RESET and files_to_delete:
        console.print(f"\n[bold red]🗑️ Removing obsolete chunks ({len(files_to_delete)} files)...[/bold red]")
        # Qdrant에서 파일 경로 기준으로 삭제
        for file_path in files_to_delete:
            try:
                db.client.delete(
                    collection_name=db.collection,
                    points_selector=Filter(
                        must=[
                            FieldCondition(
                                key="filepath",
                                match=MatchText(text=file_path)
                            )
                        ]
                    )
                )
            except Exception as e:
                console.print(f"  ⚠️ Failed to delete {file_path}: {e}")

    # ---------------------------------------------------------
    # 4. 파싱 및 그래프 빌드
    # ---------------------------------------------------------
    console.print(f"\n[bold cyan]🧠 Phase 2: Parsing & Building Structure...[/bold cyan]")
    
    all_chunks_for_graph = []       # 그래프용 (전체)
    chunks_to_upsert = []           # 벡터 저장용 (변경분만)
    
    # FULL RESET: 모든 파일 처리
    # Incremental: 변경된 파일 + 그래프용 전체 파일
    files_to_parse = all_files if FULL_RESET else all_files
    
    for file_path in track(files_to_parse, description="Parsing AST..."):
        try:
            chunks = parser.parse_file(str(file_path))
            
            # 그래프 빌더엔 무조건 추가 (전체 문맥 형성)
            for chunk in chunks:
                graph_builder.add_chunk(chunk)
                all_chunks_for_graph.append(chunk)
            
            # 벡터 DB엔 변경된 파일만 추가 (또는 FULL RESET 시 전부)
            if FULL_RESET or file_path in files_to_embed:
                chunks_to_upsert.extend(chunks)
                
        except Exception as e:
            console.print(f"  [red]Error parsing {file_path.name}: {e}[/red]")

    # ---------------------------------------------------------
    # 5. Vector DB 저장
    # ---------------------------------------------------------
    if chunks_to_upsert:
        console.print(f"\n[bold green]💾 Phase 3: Updating Vector DB ({len(chunks_to_upsert)} chunks)...[/bold green]")
        # ⭐ calls 필터링은 이미 parser.py와 database.py에서 처리됨
        db.upsert_chunks(chunks_to_upsert)
    else:
        console.print("\n[dim]💾 Phase 3: Vector DB skipped (No new content)[/dim]")

    # ---------------------------------------------------------
    # 6. Graph DB 저장 (Full Sync)
    # ---------------------------------------------------------
    console.print(f"\n[bold magenta]🕸️ Phase 4: Syncing Graph DB...[/bold magenta]")
    
    call_graph = graph_builder.build_call_graph()
    
    # Memgraph 초기화 후 전체 노드/엣지 다시 쓰기
    if FULL_RESET:
        graph_store.clear_all_data()
    graph_store.save_graph_data(all_chunks_for_graph, call_graph.edges)

    # ---------------------------------------------------------
    # 7. 상태 저장
    # ---------------------------------------------------------
    save_state(current_state)
    
    if FULL_RESET:
        console.print("\n[bold blue]✨ Full Reset Complete![/bold blue]")
    else:
        console.print("\n[bold blue]✨ Incremental Ingest Complete![/bold blue]")
    
    console.print(f"  • Total files: {len(all_files)}")
    console.print(f"  • Processed: {len(files_to_embed)}")
    console.print(f"  • Unchanged: {len(unchanged_files)}")

if __name__ == "__main__":
    main()
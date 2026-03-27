import os
import html
import psycopg2
import psycopg2.extras
from pathlib import Path
from collections import defaultdict
from dotenv import load_dotenv


def _get_connection():
    dsn = os.getenv(
        "POSTGRES_DSN",
        f"host={os.getenv('POSTGRES_HOST', 'localhost')} "
        f"port={os.getenv('POSTGRES_PORT', '5433')} "
        f"dbname={os.getenv('POSTGRES_DB', 'coderag')} "
        f"user={os.getenv('POSTGRES_USER', 'coderag')} "
        f"password={os.getenv('POSTGRES_PASSWORD', 'coderag')}",
    )
    return psycopg2.connect(dsn)


def escape_mdx(text: str) -> str:
    if not text:
        return text
    return (
        text
        .replace("&", "&amp;")
        .replace("{", "&#123;")
        .replace("}", "&#125;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def escape_code_block(code: str) -> str:
    
    if not code:
        return code
    return code.replace("{", "\u007b").replace("}", "\u007d")


def _get_relative_doc_path(filepath: str, source_root: Path) -> Path:
   
    try:
        abs_fp   = Path(filepath).resolve()
        abs_root = source_root.resolve()
        rel      = abs_fp.relative_to(abs_root.parent)
        return rel.parent
    except ValueError:
        # fallback 직계 부모 이름만 사용
        return Path(Path(filepath).parent.name)


def generate_docs():
    # 1. .env 로드
    load_dotenv()

    # SOURCE_CODE_PATH 읽기 (폴더 계층 계산 기준)
    source_root = Path(os.getenv("SOURCE_CODE_PATH", "./")).resolve()
    print(f"📂 SOURCE_CODE_PATH: {source_root}")

    print("🔄 PostgreSQL 연결 중...")
    try:
        conn = _get_connection()
    except Exception as e:
        print(f"❌ PostgreSQL 연결 실패: {e}")
        return

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT qualified_name, name, type, content, summary,
                       filepath, start_line, language,
                       module_path, docstring, calls, called_by
                FROM code_chunks
                ORDER BY filepath, start_line
                """
            )
            all_chunks = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"❌ 데이터 조회 실패: {e}")
        conn.close()
        return
    finally:
        conn.close()

    print(f"✅ 총 {len(all_chunks)}개 청크 로드됨")

    folder_chunks = {}
    grouped_files = defaultdict(lambda: {"file_chunk": None, "elements": []})

    for chunk in all_chunks:
        filepath   = chunk.get("filepath", "Unknown")
        chunk_type = chunk.get("type", "")

        if chunk_type == "folder":
            folder_chunks[filepath] = chunk
        elif chunk_type == "file":
            grouped_files[filepath]["file_chunk"] = chunk
        else:
            grouped_files[filepath]["elements"].append(chunk)

    #출력 디렉토리 준비
    output_base = Path("./website/docs/api").resolve()
    output_base.mkdir(parents=True, exist_ok=True)

    sun2_base = output_base / "AWIS2000"
    sun2_base.mkdir(parents=True, exist_ok=True)

    with open(sun2_base / "index.md", "w", encoding="utf-8") as idx:
        idx.write("---\n")
        idx.write("title: \"AWIS2000\"\n")
        idx.write("sidebar_label: \"AWIS2000\"\n")
        idx.write("---\n\n")
        idx.write("# AWIS2000 Codebase API\n\n")
        idx.write("AWIS2000 프로젝트의 소스코드 및 API 문서 메인 페이지입니다.\n\n")
        idx.write("왼쪽 사이드바 메뉴를 열어 개별 모듈의 코드 문서를 확인할 수 있습니다.\n")

    for folder_path, f_chunk in folder_chunks.items():
        if folder_path == "Unknown":
            continue

        folder_name = Path(folder_path).name
        try:
            rel_folder = Path(folder_path).resolve().relative_to(source_root.parent)
        except ValueError:
            rel_folder = Path(folder_name)
        file_dir = sun2_base / rel_folder
        file_dir.mkdir(parents=True, exist_ok=True)

        f_summary = f_chunk.get("summary") or f_chunk.get("docstring") or "폴더 요약이 없습니다."

        with open(file_dir / "index.md", "w", encoding="utf-8") as idx:
            idx.write(f"---\ntitle: \"📁 {folder_name}\"\nsidebar_position: 1\n---\n\n")
            idx.write(f"# 📁 `{folder_name}` Module\n\n")
            idx.write(f":::tip 📁 Folder Overview\n**{escape_mdx(f_summary)}**\n:::\n\n")
            idx.write("---\n\n")
            idx.write("이 폴더에 포함된 개별 파일의 상세 내용은 왼쪽 메뉴에서 확인할 수 있습니다.\n")

    # 파일별 마크다운 생성
    for filepath, data in grouped_files.items():
        if filepath == "Unknown":
            continue

        path_obj  = Path(filepath)
        file_name = path_obj.name

        rel_parent     = _get_relative_doc_path(filepath, source_root)
        parent_folder_name = rel_parent.name or "root"   # 배지 표시용 이름

        file_dir = sun2_base / rel_parent
        file_dir.mkdir(parents=True, exist_ok=True)

        file_chunk = data["file_chunk"]
        elements   = sorted(data["elements"], key=lambda x: x.get("start_line", 0))

        with open(file_dir / f"{file_name}.md", "w", encoding="utf-8") as f:
            # Front matter
            f.write(f"---\ntitle: \"{file_name}\"\nsidebar_label: \"{file_name}\"\n---\n\n")

            # 헤더
            f.write(f"# 📄 {file_name}\n\n")
            f.write(
                f"<span className=\"theme-doc-version-badge badge badge--primary\">"
                f"Module: {parent_folder_name}</span>\n\n"
            )

            # File Overview
            if file_chunk:
                f_summary = file_chunk.get("summary") or file_chunk.get("docstring")
                if f_summary:
                    f.write(f":::info 📝 File Overview\n{escape_mdx(f_summary)}\n:::\n\n")

            f.write("---\n\n")

            # Contents Summary 테이블
            if elements:
                f.write("## 🗂️ Contents Summary\n\n")
                f.write("| Type | Name | Description |\n|---|---|---|\n")
                for el in elements:
                    el_type    = el.get("type", "unknown").lower()
                    el_name    = el.get("name", "unnamed")

                    target_desc = el.get("summary") or el.get("docstring") or ""
                    short_desc  = escape_mdx(target_desc.split("\n")[0]) or "-"

                    icon = (
                        "📦" if "class"    in el_type else
                        "🔧" if "property" in el_type else
                        "⚡" if "method"   in el_type else
                        "🔹"
                    )
                    f.write(f"| {icon} `{el_type}` | [**{el_name}**](#{el_name.lower()}) | {short_desc} |\n")
                f.write("\n---\n\n")

            # Code Details
            f.write("## 💻 Code Details\n\n")
            for el in elements:
                el_type    = el.get("type", "unknown")
                el_name    = el.get("name", "unnamed")
                el_summary = el.get("summary", "")
                el_doc     = el.get("docstring", "")
                el_code    = el.get("content", "")
                el_lang    = el.get("language", "csharp")

                f.write(f"### {el_name}\n\n")
                f.write(f"<span className=\"badge badge--secondary\">Type: {el_type}</span> ")
                if el.get("qualified_name"):
                    f.write(f"<span className=\"badge badge--success\">{escape_mdx(el['qualified_name'])}</span>\n\n")
                else:
                    f.write("\n\n")

                if el_summary:
                    f.write(f"{escape_mdx(el_summary)}\n\n")

                if el_doc and el_doc != el_summary:
                    f.write(f"**Docstring:**\n{escape_mdx(el_doc)}\n\n")

                if el_code:
                    safe_code = escape_code_block(el_code)
                    f.write(f"```{el_lang}\n{safe_code}\n```\n\n")

                f.write("<br/>\n\n")

    print("✅ Docusaurus용 마크다운 문서 생성이 완료되었습니다!")


if __name__ == "__main__":
    generate_docs()
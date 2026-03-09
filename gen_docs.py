import os
from pathlib import Path
from collections import defaultdict
from dotenv import load_dotenv
from qdrant_client import QdrantClient

def generate_docs():
    # 1. .env 파일 로드 및 Qdrant 환경 변수 가져오기
    load_dotenv()
    
    qdrant_url = os.getenv("QDRANT_URL")
    collection_name = os.getenv("COLLECTION_NAME")
    
    if not qdrant_url or not collection_name:
        print("❌ .env 파일에서 QDRANT_URL이나 COLLECTION_NAME을 찾을 수 없습니다.")
        return

    print(f"🔄 Qdrant 연결 중... URL: {qdrant_url}, Collection: {collection_name}")
    
    client = QdrantClient(url=qdrant_url)
    
    # 2. 지정된 컬렉션에서 모든 데이터 가져오기
    all_points = []
    offset = None
    while True:
        result = client.scroll(
            collection_name=collection_name,
            limit=100,
            offset=offset,
            with_payload=True,
            with_vectors=False
        )
        
        points, next_offset = result
        all_points.extend(points)
        
        if next_offset is None:
            break
        offset = next_offset

    print(f"✅ 총 {len(all_points)}개 청크 로드됨 (Collection: {collection_name})")

    # 3. 파일 경로(filepath) 기준으로 데이터 그룹핑하기
    grouped_files = defaultdict(lambda: {'file_chunk': None, 'elements': []})

    for point in all_points:
        chunk = point.payload
        if not chunk: continue

        filepath = chunk.get('filepath', 'Unknown')
        chunk_type = chunk.get('type', '')

        if chunk_type == 'file':
            grouped_files[filepath]['file_chunk'] = chunk
        elif chunk_type == 'folder':
            continue 
        else:
            grouped_files[filepath]['elements'].append(chunk)

    # 4. Docusaurus용 마크다운 생성 준비
    output_base = Path("./website/docs/api").resolve()
    output_base.mkdir(parents=True, exist_ok=True)

    tidal_base = output_base / "TIDAL"
    tidal_base.mkdir(parents=True, exist_ok=True)
    index_file_path = tidal_base / "index.md"
    
    with open(index_file_path, "w", encoding="utf-8") as idx_file:
        idx_file.write("---\n")
        idx_file.write("title: \"TIDAL Overview\"\n")
        idx_file.write("sidebar_label: \"Overview\"\n")
        idx_file.write("---\n\n")
        idx_file.write("# 🌊 TIDAL Codebase API\n\n")
        idx_file.write("TIDAL 프로젝트의 소스코드 및 API 문서 메인 페이지입니다.\n\n")
        idx_file.write("왼쪽 사이드바 메뉴를 열어 개별 모듈(`Models`, `Views` 등)의 코드 문서를 확인할 수 있습니다.\n")

    # 5. 개별 코드 마크다운 파일 생성 (디자인 개선됨)
    for filepath, data in grouped_files.items():
        if filepath == 'Unknown': continue

        path_obj = Path(filepath)
        file_name = path_obj.name
        
        parent_folder_name = path_obj.parent.name
        if not parent_folder_name:
            parent_folder_name = "root"
            
        file_dir = tidal_base / parent_folder_name
        file_dir.mkdir(parents=True, exist_ok=True)
        
        md_file_path = file_dir / f"{file_name}.md"
        
        file_chunk = data['file_chunk']
        elements = data['elements']
        elements.sort(key=lambda x: x.get('start_line', 0))

        with open(md_file_path, "w", encoding="utf-8") as f:
            f.write(f"---\n")
            f.write(f"title: \"{file_name}\"\n")
            f.write(f"sidebar_label: \"{file_name}\"\n")
            f.write(f"---\n\n")

            # 문서 헤더 및 모듈 뱃지
            f.write(f"# 📄 {file_name}\n\n")
            f.write(f"<span className=\"theme-doc-version-badge badge badge--primary\">Module: {parent_folder_name}</span>\n\n") 
            
            # File Overview (Docusaurus Info 박스 활용)
            if file_chunk and file_chunk.get('docstring'):
                f.write(f":::info 📝 File Overview\n")
                f.write(f"{file_chunk.get('docstring')}\n")
                f.write(f":::\n\n")
                
            f.write("---\n\n")

            # Contents Summary (가독성 좋은 Table 형태로 변경)
            if elements:
                f.write("## 🗂️ Contents Summary\n\n")
                f.write("| Type | Name | Description |\n")
                f.write("|---|---|---|\n")
                for el in elements:
                    el_type = el.get('type', 'unknown').lower()
                    el_name = el.get('name', 'unnamed')
                    short_desc = el.get('docstring', '').split('\n')[0] if el.get('docstring') else '-'
                    
                    # 타입에 따른 이모지 매핑
                    type_icon = "📦" if "class" in el_type else "🔧" if "property" in el_type else "⚡" if "method" in el_type else "🔹"
                    
                    f.write(f"| {type_icon} `{el_type}` | [**{el_name}**](#{el_name.lower()}) | {short_desc} |\n")
                f.write("\n---\n\n")

            # 세부 요소 및 코드 블럭 (뱃지로 타입 강조)
            f.write("## 💻 Code Details\n\n")
            for el in elements:
                el_type = el.get('type', 'unknown')
                el_name = el.get('name', 'unnamed')
                el_doc = el.get('docstring', '')
                el_code = el.get('content', '')
                el_lang = el.get('language', 'csharp')

                f.write(f"### {el_name}\n\n")
                
                # 타입과 전체 이름을 뱃지로 예쁘게 표시
                f.write(f"<span className=\"badge badge--secondary\">Type: {el_type}</span> ")
                if el.get('qualified_name'):
                    f.write(f"<span className=\"badge badge--success\">{el.get('qualified_name')}</span>\n\n")
                else:
                    f.write("\n\n")

                if el_doc:
                    f.write(f"{el_doc}\n\n")

                if el_code:
                    f.write(f"```{el_lang}\n")
                    f.write(f"{el_code}\n")
                    f.write(f"```\n\n")
                
                # 요소 간 구분선 추가
                f.write("<br/>\n\n")

    print("✅ Docusaurus용 마크다운 문서 생성이 완료되었습니다! (디자인 개선 버전)")

if __name__ == "__main__":
    generate_docs()
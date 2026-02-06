from neo4j import GraphDatabase

class GraphStore:
    def __init__(self):
        # Memgraph 접속 정보
        uri = "bolt://192.168.0.87:7687"
        user = "admin"
        password = "password"

        try:
            self.driver = GraphDatabase.driver(uri, auth=(user, password))
            self.driver.verify_connectivity()
        except Exception as e:
            print(f"⚠️ Memgraph connection failed: {e}")
            self.driver = None

    def close(self):
        if self.driver:
            self.driver.close()

    def save_graph_data(self, chunks: list, call_graph_edges: dict):
        """
        데이터를 Memgraph에 저장 (Ingest용)
        """
        if not self.driver:
            return

        print("  ...Saving nodes and edges to Memgraph...")

        with self.driver.session() as session:
            # 1. 노드 생성 (Function / Widget 구분)
            for chunk in chunks:
                if chunk.type == "ui_widget":
                    # UI 위젯 노드
                    query = """
                    MERGE (w:Widget {qualified_name: $qn})
                    SET w.filepath = $filepath,
                        w.name = $name,
                        w.type = 'ui_widget',
                        w.content = $content
                    """
                else:
                    # 일반 함수/클래스 노드
                    query = """
                    MERGE (f:Function {qualified_name: $qn})
                    SET f.filepath = $filepath,
                        f.name = $name,
                        f.type = $type
                    """
                
                content_val = chunk.content if hasattr(chunk, 'content') else ""
                
                session.run(query, 
                            qn=chunk.qualified_name, 
                            filepath=chunk.filepath, 
                            name=chunk.name, 
                            type=chunk.type,
                            content=content_val)

            # 2. 관계(CALLS) 생성
            for caller_qn, callees in call_graph_edges.items():
                for callee_qn in callees:
                    query = """
                    MATCH (a {qualified_name: $caller_qn})
                    MATCH (b {qualified_name: $callee_qn})
                    MERGE (a)-[:CALLS]->(b)
                    """
                    session.run(query, caller_qn=caller_qn, callee_qn=callee_qn)

    def get_callers(self, qualified_name: str) -> list[str]:
        """특정 함수를 호출하는 상위 함수들의 이름을 반환"""
        if not self.driver: return []

        query = """
        MATCH (caller)-[:CALLS]->(callee {qualified_name: $name})
        RETURN caller.qualified_name as qn
        """
        try:
            with self.driver.session() as session:
                result = session.run(query, name=qualified_name)
                return [record["qn"] for record in result]
        except Exception:
            return []

    def get_callees(self, qualified_name: str) -> list[str]:
        """특정 함수가 내부에서 호출하는 다른 함수들의 이름을 반환"""
        if not self.driver: return []

        query = """
        MATCH (caller {qualified_name: $name})-[:CALLS]->(callee)
        RETURN callee.qualified_name as qn
        """
        try:
            with self.driver.session() as session:
                result = session.run(query, name=qualified_name)
                return [record["qn"] for record in result]
        except Exception:
            return []

    def get_execution_flow(self, qualified_name: str, depth: int = 2) -> list[str]:
        """
        특정 노드(함수/위젯)를 중심으로 호출/피호출 관계를 반환
        """
        if not self.driver:
            return []

        # [수정] :Function 라벨을 제거하여 Widget 노드도 검색되도록 함
        query = """
        MATCH (t {qualified_name: $name})
        OPTIONAL MATCH (caller)-[:CALLS]->(t)
        OPTIONAL MATCH (t)-[:CALLS]->(callee)
        RETURN 
            collect(DISTINCT "called by " + caller.qualified_name) as callers,
            collect(DISTINCT "calls " + callee.qualified_name) as callees
        """
        try:
            with self.driver.session() as session:
                result = session.run(query, name=qualified_name).single()
                if not result:
                    return []
                flows = []
                # 리스트 컴프리헨션 로직 유지
                if result['callers']: flows.extend([f"[Caller] {c}" for c in result['callers'] if c])
                if result['callees']: flows.extend([f"[Callee] {c}" for c in result['callees'] if c])
                return flows
        except Exception as e:
            print(f"Error in get_execution_flow: {e}")
            return []

    def clear_all_data(self):
        """Memgraph 초기화"""
        if not self.driver: return
        print("🧹 Clearing Graph Database (Memgraph)...")
        try:
            with self.driver.session() as session:
                session.run("MATCH (n) DETACH DELETE n")
        except Exception as e:
            print(f"Failed to clear DB: {e}")
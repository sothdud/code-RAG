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
            # 1. 노드(함수) 생성
            for chunk in chunks:
                query = """
                MERGE (f:Function {qualified_name: $qn})
                SET f.filepath = $filepath,
                    f.name = $name
                """
                session.run(query, qn=chunk.qualified_name, filepath=chunk.filepath, name=chunk.name)

            # 2. 관계(CALLS) 생성
            for caller, callees in call_graph_edges.items():
                for callee in callees:
                    query = """
                    MATCH (a:Function {qualified_name: $caller})
                    MATCH (b:Function {qualified_name: $callee})
                    MERGE (a)-[:CALLS]->(b)
                    """
                    session.run(query, caller=caller, callee=callee)

    def get_execution_flow(self, qualified_name: str, depth: int = 2) -> list[str]:
        # (기존 코드와 동일 - 검색용)
        if not self.driver:
            return []

        query = """
        MATCH (t:Function {qualified_name: $name})
        OPTIONAL MATCH (caller:Function)-[:CALLS]->(t)
        OPTIONAL MATCH (t)-[:CALLS]->(callee:Function)
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
                if result['callers']: flows.extend([f"[Caller] {c}" for c in result['callers'] if c])
                if result['callees']: flows.extend([f"[Callee] {c}" for c in result['callees'] if c])
                return flows
        except Exception:
            return []

    def get_callees(self, qualified_name: str) -> list[str]:
        """특정 함수가 내부에서 호출하는 다른 함수들의 이름을 반환"""
        if not self.driver: return []

        query = """
        MATCH (caller:Function {qualified_name: $name})-[:CALLS]->(callee:Function)
        RETURN callee.qualified_name as qn
        """
        try:
            with self.driver.session() as session:
                result = session.run(query, name=qualified_name)
                return [record["qn"] for record in result]
        except Exception:
            return []

    def clear_all_data(self):
        """Memgraph의 모든 노드와 관계를 삭제하여 초기화합니다."""
        if not self.driver:
            return

        print("🧹 Clearing Graph Database (Memgraph)...")
        try:
            with self.driver.session() as session:
                # 모든 노드(n)와 연결된 관계를 끊고(DETACH) 삭제(DELETE)
                session.run("MATCH (n) DETACH DELETE n")
            print("✨ Graph Database cleared!")
        except Exception as e:
            print(f"⚠️ Failed to clear graph: {e}")

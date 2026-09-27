"""Tests for dot.kg — Knowledge Graph RAG"""
import pytest

from dot.kg import (
    Entity,
    Relation,
    KnowledgeGraph,
    GraphNode,
    GraphEdge,
    GraphRetriever,
    EntityExtractor,
    KGPipeline,
    KGSettings,
)


class TestKnowledgeGraph:
    """测试知识图谱存储"""

    def test_add_node(self):
        """测试添加节点"""
        kg = KnowledgeGraph()
        node = GraphNode(id="Apple", type="ORGANIZATION", description="Apple Inc.")
        kg.add_node(node)

        assert kg.has_node("Apple")
        assert kg.get_node("Apple") == node
        assert kg.node_count == 1

    def test_add_edge(self):
        """测试添加边"""
        kg = KnowledgeGraph()
        node1 = GraphNode(id="Tim Cook", type="PERSON")
        node2 = GraphNode(id="Apple", type="ORGANIZATION")
        kg.add_node(node1)
        kg.add_node(node2)

        edge = GraphEdge(source="Tim Cook", target="Apple", relation_type="CEO_of")
        kg.add_edge(edge)

        assert kg.has_edge("Tim Cook", "Apple", "CEO_of")
        assert kg.edge_count == 1

    def test_add_entities_and_relations(self):
        """测试批量添加实体和关系"""
        kg = KnowledgeGraph()

        entities = [
            Entity(name="Apple", type="ORGANIZATION"),
            Entity(name="Tim Cook", type="PERSON"),
        ]
        relations = [
            Relation(source="Tim Cook", target="Apple", relation_type="CEO_of"),
        ]

        kg.add_entities(entities)
        kg.add_relations(relations)

        assert kg.node_count == 2
        assert kg.edge_count == 1

    def test_neighbors(self):
        """测试获取邻居"""
        kg = KnowledgeGraph()
        kg.add_node(GraphNode(id="A", type="TEST"))
        kg.add_node(GraphNode(id="B", type="TEST"))
        kg.add_edge(GraphEdge(source="A", target="B", relation_type="linked_to"))

        neighbors = kg.get_neighbors("A")
        assert len(neighbors) == 1
        assert neighbors[0][0].id == "B"

    def test_bfs_expand(self):
        """测试 BFS 扩展"""
        kg = KnowledgeGraph()

        # 构建链式图: A -> B -> C -> D
        for name in ["A", "B", "C", "D"]:
            kg.add_node(GraphNode(id=name, type="TEST"))

        kg.add_edge(GraphEdge(source="A", target="B", relation_type="next"))
        kg.add_edge(GraphEdge(source="B", target="C", relation_type="next"))
        kg.add_edge(GraphEdge(source="C", target="D", relation_type="next"))

        # 从 A 扩展 2 跳
        expanded = kg.bfs_expand(["A"], max_hops=2)

        assert "B" in expanded
        assert "C" in expanded
        assert "D" not in expanded  # D 是 3 跳

    def test_get_subgraph(self):
        """测试获取子图"""
        kg = KnowledgeGraph()

        for name in ["A", "B", "C", "D"]:
            kg.add_node(GraphNode(id=name, type="TEST"))

        kg.add_edge(GraphEdge(source="A", target="B", relation_type="next"))
        kg.add_edge(GraphEdge(source="B", target="C", relation_type="next"))
        kg.add_edge(GraphEdge(source="C", target="D", relation_type="next"))

        subgraph = kg.get_subgraph(["B"], max_hops=1)

        assert subgraph.has_node("B")
        assert subgraph.has_node("A")  # 中心节点
        assert subgraph.has_node("C")  # 1 跳邻居
        assert not subgraph.has_node("D")  # 2 跳

    def test_to_dict_and_from_dict(self):
        """测试序列化和反序列化"""
        kg = KnowledgeGraph(name="test")
        kg.add_node(GraphNode(id="Test", type="TEST", description="test node"))
        kg.add_edge(GraphEdge(source="Test", target="Test", relation_type="self"))

        data = kg.to_dict()
        assert data["name"] == "test"
        assert len(data["nodes"]) == 1
        assert len(data["edges"]) == 1

        loaded = KnowledgeGraph.from_dict(data)
        assert loaded.name == "test"
        assert loaded.node_count == 1


class TestGraphRetriever:
    """测试图检索器"""

    def test_retrieve(self):
        """测试检索"""
        kg = KnowledgeGraph()
        kg.add_node(GraphNode(id="Apple", type="ORGANIZATION"))
        kg.add_node(GraphNode(id="Tim Cook", type="PERSON"))
        kg.add_node(GraphNode(id="CEO", type="POSITION"))
        kg.add_edge(GraphEdge(source="Tim Cook", target="Apple", relation_type="CEO_of"))
        kg.add_edge(GraphEdge(source="Tim Cook", target="CEO", relation_type="holds"))

        retriever = GraphRetriever(kg, max_hops=2)
        result = retriever.retrieve("Apple")

        assert len(result.matched_entities) > 0
        # Apple 应该被匹配
        entity_ids = [e.id for e in result.matched_entities]
        assert "Apple" in entity_ids

    def test_entity_info(self):
        """测试获取实体信息"""
        kg = KnowledgeGraph()
        kg.add_node(GraphNode(id="Test", type="TEST"))
        kg.add_edge(GraphEdge(source="Test", target="Test", relation_type="linked"))

        retriever = GraphRetriever(kg)
        info = retriever.get_entity_info("Test")

        assert info is not None
        assert info["entity"]["id"] == "Test"
        assert len(info["relations"]) == 1

    def test_search_nodes(self):
        """测试搜索节点"""
        kg = KnowledgeGraph()
        kg.add_node(GraphNode(id="Apple Inc", type="ORGANIZATION"))
        kg.add_node(GraphNode(id="Apple Watch", type="PRODUCT"))

        # search_nodes 在 KnowledgeGraph 上
        results = kg.search_nodes("Apple")

        assert len(results) == 2


class TestEntityExtractor:
    """测试实体抽取器"""

    def test_mock_extract(self):
        """测试 mock 抽取"""
        extractor = EntityExtractor(api_key="test")

        # 测试简单模式匹配
        result = extractor._mock_extract(
            "Apple is a technology company. Tim Cook is the CEO of Apple.",
            "Apple is a technology company."
        )

        # mock 模式会尝试匹配一些模式
        assert result is not None
        assert isinstance(result.entities, list)

    def test_parse_response(self):
        """测试解析 LLM 响应"""
        extractor = EntityExtractor(api_key="test")

        response = '''
        {
          "entities": [
            {"name": "Apple", "type": "ORGANIZATION", "description": "Apple Inc."},
            {"name": "Tim Cook", "type": "PERSON", "description": "CEO"}
          ],
          "relations": [
            {"source": "Tim Cook", "target": "Apple", "relation_type": "CEO_of"}
          ]
        }
        '''

        result = extractor._parse_response(response)

        assert len(result.entities) == 2
        assert len(result.relations) == 1
        assert result.entities[0].name == "Apple"
        assert result.relations[0].relation_type == "CEO_of"

    def test_parse_response_with_markdown(self):
        """测试解析带 markdown 的响应"""
        extractor = EntityExtractor(api_key="test")

        response = '''
        ```json
        {
          "entities": [{"name": "Test", "type": "TEST"}],
          "relations": []
        }
        ```
        '''

        result = extractor._parse_response(response)
        assert len(result.entities) == 1


class TestKGPipeline:
    """测试 KG RAG Pipeline"""

    def test_init(self):
        """测试初始化"""
        pipeline = KGPipeline(KGSettings(persist_graph=False))
        assert pipeline.graph is not None
        assert pipeline.extractor is not None
        assert pipeline.retriever is not None

    def test_ingest_text_no_llm(self):
        """测试索引文本（无 LLM 时跳过抽取）"""
        # 使用一个不存在的 API key 触发 mock 模式
        import os
        old_key = os.environ.get("OPENAI_API_KEY")
        os.environ.pop("OPENAI_API_KEY", None)

        try:
            settings = KGSettings(openai_api_key="test-key")
            pipeline = KGPipeline(settings)
            pipeline.settings.persist_graph = False

            # 由于没有真实 LLM，会进入 mock 模式
            # mock 模式可能抽取不到实体，但流程应该正常完成
            result = pipeline.ingest_text(
                "This is a test document.",
                source="test"
            )

            assert result.doc_id is not None
            assert result.chunks_processed >= 1
        finally:
            if old_key:
                os.environ["OPENAI_API_KEY"] = old_key

    def test_query(self):
        """测试查询"""
        pipeline = KGPipeline(KGSettings(persist_graph=False))

        # 添加一些测试数据
        pipeline.graph.add_node(GraphNode(id="Apple", type="ORGANIZATION"))
        pipeline.graph.add_node(GraphNode(id="Tim Cook", type="PERSON"))
        pipeline.graph.add_edge(GraphEdge(source="Tim Cook", target="Apple", relation_type="CEO_of"))

        result = pipeline.query("Who is the CEO of Apple?")

        assert result.question == "Who is the CEO of Apple?"
        assert len(result.matched_entities) > 0

    def test_stats(self):
        """测试统计"""
        pipeline = KGPipeline(KGSettings(persist_graph=False))

        pipeline.graph.add_node(GraphNode(id="Test", type="TEST"))

        stats = pipeline.stats()
        assert stats["node_count"] == 1
        assert stats["edge_count"] == 0

    def test_clear(self):
        """测试清空"""
        pipeline = KGPipeline(KGSettings(persist_graph=False))

        pipeline.graph.add_node(GraphNode(id="Test", type="TEST"))

        pipeline.clear()
        assert pipeline.graph.node_count == 0


class TestKGSettings:
    """测试配置"""

    def test_default_settings(self):
        """测试默认配置"""
        settings = KGSettings()

        assert settings.max_hops == 2
        assert settings.max_entities == 50
        assert settings.min_confidence == 0.5
        assert settings.persist_graph is True

    def test_custom_settings(self):
        """测试自定义配置"""
        settings = KGSettings(
            max_hops=3,
            max_entities=100,
            neo4j_uri="bolt://localhost:7687",
        )

        assert settings.max_hops == 3
        assert settings.max_entities == 100
        assert settings.neo4j_uri == "bolt://localhost:7687"

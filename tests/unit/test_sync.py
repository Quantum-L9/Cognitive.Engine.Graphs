"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, sync]
owner: engine-team
status: active
--- /L9_META ---

Unit tests for sync query generation.
Target Coverage: 70%+
"""

from unittest.mock import MagicMock

import pytest

from engine.config.schema import DomainSpec, SyncEndpointSpec, SyncStrategy
from engine.sync.generator import SyncGenerator


def make_mock_domain_spec() -> MagicMock:
    """Create a mock DomainSpec."""
    spec = MagicMock(spec=DomainSpec)
    return spec


def make_mock_sync_endpoint(
    *,
    path: str = "/sync/test",
    strategy: SyncStrategy = SyncStrategy.UNWINDMERGE,
    target_node: str = "TestNode",
    id_property: str = "id",
    fields_updated: list[str] | None = None,
    taxonomy_edges: list | None = None,
    child_sync: list | None = None,
) -> MagicMock:
    """Create a mock SyncEndpointSpec."""
    spec = MagicMock(spec=SyncEndpointSpec)
    spec.path = path
    spec.batchstrategy = strategy
    spec.targetnode = target_node
    spec.idproperty = id_property
    spec.fieldsupdated = fields_updated
    spec.taxonomyedges = taxonomy_edges or []
    spec.childsync = child_sync or []
    return spec


@pytest.mark.unit
class TestSyncGenerator:
    """Test sync Cypher generation on the legacy (``row{.*}``) contract.

    These mock specs declare no ontology, so they exercise the C-21 rollback
    path, ``sync_declared_projection_enabled=False``. The declared projection
    (the default) is covered against real specs in
    ``tests/unit/test_sync_declared_projection.py``.
    """

    @pytest.fixture(autouse=True)
    def _legacy_row_contract(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "sync_declared_projection_enabled", False)

    def test_unwind_merge_generates_merge(self) -> None:
        """UNWINDMERGE strategy generates MERGE statements."""
        domain_spec = make_mock_domain_spec()
        generator = SyncGenerator(domain_spec)

        endpoint = make_mock_sync_endpoint(
            path="/sync/suppliers",
            strategy=SyncStrategy.UNWINDMERGE,
            target_node="Supplier",
            id_property="supplierid",
        )

        cypher = generator.generate_sync_query(endpoint, batch_data=[])

        assert "UNWIND" in cypher
        assert "MERGE" in cypher
        assert "Supplier" in cypher
        assert "supplierid" in cypher

    def test_unwind_match_set_generates_match_set(self) -> None:
        """UNWINDMATCHSET strategy generates MATCH/SET statements."""
        domain_spec = make_mock_domain_spec()
        generator = SyncGenerator(domain_spec)

        endpoint = make_mock_sync_endpoint(
            path="/sync/suppliers",
            strategy=SyncStrategy.UNWINDMATCHSET,
            target_node="Supplier",
            id_property="supplierid",
            fields_updated=["name", "city"],
        )

        cypher = generator.generate_sync_query(endpoint, batch_data=[])

        assert "UNWIND" in cypher
        assert "MATCH" in cypher
        assert "SET" in cypher
        assert "name" in cypher
        assert "city" in cypher

    def test_taxonomy_linking(self) -> None:
        """Taxonomy edges are created during sync."""
        domain_spec = make_mock_domain_spec()
        generator = SyncGenerator(domain_spec)

        tax_edge = MagicMock()
        tax_edge.targetlabel = "PolymerType"
        tax_edge.targetid = "typeid"
        tax_edge.field = "polymertypes"
        tax_edge.edgetype = "SUPPLIES"

        endpoint = make_mock_sync_endpoint(
            path="/sync/suppliers",
            strategy=SyncStrategy.UNWINDMERGE,
            target_node="Supplier",
            id_property="supplierid",
            taxonomy_edges=[tax_edge],
        )

        cypher = generator.generate_sync_query(endpoint, batch_data=[])

        assert "SUPPLIES" in cypher
        assert "PolymerType" in cypher

    def test_child_entity_sync(self) -> None:
        """Child entities are synced with parent."""
        domain_spec = make_mock_domain_spec()
        generator = SyncGenerator(domain_spec)

        child = MagicMock()
        child.targetnode = "Certification"
        child.targetid = "certid"
        child.field = "certifications"
        child.edgetype = "HAS_CERTIFICATION"
        child.edgedirection = "parenttochild"

        endpoint = make_mock_sync_endpoint(
            path="/sync/suppliers",
            strategy=SyncStrategy.UNWINDMERGE,
            target_node="Supplier",
            id_property="supplierid",
            child_sync=[child],
        )

        cypher = generator.generate_sync_query(endpoint, batch_data=[])

        assert "HAS_CERTIFICATION" in cypher
        assert "Certification" in cypher
        # Stage 2 (Stage 4 proof P3): the child collection must not be copied onto the
        # parent — Neo4j rejects a list of maps as a property value — and children are
        # created inside FOREACH so a parent with no children keeps its row.
        assert "SET n += row{.*, certifications: null}, n._tenant = $tenant" in cypher
        assert "FOREACH (child_row IN coalesce(row.certifications, []) |" in cypher
        assert "MERGE (c:Certification {certid: child_row.certid})" in cypher
        assert "MERGE (n)-[:HAS_CERTIFICATION]->(c)" in cypher
        assert "UNWIND coalesce" not in cypher

    def test_child_entity_sync_childtoparent_direction(self) -> None:
        """childtoparent emits a valid reverse pattern, not `(n)-[:T]<-(c)`."""
        domain_spec = make_mock_domain_spec()
        generator = SyncGenerator(domain_spec)

        child = MagicMock()
        child.targetnode = "Instrument"
        child.targetid = "instrument_id"
        child.field = "instruments"
        child.edgetype = "INSTALLED_IN"
        child.edgedirection = "childtoparent"

        endpoint = make_mock_sync_endpoint(
            path="/sync/laboratories",
            strategy=SyncStrategy.UNWINDMERGE,
            target_node="Laboratory",
            id_property="lab_id",
            child_sync=[child],
        )

        cypher = generator.generate_sync_query(endpoint, batch_data=[])

        assert "MERGE (n)<-[:INSTALLED_IN]-(c)" in cypher
        assert "]<-(c)" not in cypher

    def test_child_entity_sync_unknown_direction_is_refused(self) -> None:
        """An undeclared edgedirection is a spec defect, not a silent default."""
        child = MagicMock()
        child.targetnode = "Instrument"
        child.targetid = "instrument_id"
        child.field = "instruments"
        child.edgetype = "INSTALLED_IN"
        child.edgedirection = "sideways"

        endpoint = make_mock_sync_endpoint(
            path="/sync/laboratories",
            strategy=SyncStrategy.UNWINDMERGE,
            target_node="Laboratory",
            id_property="lab_id",
            child_sync=[child],
        )

        with pytest.raises(ValueError, match="edgedirection must be"):
            SyncGenerator(make_mock_domain_spec()).generate_sync_query(endpoint, batch_data=[])

    def test_child_entity_sync_legacy_path_behind_flag(self, monkeypatch) -> None:
        """C-21: childsync_foreach_projection=False restores the pre-Stage-2 UNWIND path."""
        from engine.config.settings import settings

        monkeypatch.setattr(settings, "childsync_foreach_projection", False)
        child = MagicMock()
        child.targetnode = "Instrument"
        child.targetid = "instrument_id"
        child.field = "instruments"
        child.edgetype = "INSTALLED_IN"
        child.edgedirection = "childtoparent"

        endpoint = make_mock_sync_endpoint(
            path="/sync/laboratories",
            strategy=SyncStrategy.UNWINDMERGE,
            target_node="Laboratory",
            id_property="lab_id",
            child_sync=[child],
        )

        cypher = SyncGenerator(make_mock_domain_spec()).generate_sync_query(endpoint, batch_data=[])

        assert "SET n += row, n._tenant = $tenant" in cypher
        assert "UNWIND coalesce(row.instruments, []) AS child_row" in cypher
        assert "MERGE (n)-[:INSTALLED_IN]<-(c)" in cypher
        assert "FOREACH" not in cypher

    def test_parent_without_childsync_keeps_plain_set(self) -> None:
        """Endpoints without childsync keep `SET n += row` unchanged."""
        endpoint = make_mock_sync_endpoint(
            path="/sync/routes",
            strategy=SyncStrategy.UNWINDMERGE,
            target_node="Route",
            id_property="route_code",
        )

        cypher = SyncGenerator(make_mock_domain_spec()).generate_sync_query(endpoint, batch_data=[])

        assert "SET n += row, n._tenant = $tenant" in cypher
        assert "FOREACH" not in cypher

    def test_unwind_match_set_updates_all_when_no_fields_specified(self) -> None:
        """UNWINDMATCHSET with no fields_updated uses SET n += row."""
        domain_spec = make_mock_domain_spec()
        generator = SyncGenerator(domain_spec)

        endpoint = make_mock_sync_endpoint(
            path="/sync/suppliers",
            strategy=SyncStrategy.UNWINDMATCHSET,
            target_node="Supplier",
            id_property="supplierid",
            fields_updated=None,
        )

        cypher = generator.generate_sync_query(endpoint, batch_data=[])

        assert "SET n += row" in cypher

    def test_missing_target_node_raises(self) -> None:
        """Missing targetnode raises ValueError."""
        domain_spec = make_mock_domain_spec()
        generator = SyncGenerator(domain_spec)

        endpoint = make_mock_sync_endpoint(
            path="/sync/test",
            strategy=SyncStrategy.UNWINDMERGE,
            target_node=None,
            id_property="id",
        )
        endpoint.targetnode = None

        with pytest.raises(ValueError) as exc_info:
            generator.generate_sync_query(endpoint, batch_data=[])

        assert "targetnode" in str(exc_info.value)

    def test_missing_id_property_raises(self) -> None:
        """Missing idproperty raises ValueError."""
        domain_spec = make_mock_domain_spec()
        generator = SyncGenerator(domain_spec)

        endpoint = make_mock_sync_endpoint(
            path="/sync/test",
            strategy=SyncStrategy.UNWINDMERGE,
            target_node="TestNode",
            id_property=None,
        )
        endpoint.idproperty = None

        with pytest.raises(ValueError) as exc_info:
            generator.generate_sync_query(endpoint, batch_data=[])

        assert "idproperty" in str(exc_info.value)

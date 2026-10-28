"""columns: as a mapping must not raise KeyError: 0 (dict[0])."""
import pytest
from entity_mapping import EntityMappingError, is_column_mappings


def test_is_column_mappings_rejects_dict_with_clear_error():
    with pytest.raises(EntityMappingError, match="must be a YAML list"):
        is_column_mappings({"ID": {"target": "id_pk"}, "NAME": {"destination": "n"}})


def test_is_column_mappings_accepts_list_with_target_and_destination():
    assert is_column_mappings([
        {"source": "ID", "target": "id_pk"},
        {"source": "NAME", "destination": "name_renamed"},
    ])

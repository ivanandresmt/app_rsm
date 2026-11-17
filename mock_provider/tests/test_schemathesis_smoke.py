from __future__ import annotations

import pytest

st = pytest.importorskip("schemathesis")


@pytest.mark.schemathesis
def test_contract_loads_with_schemathesis():
    """Optional smoke check: the shipped contract parses with schemathesis."""
    from conftest import SPEC

    loader = st.openapi.from_dict if hasattr(st, "openapi") else st.from_dict
    assert loader(SPEC) is not None

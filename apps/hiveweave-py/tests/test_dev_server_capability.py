"""CEO/HR must not see spawn tools they cannot run; lookup stays readable."""

from hiveweave.services.permission import permission_service
from hiveweave.services.policy import policy_service


def test_ceo_sees_lookup_not_start_dev_server():
    ceo = {"role": "ceo", "permission_type": "coordinator"}
    tools = permission_service.get_tools_for_agent(ceo)
    assert "start_dev_server" not in tools
    assert "stop_dev_server" not in tools
    assert "lookup_dev_server" in tools
    assert policy_service.hard_check(ceo, "start_dev_server") is not None
    assert policy_service.hard_check(ceo, "stop_dev_server") is not None
    assert policy_service.hard_check(ceo, "lookup_dev_server") is None


def test_hr_sees_lookup_not_start_dev_server():
    hr = {"role": "hr"}
    tools = permission_service.get_tools_for_agent(hr)
    assert "start_dev_server" not in tools
    assert "stop_dev_server" not in tools
    assert "lookup_dev_server" in tools
    assert policy_service.hard_check(hr, "start_dev_server") is not None
    assert policy_service.hard_check(hr, "stop_dev_server") is not None
    assert policy_service.hard_check(hr, "lookup_dev_server") is None


def test_executor_can_start_and_lookup_dev_server():
    ex = {"role": "frontend engineer", "permission_mode": "readwrite"}
    tools = permission_service.get_tools_for_agent(ex)
    assert "start_dev_server" in tools
    assert "stop_dev_server" in tools
    assert "lookup_dev_server" in tools
    assert policy_service.hard_check(ex, "start_dev_server") is None
    assert policy_service.hard_check(ex, "stop_dev_server") is None
    assert policy_service.hard_check(ex, "lookup_dev_server") is None

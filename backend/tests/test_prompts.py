import os
from datetime import date

from app.agent.prompts import build_system_prompt
from app.models import Tenant, User


def test_system_prompt_keeps_per_request_facts_after_the_shared_rules():
    harbour, rostr = Tenant(name="Harbour Appliance Co"), Tenant(name="Rostr Cloud")
    customer, agent = User(role="customer"), User(role="agent")
    a = build_system_prompt(harbour, customer, today=date(2026, 10, 19))
    b = build_system_prompt(rostr, agent, today=date(2026, 10, 20))

    shared = os.path.commonprefix([a, b])
    # Everything up to the rules is byte-identical across tenants, roles and days, so a
    # provider's prefix cache can serve it; the variable lines sit in the short tail.
    assert "8. Reply in Australian English" in shared
    assert "Harbour Appliance Co" not in shared and "2026-10-19" not in shared
    assert a.endswith('Current session: business "Harbour Appliance Co", talking to a customer, '
                      "today is 2026-10-19.")

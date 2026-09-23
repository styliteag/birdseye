import pytest

from birdseye_web.access import Grant
from birdseye_web.matrix import Cell
from birdseye_web.models import PortRange, Service
from birdseye_web.ui import cell_class, cell_short


def _cell(*services, conditional=(), action="accept"):
    return Cell(
        tuple(
            Grant("p", f"r{i}", ("a",), None, ("b",), None, s, action, conditional)
            for i, s in enumerate(services)
        )
    )


TCP22 = Service("tcp", (PortRange(22, 22),))


@pytest.mark.parametrize(
    "services,short,cls",
    [
        ((Service("all"),), "ALL", "c-all"),
        ((Service("icmp"),), "ping", "c-icmp"),
        ((TCP22,), "22", "c-ports"),
        ((Service("tcp", (PortRange(22, 22), PortRange(443, 443))),), "22+", "c-ports"),
        ((Service("netbird-ssh"),), "ssh", "c-ssh"),
        ((TCP22, Service("icmp")), "2×", "c-ports"),
        ((Service("udp"),), "udp", "c-ports"),
    ],
)
def test_cell_badges(services, short, cls):
    c = _cell(*services)
    assert cell_short(c) == short
    assert cell_class(c) == cls


def test_conditional_and_deny_classes():
    assert cell_class(_cell(TCP22, conditional=("pc",))) == "c-ports c-cond"
    assert cell_class(_cell(TCP22, action="drop")) == "c-deny"

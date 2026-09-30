"""json_repair's old split of an unquoted value, replayed at the boundary.

Some json-repair releases cut an unquoted string value at its first comma
and made every later ``word:`` a key of its own (the 2026-09-22 log, defect
14): ``{"text": Financial Dashboard ... Trading: $10,000 - Consulting: ...}``
came back as ``{"text": "...$10", "Consulting": "5,000", ...}``.  Measured on
that text: 0.58.7 and 0.59.0 split it; 0.29.6 (the nixpkgs pin the NixOS
image ships), 0.59.4 and 0.63.5 keep it whole.  requirements.txt and
pyproject.toml pin >= 0.59.4 so a pip install never lands in the split
range.  The executor's refusal of a split and the
history mark it leaves are still the safety net for any other input the
library splits, so their tests replay the old output here instead of relying
on an old library being installed.

Each recorded output is exactly what json-repair 0.58.7 returned for the text
``hartos.helper.parse_tool_arguments`` hands it (the text after
``_quote_overflowing_numbers``, which leaves these unchanged).  Any other text
goes to the real library, so a benign repair in the same test still runs.
"""
from contextlib import contextmanager
from unittest import mock

UNQUOTED = ('{"text": Financial Dashboard Revenue (Monthly): - Trading: '
            '$10,000\n- Consulting: $5,000\n- Education: $2,500\n- TOTAL: '
            '$17,500 Net Profit Margin: 28.6%}')

_DASHBOARD_TAIL = ('"Consulting": "5,000", "Education": "2,500", '
                   '"TOTAL": "17,500", "Margin": 28.6}')

# json-repair 0.58.7 output, per input text.
SPLIT_BY_OLD_JSON_REPAIR = {
    UNQUOTED: ('{"text": "Financial Dashboard Revenue (Monthly): - Trading: '
               '$10", ' + _DASHBOARD_TAIL),
    UNQUOTED.replace('"text"', '"command"'):
        ('{"command": "Financial Dashboard Revenue (Monthly): - Trading: '
         '$10", ' + _DASHBOARD_TAIL),
    '{"command": deploy the app, then report status: ok}':
        '{"command": "deploy the app", "status": "ok"}',
}


@contextmanager
def old_json_repair_split():
    """Patch hartos.helper.repair_json to split the recorded inputs the way
    json-repair 0.58.7 did; every other text is repaired by the real
    library."""
    from hartos import helper
    real = helper.repair_json

    def repair(text, *args, **kwargs):
        if text in SPLIT_BY_OLD_JSON_REPAIR:
            return SPLIT_BY_OLD_JSON_REPAIR[text]
        return real(text, *args, **kwargs)

    with mock.patch.object(helper, 'repair_json', side_effect=repair) as m:
        yield m

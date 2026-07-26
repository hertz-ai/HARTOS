"""The 100% gate (named zz so pytest's file ordering runs it LAST): every
route the deployed app registers must have been exercised by this suite.

This is the enforcement half of the goal 'integration tests testing the
deployed functionality, coverage should be 100%': the evidence is the HITS
registry the conftest after_request hook filled while the other files drove
the app. A new route shipped without a test fails HERE, by name -- coverage
can never silently rot below 100%.
"""
from . import conftest


def test_every_deployed_route_was_exercised(surface_app):
    surface = conftest.surface_rules(surface_app)
    missing = sorted(surface - conftest.HITS)
    covered = len(surface) - len(missing)
    assert not missing, (
        'DEPLOYED SURFACE NOT 100%% COVERED: %d/%d routes exercised; missing:\n  %s'
        % (covered, len(surface),
           '\n  '.join('%s %s' % m for m in missing)))


def test_surface_is_nontrivial(surface_app):
    """Guard the gate itself: if the app factory ever silently registered a
    fraction of the real surface (import failure eating blueprints), the 100%
    above would pass vacuously. The deployed surface is ~319 routes today;
    a collapse below 250 means blueprints went missing, not that the OS
    shrank."""
    assert len(conftest.surface_rules(surface_app)) >= 250

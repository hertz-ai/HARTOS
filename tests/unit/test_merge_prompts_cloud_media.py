"""_merge_prompts_with_cloud — cloud media survives local-first dedup.

Human-created agents share the SAME prompt_id locally and on the cloud
(central-DB integer id; agent-sync writes {pid}.json under that id), so the
old dedup discarded the cloud twin wholesale — losing the idle/intro filler
videos that only exist cloud-side ("idle videos not loading", 2026-06-10).
The merge must keep local authoritative for state/recipe fields while
ADOPTING the cloud twin's media fields when the local record lacks them.

Behavioural via extract-and-exec: importing hart_intelligence_entry boots the
whole Flask app, so the function (+ its _CLOUD_MEDIA_FIELDS constant) is
extracted from source and exec'd with a stubbed ``pooled_get`` boundary; the
REAL merge logic runs and observable mutations are asserted.
"""
import logging
import os
import re
import sys
from unittest.mock import MagicMock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def _load_merge(cloud_items, status=200, raise_exc=None):
    src = open(os.path.join(_ROOT, 'hart_intelligence_entry.py'),
               encoding='utf-8').read()
    block = re.search(
        r'_CLOUD_MEDIA_FIELDS = .*?\n\n\ndef _merge_prompts_with_cloud.*?\n    return local_prompts\n',
        src, re.DOTALL).group(0)

    def pooled_get(url, timeout=5):
        if raise_exc:
            raise raise_exc
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = cloud_items
        return resp

    # The exec namespace must carry every module-level name the extracted
    # block touches, INCLUDING on its error paths. `logging` and `__name__`
    # are needed by the except arm's
    # `logging.getLogger(__name__).exception(...)`; without them the handler
    # itself raised NameError, so the one test that drives a cloud failure
    # exploded instead of asserting the swallow. That is the standing cost of
    # extract-and-exec: a namespace is a hand-maintained copy of the module's
    # imports, and it silently rots when production grows a new one (here, the
    # no-silent-exception-gulping fix).
    ns = {
        'pooled_get': pooled_get,
        'logging': logging,
        '__name__': 'hart_intelligence_entry',
    }
    exec(block, ns)
    return ns['_merge_prompts_with_cloud']


class TestCloudMediaGraft:
    def test_colliding_id_adopts_cloud_media_keeps_local_state(self):
        local = [{'prompt_id': '10009855073', 'name': 'LocalName',
                  'has_recipe': True, 'source': 'local'}]
        cloud = [{'prompt_id': '10009855073', 'name': 'CloudName',
                  'fillers': [{'type': 'idle', 'video_link': 'https://cdn/x.mp4'}],
                  'image_url': 'https://cdn/x.png'}]
        merge = _load_merge(cloud)
        out = merge(local, 'http://cloud/x')
        assert len(out) == 1, "collision must not duplicate the agent"
        rec = out[0]
        assert rec['name'] == 'LocalName', "local stays authoritative"
        assert rec['has_recipe'] is True
        assert rec['fillers'][0]['video_link'] == 'https://cdn/x.mp4', \
            "cloud idle filler must survive localization"
        assert rec['image_url'] == 'https://cdn/x.png'

    def test_existing_local_media_not_overwritten(self):
        local = [{'prompt_id': '1', 'image_url': 'local.png'}]
        cloud = [{'prompt_id': '1', 'image_url': 'cloud.png',
                  'fillers': [{'type': 'idle', 'video_link': 'v.mp4'}]}]
        out = _load_merge(cloud)(local, 'u')
        assert out[0]['image_url'] == 'local.png'
        assert out[0]['fillers'][0]['video_link'] == 'v.mp4'

    def test_cloud_only_agent_still_appended(self):
        local = [{'prompt_id': '1'}]
        cloud = [{'prompt_id': '2', 'name': 'CloudOnly'}]
        out = _load_merge(cloud)(local, 'u')
        assert len(out) == 2
        added = [p for p in out if p['prompt_id'] == '2'][0]
        assert added['source'] == 'cloud' and added['has_recipe'] is False

    def test_cloud_failure_leaves_local_untouched(self):
        local = [{'prompt_id': '1', 'name': 'A'}]
        out = _load_merge([], raise_exc=ConnectionError('down'))(local, 'u')
        assert out == [{'prompt_id': '1', 'name': 'A'}]

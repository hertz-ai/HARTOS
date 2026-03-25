"""
Native Erxes CRM v1 Client for HARTOS

Handles:
  - Cookie-based authentication (login + auto-refresh)
  - Customer CRUD (create, find by email, update)
  - Company CRUD
  - Deal pipeline management (create, move stage, list)
  - Stage mapping between HARTOS names and Erxes IDs
  - Pipeline status queries

Uses only stdlib (urllib, http.cookiejar) — no requests dependency.

Environment:
  ERXES_API_URL      — e.g. http://192.168.0.83:3300
  ERXES_EMAIL        — admin email (default: sathish@hevolve.ai)
  ERXES_PASSWORD     — admin password
  ERXES_BOARD_ID     — deal board ID (auto-discovered if not set)
  ERXES_PIPELINE_ID  — pipeline ID (auto-discovered if not set)
"""
import http.cookiejar
import json
import logging
import os
import threading
import time
import urllib.request
import urllib.error
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger('hevolve_erxes')

# ── Singleton client ──
_client_instance = None
_client_lock = threading.Lock()


class ErxesCRMClient:
    """Thread-safe Erxes v1 GraphQL client with cookie-based auth."""

    def __init__(self, api_url: str, email: str, password: str):
        self.api_url = api_url.rstrip('/')
        self.graphql_url = self.api_url + '/graphql'
        self.email = email
        self.password = password

        self._cookie_jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookie_jar)
        )
        self._auth_lock = threading.Lock()
        self._logged_in = False
        self._login_time = 0

        # Stage mapping: populated on first use
        self._stage_map = {}       # name -> id
        self._stage_map_rev = {}   # id -> name
        self._board_id = os.environ.get('ERXES_BOARD_ID', '')
        self._pipeline_id = os.environ.get('ERXES_PIPELINE_ID', '')

    # ═══════════════════════════════════════════════════════════
    # Core GraphQL transport
    # ═══════════════════════════════════════════════════════════

    def _gql(self, query: str, variables: Dict = None, retry_auth: bool = True) -> Dict:
        """Execute a GraphQL query/mutation. Auto-logs-in if needed."""
        if not self._logged_in:
            self._login()

        payload = {'query': query}
        if variables:
            payload['variables'] = variables

        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            self.graphql_url, data=data,
            headers={'Content-Type': 'application/json'}
        )
        try:
            resp = self._opener.open(req, timeout=15)
            result = json.loads(resp.read().decode('utf-8'))

            # Check for auth errors
            errors = result.get('errors', [])
            if errors and any('Login required' in e.get('message', '') for e in errors):
                if retry_auth:
                    self._logged_in = False
                    self._login()
                    return self._gql(query, variables, retry_auth=False)
            return result

        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')[:500]
            if 'Login required' in body and retry_auth:
                self._logged_in = False
                self._login()
                return self._gql(query, variables, retry_auth=False)
            return {'errors': [{'message': str(e), 'body': body}]}
        except Exception as e:
            return {'errors': [{'message': str(e)}]}

    def _login(self):
        """Authenticate and store session cookie."""
        with self._auth_lock:
            if self._logged_in and (time.time() - self._login_time) < 3600:
                return

            payload = json.dumps({
                'query': f'mutation {{ login(email: "{self.email}", password: "{self.password}") }}'
            }).encode('utf-8')
            req = urllib.request.Request(
                self.graphql_url, data=payload,
                headers={'Content-Type': 'application/json'}
            )
            try:
                resp = self._opener.open(req, timeout=15)
                result = json.loads(resp.read().decode('utf-8'))
                if result.get('data', {}).get('login') == 'loggedIn':
                    self._logged_in = True
                    self._login_time = time.time()
                    logger.info('Erxes: logged in as %s', self.email)
                else:
                    logger.error('Erxes login failed: %s', result)
            except Exception as e:
                logger.error('Erxes login error: %s', e)

    # ═══════════════════════════════════════════════════════════
    # Stage Mapping
    # ═══════════════════════════════════════════════════════════

    def _ensure_stage_map(self):
        """Load stage IDs from Erxes pipeline. Auto-discovers board/pipeline if not set."""
        if self._stage_map:
            return

        # Discover board if needed
        if not self._board_id:
            r = self._gql('{ boards(type: "deal") { _id name } }')
            boards = r.get('data', {}).get('boards', [])
            if boards:
                self._board_id = boards[0]['_id']
                logger.info('Erxes: auto-discovered board %s (%s)', boards[0]['name'], self._board_id)

        # Discover pipeline if needed — prefer the one that has stages
        if self._board_id and not self._pipeline_id:
            r = self._gql(
                '{ pipelines(boardId: "%s") { _id name } }' % self._board_id
            )
            pipelines = r.get('data', {}).get('pipelines', [])
            best = None
            for p in pipelines:
                sr = self._gql('{ stages(pipelineId: "%s") { _id } }' % p['_id'])
                stages = sr.get('data', {}).get('stages', [])
                if stages:
                    best = p
                    break
            if best:
                self._pipeline_id = best['_id']
                logger.info('Erxes: auto-discovered pipeline %s (%s) with stages', best['name'], self._pipeline_id)
            elif pipelines:
                self._pipeline_id = pipelines[0]['_id']
                logger.info('Erxes: auto-discovered pipeline %s (%s) (no stages)', pipelines[0]['name'], self._pipeline_id)

        # Load stages
        if self._pipeline_id:
            r = self._gql(
                '{ stages(pipelineId: "%s") { _id name order } }' % self._pipeline_id
            )
            stages = r.get('data', {}).get('stages', [])
            for s in stages:
                name_lower = s['name'].lower()
                self._stage_map[name_lower] = s['_id']
                self._stage_map_rev[s['_id']] = name_lower
            logger.info('Erxes: loaded %d stages', len(self._stage_map))

    def stage_id(self, stage_name: str) -> Optional[str]:
        """Get Erxes stage ID from HARTOS stage name."""
        self._ensure_stage_map()
        return self._stage_map.get(stage_name.lower())

    def stage_name(self, stage_id: str) -> Optional[str]:
        """Get HARTOS stage name from Erxes stage ID."""
        self._ensure_stage_map()
        return self._stage_map_rev.get(stage_id)

    # ═══════════════════════════════════════════════════════════
    # Customer Operations
    # ═══════════════════════════════════════════════════════════

    def find_customer_by_email(self, email: str) -> Optional[Dict]:
        """Find a customer by email. Returns None if not found.

        Uses paginated listing with client-side match because Erxes v1
        searchValue has ES field mapping issues.
        """
        page = 1
        while page <= 10:  # safety limit
            r = self._gql(
                '{ customers(page: %d, perPage: 50) '
                '{ _id firstName lastName primaryEmail state } }' % page
            )
            customers = r.get('data', {}).get('customers', [])
            if not customers:
                break
            for c in customers:
                if c.get('primaryEmail', '').lower() == email.lower():
                    return c
            page += 1
        return None

    def create_customer(self, first_name: str, last_name: str, email: str,
                        state: str = 'lead') -> Optional[Dict]:
        """Create a customer. Returns created customer or None on failure."""
        # Check if already exists
        existing = self.find_customer_by_email(email)
        if existing:
            return existing

        mutation = '''
        mutation AddCustomer($firstName: String, $lastName: String,
                             $primaryEmail: String, $state: String) {
          customersAdd(firstName: $firstName, lastName: $lastName,
                       primaryEmail: $primaryEmail, state: $state) {
            _id firstName lastName primaryEmail
          }
        }'''
        r = self._gql(mutation, {
            'firstName': first_name,
            'lastName': last_name,
            'primaryEmail': email,
            'state': state,
        })
        created = r.get('data', {}).get('customersAdd')
        errors = r.get('errors', [])
        is_url_error = errors and all('Url is invalid' in e.get('message', '') for e in errors)

        if created:
            logger.info('Erxes: created customer %s %s (%s)', first_name, last_name, email)
            return created
        elif 'Duplicated email' in str(errors):
            return self.find_customer_by_email(email)
        elif is_url_error:
            # Customer was created despite webhook error -- find it
            found = self.find_customer_by_email(email)
            if found:
                logger.info('Erxes: created customer %s (%s) (webhook warning suppressed)', first_name, email)
                return found
        else:
            logger.warning('Erxes: customer create failed: %s', errors)
        return None

    # ═══════════════════════════════════════════════════════════
    # Deal Operations
    # ═══════════════════════════════════════════════════════════

    def create_deal(self, name: str, stage_name: str = 'new',
                    customer_ids: List[str] = None) -> Optional[Dict]:
        """Create a deal in the pipeline at the specified stage."""
        self._ensure_stage_map()
        sid = self.stage_id(stage_name)
        if not sid:
            logger.error('Erxes: unknown stage "%s"', stage_name)
            return None

        mutation = '''
        mutation AddDeal($name: String!, $stageId: String!, $customerIds: [String]) {
          dealsAdd(name: $name, stageId: $stageId, customerIds: $customerIds) {
            _id name stageId
          }
        }'''
        r = self._gql(mutation, {
            'name': name,
            'stageId': sid,
            'customerIds': customer_ids or [],
        })
        deal = r.get('data', {}).get('dealsAdd')
        errors = r.get('errors', [])
        is_url_error = errors and all('Url is invalid' in e.get('message', '') for e in errors)

        if deal:
            logger.info('Erxes: created deal "%s" in stage %s', name, stage_name)
            return deal
        elif is_url_error:
            # Deal was created in MongoDB despite webhook error -- find it
            found = self.find_deal_by_name(name)
            if found:
                logger.info('Erxes: created deal "%s" in stage %s (webhook warning suppressed)', name, stage_name)
                return found
            logger.warning('Erxes: deal create had webhook error but deal not found in DB')
        else:
            logger.warning('Erxes: deal create failed: %s', errors)
        return None

    def move_deal(self, deal_id: str, new_stage_name: str) -> Optional[Dict]:
        """Move a deal to a different pipeline stage.

        Note: Erxes v1 returns 'Url is invalid' error from a post-update
        webhook, but the DB mutation succeeds. We treat this as success.
        """
        sid = self.stage_id(new_stage_name)
        if not sid:
            logger.error('Erxes: unknown stage "%s"', new_stage_name)
            return None

        mutation = '''
        mutation EditDeal($id: String!, $stageId: String) {
          dealsEdit(_id: $id, stageId: $stageId) {
            _id name stageId
          }
        }'''
        r = self._gql(mutation, {'id': deal_id, 'stageId': sid})
        deal = r.get('data', {}).get('dealsEdit')

        # "Url is invalid" is a non-fatal webhook error -- the DB update succeeds
        errors = r.get('errors', [])
        is_url_error = errors and all('Url is invalid' in e.get('message', '') for e in errors)
        if deal:
            logger.info('Erxes: moved deal %s to stage %s', deal_id, new_stage_name)
            return deal
        elif is_url_error:
            logger.info('Erxes: moved deal %s to stage %s (webhook warning suppressed)', deal_id, new_stage_name)
            return {'_id': deal_id, 'stageId': sid, 'webhook_warning': True}
        return None

    def find_deal_by_name(self, name: str) -> Optional[Dict]:
        """Find a deal by name (partial match via client-side filter)."""
        r = self._gql('{ deals { _id name stageId customerIds } }')
        deals = r.get('data', {}).get('deals', [])
        name_lower = name.lower()
        for d in deals:
            if name_lower in d.get('name', '').lower():
                return d
        return None

    def list_deals_by_stage(self, stage_name: str) -> List[Dict]:
        """List all deals in a given stage."""
        sid = self.stage_id(stage_name)
        if not sid:
            return []
        r = self._gql('{ deals { _id name stageId customerIds } }')
        deals = r.get('data', {}).get('deals', [])
        return [d for d in deals if d.get('stageId') == sid]

    def get_pipeline_status(self) -> Dict:
        """Get full pipeline status: deals grouped by stage with counts."""
        self._ensure_stage_map()
        pipeline = {}
        total = 0
        for stage_name, stage_id in self._stage_map.items():
            r = self._gql(
                '{ deals(stageId: "%s") { _id name } }' % stage_id
            )
            deals = r.get('data', {}).get('deals', [])
            pipeline[stage_name] = {
                'stage_id': stage_id,
                'count': len(deals),
                'deals': [{'id': d['_id'], 'name': d['name']} for d in deals],
            }
            total += len(deals)
        return {'pipeline': pipeline, 'total_deals': total}

    # ═══════════════════════════════════════════════════════════
    # Prospect Sync — bidirectional HARTOS <-> Erxes
    # ═══════════════════════════════════════════════════════════

    def sync_prospect_to_erxes(self, prospect: Dict) -> Dict:
        """Sync a HARTOS prospect to Erxes (customer + deal).

        Returns dict with erxes_customer_id and erxes_deal_id.
        Uses existing IDs from prospect if available to avoid duplicates.
        """
        result = {'synced': False}

        # Use existing customer ID or create/find
        customer_id = prospect.get('erxes_customer_id')
        if not customer_id:
            name_parts = prospect.get('contact_name', '').split(None, 1)
            first_name = name_parts[0] if name_parts else prospect.get('company', '')
            last_name = name_parts[1] if len(name_parts) > 1 else ''
            customer = self.create_customer(first_name, last_name, prospect['email'])
            if customer:
                customer_id = customer['_id']

        if customer_id:
            result['erxes_customer_id'] = customer_id

            # Use existing deal ID or find/create
            deal_id = prospect.get('erxes_deal_id')
            if deal_id:
                # Already linked -- just ensure correct stage
                self._ensure_stage_map()
                target_stage = prospect.get('stage', 'new')
                sid = self.stage_id(target_stage)
                if sid:
                    # Verify deal exists and move if needed
                    self.move_deal(deal_id, target_stage)
                result['erxes_deal_id'] = deal_id
            else:
                # Find existing deal by company name
                deal = self.find_deal_by_name(prospect.get('company', ''))
                if not deal:
                    deal_name = '%s - %s' % (
                        prospect.get('company', 'Unknown'),
                        prospect.get('vertical', 'General').replace('_', ' ').title()
                    )
                    deal = self.create_deal(
                        name=deal_name,
                        stage_name=prospect.get('stage', 'new'),
                        customer_ids=[customer_id],
                    )
                if deal:
                    result['erxes_deal_id'] = deal['_id']

            result['synced'] = True

        return result

    def sync_stage_change(self, prospect: Dict, new_stage: str) -> bool:
        """Sync a stage change from HARTOS to Erxes deal pipeline."""
        deal_id = prospect.get('erxes_deal_id')
        if not deal_id:
            # Try to find the deal
            deal = self.find_deal_by_name(prospect.get('company', ''))
            if deal:
                deal_id = deal['_id']

        if deal_id:
            result = self.move_deal(deal_id, new_stage)
            return result is not None
        return False

    # ═══════════════════════════════════════════════════════════
    # Health / Status
    # ═══════════════════════════════════════════════════════════

    def is_available(self) -> bool:
        """Check if Erxes API is reachable and we can authenticate."""
        try:
            self._login()
            return self._logged_in
        except Exception:
            return False

    def status(self) -> Dict:
        """Get connection status and pipeline summary."""
        available = self.is_available()
        result = {
            'available': available,
            'api_url': self.api_url,
            'email': self.email,
            'logged_in': self._logged_in,
        }
        if available:
            pipeline = self.get_pipeline_status()
            result['pipeline'] = pipeline
        return result


def get_erxes_client() -> Optional[ErxesCRMClient]:
    """Get or create the singleton Erxes client.

    Returns None if ERXES_API_URL is not configured.
    """
    global _client_instance

    api_url = os.environ.get('ERXES_API_URL', '')
    if not api_url:
        return None

    with _client_lock:
        if _client_instance is None:
            email = os.environ.get('ERXES_EMAIL', 'sathish@hevolve.ai')
            password = os.environ.get('ERXES_PASSWORD', 'Hertzai2021')
            _client_instance = ErxesCRMClient(api_url, email, password)
            logger.info('Erxes client initialized: %s', api_url)
        return _client_instance

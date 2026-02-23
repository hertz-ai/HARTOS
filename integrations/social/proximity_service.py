"""
HevolveSocial - Proximity & Missed Connections Service
Same Place Same Time geolocation encounters.
"""
import math
import logging
from datetime import datetime, timedelta

logger = logging.getLogger('hevolve_social')

PROXIMITY_RADIUS_M = 100  # Default detection radius
PING_TTL_HOURS = 24
MATCH_TTL_HOURS = 4
MISSED_TTL_DAYS = 7
MAX_MISSED_PER_DAY = 3
PING_COOLDOWN_SECONDS = 30
MATCH_COOLDOWN_HOURS = 4


class ProximityService:

    @staticmethod
    def haversine_distance(lat1, lon1, lat2, lon2):
        """Distance in meters between two GPS points."""
        R = 6371000  # Earth radius in meters
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlam = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    @staticmethod
    def bounding_box(lat, lon, radius_m):
        """Bounding box for efficient pre-filter (min_lat, max_lat, min_lon, max_lon)."""
        dlat = radius_m / 111320.0
        dlon = radius_m / (111320.0 * math.cos(math.radians(lat)))
        return (lat - dlat, lat + dlat, lon - dlon, lon + dlon)

    @staticmethod
    def update_location(db, user_id, lat, lon, accuracy):
        """Store location ping, detect nearby users, return match count."""
        from .models import LocationPing, User
        now = datetime.utcnow()

        # Rate limit: 1 ping per 30 seconds
        last_ping = db.query(LocationPing).filter(
            LocationPing.user_id == user_id,
            LocationPing.created_at > now - timedelta(seconds=PING_COOLDOWN_SECONDS)
        ).first()
        if last_ping:
            return {'nearby_count': ProximityService.get_nearby_count(db, user_id), 'rate_limited': True}

        # Store ping
        ping = LocationPing(
            user_id=user_id, lat=lat, lon=lon,
            accuracy_m=accuracy or 0.0,
            expires_at=now + timedelta(hours=PING_TTL_HOURS)
        )
        db.add(ping)

        # Update user's last known location
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            user.last_location_lat = lat
            user.last_location_lon = lon
            user.last_location_at = now

        db.flush()

        # Detect nearby users
        matches_created = ProximityService._detect_proximity(db, user_id, lat, lon, now)

        return {
            'nearby_count': ProximityService.get_nearby_count(db, user_id),
            'new_matches': matches_created,
            'rate_limited': False,
        }

    @staticmethod
    def _detect_proximity(db, user_id, lat, lon, now):
        """Find nearby users and create ProximityMatch records."""
        from .models import LocationPing, ProximityMatch
        min_lat, max_lat, min_lon, max_lon = ProximityService.bounding_box(lat, lon, PROXIMITY_RADIUS_M)

        # Bounding box pre-filter on recent pings (not expired, not self)
        candidates = db.query(LocationPing).filter(
            LocationPing.user_id != user_id,
            LocationPing.expires_at > now,
            LocationPing.lat >= min_lat, LocationPing.lat <= max_lat,
            LocationPing.lon >= min_lon, LocationPing.lon <= max_lon,
        ).all()

        # Dedupe by user_id (keep most recent ping per user)
        user_pings = {}
        for p in candidates:
            if p.user_id not in user_pings or p.created_at > user_pings[p.user_id].created_at:
                user_pings[p.user_id] = p

        matches_created = 0
        for other_id, ping in user_pings.items():
            dist = ProximityService.haversine_distance(lat, lon, ping.lat, ping.lon)
            if dist > PROXIMITY_RADIUS_M:
                continue

            # Canonical ordering: a_id < b_id
            a_id, b_id = (min(user_id, other_id), max(user_id, other_id))

            # Check cooldown: no pending/recent match between this pair
            recent = db.query(ProximityMatch).filter(
                ProximityMatch.user_a_id == a_id,
                ProximityMatch.user_b_id == b_id,
                ProximityMatch.created_at > now - timedelta(hours=MATCH_COOLDOWN_HOURS),
            ).first()
            if recent:
                continue

            match = ProximityMatch(
                user_a_id=a_id, user_b_id=b_id,
                lat=(lat + ping.lat) / 2,  # midpoint (never exposed to users)
                lon=(lon + ping.lon) / 2,
                distance_m=round(dist, 1),
                detected_at=now,
                expires_at=now + timedelta(hours=MATCH_TTL_HOURS),
            )
            db.add(match)
            matches_created += 1

            # Create notification for both users
            try:
                from .services import NotificationService
                for uid in [a_id, b_id]:
                    NotificationService.create(
                        db, user_id=uid, type='proximity_match',
                        message='Someone is nearby! Check your encounters.',
                    )
            except Exception:
                pass  # Notifications optional

        return matches_created

    @staticmethod
    def get_nearby_count(db, user_id):
        """Anonymous count of users with active pings within proximity."""
        from .models import LocationPing, User
        user = db.query(User).filter(User.id == user_id).first()
        if not user or not user.last_location_lat:
            return 0

        now = datetime.utcnow()
        min_lat, max_lat, min_lon, max_lon = ProximityService.bounding_box(
            user.last_location_lat, user.last_location_lon, PROXIMITY_RADIUS_M * 5)

        pings = db.query(LocationPing.user_id).filter(
            LocationPing.user_id != user_id,
            LocationPing.expires_at > now,
            LocationPing.lat >= min_lat, LocationPing.lat <= max_lat,
            LocationPing.lon >= min_lon, LocationPing.lon <= max_lon,
        ).distinct().all()

        count = 0
        for (uid,) in pings:
            # Get latest ping for this user
            p = db.query(LocationPing).filter(
                LocationPing.user_id == uid,
                LocationPing.expires_at > now
            ).order_by(LocationPing.created_at.desc()).first()
            if p:
                d = ProximityService.haversine_distance(
                    user.last_location_lat, user.last_location_lon, p.lat, p.lon)
                if d <= PROXIMITY_RADIUS_M * 5:
                    count += 1
        return count

    @staticmethod
    def get_matches(db, user_id, status=None):
        """Get proximity matches for a user."""
        from .models import ProximityMatch
        now = datetime.utcnow()
        q = db.query(ProximityMatch).filter(
            ProximityMatch.expires_at > now,
            ((ProximityMatch.user_a_id == user_id) | (ProximityMatch.user_b_id == user_id))
        )
        if status:
            q = q.filter(ProximityMatch.status == status)
        else:
            q = q.filter(ProximityMatch.status != 'expired')
        return [m.to_dict(viewer_id=user_id) for m in q.order_by(ProximityMatch.detected_at.desc()).all()]

    @staticmethod
    def reveal_self(db, match_id, user_id):
        """Reveal yourself to a proximity match. Returns updated match."""
        from .models import ProximityMatch, User
        now = datetime.utcnow()
        match = db.query(ProximityMatch).filter(
            ProximityMatch.id == match_id,
            ProximityMatch.expires_at > now,
        ).first()
        if not match:
            raise ValueError("Match not found or expired")

        is_a = user_id == match.user_a_id
        is_b = user_id == match.user_b_id
        if not is_a and not is_b:
            raise ValueError("Not your match")

        if match.status == 'matched':
            raise ValueError("Already matched")
        if match.status == 'expired':
            raise ValueError("Match expired")

        if is_a:
            if match.a_revealed_at:
                raise ValueError("Already revealed")
            match.a_revealed_at = now
            if match.status == 'pending':
                match.status = 'revealed_a'
            elif match.status == 'revealed_b':
                match.status = 'matched'
        else:
            if match.b_revealed_at:
                raise ValueError("Already revealed")
            match.b_revealed_at = now
            if match.status == 'pending':
                match.status = 'revealed_b'
            elif match.status == 'revealed_a':
                match.status = 'matched'

        # If both revealed, create an encounter
        if match.status == 'matched':
            try:
                from .encounter_service import EncounterService
                EncounterService.record_encounter(
                    db, match.user_a_id, match.user_b_id,
                    context_type='proximity',
                    context_id=match.id,
                    location_label=match.location_label or 'Nearby'
                )
                # Award Pulse to both
                try:
                    from .resonance_engine import ResonanceService
                    for uid in [match.user_a_id, match.user_b_id]:
                        ResonanceService.award_pulse(db, uid, 10, 'proximity_match', match.id,
                                                     'Matched with someone nearby!')
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"Failed to create encounter from proximity: {e}")

        # Get user info for matched state
        result = match.to_dict(viewer_id=user_id)
        if match.status == 'matched':
            a = db.query(User).filter(User.id == match.user_a_id).first()
            b = db.query(User).filter(User.id == match.user_b_id).first()
            if a:
                result['user_a'] = {'id': a.id, 'username': a.username, 'display_name': a.display_name, 'avatar_url': a.avatar_url}
            if b:
                result['user_b'] = {'id': b.id, 'username': b.username, 'display_name': b.display_name, 'avatar_url': b.avatar_url}

        return result

    @staticmethod
    def create_missed_connection(db, user_id, lat, lon, location_name, description, was_at_iso):
        """Create a missed connection post. Rate limited to 3/day."""
        from .models import MissedConnection
        now = datetime.utcnow()

        # Parse was_at
        try:
            was_at = datetime.fromisoformat(was_at_iso.replace('Z', '+00:00').replace('+00:00', ''))
        except (ValueError, AttributeError):
            raise ValueError("Invalid was_at datetime format")

        # Validate: must be within past 7 days
        if was_at > now:
            raise ValueError("was_at cannot be in the future")
        if (now - was_at).days > 7:
            raise ValueError("was_at must be within the past 7 days")

        # Rate limit: 3 per day
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        count_today = db.query(MissedConnection).filter(
            MissedConnection.user_id == user_id,
            MissedConnection.created_at >= today_start,
        ).count()
        if count_today >= MAX_MISSED_PER_DAY:
            raise ValueError(f"Maximum {MAX_MISSED_PER_DAY} missed connections per day")

        # Validate inputs
        if not location_name or len(location_name.strip()) < 3:
            raise ValueError("Location name must be at least 3 characters")
        if len(location_name) > 200:
            raise ValueError("Location name must be under 200 characters")
        if description and len(description) > 500:
            raise ValueError("Description must be under 500 characters")

        mc = MissedConnection(
            user_id=user_id, lat=lat, lon=lon,
            location_name=location_name.strip(),
            description=(description or '').strip(),
            was_at=was_at,
            expires_at=now + timedelta(days=MISSED_TTL_DAYS),
        )
        db.add(mc)
        db.flush()
        return mc.to_dict()

    @staticmethod
    def search_missed_connections(db, lat, lon, radius_m, limit=20, offset=0, exclude_user_id=None, sort='recent'):
        """Search missed connections within radius. Returns list + has_more."""
        from .models import MissedConnection
        now = datetime.utcnow()
        min_lat, max_lat, min_lon, max_lon = ProximityService.bounding_box(lat, lon, radius_m)

        q = db.query(MissedConnection).filter(
            MissedConnection.is_active == True,
            MissedConnection.expires_at > now,
            MissedConnection.lat >= min_lat, MissedConnection.lat <= max_lat,
            MissedConnection.lon >= min_lon, MissedConnection.lon <= max_lon,
        )
        if exclude_user_id:
            q = q.filter(MissedConnection.user_id != exclude_user_id)

        # Get all candidates for haversine filtering
        candidates = q.all()

        # Post-filter by exact distance
        results = []
        for mc in candidates:
            dist = ProximityService.haversine_distance(lat, lon, mc.lat, mc.lon)
            if dist <= radius_m:
                d = mc.to_dict(viewer_lat=lat, viewer_lon=lon)
                d['_distance'] = dist
                results.append(d)

        # Sort
        if sort == 'nearest':
            results.sort(key=lambda x: x['_distance'])
        elif sort == 'responses':
            results.sort(key=lambda x: x.get('response_count', 0), reverse=True)
        else:  # recent
            results.sort(key=lambda x: x.get('created_at', ''), reverse=True)

        # Paginate
        total = len(results)
        page = results[offset:offset + limit]
        for r in page:
            r.pop('_distance', None)

        return {'data': page, 'meta': {'total': total, 'has_more': offset + limit < total}}

    @staticmethod
    def get_my_missed_connections(db, user_id, limit=20, offset=0):
        """Get user's own missed connections."""
        from .models import MissedConnection
        q = db.query(MissedConnection).filter(
            MissedConnection.user_id == user_id,
        ).order_by(MissedConnection.created_at.desc())
        total = q.count()
        items = q.offset(offset).limit(limit).all()
        return {
            'data': [mc.to_dict() for mc in items],
            'meta': {'total': total, 'has_more': offset + limit < total}
        }

    @staticmethod
    def get_missed_with_responses(db, missed_id, viewer_id=None):
        """Get missed connection with all responses."""
        from .models import MissedConnection, MissedConnectionResponse, User
        mc = db.query(MissedConnection).filter(MissedConnection.id == missed_id).first()
        if not mc:
            raise ValueError("Missed connection not found")

        d = mc.to_dict()
        # Get poster info
        poster = db.query(User).filter(User.id == mc.user_id).first()
        if poster:
            d['user'] = {'id': poster.id, 'username': poster.username,
                         'display_name': poster.display_name, 'avatar_url': poster.avatar_url}

        # Get all responses with responder info
        responses = db.query(MissedConnectionResponse).filter(
            MissedConnectionResponse.missed_connection_id == missed_id,
        ).order_by(MissedConnectionResponse.created_at.desc()).all()

        resp_list = []
        for r in responses:
            rd = r.to_dict()
            responder = db.query(User).filter(User.id == r.responder_id).first()
            if responder:
                rd['responder'] = {'id': responder.id, 'username': responder.username,
                                   'display_name': responder.display_name, 'avatar_url': responder.avatar_url}
            resp_list.append(rd)

        d['responses'] = resp_list
        d['is_owner'] = viewer_id == mc.user_id if viewer_id else False
        return d

    @staticmethod
    def respond_to_missed(db, missed_id, user_id, message):
        """Add 'I was there too' response."""
        from .models import MissedConnection, MissedConnectionResponse
        mc = db.query(MissedConnection).filter(
            MissedConnection.id == missed_id,
            MissedConnection.is_active == True,
        ).first()
        if not mc:
            raise ValueError("Missed connection not found or expired")
        if mc.user_id == user_id:
            raise ValueError("Cannot respond to your own missed connection")

        # Check if already responded
        existing = db.query(MissedConnectionResponse).filter(
            MissedConnectionResponse.missed_connection_id == missed_id,
            MissedConnectionResponse.responder_id == user_id,
        ).first()
        if existing:
            raise ValueError("Already responded to this missed connection")

        if message and len(message) > 300:
            raise ValueError("Message must be under 300 characters")

        resp = MissedConnectionResponse(
            missed_connection_id=missed_id,
            responder_id=user_id,
            message=(message or '').strip(),
        )
        db.add(resp)
        mc.response_count = (mc.response_count or 0) + 1
        db.flush()
        return resp.to_dict()

    @staticmethod
    def accept_missed_response(db, missed_id, response_id, user_id):
        """Owner accepts a response -> creates encounter."""
        from .models import MissedConnection, MissedConnectionResponse
        mc = db.query(MissedConnection).filter(MissedConnection.id == missed_id).first()
        if not mc:
            raise ValueError("Missed connection not found")
        if mc.user_id != user_id:
            raise ValueError("Only the poster can accept responses")

        resp = db.query(MissedConnectionResponse).filter(
            MissedConnectionResponse.id == response_id,
            MissedConnectionResponse.missed_connection_id == missed_id,
        ).first()
        if not resp:
            raise ValueError("Response not found")
        if resp.status != 'pending':
            raise ValueError(f"Response already {resp.status}")

        resp.status = 'accepted'

        # Create encounter
        try:
            from .encounter_service import EncounterService
            EncounterService.record_encounter(
                db, mc.user_id, resp.responder_id,
                context_type='missed_connection',
                context_id=missed_id,
                location_label=mc.location_name
            )
        except Exception as e:
            logger.warning(f"Failed to create encounter from missed connection: {e}")

        # Award Pulse
        try:
            from .resonance_engine import ResonanceService
            for uid in [mc.user_id, resp.responder_id]:
                ResonanceService.award_pulse(db, uid, 5, 'missed_connection', missed_id,
                                             f'Connected at {mc.location_name}!')
        except Exception:
            pass

        return resp.to_dict()

    @staticmethod
    def delete_missed_connection(db, missed_id, user_id):
        """Remove own missed connection."""
        from .models import MissedConnection
        mc = db.query(MissedConnection).filter(MissedConnection.id == missed_id).first()
        if not mc:
            raise ValueError("Not found")
        if mc.user_id != user_id:
            raise ValueError("Not your missed connection")
        mc.is_active = False
        return {'deleted': True}

    @staticmethod
    def auto_suggest_locations(db, lat, lon, radius_m=5000):
        """Return popular location names from nearby recent missed connections."""
        from .models import MissedConnection
        from sqlalchemy import func as sqlfunc
        now = datetime.utcnow()
        min_lat, max_lat, min_lon, max_lon = ProximityService.bounding_box(lat, lon, radius_m)

        results = db.query(
            MissedConnection.location_name,
            sqlfunc.count(MissedConnection.id).label('count')
        ).filter(
            MissedConnection.is_active == True,
            MissedConnection.expires_at > now,
            MissedConnection.lat >= min_lat, MissedConnection.lat <= max_lat,
            MissedConnection.lon >= min_lon, MissedConnection.lon <= max_lon,
        ).group_by(MissedConnection.location_name).order_by(
            sqlfunc.count(MissedConnection.id).desc()
        ).limit(10).all()

        return [{'name': name, 'count': count} for name, count in results]

    @staticmethod
    def get_location_settings(db, user_id):
        """Get user's location sharing settings."""
        from .models import User
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise ValueError("User not found")
        return {
            'location_sharing_enabled': user.location_sharing_enabled or False,
            'has_location': user.last_location_lat is not None,
        }

    @staticmethod
    def update_location_settings(db, user_id, enabled):
        """Toggle location sharing."""
        from .models import User
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise ValueError("User not found")
        user.location_sharing_enabled = bool(enabled)
        if not enabled:
            user.last_location_lat = None
            user.last_location_lon = None
            user.last_location_at = None
        return {'location_sharing_enabled': user.location_sharing_enabled}

    @staticmethod
    def cleanup_expired(db):
        """Delete expired pings, deactivate expired missed connections, expire pending matches."""
        from .models import LocationPing, ProximityMatch, MissedConnection
        now = datetime.utcnow()

        # Delete expired pings
        deleted_pings = db.query(LocationPing).filter(LocationPing.expires_at <= now).delete()

        # Expire matches
        expired_matches = db.query(ProximityMatch).filter(
            ProximityMatch.expires_at <= now,
            ProximityMatch.status.in_(['pending', 'revealed_a', 'revealed_b'])
        ).update({'status': 'expired'}, synchronize_session='fetch')

        # Deactivate expired missed connections
        expired_missed = db.query(MissedConnection).filter(
            MissedConnection.expires_at <= now,
            MissedConnection.is_active == True,
        ).update({'is_active': False}, synchronize_session='fetch')

        logger.info(f"Proximity cleanup: {deleted_pings} pings, {expired_matches} matches, {expired_missed} missed connections")
        return {'pings': deleted_pings, 'matches': expired_matches, 'missed': expired_missed}

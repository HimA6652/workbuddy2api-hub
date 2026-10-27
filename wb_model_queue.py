"""Shared model/endpoint queue Gate for upstream codes 6020 and 6021.

Queue failures stay separate from Account cooldowns and affinity. Every real
request carries a generation ticket: late responses cannot erase newer queue
evidence or reopen a Gate after a newer probe has confirmed recovery.
"""
import json
import threading
import time

QUEUE_WAITING = 6020
QUEUE_FULL = 6021
QUEUE_CODES = frozenset((QUEUE_WAITING, QUEUE_FULL))
DEFAULT_RETRY_SECONDS = 5.0
MAX_RETRY_SECONDS = 60.0
MAX_BACKOFF_STREAK = 5
# Crash backstop, not a network timeout. Operators can configure longer header
# timeouts; a lease expiring never makes an old response current again.
PROBE_LEASE_SECONDS = 180.0
MAX_WAIT_HINT_SECONDS = 3600.0
MAX_SCOPES = 256
# Only JSON error bodies are decoded. This is larger than observed queue envelopes
# (including padded fixtures) while retaining a finite per-error memory bound.
QUEUE_ERROR_BODY_LIMIT = 8 * 1024 * 1024


def _integer(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        try:
            return int(value.strip())
        except (ValueError, OverflowError):
            return None
    return None


def parse_upstream_error(detail):
    """Return (6020|6021, data) from explicit JSON code locations only.

    retry_after is seconds; estimated_wait is milliseconds and is intentionally
    not added to or substituted for that hint. Prose alone never opens a Gate.
    """
    try:
        payload = json.loads(detail or "")
    except (TypeError, ValueError):
        return None, {}

    def visit(obj, depth=0):
        if not isinstance(obj, dict) or depth > 16:
            return None
        code = _integer(obj.get("code"))
        if code in QUEUE_CODES:
            data = obj.get("data")
            return code, data if isinstance(data, dict) else obj
        for key in ("error", "data"):
            found = visit(obj.get(key), depth + 1)
            if found is not None:
                return found
        return None

    return visit(payload) or (None, {})


def classify(detail):
    return parse_upstream_error(detail)


def queue_scope(account, model, chat_url=None, product=None):
    """Return the exact (realm, model, product, endpoint) Gate key.

    A transport attempt passes the same product snapshot that built chat_url.
    Account.product is mutable during an explicit/automatic product switch;
    reading it again here could attribute a response to a different endpoint.
    """
    if chat_url:
        # wb_proxy historically used /v2; wb_model_queue documents /v3.
        # Both identify the same chat product endpoint, never part of its key.
        for suffix in ("/v3/chat/completions", "/v2/chat/completions"):
            if chat_url.endswith(suffix):
                base = chat_url[:-len(suffix)]
                break
        else:
            base = chat_url
    else:
        base = account.chat_base_url()
    if product is None:
        product = getattr(account, "product", "") or ""
    return (str(account.realm), str(model), str(product), str(base))


def scope_key(scope):
    return tuple(scope)


class ModelQueueGate(object):
    """Lock-protected Gate with generation-checked real-request admissions.

    begin() returns (proceed, retry_after, ticket). A ticket is
    (generation, probe_token); probe_token is False for an ordinary request.
    False as the whole ticket means that admission has not happened yet.
    """

    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self.lock = threading.Lock()
        self.states = {}
        self.probes = {}

    @staticmethod
    def _delay(streak, retry_after=None):
        hint = _integer(retry_after)
        if hint is not None:
            return float(min(MAX_WAIT_HINT_SECONDS, max(1, hint)))
        step = min(MAX_BACKOFF_STREAK, max(0, int(streak) - 1))
        return min(MAX_RETRY_SECONDS, DEFAULT_RETRY_SECONDS * (2 ** step))

    @staticmethod
    def _fresh(row, now):
        return bool(row and now < row["until"])

    @staticmethod
    def _generation(row):
        return int(row.get("generation", 0)) if row else 0

    @staticmethod
    def _probe_token(ticket):
        return ticket[1] if isinstance(ticket, tuple) and len(ticket) == 2 else False

    def _retain(self):
        if len(self.states) <= MAX_SCOPES:
            return
        for key, _row in sorted(self.states.items(),
                                key=lambda item: item[1]["until"]):
            if key not in self.probes:
                del self.states[key]
            if len(self.states) <= MAX_SCOPES:
                break

    def _note_locked(self, key, retry_after):
        now = self.clock()
        row = self.states.get(key)
        # note() is used only by direct tests/administrative priming. Real
        # request failures use fail(), which invalidates only the matching lease.
        generation = self._generation(row) + 1
        streak = min(MAX_BACKOFF_STREAK + 1,
                     int(row["streak"]) + 1 if row else 1)
        delay = self._delay(streak, retry_after)
        until = min(max(now + delay, row["until"] if row else 0.0),
                    now + MAX_WAIT_HINT_SECONDS)
        self.states[key] = {"until": until, "streak": streak,
                            "generation": generation}
        # Fresh evidence invalidates every outstanding probe in this generation.
        # The next begin() will issue exactly one new lease after the new delay.
        self.probes.pop(key, None)
        self._retain()
        return max(1, int(round(until - now)))

    def note(self, scope, retry_after=None):
        """Record explicit fresh evidence; request paths should use fail()."""
        with self.lock:
            return self._note_locked(scope_key(scope), retry_after)

    def begin(self, scope):
        """Atomically admit a request and possibly issue one probe lease."""
        now = self.clock()
        with self.lock:
            key = scope_key(scope)
            row = self.states.get(key)
            generation = self._generation(row)
            if row is None or (row["streak"] == 0 and now >= row["until"]):
                return True, 0, (generation, False)
            if now < row["until"]:
                return False, max(1, int(round(row["until"] - now))), False
            probe = self.probes.get(key)
            if probe is not None:
                if now < probe["deadline"]:
                    return False, max(1, int(round(probe["deadline"] - now))), False
                del self.probes[key]
            token = object()
            self.probes[key] = {"token": token, "generation": generation,
                                "deadline": now + PROBE_LEASE_SECONDS}
            return True, 0, (generation, token)

    def _owns(self, key, ticket):
        probe_token = self._probe_token(ticket)
        row = self.probes.get(key)
        return bool(probe_token is not False and probe_token is not None and row
                    and row["token"] is probe_token
                    and row["generation"] == ticket[0]
                    and self._generation(self.states.get(key)) == ticket[0])

    def _current(self, key, ticket):
        return (isinstance(ticket, tuple) and len(ticket) == 2
                and self._generation(self.states.get(key)) == ticket[0])

    def _stale_wait(self, row, retry_after):
        now = self.clock()
        if self._fresh(row, now):
            return max(1, int(round(row["until"] - now)))
        # A closed tombstone is the opposite of evidence of congestion. A late
        # ignored failure gets the minimal client retry hint, never a new Gate.
        if row is not None and not row["streak"]:
            return 1
        return max(1, int(self._delay(1, retry_after)))

    def success(self, scope, ticket=False):
        """Close only the generation that this exact real request admitted."""
        with self.lock:
            key = scope_key(scope)
            if not self._current(key, ticket):
                return False
            if self._probe_token(ticket) is False:
                # A closed tombstone still rejects older queue errors. Ordinary
                # success does not infer recovery for another generation.
                return not self.probes.get(key) and not self._fresh(
                    self.states.get(key), self.clock())
            if not self._owns(key, ticket):
                return False
            row = self.states.get(key)
            self.states[key] = {"until": 0.0, "streak": 0,
                                "generation": self._generation(row) + 1}
            self.probes.pop(key, None)
            return True

    def fail(self, scope, ticket=False, retry_after=None):
        """Open only for a current admission; ignore late pre-Gate responses."""
        with self.lock:
            key = scope_key(scope)
            row = self.states.get(key)
            probe_token = self._probe_token(ticket)
            if probe_token is not False:
                if not self._owns(key, ticket):
                    return self._stale_wait(row, retry_after)
            elif not self._current(key, ticket):
                # Once a Gate exists, the current half-open real request owns
                # recovery. Every older ordinary response is stale: letting one
                # advance the generation would invalidate that newer probe and
                # reopen the endpoint even when the probe subsequently succeeds.
                return self._stale_wait(row, retry_after)
            return self._note_locked(key, retry_after)

    def abandon(self, scope, ticket):
        """Release only this probe; do not invent recovery or extra delay."""
        if self._probe_token(ticket) is False:
            return
        with self.lock:
            key = scope_key(scope)
            if self._owns(key, ticket):
                self.probes.pop(key, None)

    def check(self, scope):
        """Read-only fallback check, including an active half-open lease."""
        now = self.clock()
        with self.lock:
            key = scope_key(scope)
            row = self.states.get(key)
            if self._fresh(row, now):
                return False, max(1, int(round(row["until"] - now)))
            probe = self.probes.get(key)
            if probe and now < probe["deadline"]:
                return False, max(1, int(round(probe["deadline"] - now)))
            return True, 0

    def snapshot(self):
        now = self.clock()
        out = []
        with self.lock:
            for scope, row in list(self.states.items()):
                probe = self.probes.get(scope)
                half_open = bool(probe and now < probe["deadline"])
                if not self._fresh(row, now) and not half_open:
                    continue
                wait = max(row["until"] - now,
                           probe["deadline"] - now if half_open else 0.0)
                out.append({"realm": scope[0], "model": scope[1],
                            "product": scope[2], "endpoint": scope[3],
                            "phase": "half_open" if half_open else "open",
                            "retry_after": max(1, int(round(wait))),
                            "backoff_streak": row["streak"]})
        out.sort(key=lambda row: (row["realm"], row["model"],
                                  row["product"], row["endpoint"]))
        return out

    def clear(self):
        with self.lock:
            self.states.clear()
            self.probes.clear()

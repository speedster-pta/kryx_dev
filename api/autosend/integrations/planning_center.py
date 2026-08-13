import httpx

BASE_URL = "https://api.planningcenteronline.com"

class PlanningCenterClient:

    def __init__(self, token_id: str, token_secret: str, campus_id: str):
        self.campus_id = campus_id
        self.client = httpx.AsyncClient(
            base_url=BASE_URL,
            auth=(token_id, token_secret),
            timeout=30,
        )

    async def get_me(self):
        response = await self.client.get("/people/v2/me")
        response.raise_for_status()
        return response.json()

    async def get_eligible_signups(self) -> list[dict]:
        """
        Returns ALL unarchived Pretoria-campus signups (both free and
        paid), tagged with is_paid so the poller can route to the correct
        WhatsApp template.

        Result shape: [{"id": ..., "name": ..., "is_paid": bool}, ...]
        """
        eligible = []
        url = "/registrations/v2/signups"
        params = {
            "filter": "unarchived",
            "include": "campuses,selection_types",
            "per_page": 100,
        }

        while url:
            response = await self.client.get(
                url, params=params if url == "/registrations/v2/signups" else None
            )
            response.raise_for_status()
            payload = response.json()
            included = payload.get("included", [])

            campuses_by_signup: dict[str, set[str]] = {}
            selection_types_by_signup: dict[str, list[dict]] = {}

            for signup in payload.get("data", []):
                sid = signup["id"]
                rels = signup.get("relationships", {})
                campus_ids = {
                    c["id"] for c in rels.get("campuses", {}).get("data", []) or []
                }
                selection_type_ids = {
                    s["id"] for s in rels.get("selection_types", {}).get("data", []) or []
                }
                campuses_by_signup[sid] = campus_ids
                selection_types_by_signup[sid] = [
                    item
                    for item in included
                    if item["type"] == "SelectionType" and item["id"] in selection_type_ids
                ]

            for signup in payload.get("data", []):
                sid = signup["id"]
                if self.campus_id not in campuses_by_signup.get(sid, set()):
                    continue
                prices = [
                    st["attributes"]["price_cents"]
                    for st in selection_types_by_signup.get(sid, [])
                ]
                is_paid = bool(prices) and max(prices) > 0
                eligible.append(
                    {
                        "id": sid,
                        "name": signup["attributes"]["name"],
                        "is_paid": is_paid,
                    }
                )

            next_link = payload.get("links", {}).get("next")
            url = next_link if next_link else None

        return eligible

    async def get_registrations_for_signup(
        self, signup_id: str, stop_at_registration_id: str | None
    ) -> list[dict]:
        """
        Pages a signup's registrations newest-first, stopping as soon as
        we hit a registration ID the caller has already processed. This
        avoids relying on created_at filtering, which PCO's Registration
        resource doesn't appear to support server-side.

        Returns newly-seen registrations, oldest-first (so callers process
        them in the order they arrived).
        """
        found: list[dict] = []
        base_url = f"/registrations/v2/signups/{signup_id}/registrations"
        url = base_url
        params = {"order": "-created_at", "per_page": 25}

        while url:
            response = await self.client.get(
                url, params=params if url == base_url else None
            )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data", [])

            hit_boundary = False
            for reg in data:
                if reg["id"] == stop_at_registration_id:
                    hit_boundary = True
                    break
                found.append(reg)

            if hit_boundary or not data:
                break

            next_link = payload.get("links", {}).get("next")
            url = next_link if next_link else None

        found.reverse()  # oldest-first
        return found

    async def get_registration_detail(self, registration_id: str) -> dict:
        response = await self.client.get(
            f"/registrations/v2/registrations/{registration_id}",
            params={"include": "registrant_contact"},
        )
        response.raise_for_status()
        return response.json()

    async def get_person_phone(self, person_id: str) -> str | None:
        response = await self.client.get(f"/people/v2/people/{person_id}/phone_numbers")
        response.raise_for_status()
        data = response.json().get("data", [])
        if not data:
            return None
        primary = next((p for p in data if p["attributes"].get("primary")), data[0])
        return primary["attributes"].get("e164")

    async def get_person(self, person_id: str) -> dict:
        response = await self.client.get(f"/people/v2/people/{person_id}")
        response.raise_for_status()
        return response.json()

    # ---- Services API (Serving Reminders automation) ----
    # Same org-wide PAT as everything above - PCO Personal Access Tokens
    # aren't scoped to a single API product, so no separate credentials
    # are needed to call services/v2 alongside people/v2 and
    # registrations/v2.

    async def get_service_types(self) -> list[dict]:
        """Returns every Service Type in the organization (e.g. "Sunday
        Service", "Youth Service") for the rule-editor dropdown. Services
        API has no campus filter on service_types itself - service types
        aren't campus-scoped, plans under them are (via their own
        relationships) - so this deliberately does not filter by
        self.campus_id."""
        service_types = []
        url = "/services/v2/service_types"
        params = {"per_page": 100}
        while url:
            response = await self.client.get(url, params=params if url == "/services/v2/service_types" else None)
            response.raise_for_status()
            payload = response.json()
            for st in payload.get("data", []):
                service_types.append({"id": st["id"], "name": st["attributes"]["name"]})
            url = payload.get("links", {}).get("next")
        return service_types

    async def get_campus_name(self, campus_id: str) -> str:
        """Campus name from the People API - used to match against PCO
        Services folder names, since Services folders' own `campus`
        relationship (see get_service_types_for_campus below) turned out
        to be set on only a handful of folders org-wide, not a reliable
        signal on its own."""
        response = await self.client.get(f"/people/v2/campuses/{campus_id}")
        response.raise_for_status()
        return response.json()["data"]["attributes"]["name"]

    async def get_service_types_for_campus(self, campus_id: str) -> list[dict]:
        """Scopes the org-wide service type list down to one campus by
        matching PCO Services folder names against the campus's name in
        People, e.g. a folder literally named "Pretoria" for the
        Pretoria campus. Services v2 Folders do have a formal `campus`
        relationship, but in this org it's only populated on a handful of
        folders - most campus folders (including "Pretoria" itself) have
        no campus relationship set at all - so name matching is the
        reliable signal here, not the relationship.

        Folders nest (via `parent`), and only the top-level, campus-named
        folder is expected to carry the name match - so once a match is
        found, this walks that folder's full subtree and pulls service
        types from every folder in it, not just the top match itself."""
        campus_name = (await self.get_campus_name(campus_id)).strip().lower()

        folders = []
        url = "/services/v2/folders"
        params = {"per_page": 100}
        while url:
            response = await self.client.get(url, params=params if url == "/services/v2/folders" else None)
            response.raise_for_status()
            payload = response.json()
            for f in payload.get("data", []):
                parent_rel = f.get("relationships", {}).get("parent", {}).get("data")
                folders.append({
                    "id": f["id"],
                    "name": f["attributes"].get("name") or "",
                    "parent_id": parent_rel["id"] if parent_rel else None,
                })
            url = payload.get("links", {}).get("next")

        children_by_parent: dict[str | None, list[str]] = {}
        for folder in folders:
            children_by_parent.setdefault(folder["parent_id"], []).append(folder["id"])

        in_scope: set[str] = set()
        stack = [f["id"] for f in folders if f["name"].strip().lower() == campus_name]
        while stack:
            folder_id = stack.pop()
            if folder_id in in_scope:
                continue
            in_scope.add(folder_id)
            stack.extend(children_by_parent.get(folder_id, []))

        service_types: dict[str, dict] = {}  # keyed by id to dedupe
        for folder_id in in_scope:
            base_url = f"/services/v2/folders/{folder_id}/service_types"
            url = base_url
            params = {"per_page": 100}
            while url:
                response = await self.client.get(url, params=params if url == base_url else None)
                response.raise_for_status()
                payload = response.json()
                for st in payload.get("data", []):
                    service_types[st["id"]] = {"id": st["id"], "name": st["attributes"]["name"]}
                url = payload.get("links", {}).get("next")

        return sorted(service_types.values(), key=lambda st: st["name"])

    async def get_next_plan(self, service_type_id: str) -> dict | None:
        """The soonest future-dated Plan under this Service Type, or None
        if nothing is scheduled yet. order=sort_date with filter=future
        already returns plans nearest-first, so the first result is what
        "the coming weekend's service" resolves to at fire time - no local
        date math needed here."""
        response = await self.client.get(
            f"/services/v2/service_types/{service_type_id}/plans",
            params={"filter": "future", "order": "sort_date", "per_page": 1},
        )
        response.raise_for_status()
        data = response.json().get("data", [])
        if not data:
            return None
        plan = data[0]
        return {
            "id": plan["id"],
            "title": plan["attributes"].get("title") or "",
            "dates": plan["attributes"].get("dates", ""),
            "sort_date": plan["attributes"].get("sort_date"),
        }

    async def get_plan(self, service_type_id: str, plan_id: str) -> dict | None:
        """Fetches one specific, already-known Plan by ID directly - unlike
        get_next_plan/get_upcoming_plans, which filter+page a *list* of
        plans to figure out what's upcoming. Used by the deferred serving
        reminder retry path (services/serving_reminder.py::
        retry_deferred_plan), which already has plan_id from
        serving_reminder_log and just needs its current title/dates
        refreshed, not "what's next".

        Returns None if the plan no longer exists (deleted/cancelled in
        PCO since the original deferral) rather than raising - same
        "nothing found" contract as get_next_plan, so callers handle both
        the same way: not an error, just nothing to retry against.
        """
        response = await self.client.get(
            f"/services/v2/service_types/{service_type_id}/plans/{plan_id}"
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        plan = response.json()["data"]
        return {
            "id": plan["id"],
            "title": plan["attributes"].get("title") or "",
            "dates": plan["attributes"].get("dates", ""),
            "sort_date": plan["attributes"].get("sort_date"),
        }

    async def get_upcoming_plans(self, service_type_id: str, days_ahead: int) -> list[dict]:
        """Every future-dated Plan under this Service Type whose sort_date
        falls within the next `days_ahead` days - the 'all events in the
        next N days' rule mode, as opposed to get_next_plan's single
        soonest-plan lookup. The Plans endpoint has no server-side "within
        N days" filter, only filter=future, so this pages through
        order=sort_date results (nearest-first, same as get_next_plan) and
        stops as soon as a plan's sort_date passes the cutoff, rather than
        paging through the entire future list every time."""
        from datetime import datetime, timedelta, timezone as dt_timezone

        cutoff = datetime.now(dt_timezone.utc) + timedelta(days=days_ahead)
        plans: list[dict] = []
        base_url = f"/services/v2/service_types/{service_type_id}/plans"
        url = base_url
        params = {"filter": "future", "order": "sort_date", "per_page": 100}
        while url:
            response = await self.client.get(url, params=params if url == base_url else None)
            response.raise_for_status()
            payload = response.json()
            reached_cutoff = False
            for plan in payload.get("data", []):
                sort_date = plan["attributes"].get("sort_date")
                parsed = None
                if sort_date:
                    try:
                        parsed = datetime.fromisoformat(sort_date.replace("Z", "+00:00"))
                    except ValueError:
                        parsed = None
                if parsed is not None and parsed > cutoff:
                    reached_cutoff = True
                    break
                plans.append({
                    "id": plan["id"],
                    "title": plan["attributes"].get("title") or "",
                    "dates": plan["attributes"].get("dates", ""),
                    "sort_date": sort_date,
                })
            if reached_cutoff:
                break
            url = payload.get("links", {}).get("next")
        return plans

    async def get_plan_team_members(self, service_type_id: str, plan_id: str) -> list[dict]:
        """Everyone scheduled on this Plan, with their position/team and
        scheduling status. team_members' `status` attribute is one of
        "U" (unconfirmed), "C" (confirmed), or "D" (declined) - the
        caller (services/serving_reminder.py) maps status_filter against
        this raw code rather than this method interpreting it, so the
        filtering rule lives in one place."""
        team_members = []
        url = f"/services/v2/service_types/{service_type_id}/plans/{plan_id}/team_members"
        params = {"include": "person", "per_page": 100}
        while url:
            response = await self.client.get(
                url,
                params=params if url == f"/services/v2/service_types/{service_type_id}/plans/{plan_id}/team_members" else None,
            )
            response.raise_for_status()
            payload = response.json()
            for tm in payload.get("data", []):
                attrs = tm["attributes"]
                person_rel = tm.get("relationships", {}).get("person", {}).get("data")
                team_members.append(
                    {
                        "person_id": person_rel["id"] if person_rel else None,
                        "name": attrs.get("name", ""),
                        "team_position_name": attrs.get("team_position_name", ""),
                        "status": attrs.get("status"),  # "U" | "C" | "D"
                    }
                )
            url = payload.get("links", {}).get("next")
        return team_members

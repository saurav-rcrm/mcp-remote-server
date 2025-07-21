# recruitcrm_mcp.py  –  MCP wrapper that returns a compact payload
#
# 1) pip install httpx python-dotenv "mcp[cli]"
# 2) echo 'RCRM_TOKEN=<your-token>' > .env
# 3) python recruitcrm_mcp.py   (Claude Desktop autostarts it via stdio)

import os, sys, datetime as dt, json, httpx
from typing import Annotated, Optional, List, Dict, Any
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# ──────────────────────────────────────────────────────────────
# 0.  Env & constants
# ──────────────────────────────────────────────────────────────
load_dotenv()
API_TOKEN = os.getenv("RCRM_TOKEN")
if not API_TOKEN:
    sys.exit("❌  Set RCRM_TOKEN in your environment or .env file")

HEADERS = {
    "Authorization": f"Bearer {API_TOKEN}",
    "Content-Type":  "application/json",
    "Origin":        "https://app.recruitcrm.io"
}
BASE_URL = "https://albatross.recruitcrm.io/v1/reports/search/get"

DEBUG = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")
def log(*a):
    if DEBUG:
        print("[recruitcrm_mcp]", *a, file=sys.stderr)

# ──────────────────────────────────────────────────────────────
# 1.  Dynamic maps from APIs
# ──────────────────────────────────────────────────────────────

# Cache for dynamic data
_user_map_cache = None
_stage_map_cache = None

async def fetch_hiring_stages() -> Dict[str, int]:
    """Fetch hiring stages from the API and return a label->id mapping."""
    global _stage_map_cache
    if _stage_map_cache is not None:
        return _stage_map_cache
    
    url = "https://hiring-pipeline.recruitcrm.io/v1/pipelines/list"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=HEADERS)
        resp.raise_for_status()
        data = resp.json()
        
        # Get the master hiring pipeline stages
        master_pipeline = data.get("default-pipeline", {})
        hiring_stages = master_pipeline.get("hiring_stages", [])
        
        # Create label->id mapping
        stage_map = {}
        for stage in hiring_stages:
            label = stage.get("label", "").strip()
            stage_id = stage.get("id")
            if label and stage_id is not None:
                stage_map[label.lower()] = stage_id
        
        _stage_map_cache = stage_map
        return stage_map

async def fetch_users() -> Dict[str, int]:
    """Fetch users from the API and return a name->id mapping."""
    global _user_map_cache
    if _user_map_cache is not None:
        return _user_map_cache
    
    url = "https://albatross.recruitcrm.io/v1/global/get-users-for-rpr?report=recruiter"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, headers=HEADERS, json={})
        resp.raise_for_status()
        data = resp.json().get("data", [])
        
        # Create name->id mapping for active users
        user_map = {}
        for user in data:
            if user.get("userstatus") == 0:  # Active users only
                name = user.get("name", "").strip()
                user_id = user.get("id")
                if name and user_id is not None:
                    user_map[name.lower()] = user_id
        
        _user_map_cache = user_map
        return user_map

def label_to_id(label: Optional[str], mapping: Dict[str, int]) -> Optional[int]:
    if not label: return None
    key = label.lower().strip()
    if key in mapping: return mapping[key]
    for k, v in mapping.items():
        if key in k: return v
    return None

# ──────────────────────────────────────────────────────────────
# 2.  Build Recruit CRM request body
# ──────────────────────────────────────────────────────────────
def iso_to_epoch(date_str: str) -> str:
    ts = int(dt.datetime.fromisoformat(date_str)
             .replace(tzinfo=dt.timezone.utc).timestamp())
    return str(ts)                       # API expects *string* epoch seconds

async def build_body(args: Dict[str, Any]) -> Dict[str, Any]:
    columns = {
        "stagedate": {
            "entity": "assignjobcandidate",
            "field":  "stagedate",
            "type":   "date",
            "filter_type": "range",
            "filter_value":        iso_to_epoch(args["stage_from"]),
            "filter_value_second": iso_to_epoch(args["stage_to"])
        }
    }

    # Fetch dynamic maps
    user_map = await fetch_users()
    stage_map = await fetch_hiring_stages()

    upd_id = label_to_id(args.get("updated_by"), user_map)
    if upd_id is not None:
        columns["updatedby"] = {
            "entity": "candidate","field":"updatedby","pseudo":"True",
            "type":"dropdown","filter_type":"is","filter_value":str(upd_id)
        }

    stage_id = label_to_id(args.get("hiring_stage"), stage_map)
    if stage_id is not None:
        columns["candidatestatusid"] = {
            "entity":"candidate","field":"candidatestatusid",
            "type":"dropdown","filter_type":"is","filter_value":str(stage_id)
        }

    if args.get("job_or_keywords"):
        columns["jobname"] = {
            "entity":"assignjobcandidate","field":"jobname","type":"text",
            "filter_type":"or_search",
            "filter_value": ",".join(s.strip() for s in args["job_or_keywords"])
        }

    if args.get("current_stage"):
        columns["currenthiringstage"] = {
            "entity":"candidate","field":"currenthiringstage",
            "type":"multiselect","filter_type":"contains",
            "filter_value": args["current_stage"]
        }
    if args.get("company"):
        columns["companyname"] = {
            "entity":"company","field":"companyname","type":"text",
            "filter_type":"contains","filter_value": args["company"]
        }

    def add_not_contains(field_key, entity, value):
        columns[field_key] = {
            "entity": entity,"field":field_key,"type":"text",
            "filter_type":"not_contains","filter_value": value
        }
    if args.get("company_not_contains"):
        add_not_contains("companyname", "company",
                         args["company_not_contains"])
    if args.get("current_stage_not_contains"):
        add_not_contains("currenthiringstage", "candidate",
                         args["current_stage_not_contains"])
    if args.get("hiring_stage_not_contains"):
        add_not_contains("candidatestatusid", "candidate",
                         args["hiring_stage_not_contains"])
    if args.get("job_not_contains"):
        add_not_contains("jobname", "assignjobcandidate",
                         args["job_not_contains"])

    body = {
        "page":      args.get("page", 1),
        "page_size": args.get("page_size", 50),
        "sort_by":   "stagedate",
        "sortOrder": "desc",
        "andsearch": [],
        "orsearch":  [],
        "notsearch": [],
        "columns":   columns,
        "fullTextSearch": True
    }
    log("▶️  Request body", json.dumps(body)[:500], "…")
    return body

# ──────────────────────────────────────────────────────────────
# 3.  Summarise Recruit CRM response (match Node code)
# ──────────────────────────────────────────────────────────────
def summarise(args: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    recs = payload.get("records") or []
    candidates = [{
        "name":    r.get("candidatename") or "(name N/A)",
        "job":     r.get("jobname")       or "(job N/A)",
        "company": r.get("companyname")   or "(company N/A)",
        "url":     f"https://app.recruitcrm.io/candidate/{r.get('slug')}",
        "candidatestatus": r.get("candidatestatus"),
        "currenthiringstage": r.get("currenthiringstage")
    } for r in recs]

    s = []
    if args.get("hiring_stage"):              s.append(f'stage = "{args["hiring_stage"]}"')
    if args.get("hiring_stage_not_contains"): s.append(f'stage NOT "{args["hiring_stage_not_contains"]}"')
    if args.get("company"):                   s.append(f'company ~ "{args["company"]}"')
    if args.get("company_not_contains"):      s.append(f'company NOT "{args["company_not_contains"]}"')
    if args.get("current_stage"):             s.append(f'current stage ~ "{args["current_stage"]}"')
    if args.get("current_stage_not_contains"):s.append(f'current stage NOT "{args["current_stage_not_contains"]}"')
    if args.get("job_or_keywords"):           s.append(f'job contains any of [{", ".join(args["job_or_keywords"])}]')
    if args.get("job_not_contains"):          s.append(f'job NOT "{args["job_not_contains"]}"')
    if args.get("updated_by"):                s.append(f'updated by {args["updated_by"]}')
    summary = ", ".join(s) if s else "no extra filters"

    return {
        "count":   payload.get("filtered_count", len(candidates)),
        "summary": summary,
        "candidates": candidates
    }

# ──────────────────────────────────────────────────────────────
# 4.  MCP server + tool
# ──────────────────────────────────────────────────────────────
mcp = FastMCP("recruitcrm")
app = mcp.asgi_app()


@mcp.tool()
async def candidate_job_assignment_search(
    stage_from: Annotated[str,  "Start date YYYY-MM-DD"],
    stage_to:   Annotated[str,  "End date YYYY-MM-DD"],

    hiring_stage:  Annotated[Optional[str], "Pipeline stage label (e.g., 'Submitted to client'). Mapped to API field 'candidatestatusid'."] = None,
    updated_by:    Annotated[Optional[str], "Recruiter name"]       = None,

    job_or_keywords:            Annotated[Optional[List[str]], "Job keywords (OR)"] = None,
    company_not_contains:       Annotated[Optional[str], "Exclude company contains"] = None,
    current_stage_not_contains: Annotated[Optional[str], "Exclude current stage contains (mapped to API field 'currenthiringstage')"] = None,
    hiring_stage_not_contains:  Annotated[Optional[str], "Exclude pipeline stage contains"] = None,
    job_not_contains:           Annotated[Optional[str], "Exclude job title contains"] = None,

    company:        Annotated[Optional[str], "Company contains"]       = None,
    current_stage:  Annotated[Optional[str], "Current stage contains (mapped to API field 'currenthiringstage')"] = None,

    page:      Annotated[int, "Page index"]  = 1,
    page_size: Annotated[int, "Rows per page"] = 50
) -> dict:
    """
    Use for: Searching for candidates by job assignment, pipeline stage, recruiter, company, job, etc.

    Filter field mapping:
    - hiring_stage: The pipeline stage the candidate was at (mapped to API field 'candidatestatusid').
    - current_stage: The candidate's current stage (mapped to API field 'currenthiringstage').
    - current_stage_not_contains: Exclude candidates whose current stage matches this value.
    - job_or_keywords: Filter by job title or keywords.
    - stage_from, stage_to: Date range for the stage event.

    Example filter dict for:
    "Show all candidates who were submitted to client for Business Analyst jobs in the last year but not placed":
    {
        "hiring_stage": "Submitted to client",
        "current_stage_not_contains": "Placed",
        "job_or_keywords": ["Business Analyst"],
        "stage_from": "2023-07-10",
        "stage_to": "2024-07-10"
    }
    """
    all_args = locals()
    body = await build_body(all_args)

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(BASE_URL, headers=HEADERS, json=body)
        if resp.status_code >= 400:
            print("\n── Recruit CRM error", resp.status_code, "──",
                  resp.text[:1000], file=sys.stderr)
        resp.raise_for_status()
        data = resp.json().get("data", resp.json())
        return summarise(all_args, data)

TEAM_PERFORMANCE_URL = "https://report.recruitcrm.io/v1/reports/team-performance-report"
TEAM_PERFORMANCE_HEADERS = {  # You may want to move this to .env for security
    "Authorization": f"Bearer {API_TOKEN}",
    "Content-Type": "application/json"
}

KPI_LIST_URL = "https://report.recruitcrm.io/v1/reports-kpi?report_type=1"
KPI_LIST_HEADERS = {
    "Authorization": f"Bearer {API_TOKEN}",
    "Content-Type": "application/json"
}

async def fetch_kpi_list() -> list:
    """Fetch the available KPIs from the RecruitCRM API."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(KPI_LIST_URL, headers=KPI_LIST_HEADERS)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return data

@mcp.tool()
async def get_available_hiring_stages() -> dict:
    """
    Returns the list of available hiring stages (id and label) from the master hiring pipeline.
    Use this tool to get the list of stages for filtering candidate reports or team performance reports.
    """
    stage_map = await fetch_hiring_stages()
    stages = [{"id": stage_id, "label": label} for label, stage_id in stage_map.items()]
    return {"stages": stages}

@mcp.tool()
async def get_available_users() -> dict:
    """
    Returns the list of available active users (id and name) for filtering candidate reports.
    Use this tool to get the list of recruiters for filtering by updated_by.
    """
    user_map = await fetch_users()
    users = [{"id": user_id, "name": name} for name, user_id in user_map.items()]
    return {"users": users}

@mcp.tool()
async def get_available_kpis() -> list:
    """Return the available KPIs (value, label, checked) for team performance reports."""
    original_kpi_list = await fetch_kpi_list()
    filtered_kpis = [
        {"value": kpi["value"], "label": kpi["label"]}
        for kpi in original_kpi_list
    ]
    return filtered_kpis

USERS_URL = "https://albatross.recruitcrm.io/v1/global/get-users-for-rpr?report=recruiter"
USERS_HEADERS = {
    "Authorization": f"Bearer {API_TOKEN}",
    "Content-Type": "application/json"
}

async def fetch_active_recruiters() -> list:
    """Fetch the list of active recruiters (userstatus=0) with id and name."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(USERS_URL, headers=USERS_HEADERS, json={})
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return [{"id": u["id"], "name": u["name"]} for u in data if u.get("userstatus") == 0]

@mcp.tool()
async def get_active_recruiters() -> list:
    """Return the list of active recruiters (id and name only)."""
    return await fetch_active_recruiters()

@mcp.tool()
async def get_team_performance_report(
    recruiter_names_or_ids: Annotated[list, "List of recruiter names or IDs (int or str)"],
    kpis_to_include: Annotated[list, "List of KPI values or labels to include (case-insensitive)"],
    from_date: Annotated[int, "Start date (epoch seconds)"],
    to_date: Annotated[int, "End date (epoch seconds)"],
    team_ids: Annotated[Optional[list], "List of team IDs (int)"] = None
) -> dict:
    """
    Use for: KPI counts and performance metrics (e.g., how many notes, jobs, calls, placements, etc.) for recruiters or teams.
    get_active_recruiters & get_available_kpis are tools are mandatory to be used to get the list of recruiters and KPIs for the get_team_performance_report tool.
    Example: "How many notes did Sean add last year?" or "Show me the number of placements per recruiter this month."
    Do NOT use for: Listing individual candidates or their details.
    """
    # Fetch all available KPIs
    kpi_list = await fetch_kpi_list()
    kpis_to_include_set = set([str(k).lower().strip() for k in kpis_to_include])
    # Exclude 'email1' and 'email2' from the kpi_lists entirely
    kpi_lists = []
    for kpi in kpi_list:
        kpi_value = str(kpi.get("value", "")).lower().strip()
        kpi_label = str(kpi.get("label", "")).lower().strip()
        if kpi_value in ("email1", "email2"):
            continue  # skip forbidden KPIs
        # Only include KPIs that are requested
        if kpi_value in kpis_to_include_set or kpi_label in kpis_to_include_set:
            kpi_lists.append({
                "value": kpi["value"],
                "label": kpi["label"],
                "checked": True
            })
    # Fetch recruiter list and resolve names/IDs
    recruiter_list = await fetch_active_recruiters()
    name_to_id = {u["name"].lower().strip(): u["id"] for u in recruiter_list}
    valid_ids = {u["id"] for u in recruiter_list}
    recruiter_ids = []
    for r in recruiter_names_or_ids:
        if isinstance(r, int) or (isinstance(r, str) and r.isdigit()):
            rid = int(r)
            if rid not in valid_ids:
                raise ValueError(f"Recruiter ID {rid} is not active or not found.")
            recruiter_ids.append(rid)
        else:
            rname = str(r).lower().strip()
            if rname not in name_to_id:
                raise ValueError(f"Recruiter name '{r}' not found among active recruiters.")
            recruiter_ids.append(name_to_id[rname])
    body = {
        "recruiter_ids": recruiter_ids,
        "kpi_lists": kpi_lists,
        "from_date": from_date,
        "to_date": to_date,
        "team_ids": team_ids or []
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(TEAM_PERFORMANCE_URL, headers=TEAM_PERFORMANCE_HEADERS, json=body)
        if resp.status_code >= 400:
            print("\n── Team Performance Report error", resp.status_code, "──", resp.text[:1000], file=sys.stderr)
        resp.raise_for_status()
        data = resp.json().get("data", resp.json())
        summary = {}
        for rid, kpis in data.items():
            summary[rid] = {k: v for k, v in kpis.items() if isinstance(v, (int, float))}
            summary[rid]["recruiter_name"] = kpis.get("recruiter_name")
        return {"summary": summary}

# ──────────────────────────────────────────────────────────────
# 5.  Additional MCP tools: Global Search, Create Meeting, Gmeet Link
# ──────────────────────────────────────────────────────────────

GLOBAL_SEARCH_URL = "https://albatross.recruitcrm.io/v1/global/search-entity"
MEETING_URL = "https://albatross.recruitcrm.io/v1/meetings"
GMEET_URL = "https://albatross.recruitcrm.io/v1/conference-settings/g-meet"

# Helper to filter global search results
# Update: keep 'id' and 'srno' (renamed as 'srno') in the filtered result
_DEF_GLOBAL_SEARCH_REMOVE = {"firstname", "phone", "mlink", "photo"}
def filter_global_search_results(results):
    filtered = []
    for r in results:
        entry = {k: v for k, v in r.items() if k not in _DEF_GLOBAL_SEARCH_REMOVE}
        # Always include 'id' if present
        if "id" in r:
            entry["id"] = r["id"]
        # Include 'srno' as 'srno' if present
        if "srno" in r:
            entry["srno"] = r["srno"]
        filtered.append(entry)
    return filtered

# Helper to filter meeting creation response (keep only LLM-relevant fields)
def filter_meeting_response(data):
    # Only keep a minimal set of fields for LLM
    appointment = data.get("appointment", {})
    keep_fields = [
        "title", "description", "reminder", "address", "relatedto", "relatedtotypeid",
        "startdate", "enddate", "notetype", "type", "ownerid", "creatorname",
        "relatedtoname", "collaborator_user_ids", "collaborator_team_ids"
    ]
    filtered = {k: v for k, v in appointment.items() if k in keep_fields}
    # Optionally, add candidate summary if present
    if "candidate" in appointment:
        candidate = appointment["candidate"]
        filtered["candidate"] = {
            "slug": candidate.get("slug"),
            "emailid": candidate.get("emailid"),
            "profilepic": candidate.get("profilepic"),
            "locality": candidate.get("locality"),
            "languageskills": candidate.get("languageskills"),
            "position": candidate.get("position"),
            "relatedtoname": appointment.get("relatedtoname"),
        }
    return filtered

# Helper to filter Gmeet response
def filter_gmeet_response(data):
    return {"meeting_link": data.get("meeting_link")}

@mcp.tool()
async def global_search(
    search: Annotated[str, "Search string (e.g. name, email, etc.)"],
    candidates: Annotated[bool, "Search candidates"] = True,
    contacts: Annotated[bool, "Search contacts"] = True,
    companies: Annotated[bool, "Search companies"] = False,
    jobs: Annotated[bool, "Search jobs"] = False,
    deals: Annotated[bool, "Search deals"] = False
) -> dict:
    """
    Use for: Searching across candidates, contacts, companies, jobs, or deals by keyword.
    Filters out firstname, phone, id, mlink from results for LLM safety.
    """
    payload = {
        "search": search,
        "candidates": candidates,
        "contacts": contacts,
        "compnaies": companies,  # Note: API typo is intentional
        "jobs": jobs,
        "deals": deals
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(GLOBAL_SEARCH_URL, headers=HEADERS, json=payload)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        filtered = filter_global_search_results(data)
        return {"results": filtered}

@mcp.tool()
async def add_to_hotlist(
    entity_name: Annotated[str, "Entity type: candidates, contacts, companies, jobs, deals"],
    selectedrows: Annotated[List[int], "List of entity IDs to add to hotlist (can be one or many)"],
    name: Annotated[List[str], "List of hotlist names (exact match, can be one or many)"]
) -> dict:
    """
    Add one or more entity IDs to one or more hotlists by name for a given entity type.
    Only the 'message' field from the response is returned to the LLM.
    """
    url = "https://albatross.recruitcrm.io/v1/hotlists"
    payload = {
        "entity_name": entity_name,
        "selectedrows": selectedrows,
        "name": name
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, headers=HEADERS, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return {"message": data.get("message", "")}

def ensure_epoch(dt_val):
    """Convert ISO8601 string or datetime to epoch seconds (int)."""
    import datetime as dt
    if isinstance(dt_val, int):
        return dt_val
    if isinstance(dt_val, float):
        return int(dt_val)
    if isinstance(dt_val, str):
        try:
            # Try parse as int string
            return int(dt_val)
        except ValueError:
            # Try parse as ISO8601
            try:
                return int(dt.datetime.fromisoformat(dt_val).replace(tzinfo=dt.timezone.utc).timestamp())
            except Exception:
                raise ValueError(f"Invalid date format for epoch conversion: {dt_val}")
    raise ValueError(f"Unsupported type for epoch conversion: {type(dt_val)}")

def validate_meeting_payload_minimal(appointment):
    required_fields = [
        "title", "description", "startdate", "enddate", "reminder", "ownerid",
        "relatedto", "relatedtotypeid", "relatedtoname", "meetingtype", "no_cal_invites", "address"
    ]
    missing = [f for f in required_fields if f not in appointment]
    if missing:
        raise ValueError(f"Missing required appointment fields: {missing}. Example structure: see docstring.")

@mcp.tool()
async def create_meeting(
    appointment: Annotated[dict, "Appointment object as per RecruitCRM API. All fields are mandatory. See docstring for canonical example."],
    action_source: Annotated[str, "Action source string. Mandatory."] = "add_edit_appointment"
) -> dict:
    """
    Use for: Creating a meeting (or task) in RecruitCRM. All fields in the payload are mandatory and must be provided. Returns only LLM-relevant fields from the response.
    The appointment dict must include all required fields, and startdate/enddate must be epoch seconds (int).

    relatedtotypeid mapping:
      4 = job
      5 = candidate
      2 = contact
      3 = company

    Canonical example payload:
    {
        "task": false,
        "appointment": {
            "title": "title of the meeting",
            "description": "description of the meeting",
            "startdate": 1751360400, # epoch seconds
            "enddate": 1751362200, # epoch seconds
            "reminder": 30, # 30 = 30 minutes before the meeting
            "ownerid": 99069, # recruiter id
            "relatedto": "17254734744480070635nEo", # candidate slug
            "relatedtotypeid": 5, # 5 = candidate
            "relatedtoname": "Saurav Gupta", # candidate name
            "meetingtype": 81832, # 81832 = meeting
            "no_cal_invites": 0, # 0 = no, 1 = yes
            "address": "meet.google.com/xps-isy-iasd" # meeting location or link
        },
        "action_source": "add_edit_appointment"
    }
    """
    validate_meeting_payload_minimal(appointment)
    appt = {k: appointment[k] for k in [
        "title", "description", "startdate", "enddate", "reminder", "ownerid",
        "relatedto", "relatedtotypeid", "relatedtoname", "meetingtype", "no_cal_invites", "address"
    ]}
    payload = {
        "task": False,
        "appointment": appt,
        "action_source": action_source
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(MEETING_URL, headers=HEADERS, json=payload)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        filtered = filter_meeting_response(data)
        return {"meeting": filtered}

@mcp.tool()
async def create_gmeet_link() -> dict:
    """
    Use for: Generating a Google Meet link to be used in meeting creation. Returns only the meeting_link.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(GMEET_URL, headers=HEADERS)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        filtered = filter_gmeet_response(data)
        return filtered

OSTRICH_CALENDAR_URL = "https://ostrich.recruitcrm.io/v1/calendar/meetings"

def filter_calendar_meetings(meetings):
    # Remove 'description' and 'calendarid' from each meeting
    filtered = []
    for m in meetings:
        filtered.append({k: v for k, v in m.items() if k not in ("description", "calendarid")})
    return filtered

@mcp.tool()
async def check_calendar_meetings(
    user_ids: Annotated[list, "List of recruiter/user IDs (int) whose calendar to check. Can be a single int as list."],
    startDate: Annotated[int, "Start date (epoch seconds) for calendar search."],
    endDate: Annotated[int, "End date (epoch seconds) for calendar search."],
    searchTerm: Annotated[str, "Optional search term for meeting title or content."] = ""
) -> dict:
    """
    Use for: Checking a recruiter's or team's calendar for meetings in a given time range. Filters out long fields (description, calendarid) from each meeting before returning to the LLM. Use this to find empty slots for scheduling.
    """
    payload = {
        "user_ids": user_ids if isinstance(user_ids, list) and len(user_ids) > 1 else (user_ids[0] if isinstance(user_ids, list) else user_ids),
        "startDate": ensure_epoch(startDate),
        "endDate": ensure_epoch(endDate),
        "searchTerm": searchTerm or ""
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(OSTRICH_CALENDAR_URL, headers=HEADERS, json=payload)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        meetings = data.get("meetings", [])
        filtered = filter_calendar_meetings(meetings)
        return {"meetings": filtered}

@mcp.tool()
async def get_current_time_and_timezone() -> dict:
    """
    Returns the current UTC time (ISO string and epoch), the server's local timezone name, and the current offset from UTC in hours.
    Use this tool to help the LLM determine the current time and timezone context for scheduling or conversions.
    """
    import datetime as dt
    import time
    now_utc = dt.datetime.utcnow().replace(microsecond=0)
    now_utc_iso = now_utc.isoformat() + 'Z'
    now_utc_epoch = int(now_utc.timestamp())
    # Local timezone info
    local_time = dt.datetime.now()
    if time.localtime().tm_isdst and time.daylight:
        offset_sec = -time.altzone
    else:
        offset_sec = -time.timezone
    offset_hours = offset_sec / 3600
    tz_name = time.tzname[time.localtime().tm_isdst]
    return {
        "utc_iso": now_utc_iso,
        "utc_epoch": now_utc_epoch,
        "local_timezone": tz_name,
        "local_utc_offset_hours": offset_hours
    }

NYMA_EMAIL_URL = "https://nyma.recruitcrm.io/v2/nylas-v3/emails"

_DEF_EMAIL_KEEP_FIELDS = [
    "emailSubject", "failed_count", "skipped_count", "success_count", "successCandidateSlugs", "successContactSlugs"
]

def filter_send_email_response(data):
    # Only keep a minimal set of fields for LLM
    filtered = {k: v for k, v in data.items() if k in _DEF_EMAIL_KEEP_FIELDS}
    return filtered

def extract_email_preview(email: dict) -> dict:
    """
    Extracts a preview of the email for human review, including subject, body (HTML and plain text), recipients, cc, bcc.
    """
    subject = email.get("subject", "")
    body_html = email.get("body", "")
    # Note: html2text is optional for email preview
    body_text = "[plain text preview unavailable: install html2text]" if body_html else ""
    recivers = email.get("recivers", [])
    cc = email.get("cc", [])
    bcc = email.get("bcc", [])
    return {
        "subject": subject,
        "body_html": body_html,
        "body_text": body_text,
        "recipients": recivers,
        "cc": cc,
        "bcc": bcc
    }

@mcp.tool()
async def preview_email(
    email: Annotated[dict, "Email object as per RecruitCRM API. All fields are mandatory. See docstring for canonical example."],
    is_send: Annotated[bool, "Whether to send the email (should be true)"] = True,
    linked_email_type: Annotated[int, "Linked email type (should be 1)"] = 1
) -> dict:
    """
    Use for: Previewing an email to be sent to one or more candidates, contacts, or other entities in RecruitCRM. This tool does NOT send the email, but returns a preview for human review. Always use this tool before calling send_email, and only send after explicit user confirmation.

    entity_type mapping (for recivers):
      4 = job
      5 = candidate
      2 = contact
      3 = company

    Canonical example payload:
    {
        "email": {
            "id": null,
            "recivers": [
                {
                    "name": "Saurav ",
                    "email": "saurav.netflix@yopmail.com",
                    "entity_slug": "17103393879850002890CtE",
                    "entity_type": 5
                },
                {
                    "name": "Another Contact",
                    "email": "another@example.com",
                    "entity_slug": "...",
                    "entity_type": 2
                }
            ],
            "cc": [],
            "bcc": [],
            "subject": "Request for First Interview",
            "body": "<p>Hi {candidate_first_name},<br><br>Happy to announce that you have been shortlisted. We would like to have an interview with you at {candidate_custom_interview date}.<br><br>Best,</p>",
            "type": ""
        },
        "is_send": true,
        "linked_email_type": 1
    }
    """
    return {"preview": extract_email_preview(email)}

@mcp.tool()
async def send_email(
    email: Annotated[dict, "Email object as per RecruitCRM API. All fields are mandatory. See docstring for canonical example. Always use preview_email first and only send after explicit user confirmation."],
    is_send: Annotated[bool, "Whether to send the email (should be true)"] = True,
    linked_email_type: Annotated[int, "Linked email type (should be 1)"] = 1
) -> dict:
    """
    Use for: Sending an email to one or more candidates, contacts, or other entities in RecruitCRM. All fields in the payload are mandatory and must be provided. Returns only LLM-relevant fields from the response.

    Always use preview_email first and only call this tool after explicit user confirmation.

    entity_type mapping (for recivers):
      4 = job
      5 = candidate
      2 = contact
      3 = company

    Canonical example payload:
    {
        "email": {
            "id": null,
            "recivers": [
                {
                    "name": "Saurav ",
                    "email": "saurav.netflix@yopmail.com",
                    "entity_slug": "17103393879850002890CtE",
                    "entity_type": 5
                },
                {
                    "name": "Another Contact",
                    "email": "another@example.com",
                    "entity_slug": "...",
                    "entity_type": 2
                }
            ],
            "cc": [],
            "bcc": [],
            "subject": "Request for First Interview",
            "body": "<p>Hi {candidate_first_name},<br><br>Happy to announce that you have been shortlisted. We would like to have an interview with you at {candidate_custom_interview date}.<br><br>Best,</p>",
            "type": ""
        },
        "is_send": true,
        "linked_email_type": 1
    }
    """
    payload = {
        "email": email,
        "is_send": is_send,
        "linked_email_type": linked_email_type
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(NYMA_EMAIL_URL, headers=HEADERS, json=payload)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        filtered = filter_send_email_response(data)
        return {"result": filtered}

NOTES_URL = "https://albatross.recruitcrm.io/v1/notes"
NOTE_TYPES_URL = "https://albatross.recruitcrm.io/v1/notes/get-note-types"

@mcp.tool()
async def create_note(
    relatedto: Annotated[str, "Entity slug (e.g., candidate, job, contact, company)"],
    relatedtotypeid: Annotated[int, "Entity type: 4=job, 5=candidate, 2=contact, 3=company (same as create_meeting)"],
    description: Annotated[str, "HTML note body (e.g., <p>adding a note</p>)"],
    relatedtoname: Annotated[str, "Name of the related entity (e.g., candidate name)"],
    relatedtocompany: Annotated[Optional[str], "Company name (optional)"] = None,
    userInNote: Annotated[dict, "User in note (can be empty dict)"] = {},
    notetype: Annotated[Optional[int], "Note type ID (see get_note_types)"] = None
) -> dict:
    """
    Use for: Creating a note on a candidate, job, contact, or company in RecruitCRM. Returns only LLM-relevant fields from the response.

    relatedtotypeid mapping:
      4 = job
      5 = candidate
      2 = contact
      3 = company

    Canonical example payload:
    {
        "relatedto": "17513813455950000453Hzl", # candidate slug
        "relatedtotypeid": 5, # 5 = candidate
        "description": "<p>Note content</p>", # Note content
        "relatedtoname": "Bridget Yates", # candidate name
        "relatedtocompany": null, # can be null
        "userInNote": {}, # can be empty dict
        "notetype": 85572 # Note type ID
    }
    """
    payload = {
        "relatedto": relatedto,
        "relatedtotypeid": relatedtotypeid,
        "description": description,
        "relatedtoname": relatedtoname,
        "relatedtocompany": relatedtocompany,
        "userInNote": userInNote,
        "notetype": notetype
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(NOTES_URL, headers=HEADERS, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return {
            "message": data.get("message"),
            "message_type": data.get("message_type")
        }

@mcp.tool()
async def get_note_types() -> dict:
    """
    Use for: Getting the list of available note types (id and label only). Only use this tool if the user asks for a specific note type.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(NOTE_TYPES_URL, headers=HEADERS)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        note_types = [{"id": n["id"], "label": n["label"]} for n in data if "id" in n and "label" in n]
        return {"note_types": note_types}

CANDIDATE_BOOLEAN_COUNT_URL = "https://candidate.recruitcrm.io/v2/candidates/search/count"
CANDIDATE_BOOLEAN_SEARCH_URL = "https://candidate.recruitcrm.io/v2/candidates/search/get"
CANDIDATE_ENTITY_COLUMNS_URL = "https://candidate.recruitcrm.io/v2/entity-columns?entity=candidates"

@mcp.tool()
async def boolean_search_count(
    keyword: Annotated[str, "Boolean search string (e.g., '(java OR php) AND (senior OR manager)')"],
    selectedOptions: Annotated[list, "List of selected options for boolean search (e.g., ['entity'])"] = ["entity"]
) -> dict:
    """
    Use for: Getting the count of candidates matching a boolean search. The keyword should be constructed to maximize relevant matches (e.g., include synonyms, similar titles, etc.) for intelligent LLM assistance.

    Example keyword: '(java OR php) AND (senior OR manager)'
    """
    payload = {
        "defaultFilterList": None,
        "filterSearchList": None,
        "booleanSearchList": {
            "keyword": keyword,
            "selectedOptions": selectedOptions
        },
        "sortPriorityList": []
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(CANDIDATE_BOOLEAN_COUNT_URL, headers=HEADERS, json=payload)
        resp.raise_for_status()
        data = resp.json().get("data", None)
        return {"count": data}

@mcp.tool()
async def boolean_search_candidates(
    keyword: Annotated[str, "Boolean search string (e.g., '(java OR php) AND (senior OR manager)')"],
    page: Annotated[int, "Page number (1-based)"] = 1,
    size: Annotated[int, "Page size (max 100)"] = 100,
    selectedOptions: Annotated[list, "List of selected options for boolean search (e.g., ['entity'])"] = ["entity"]
) -> dict:
    """
    Use for: Getting a list of candidates matching a boolean search. Only returns LLM-relevant fields: candidatename, emailid, lastorganisation, position, profilepicUrl, slug, and url. The keyword should be constructed to maximize relevant matches (e.g., include synonyms, similar titles, etc.) for intelligent LLM assistance.

    Example keyword: '(java OR php) AND (senior OR manager)'
    """
    import urllib.parse
    url = f"{CANDIDATE_BOOLEAN_SEARCH_URL}?page={page}&size={size}"
    payload = {
        "defaultFilterList": None,
        "filterSearchList": None,
        "booleanSearchList": {
            "keyword": keyword,
            "selectedOptions": selectedOptions
        },
        "sortPriorityList": []
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, headers=HEADERS, json=payload)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        filtered = [
            {
                "candidatename": c.get("candidatename"),
                "emailid": c.get("emailid"),
                "lastorganisation": c.get("lastorganisation"),
                "position": c.get("position"),
                "profilepicUrl": c.get("profilepicUrl"),
                "slug": c.get("slug"),
                "url": f"https://app.recruitcrm.io/v1/candidate/{c.get('slug')}" if c.get("slug") else None
            }
            for c in data if any(c.get(f) for f in ("candidatename", "emailid", "lastorganisation", "position", "profilepicUrl", "slug"))
        ]
        return {"candidates": filtered}

@mcp.tool()
async def get_candidate_search_fields() -> dict:
    """
    Use for: Getting the list of available fields/columns for advanced candidate search. Returns only label, field, and type, and skips any field where field starts with 'custcolumn'.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(CANDIDATE_ENTITY_COLUMNS_URL, headers=HEADERS)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        # Defensive: handle if data is not as expected
        if not data or not isinstance(data, list) or "columns" not in data[0]:
            return {"fields": []}
        columns = data[0]["columns"]
        filtered = [
            {"label": f.get("label"), "field": f.get("field"), "type": f.get("type")}
            for f in columns.values()
            if f.get("label") and f.get("field") and f.get("type") and not f.get("field", "").startswith("custcolumn")
        ]
        return {"fields": filtered}

@mcp.tool()
async def advanced_search_candidates(
    groupFilterList: Annotated[list, "List of filter groups, each with groupFilterJoinOperator and filters (see docstring for structure)"],
    groupJoinOperator: Annotated[str, "How to join groups: 'AND' or 'OR'"] = "OR",
    page: Annotated[int, "Page number (1-based)"] = 1,
    size: Annotated[int, "Page size (max 100)"] = 100
) -> dict:
    """
    Use for: Performing an advanced candidate search with filter groups. Each group can have its own join operator and filters. Returns only LLM-relevant fields: candidatename, emailid, lastorganisation, position, profilepicUrl, slug, url.

    IMPORTANT:
    - DO NOT use a flat list of filters in 'groupFilterList'. Each item in 'groupFilterList' must be a group dict with:
        - 'groupFilterJoinOperator': "AND" or "OR"
        - 'filters': a list of filter dicts (see below)
    - The correct structure is:
        {
            "defaultFilterList": null,
            "filterSearchList": {
                "groupFilterList": [
                    {
                        "groupFilterJoinOperator": "AND",
                        "filters": [
                            {"groupType": "candidates", "filterName": "Title", "dbField": "position", "filterValue": "VP Product", "filterType": "contains", "fieldType": "text"},
                            {"groupType": "work_history", "filterName": "Company Name", "dbField": "work_company_name", "filterValue": "Facebook", "filterType": "contains", "fieldType": "text"},
                            {"groupType": "candidates", "filterName": "Skills", "dbField": "skill", "filterValue": "agile", "filterType": "contains", "fieldType": "text"}
                        ]
                    },
                    ...
                ],
                "groupJoinOperator": "OR"
            },
            "booleanSearchList": null,
            "sortPriorityList": []
        }
    - BAD EXAMPLE (do NOT use):
        {
            "groupFilterList": [
                { ...filter... },
                { ...filter... }
            ],
            "groupJoinOperator": "AND"
        }
    - Pagination ('page', 'size') must be in the URL query string, NOT in the JSON body.
    - Use 'contains' for filterType for text fields, not 'like'.
    """
    # Validate groupFilterList structure
    if isinstance(groupFilterList, list) and groupFilterList and isinstance(groupFilterList[0], dict) and 'filters' not in groupFilterList[0]:
        raise ValueError(
            """Invalid groupFilterList structure: must be a list of group dicts, each with 'groupFilterJoinOperator' and 'filters' (list of filters).\nSee docstring for correct format."""
        )
    url = f"{CANDIDATE_BOOLEAN_SEARCH_URL}?page={page}&size={size}"

    # Only include the required fields in each filter
    def filter_required_fields(f):
        return {
            "groupType": f.get("groupType", ""),
            "filterName": f.get("filterName", ""),
            "dbField": f.get("dbField", ""),
            "filterValue": f.get("filterValue", ""),
            "filterType": f.get("filterType", ""),
            "fieldType": f.get("fieldType", "")
        }

    normalized_groupFilterList = []
    for group in groupFilterList:
        filters = group.get("filters", [])
        normalized_filters = [filter_required_fields(f) for f in filters]
        normalized_group = {
            "groupFilterJoinOperator": group.get("groupFilterJoinOperator", "AND"),
            "filters": normalized_filters
        }
        normalized_groupFilterList.append(normalized_group)

    payload = {
        "defaultFilterList": None,
        "filterSearchList": {
            "groupFilterList": normalized_groupFilterList,
            "groupJoinOperator": groupJoinOperator
        },
        "booleanSearchList": None,
        "sortPriorityList": []
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, headers=HEADERS, json=payload)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        filtered = [
            {
                "candidatename": c.get("candidatename"),
                "emailid": c.get("emailid"),
                "lastorganisation": c.get("lastorganisation"),
                "position": c.get("position"),
                "profilepicUrl": c.get("profilepicUrl"),
                "slug": c.get("slug"),
                "url": f"https://app.recruitcrm.io/v1/candidate/{c.get('slug')}" if c.get("slug") else None
            }
            for c in data if any(c.get(f) for f in ("candidatename", "emailid", "lastorganisation", "position", "profilepicUrl", "slug"))
        ]
        return {"candidates": filtered}


# ──────────────────────────────────────────────────────────────
# Tool Registry and Orchestration Engine Integration
# ──────────────────────────────────────────────────────────────
import asyncio
from typing import Dict, List, Any, Optional
from dataclasses import dataclass
from enum import Enum

class ToolCategory(Enum):
    SEARCH = "search"
    REPORTS = "reports"
    ACTIONS = "actions"
    HELPERS = "helpers"
    COMMUNICATION = "communication"

@dataclass
class ToolMetadata:
    name: str
    category: ToolCategory
    description: str
    keywords: List[str]
    required_params: List[str]
    optional_params: List[str]
    helper_tools: List[str]
    requires_confirmation: bool = False
    typical_usage_pattern: str = ""

class RecruitCRMToolRegistry:
    def __init__(self):
        self.tools: Dict[str, ToolMetadata] = {}
        self.category_map: Dict[ToolCategory, List[str]] = {
            category: [] for category in ToolCategory
        }
        self.usage_patterns: Dict[str, List[str]] = {}
        self._register_existing_tools()
    
    def _register_existing_tools(self):
        """Register all your existing MCP tools with metadata"""
        
        # Search Tools
        self.register_tool(ToolMetadata(
            name="global_search",
            category=ToolCategory.SEARCH,
            description="Search across candidates, contacts, companies, jobs, or deals by keyword",
            keywords=["search", "find", "lookup", "candidates", "contacts", "companies", "jobs"],
            required_params=["search"],
            optional_params=["candidates", "contacts", "companies", "jobs", "deals"],
            helper_tools=[],
            typical_usage_pattern="Use when user wants to find entities by name/keyword"
        ))
        
        self.register_tool(ToolMetadata(
            name="boolean_search_candidates",
            category=ToolCategory.SEARCH,
            description="Advanced boolean search for candidates with complex queries",
            keywords=["boolean", "advanced search", "candidates", "skills", "AND", "OR"],
            required_params=["keyword"],
            optional_params=["page", "size", "selectedOptions"],
            helper_tools=["boolean_search_count"],
            typical_usage_pattern="Use for complex skill-based or multi-criteria candidate searches"
        ))
        
        self.register_tool(ToolMetadata(
            name="advanced_search_candidates", 
            category=ToolCategory.SEARCH,
            description="Filter-based advanced candidate search with groups",
            keywords=["advanced search", "filter", "candidates", "criteria"],
            required_params=["groupFilterList"],
            optional_params=["groupJoinOperator", "page", "size"],
            helper_tools=["get_candidate_search_fields"],
            typical_usage_pattern="Use for structured filtering with specific field criteria"
        ))
        
        # Report Tools
        self.register_tool(ToolMetadata(
            name="candidate_job_assignment_search",
            category=ToolCategory.REPORTS,
            description="Search for candidates by job assignment, pipeline stage, recruiter, company, job, etc.",
            keywords=["reports", "candidates", "analytics", "pipeline", "placed", "assigned"],
            required_params=["stage_from", "stage_to"],
            optional_params=["hiring_stage", "updated_by", "job_or_keywords", "company"],
            helper_tools=["get_available_hiring_stages", "get_available_users"],
            typical_usage_pattern="Use for candidate job assignment, pipeline, and placement searches"
        ))
        
        self.register_tool(ToolMetadata(
            name="get_team_performance_report",
            category=ToolCategory.REPORTS,
            description="Get KPI and performance metrics for recruiters or teams",
            keywords=["performance", "kpi", "metrics", "team", "recruiter", "stats"],
            required_params=["recruiter_names_or_ids", "kpis_to_include", "from_date", "to_date"],
            optional_params=["team_ids"],
            helper_tools=["get_active_recruiters", "get_available_kpis"],
            requires_confirmation=False,
            typical_usage_pattern="Use for recruiter performance analysis and KPI tracking"
        ))
        
        # Action Tools
        self.register_tool(ToolMetadata(
            name="create_meeting",
            category=ToolCategory.ACTIONS,
            description="Create a meeting or appointment in RecruitCRM",
            keywords=["schedule", "meeting", "appointment", "interview", "call"],
            required_params=["appointment"],
            optional_params=["action_source"],
            helper_tools=["global_search", "get_available_users", "create_gmeet_link", "check_calendar_meetings"],
            requires_confirmation=True,
            typical_usage_pattern="Search entity → Check calendar → Create gmeet → Create meeting"
        ))
        
        self.register_tool(ToolMetadata(
            name="create_note",
            category=ToolCategory.ACTIONS,
            description="Create a note on candidate, job, contact, or company",
            keywords=["note", "add note", "create note", "comment", "log"],
            required_params=["relatedto", "relatedtotypeid", "description", "relatedtoname"],
            optional_params=["relatedtocompany", "userInNote", "notetype"],
            helper_tools=["global_search", "get_note_types"],
            requires_confirmation=False,
            typical_usage_pattern="Search entity → Get note types (if needed) → Create note"
        ))
        
        self.register_tool(ToolMetadata(
            name="add_to_hotlist",
            category=ToolCategory.ACTIONS,
            description="Add one or more entity IDs to one or more hotlists by name for a given entity type. Returns only the 'message' field.",
            keywords=["hotlist", "add", "candidates", "contacts", "companies", "jobs", "deals"],
            required_params=["entity_name", "selectedrows", "name"],
            optional_params=[],
            helper_tools=[],
            requires_confirmation=True,
            typical_usage_pattern="Search entity → Get hotlist name → Add to hotlist"
        ))
        
        # Communication Tools
        self.register_tool(ToolMetadata(
            name="send_email",
            category=ToolCategory.COMMUNICATION,
            description="Send email to candidates, contacts, or other entities",
            keywords=["email", "send", "message", "communicate", "outreach"],
            required_params=["email"],
            optional_params=["is_send", "linked_email_type"],
            helper_tools=["global_search", "preview_email"],
            requires_confirmation=True,
            typical_usage_pattern="Search recipients → Preview email → Confirm → Send email"
        ))
        
        # Helper Tools
        helper_tools = [
            ("get_available_hiring_stages", "Get list of hiring pipeline stages"),
            ("get_available_users", "Get list of active users/recruiters"),
            ("get_available_kpis", "Get list of available KPIs for reports"),
            ("get_note_types", "Get list of note types"),
            ("get_candidate_search_fields", "Get searchable candidate fields"),
            ("create_gmeet_link", "Generate Google Meet link"),
            ("check_calendar_meetings", "Check calendar availability"),
            ("preview_email", "Preview email before sending"),
            ("get_current_time_and_timezone", "Get current time and timezone info"),
            ("boolean_search_count", "Get count for boolean search")
        ]
        
        for tool_name, desc in helper_tools:
            self.register_tool(ToolMetadata(
                name=tool_name,
                category=ToolCategory.HELPERS,
                description=desc,
                keywords=[tool_name.replace("get_", "").replace("_", " ")],
                required_params=[],  # Most helpers have minimal required params
                optional_params=[],
                helper_tools=[],
                requires_confirmation=False,
                typical_usage_pattern=""
            ))
    
    def register_tool(self, metadata: ToolMetadata):
        """Register a tool with its metadata"""
        self.tools[metadata.name] = metadata
        self.category_map[metadata.category].append(metadata.name)
    
    def get_tools_by_category(self, category: ToolCategory) -> List[ToolMetadata]:
        """Get all tools in a specific category"""
        return [self.tools[name] for name in self.category_map[category]]
    
    def find_relevant_tools(self, user_query: str, limit: int = 5) -> List[ToolMetadata]:
        """Find tools most relevant to a user query"""
        query_lower = user_query.lower()
        scored_tools = []
        
        for tool in self.tools.values():
            score = 0
            
            # Check keywords
            for keyword in tool.keywords:
                if keyword.lower() in query_lower:
                    score += 3
            
            # Check description
            desc_words = tool.description.lower().split()
            query_words = query_lower.split()
            common_words = set(desc_words) & set(query_words)
            score += len(common_words)
            
            # Boost score for action words
            action_words = ["create", "add", "schedule", "send", "search", "find", "get", "show"]
            for word in action_words:
                if word in query_lower and word in tool.name.lower():
                    score += 5
            
            if score > 0:
                scored_tools.append((score, tool))
        
        # Sort by score and return top results
        scored_tools.sort(key=lambda x: x[0], reverse=True)
        return [tool for _, tool in scored_tools[:limit]]

    def get_execution_plan(self, primary_tool: str, user_query: str = "") -> Dict[str, Any]:
        """Get suggested execution plan for a tool"""
        if primary_tool not in self.tools:
            return {"error": f"Tool {primary_tool} not found"}
        
        tool = self.tools[primary_tool]
        plan = {
            "primary_tool": primary_tool,
            "helper_tools": tool.helper_tools,
            "requires_confirmation": tool.requires_confirmation,
            "execution_order": [],
            "typical_pattern": tool.typical_usage_pattern
        }
        
        # Build execution order
        execution_order = []
        
        # Add helper tools first
        for helper in tool.helper_tools:
            execution_order.append({
                "tool": helper,
                "purpose": f"Get required data for {primary_tool}",
                "required": True
            })
        
        # Add primary tool last
        execution_order.append({
            "tool": primary_tool,
            "purpose": "Execute main action",
            "required": True,
            "requires_confirmation": tool.requires_confirmation
        })
        
        plan["execution_order"] = execution_order
        return plan

# Initialize the registry
tool_registry = RecruitCRMToolRegistry()

class ExecutionStep:
    def __init__(self, tool_name: str, params: Dict[str, Any], purpose: str = "", store_as: str = ""):
        self.tool_name = tool_name
        self.params = params
        self.purpose = purpose
        self.store_as = store_as
        self.requires_confirmation = False

class RecruitCRMOrchestrator:
    def __init__(self, registry: RecruitCRMToolRegistry):
        self.registry = registry
        self.execution_context = {}
    
    def create_execution_plan(self, user_query: str, primary_tool: Optional[str] = None) -> List[ExecutionStep]:
        """Create an execution plan based on user query"""
        
        if not primary_tool:
            relevant_tools = self.registry.find_relevant_tools(user_query, limit=1)
            if not relevant_tools:
                return []
            primary_tool = relevant_tools[0].name
        
        plan_info = self.registry.get_execution_plan(primary_tool, user_query)
        steps = []
        
        # Convert plan to execution steps
        for step_info in plan_info.get("execution_order", []):
            step = ExecutionStep(
                tool_name=step_info["tool"],
                params={},  # Will be filled by LLM
                purpose=step_info["purpose"],
                store_as=f"{step_info['tool']}_result"
            )
            step.requires_confirmation = step_info.get("requires_confirmation", False)
            steps.append(step)
        
        return steps
    
    def get_tool_suggestions_for_query(self, user_query: str) -> Dict[str, Any]:
        """Get tool suggestions and execution guidance for a query"""
        relevant_tools = self.registry.find_relevant_tools(user_query, limit=3)
        
        suggestions = {
            "primary_suggestions": [],
            "query_analysis": self._analyze_query_intent(user_query),
            "execution_guidance": ""
        }
        
        for tool in relevant_tools:
            tool_suggestion = {
                "tool_name": tool.name,
                "category": tool.category.value,
                "description": tool.description,
                "required_params": tool.required_params,
                "helper_tools": tool.helper_tools,
                "requires_confirmation": tool.requires_confirmation,
                "execution_plan": self.registry.get_execution_plan(tool.name, user_query)
            }
            suggestions["primary_suggestions"].append(tool_suggestion)
        
        return suggestions
    
    def _analyze_query_intent(self, query: str) -> Dict[str, Any]:
        """Analyze user query to determine intent and extract key information"""
        query_lower = query.lower()
        
        intent_keywords = {
            "search": ["find", "search", "show", "list", "get", "who", "which"],
            "create": ["create", "add", "make", "schedule", "send"],
            "report": ["report", "analytics", "how many", "count", "performance", "metrics"],
            "email": ["email", "send", "message", "contact", "reach out"],
            "meeting": ["meeting", "schedule", "appointment", "interview", "call"]
        }
        
        detected_intents = []
        for intent, keywords in intent_keywords.items():
            if any(keyword in query_lower for keyword in keywords):
                detected_intents.append(intent)
        
        # Extract entities
        entities = {
            "candidates": "candidate" in query_lower or "people" in query_lower,
            "jobs": "job" in query_lower or "position" in query_lower,
            "companies": "company" in query_lower or "client" in query_lower,
            "time_range": self._extract_time_references(query_lower)
        }
        
        return {
            "intents": detected_intents,
            "entities": entities,
            "complexity": "high" if len(detected_intents) > 1 else "low"
        }
    
    def _extract_time_references(self, query: str) -> List[str]:
        """Extract time references from query"""
        time_refs = []
        time_keywords = ["yesterday", "today", "tomorrow", "week", "month", "year", "last", "this", "next"]
        
        for keyword in time_keywords:
            if keyword in query:
                time_refs.append(keyword)
        
        return time_refs

# Initialize orchestrator
orchestrator = RecruitCRMOrchestrator(tool_registry)


# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    print(f"🚀 RecruitCRM MCP server starting for local dev on http://localhost:{port}", file=sys.stderr)
    uvicorn.run("recruitcrm_mcp:app", host="0.0.0.0", port=port, reload=True)
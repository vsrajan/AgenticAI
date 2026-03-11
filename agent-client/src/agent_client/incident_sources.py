"""Incident data sources for the scanner.

Provides the Incident dataclass and pluggable source classes that fetch
open incidents from different backends:

- CsvIncidentSource  -- reads from a local CSV file (default for development)
- ServiceNowIncidentSource -- fetches from a ServiceNow instance via REST API
                              (stub -- not yet implemented)

All sources expose the same interface: fetch_open_incidents() -> list[Incident].
"""

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("agent_client.incident_sources")


# -- Data classes --

@dataclass
class Incident:
    """A single incident record, regardless of source.

    Field names are generic (not tied to CSV headers or ServiceNow column
    names). Each source class is responsible for mapping its own schema
    to these fields.
    """
    id: str
    short_description: str
    description: str
    priority: str
    state: str
    category: str
    subcategory: str
    assignment_group: str
    assigned_to: str
    opened_date: str
    resolved_date: str
    resolution_notes: str


# -- CSV source --

class CsvIncidentSource:
    """Reads incidents from a local CSV file.

    Expected CSV columns (ServiceNow export format):
        IncidentID, ShortDescription, Description, Priority, State,
        Category, Subcategory, AssignmentGroup, AssignedTo, OpenedDate,
        ResolvedDate, ResolutionNotes

    Usage:
        source = CsvIncidentSource(Path("data/Incidents.csv"))
        incidents = source.fetch_open_incidents()
    """

    # maps CSV column headers to Incident field names
    _FIELD_MAP = {
        "IncidentID": "id",
        "ShortDescription": "short_description",
        "Description": "description",
        "Priority": "priority",
        "State": "state",
        "Category": "category",
        "Subcategory": "subcategory",
        "AssignmentGroup": "assignment_group",
        "AssignedTo": "assigned_to",
        "OpenedDate": "opened_date",
        "ResolvedDate": "resolved_date",
        "ResolutionNotes": "resolution_notes",
    }

    def __init__(self, csv_path: Path):
        self.csv_path = csv_path

    def fetch_open_incidents(self) -> list[Incident]:
        """Read CSV and return only open/in-progress incidents."""
        incidents: list[Incident] = []
        with open(self.csv_path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                mapped = {
                    field_name: row.get(csv_col, "").strip()
                    for csv_col, field_name in self._FIELD_MAP.items()
                }
                incident = Incident(**mapped)
                if incident.state.lower() in ("open", "in progress", "new"):
                    incidents.append(incident)
        logger.info("Loaded %d open incidents from %s", len(incidents), self.csv_path)
        return incidents


# -- ServiceNow source (stub) --

class ServiceNowIncidentSource:
    """Fetches incidents from a ServiceNow instance via the Table API.

    This is a stub -- all methods raise NotImplementedError. To implement:

    1. Install the requests library (add to pyproject.toml dependencies).
    2. Fill in fetch_open_incidents() to call the ServiceNow Table API:
       GET https://<instance>.service-now.com/api/now/table/incident
    3. Fill in _map_record() to convert ServiceNow JSON to Incident.

    ServiceNow Table API reference:
        endpoint: /api/now/table/incident
        auth: basic auth (username + password) or OAuth token
        query params:
            sysparm_query  -- encoded query, e.g. "state=1^ORstate=2" for open
            sysparm_fields -- comma-separated field list to reduce payload
            sysparm_limit  -- max records (default 10000)

    ServiceNow field -> Incident field mapping:
        sys_id              -> id (or use "number" for the INC0001-style ID)
        number              -> id
        short_description   -> short_description
        description         -> description
        priority            -> priority (ServiceNow uses 1-5 numeric)
        state               -> state (1=New, 2=In Progress, 3=On Hold, etc.)
        category            -> category
        subcategory         -> subcategory
        assignment_group    -> assignment_group (display_value or .name)
        assigned_to         -> assigned_to (display_value or .name)
        opened_at           -> opened_date
        resolved_at         -> resolved_date
        close_notes         -> resolution_notes

    Usage (once implemented):
        source = ServiceNowIncidentSource(
            instance_url="https://mycompany.service-now.com",
            username=os.environ["SERVICENOW_USER"],
            password=os.environ["SERVICENOW_PASSWORD"],
        )
        incidents = source.fetch_open_incidents()
    """

    def __init__(
        self,
        instance_url: str,
        username: str,
        password: str,
        query: str | None = None,
    ):
        """Initialise the ServiceNow source.

        Args:
            instance_url: base URL, e.g. "https://mycompany.service-now.com"
            username: ServiceNow username for basic auth
            password: ServiceNow password for basic auth
            query: optional encoded query string to filter incidents.
                   Defaults to open/new incidents if not provided.
        """
        self.instance_url = instance_url.rstrip("/")
        self.username = username
        self.password = password
        self.query = query or "state=1^ORstate=2"  # 1=New, 2=In Progress

    def fetch_open_incidents(self) -> list[Incident]:
        """Fetch open incidents from ServiceNow.

        Implementation steps:
        1. Build the request URL:
           {instance_url}/api/now/table/incident
        2. Set query params:
           sysparm_query = self.query
           sysparm_fields = "number,short_description,description,priority,
                             state,category,subcategory,assignment_group,
                             assigned_to,opened_at,resolved_at,close_notes"
           sysparm_display_value = "true"  (to get readable names for
                                            assignment_group, assigned_to)
           sysparm_limit = 500
        3. Send GET with basic auth (self.username, self.password).
        4. Parse JSON response -- records are in response["result"].
        5. Map each record via _map_record().
        """
        raise NotImplementedError(
            "ServiceNowIncidentSource.fetch_open_incidents() is not yet "
            "implemented. See class docstring for implementation guide."
        )

    def _map_record(self, record: dict) -> Incident:
        """Convert a single ServiceNow JSON record to an Incident.

        Implementation steps:
        1. Extract fields from the record dict using ServiceNow column names.
        2. Map ServiceNow state codes to human-readable strings if
           sysparm_display_value is not set to "true".
        3. Handle nested objects -- assignment_group and assigned_to may be
           dicts like {"display_value": "IAM Support", "link": "..."} when
           using display_value=true, or just strings.
        4. Return an Incident dataclass instance.

        Example mapping:
            return Incident(
                id=record.get("number", ""),
                short_description=record.get("short_description", ""),
                description=record.get("description", ""),
                priority=record.get("priority", ""),
                state=record.get("state", ""),
                category=record.get("category", ""),
                subcategory=record.get("subcategory", ""),
                assignment_group=record.get("assignment_group", ""),
                assigned_to=record.get("assigned_to", ""),
                opened_date=record.get("opened_at", ""),
                resolved_date=record.get("resolved_at", ""),
                resolution_notes=record.get("close_notes", ""),
            )
        """
        raise NotImplementedError(
            "ServiceNowIncidentSource._map_record() is not yet implemented. "
            "See class docstring for field mapping guide."
        )

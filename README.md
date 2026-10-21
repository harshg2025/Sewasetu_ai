Sewa Setu — Old Age Pension Portal & Agentic MCP Platform
Department of Social Welfare, Government of Purvanchal.
Re-imagined for an Agentic World — Build for Bharat Fellowship (AI Track).

Quickstart: Running on Localhost
The platform supports a seamless Dual Setup: instant zero-dependency execution locally via Python with embedded database and services, OR production deployment via Docker Compose.

Option 1: Instant Local Runner (Currently Running on Localhost)
python run_local.py
This automatically initializes the 100,000-application database, starts the deemed-approval scheduler, runs the simulated telecom SMS gateway, and serves the portal and both MCP servers on port 8000.

Option 2: Docker Compose (for VM & Production Deployment)
ADMIN_PASSWORD=admin123 docker compose up --build
Active Endpoints & Portals
Portal Home: http://localhost:8000
Evaluation & Benchmark Dashboard: http://localhost:8000/eval-dashboard
Citizen MCP Server (Streamable HTTP): http://localhost:8000/mcp/citizen
Citizen MCP Handshake (SSE): http://localhost:8000/mcp/citizen/sse
Officer MCP Server (Streamable HTTP): http://localhost:8000/mcp/officer
Officer MCP Handshake (SSE): http://localhost:8000/mcp/officer/sse
Citizen Status Portal: http://localhost:8000/status
(Mobile number + password; password is applicant's date of birth as DDMMYYYY)
Departmental Officer Login: http://localhost:8000/admin
(Username: admin, Password: admin123)
SMS Gateway Console (Simulated Telecom): http://localhost:8000/__gateway/
(Read inbox via JSON at /__gateway/api/messages?to=<mobile>)
OAuth 2.1 Metadata: http://localhost:8000/.well-known/oauth-authorization-server
JWKS Key Set: http://localhost:8000/.well-known/jwks.json
Architectural Design: Two Streamable HTTP MCP Servers
In public welfare systems, security and prompt-injection defense must be enforced at the transport and schema layer, not delegated to model compliance. We implemented two completely separate MCP endpoints over Streamable HTTP and Server-Sent Events (SSE) following the Model Context Protocol (2024-11-05 spec):

1. Citizen / Applicant MCP Server (/mcp/citizen)
Designed specifically for elderly citizens and applicant agents. An assistant connected here cannot even see or execute officer tools.

check_eligibility: Verifies age (>= 60), validates block, checks scheme deadline, verifies no duplicate active application exists.
get_scheme_guidelines: Returns pension amount (₹1,000/mo), accepted age proofs, and the 15-day statutory SLA under the RTPS Act.
submit_application: Validates all scheme rules, inserts into database, provisions status portal credentials (portal_users), logs audit entry (SUBMIT), and triggers SMS receipt.
check_application_status: Queries application status, calculates days elapsed against 15-day SLA, and provides status explanation.
withdraw_application: Safely withdraws a PENDING application so an applicant who entered incorrect bank details can reapply.
download_acknowledgment: Provides computer-generated PDF acknowledgment URL (/ack/<id>.pdf).
find_nearest_helpdesk: Returns Block Development Office (BDO) address, contact officer, and Seva Kendra locations for the citizen's block.
2. Block Officer MCP Server (/mcp/officer)
Designed strictly for departmental officers and queue administrators:

list_applications: Queue inspection with filters by status (PENDING, APPROVED, REJECTED, etc.), block, and overdue SLA breaches (>15 days).
get_application_details: Complete dossier with personal details, bank accounts, uploaded age proof documents, and full audit trail.
review_application_eligibility: Automated compliance audit checking age, block, IFSC format, and bank digits with pass/fail scorecards.
approve_application: Sanctions pending applications, stamps decided_at/decided_by, records audit entry, and sends SMS to citizen.
reject_application: Rejects pending applications with mandatory plain-language justification, logs audit trail, and sends SMS.
get_dashboard_analytics: High-level metrics: total applications, status distribution, overdue SLA breaches, approval rates.
trigger_sla_deemed_approvals: Enforces the Purvanchal Right to Public Services Act by automatically approving applications pending > 15 days.
Evaluation Benchmark Suite (/eval-dashboard)
Accessible at http://localhost:8000/eval-dashboard. The dashboard provides:

7 Realistic Personas:
Bhabesh Kalita (68, Namti): Rural farmer, Assamese-inflected English, submitting fresh application.
Shanti Devi (62, Sonari): Elderly widow, inquiries about accepted documents and applies with widow status.
Tarun Gogoi (57, Rajapara): Underage applicant boundary test. Assistant politely refuses and guides him to wait 3 years.
Rumi Barua (71, Borgaon): Status inquiry and statutory 15-day SLA timeline check.
Nogen Bora (65, Khelua): Applicant with bank account mistake, safely withdraws and reapplies with correct passbook.
Pratima Hazarika (64, Dhemaji Pathar): Duplicate prevention test ensuring single active application per phone number.
Officer Ramesh Sharma: Sonari Block Development Officer clearing queue backlog and approving verified files.
System of Record Database Assertions: Every evaluation verifies post-conversation database states against live SQL queries (e.g. verifying status='PENDING', decided_by='APPLICANT', count(*)=0 for underage, etc.).
Live Interactive Playground: Test conversational prompts against either MCP server live with real-time tool inspection.
Key Files & Layout
run_local.py         Unified launcher script for localhost
DESIGN_NOTE.md       Official 1-page Design Note (three decisions + one different)
LOOM_SCRIPT.md       3-minute video presentation script for Loom recording
BRIEF.md             Original fellowship brief & challenge specifications
README.md            System documentation and API reference
app/
  app.py             Flask core application & routes
  db_adapter.py      High-performance dual Postgres / SQLite database adapter
  mcp_citizen.py     Citizen Assistant MCP Server (Streamable HTTP + SSE)
  mcp_officer.py     Block Officer MCP Server (Streamable HTTP + SSE)
  eval_dashboard.py  Evaluation benchmark suite, personas & dashboard UI
  sms_gateway.py     Simulated telecom SMS gateway (embedded & standalone)
  oauth.py           OAuth 2.1 authorization server
  schema.sql         Database schema
  scripts/
    deemed_approval.py  Nightly RTPS deemed approval job
seed/
  seed.sql           100,000 production applications export
smsgw/               Standalone Docker container for simulated SMS gateway

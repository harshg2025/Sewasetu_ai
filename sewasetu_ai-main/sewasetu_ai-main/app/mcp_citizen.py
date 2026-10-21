# -*- coding: utf-8 -*-
"""
Sewa Setu — Citizen / Applicant MCP Server
Exposes Model Context Protocol (MCP) over Streamable HTTP and Server-Sent Events (SSE).

Scope: citizen
Endpoints:
  - GET  /mcp/citizen/sse          (SSE handshake endpoint)
  - POST /mcp/citizen/messages     (Session message endpoint)
  - POST /mcp/citizen              (Direct Streamable HTTP JSON-RPC endpoint)

Guaranteed Separation: This server exposes ONLY applicant tools.
Officer administrative tools are completely absent from this server.
"""

import os
import json
import uuid
import queue
import secrets
from datetime import datetime
from flask import Blueprint, request, Response, jsonify, current_app

import db_adapter
from sms_gateway import record_sms

citizen_mcp_bp = Blueprint("mcp_citizen", __name__)

# Active SSE sessions: session_id -> queue.Queue
SSE_SESSIONS = {}

MIN_AGE = 60
BLOCKS = ["Sonari", "Rajapara", "Dhemaji Pathar", "Borgaon", "Namti", "Khelua"]
SCHEME_DEADLINE = "2026-10-31 23:59"
PENSION_AMOUNT = "₹1,000 / month"
SLA_DAYS = 15

# ---------------------------------------------------------------------------
# Tool Implementations
# ---------------------------------------------------------------------------

def tool_check_eligibility(args):
    """Checks whether an applicant qualifies for the Old Age Pension scheme."""
    dob_raw = args.get("dob", "").strip()
    block = args.get("block", "").strip()
    mobile = args.get("mobile", "").strip()
    village = args.get("village", "").strip()

    errors = []
    
    # Parse DOB
    dob = None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            dob = datetime.strptime(dob_raw, fmt).date()
            break
        except ValueError:
            pass

    if not dob:
        errors.append("Invalid date of birth format. Please provide DD/MM/YYYY (e.g. 15/08/1958).")
    else:
        today = datetime.now().date()
        age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
        if age < MIN_AGE:
            errors.append(f"Applicant is {age} years old. Minimum statutory age required is {MIN_AGE} years (needs {MIN_AGE - age} more year(s)).")

    if block and block not in BLOCKS:
        errors.append(f"Block '{block}' is not within the 6 operational blocks: {', '.join(BLOCKS)}.")

    existing_app = None
    if mobile:
        conn = db_adapter.get_connection("localhost", 5432, "sewasetu", "sewasetu", "sewasetu123")
        cur = conn.cursor()
        cur.execute("SELECT application_no, status, applicant_name FROM applications "
                    "WHERE mobile = %s AND status IN ('PENDING', 'APPROVED', 'DEEMED_APPROVED') "
                    "ORDER BY submitted_at DESC LIMIT 1", (mobile,))
        existing_app = cur.fetchone()
        conn.close()
        if existing_app:
            errors.append(f"Mobile number {mobile} already holds an active application {existing_app[0]} "
                          f"in status '{existing_app[1]}' for '{existing_app[2]}'. Under scheme rules, "
                          f"an applicant may hold at most one active application at a time.")

    is_eligible = len(errors) == 0
    return {
        "eligible": is_eligible,
        "applicant_dob": dob_raw,
        "age_verified": (age >= MIN_AGE) if dob else False,
        "operational_block": block in BLOCKS if block else "Pending confirmation",
        "allowed_blocks": BLOCKS,
        "scheme_benefit": PENSION_AMOUNT,
        "statutory_sla_days": SLA_DAYS,
        "disqualifying_reasons": errors if not is_eligible else [],
        "guidance": "Applicant satisfies all eligibility requirements and may proceed to submit their application."
                    if is_eligible else "Applicant does not meet current scheme criteria: " + "; ".join(errors)
    }


def tool_get_scheme_guidelines(args):
    """Returns official details about the Old Age Pension scheme in Purvanchal."""
    return {
        "scheme_name": "Purvanchal Old Age Pension Scheme (Sewa Setu)",
        "department": "Department of Social Welfare, Government of Purvanchal",
        "monthly_pension": PENSION_AMOUNT,
        "minimum_age": MIN_AGE,
        "eligible_blocks": BLOCKS,
        "statutory_sla": f"{SLA_DAYS} calendar days under the Purvanchal Right to Public Services Act",
        "deemed_approval": "If the department does not approve or reject an application within 15 days, it automatically stands DEEMED_APPROVED by law.",
        "documents_accepted": [
            "Aadhaar Card (with complete date of birth)",
            "Voter Identity Card (EPIC)",
            "School Leaving Certificate or Birth Certificate",
            "Gaon Bura / Panchayat Age Certificate"
        ],
        "bank_requirements": "Valid single-holder or joint bank account with valid IFSC code in any commercial or regional rural bank.",
        "tracking_and_login": "Citizens can track their status anytime at /status using their 10-digit mobile number and their date of birth as password (DDMMYYYY)."
    }


def tool_submit_application(args):
    """Submits a pension application to the Sewa Setu portal database."""
    applicant_name = args.get("applicant_name", "").strip()
    mobile = args.get("mobile", "").strip()
    dob_raw = args.get("dob", "").strip()
    gender = args.get("gender", "Other").strip()
    marital_status = args.get("marital_status", "Single").strip()
    husband_name = args.get("husband_name", "").strip()
    village = args.get("village", "").strip()
    block = args.get("block", "").strip()
    bank_account = str(args.get("bank_account", "")).strip().replace(" ", "").replace("-", "")
    ifsc = args.get("ifsc", "").strip().upper()
    doc_path = args.get("doc_path") or args.get("age_proof_doc_name") or "age_proof_aadhaar.pdf"

    # Validation
    errors = []
    if not applicant_name:
        errors.append("Applicant full name is required.")
    if not mobile or len(mobile) != 10 or not mobile.isdigit():
        errors.append("Valid 10-digit mobile number is required.")
    if not village:
        errors.append("Village is required.")
    if block not in BLOCKS:
        errors.append(f"Block must be one of: {', '.join(BLOCKS)}.")
    if not bank_account or not (9 <= len(bank_account) <= 18) or not bank_account.isdigit():
        errors.append("Bank account number must be between 9 and 18 digits.")
    import re
    if not ifsc or not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", ifsc):
        errors.append("IFSC must be 11 characters (e.g. SBIN0003077).")

    dob = None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            dob = datetime.strptime(dob_raw, fmt).date()
            break
        except ValueError:
            pass

    if not dob:
        errors.append("Date of birth must be DD/MM/YYYY.")
    else:
        today = datetime.now().date()
        age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
        if age < MIN_AGE:
            errors.append(f"Applicant must be at least {MIN_AGE} years of age (current age: {age}).")

    if errors:
        return {"success": False, "errors": errors}

    # Check for active existing application
    conn = db_adapter.get_connection("localhost", 5432, "sewasetu", "sewasetu", "sewasetu123")
    cur = conn.cursor()
    cur.execute("SELECT id, application_no, status FROM applications "
                "WHERE mobile = %s AND status IN ('PENDING', 'APPROVED', 'DEEMED_APPROVED')", (mobile,))
    existing = cur.fetchone()
    if existing:
        conn.close()
        return {
            "success": False,
            "errors": [f"Application {existing[1]} ({existing[2]}) already exists for mobile {mobile}. "
                       f"Withdraw the pending application if you wish to apply afresh."]
        }

    # Generate application number
    app_no = "SSP" + datetime.now().strftime("%y") + str(secrets.randbelow(900000) + 100000)
    now = datetime.now()

    cur.execute("""
    INSERT INTO applications
    (application_no, applicant_name, mobile, dob, gender, marital_status,
     husband_name, husband_employer, village, block, bank_account, ifsc,
     doc_path, status, submitted_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s, '', %s, %s, %s, %s, %s, 'PENDING', %s)
    RETURNING id
    """, (app_no, applicant_name, mobile, dob, gender, marital_status,
          husband_name, village, block, bank_account, ifsc, doc_path, now))
    new_id = cur.fetchone()[0]

    # Create status portal credentials (password is DOB as DDMMYYYY)
    import hashlib
    pw_str = dob.strftime("%d%m%Y")
    pw_hash = hashlib.sha256(pw_str.encode("utf-8")).hexdigest()
    cur.execute("SELECT count(*) FROM portal_users WHERE mobile = %s", (mobile,))
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO portal_users (mobile, password_hash) VALUES (%s, %s)", (mobile, pw_hash))

    # Audit log
    cur.execute("INSERT INTO audit_log (application_id, action, actor, note, at) "
                "VALUES (%s, 'SUBMIT', %s, 'Application submitted via Citizen Assistant', %s)",
                (new_id, f"applicant:{mobile}", now))
    conn.commit()
    conn.close()

    # Send SMS notification
    sms_text = f"Sewa Setu: application {app_no} received. Track at /status with mobile no. and password (DOB as DDMMYYYY)."
    record_sms(mobile, sms_text)

    return {
        "success": True,
        "application_no": app_no,
        "application_id": new_id,
        "applicant_name": applicant_name,
        "status": "PENDING",
        "submitted_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "portal_login": {
            "url": "/status",
            "mobile": mobile,
            "password_hint": f"Your Date of Birth as DDMMYYYY ({pw_str})"
        },
        "statutory_sla_days": SLA_DAYS,
        "confirmation_message": f"Your pension application {app_no} has been successfully submitted! "
                                f"Under the Right to Public Services Act, your application will be processed within {SLA_DAYS} days."
    }


def tool_check_application_status(args):
    """Looks up current status, statutory SLA countdown, and history of an application."""
    mobile = args.get("mobile", "").strip()
    app_no = args.get("application_no", "").strip()

    if not mobile and not app_no:
        return {"error": "Either mobile number or application_no is required."}

    conn = db_adapter.get_connection("localhost", 5432, "sewasetu", "sewasetu", "sewasetu123")
    cur = conn.cursor()
    if app_no:
        cur.execute("SELECT id, application_no, applicant_name, mobile, dob, village, block, "
                    "bank_account, ifsc, status, submitted_at, decided_at, decided_by "
                    "FROM applications WHERE application_no = %s", (app_no,))
    else:
        cur.execute("SELECT id, application_no, applicant_name, mobile, dob, village, block, "
                    "bank_account, ifsc, status, submitted_at, decided_at, decided_by "
                    "FROM applications WHERE mobile = %s ORDER BY submitted_at DESC LIMIT 1", (mobile,))

    row = cur.fetchone()
    if not row:
        conn.close()
        return {"found": False, "message": "No application found matching the provided details."}

    app_id, no, name, mob, dob, vill, blk, acct, ifsc, status, submitted_at, decided_at, decided_by = row

    # Calculate SLA status
    days_in_queue = 0
    sla_remaining_days = SLA_DAYS
    if submitted_at:
        try:
            sub_dt = datetime.strptime(str(submitted_at)[:19], "%Y-%m-%d %H:%M:%S")
            delta = datetime.now() - sub_dt
            days_in_queue = delta.days
            sla_remaining_days = max(0, SLA_DAYS - days_in_queue)
        except Exception:
            pass

    # Audit history
    cur.execute("SELECT at, action, actor, note FROM audit_log WHERE application_id = %s ORDER BY at ASC", (app_id,))
    history = []
    for h in cur.fetchall():
        history.append({
            "timestamp": str(h[0]),
            "action": h[1],
            "actor": h[2],
            "note": h[3] or ""
        })

    conn.close()

    status_meanings = {
        "PENDING": f"Under departmental verification. Currently at day {days_in_queue} of {SLA_DAYS} statutory SLA.",
        "APPROVED": "Sanctioned! Monthly pension ₹1,000 will be credited directly to your registered bank account.",
        "DEEMED_APPROVED": f"Automatically approved by law under the RTPS Act after exceeding the {SLA_DAYS}-day statutory SLA.",
        "REJECTED": "Application rejected by block officer. Reason recorded in audit log.",
        "WITHDRAWN": "Withdrawn by applicant. You may submit a fresh application anytime."
    }

    return {
        "found": True,
        "application_no": no,
        "applicant_name": name,
        "status": status,
        "status_description": status_meanings.get(status, status),
        "block": blk,
        "village": vill,
        "submitted_at": str(submitted_at),
        "decided_at": str(decided_at) if decided_at else None,
        "decided_by": decided_by or None,
        "days_elapsed": days_in_queue,
        "sla_days_remaining": sla_remaining_days if status == "PENDING" else 0,
        "can_withdraw": status == "PENDING",
        "audit_trail": history
    }


def tool_withdraw_application(args):
    """Allows an applicant to withdraw their PENDING application so they can reapply."""
    mobile = args.get("mobile", "").strip()
    app_no = args.get("application_no", "").strip()
    reason = args.get("reason", "Applicant requested withdrawal").strip()

    if not mobile and not app_no:
        return {"success": False, "error": "Either mobile number or application_no is required."}

    conn = db_adapter.get_connection("localhost", 5432, "sewasetu", "sewasetu", "sewasetu123")
    cur = conn.cursor()
    if app_no:
        cur.execute("SELECT id, application_no, mobile, status FROM applications WHERE application_no = %s", (app_no,))
    else:
        cur.execute("SELECT id, application_no, mobile, status FROM applications WHERE mobile = %s AND status = 'PENDING' ORDER BY submitted_at DESC LIMIT 1", (mobile,))

    row = cur.fetchone()
    if not row:
        conn.close()
        return {"success": False, "error": "No pending application found to withdraw."}

    app_id, matched_no, matched_mobile, status = row
    if status != "PENDING":
        conn.close()
        return {"success": False, "error": f"Application {matched_no} is currently '{status}'. Only PENDING applications can be withdrawn."}

    now = datetime.now()
    cur.execute("UPDATE applications SET status = 'WITHDRAWN', decided_at = %s, decided_by = 'APPLICANT' "
                "WHERE id = %s", (now, app_id))
    cur.execute("INSERT INTO audit_log (application_id, action, actor, note, at) "
                "VALUES (%s, 'WITHDRAW', %s, %s, %s)", (app_id, f"applicant:{matched_mobile}", reason, now))
    conn.commit()
    conn.close()

    record_sms(matched_mobile, f"Sewa Setu: application {matched_no} has been withdrawn upon your request. You may apply afresh.")

    return {
        "success": True,
        "application_no": matched_no,
        "status": "WITHDRAWN",
        "message": f"Application {matched_no} has been successfully withdrawn. You are now free to submit a fresh application."
    }


def tool_download_acknowledgment(args):
    """Returns official receipt and acknowledgment PDF download link."""
    app_no = args.get("application_no", "").strip()
    app_id = args.get("app_id")

    conn = db_adapter.get_connection("localhost", 5432, "sewasetu", "sewasetu", "sewasetu123")
    cur = conn.cursor()
    if app_no:
        cur.execute("SELECT id, application_no, applicant_name, mobile, submitted_at, status FROM applications WHERE application_no = %s", (app_no,))
    elif app_id:
        cur.execute("SELECT id, application_no, applicant_name, mobile, submitted_at, status FROM applications WHERE id = %s", (app_id,))
    else:
        conn.close()
        return {"error": "application_no or app_id is required."}

    row = cur.fetchone()
    conn.close()
    if not row:
        return {"error": "Application not found."}

    return {
        "application_no": row[1],
        "applicant_name": row[2],
        "mobile": row[3],
        "submitted_at": str(row[4]),
        "status": row[5],
        "acknowledgment_pdf_url": f"/ack/{row[0]}.pdf",
        "instructions": f"Visit http://localhost:8000/ack/{row[0]}.pdf to download your official digitally stamped acknowledgment."
    }


def tool_find_nearest_helpdesk(args):
    """Returns block development office and Seva Kendra contact info."""
    block = args.get("block", "").strip()
    helpdesks = {
        "Sonari": {"bdo": "Shri P. Sonowal, ACS", "office": "Block Development Office, Sonari Town", "helpline": "03772-256101", "kendra": "Sonari Public Seva Kendra, Near DC Court"},
        "Rajapara": {"bdo": "Smt. M. Baruah, ACS", "office": "BDO Complex, Rajapara Chariali", "helpline": "03772-258204", "kendra": "Rajapara Panchayat Seva Kendra"},
        "Dhemaji Pathar": {"bdo": "Shri K. Doley, ACS", "office": "Mini Secretariat Road, Dhemaji Pathar", "helpline": "03772-254118", "kendra": "Dhemaji Pathar Block Helpdesk"},
        "Borgaon": {"bdo": "Shri S. Gogoi, ACS", "office": "Borgaon Panchayat Samiti Bhawan", "helpline": "03772-253902", "kendra": "Borgaon Seva Setu Facilitation Center"},
        "Namti": {"bdo": "Smt. N. Saikia, ACS", "office": "Namti Block Office, Ali Road", "helpline": "03772-257319", "kendra": "Namti Community Facilitation Desk"},
        "Khelua": {"bdo": "Shri T. Chetia, ACS", "office": "Khelua Gaon Panchayat Office Complex", "helpline": "03772-259881", "kendra": "Khelua Citizen Seva Point"}
    }
    if block in helpdesks:
        return {"block": block, "details": helpdesks[block], "timings": "10:00 AM to 5:00 PM (Monday to Saturday)"}
    return {"all_blocks": helpdesks, "timings": "10:00 AM to 5:00 PM (Monday to Saturday)"}


# ---------------------------------------------------------------------------
# MCP Tool Registry (Citizen Assistant)
# ---------------------------------------------------------------------------

CITIZEN_TOOLS = [
    {
        "name": "check_eligibility",
        "description": "Verify if a citizen meets scheme criteria: age 60+, valid operational block, and no existing active application.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "dob": {"type": "string", "description": "Date of birth in DD/MM/YYYY or YYYY-MM-DD"},
                "block": {"type": "string", "description": "Block name (e.g. Sonari, Rajapara, Namti, etc.)"},
                "mobile": {"type": "string", "description": "10-digit mobile number to check for existing active applications"},
                "village": {"type": "string", "description": "Village name"}
            },
            "required": ["dob"]
        }
    },
    {
        "name": "get_scheme_guidelines",
        "description": "Get official rules, pension amount (₹1,000/mo), accepted age proofs, 15-day SLA, and deemed approval mechanism.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "submit_application",
        "description": "Submit a validated Old Age Pension application to the official portal database. Creates portal account and sends SMS.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "applicant_name": {"type": "string", "description": "Full legal name of the applicant"},
                "dob": {"type": "string", "description": "Date of birth (DD/MM/YYYY)"},
                "gender": {"type": "string", "enum": ["Male", "Female", "Other"], "description": "Gender"},
                "marital_status": {"type": "string", "enum": ["Single", "Married", "Widowed", "Divorced"], "description": "Marital status"},
                "husband_name": {"type": "string", "description": "Husband's name if widowed/married female"},
                "village": {"type": "string", "description": "Village name"},
                "block": {"type": "string", "description": "Operational block in Purvanchal"},
                "bank_account": {"type": "string", "description": "Bank account number (9 to 18 digits)"},
                "ifsc": {"type": "string", "description": "11-character bank IFSC code (e.g. SBIN0003077)"},
                "mobile": {"type": "string", "description": "10-digit applicant mobile number"},
                "age_proof_doc_name": {"type": "string", "description": "Document name or type (e.g. aadhaar_card.pdf, voter_id.jpg)"}
            },
            "required": ["applicant_name", "dob", "village", "block", "bank_account", "ifsc", "mobile"]
        }
    },
    {
        "name": "check_application_status",
        "description": "Check current status (PENDING, APPROVED, DEEMED_APPROVED, REJECTED, WITHDRAWN), SLA progress, and decision history.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mobile": {"type": "string", "description": "10-digit registered mobile number"},
                "application_no": {"type": "string", "description": "Application number (e.g. SSP26123456)"}
            }
        }
    },
    {
        "name": "withdraw_application",
        "description": "Withdraw a PENDING application if there was an error in details, enabling the citizen to reapply afresh.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mobile": {"type": "string", "description": "10-digit applicant mobile number"},
                "application_no": {"type": "string", "description": "Application number to withdraw"},
                "reason": {"type": "string", "description": "Reason for withdrawal"}
            }
        }
    },
    {
        "name": "download_acknowledgment",
        "description": "Get official computer-generated PDF acknowledgment details and download link for an application.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "application_no": {"type": "string", "description": "Application number"},
                "app_id": {"type": "integer", "description": "Database application ID"}
            }
        }
    },
    {
        "name": "find_nearest_helpdesk",
        "description": "Get contact information, BDO officer name, and Seva Kendra location for a specific block.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "block": {"type": "string", "description": "Block name in Purvanchal"}
            }
        }
    }
]

TOOL_DISPATCH = {
    "check_eligibility": tool_check_eligibility,
    "get_scheme_guidelines": tool_get_scheme_guidelines,
    "submit_application": tool_submit_application,
    "check_application_status": tool_check_application_status,
    "withdraw_application": tool_withdraw_application,
    "download_acknowledgment": tool_download_acknowledgment,
    "find_nearest_helpdesk": tool_find_nearest_helpdesk,
}


def handle_jsonrpc(req_data):
    """Processes JSON-RPC 2.0 requests for the Citizen MCP Server."""
    req_id = req_data.get("id")
    method = req_data.get("method")
    params = req_data.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {"listChanged": False}
                },
                "serverInfo": {
                    "name": "sewasetu-citizen-mcp",
                    "version": "1.0.0",
                    "description": "Sewa Setu Citizen Assistant MCP Server (Scope: citizen)"
                }
            }
        }

    elif method == "notifications/initialized":
        return None  # Notifications do not return a response

    elif method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": CITIZEN_TOOLS}
        }

    elif method == "tools/call":
        tool_name = params.get("name")
        tool_args = params.get("arguments", {})
        if tool_name not in TOOL_DISPATCH:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Tool '{tool_name}' not found on Citizen MCP server."}
            }
        try:
            result = TOOL_DISPATCH[tool_name](tool_args)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result, indent=2, ensure_ascii=False)
                        }
                    ]
                }
            }
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": str(e)}
            }

    else:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method '{method}' not implemented"}
        }


# ---------------------------------------------------------------------------
# HTTP & SSE Routes
# ---------------------------------------------------------------------------

@citizen_mcp_bp.route("/mcp/citizen", methods=["POST"])
def direct_mcp():
    """Streamable HTTP endpoint for direct JSON-RPC 2.0 communication."""
    data = request.get_json(force=True, silent=True) or {}
    resp = handle_jsonrpc(data)
    if resp is None:
        return ("", 204)
    return jsonify(resp)


@citizen_mcp_bp.route("/mcp/citizen/sse", methods=["GET"])
def sse_handshake():
    """Server-Sent Events stream initialization. Sends endpoint event with session URI."""
    session_id = secrets.token_hex(16)
    q = queue.Queue()
    SSE_SESSIONS[session_id] = q

    def event_stream():
        # Yield the endpoint URL as per standard MCP SSE transport spec
        endpoint_url = f"/mcp/citizen/messages?session_id={session_id}"
        yield f"event: endpoint\ndata: {endpoint_url}\n\n"
        while True:
            try:
                msg = q.get(timeout=25)
                yield f"event: message\ndata: {json.dumps(msg)}\n\n"
            except queue.Empty:
                # Keep-alive heartbeat ping
                yield ": ping\n\n"

    return Response(event_stream(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive"
    })


@citizen_mcp_bp.route("/mcp/citizen/messages", methods=["POST"])
def sse_messages():
    """Receives JSON-RPC messages for an active SSE session."""
    session_id = request.args.get("session_id")
    data = request.get_json(force=True, silent=True) or {}
    resp = handle_jsonrpc(data)

    if session_id and session_id in SSE_SESSIONS and resp is not None:
        SSE_SESSIONS[session_id].put(resp)

    # Also return response directly in HTTP POST body for clients that expect it
    if resp is not None:
        return jsonify(resp)
    return ("", 202)

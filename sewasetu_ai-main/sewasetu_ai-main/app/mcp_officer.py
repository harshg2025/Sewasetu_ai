# -*- coding: utf-8 -*-
"""
Sewa Setu — Block Officer MCP Server
Exposes Model Context Protocol (MCP) over Streamable HTTP and Server-Sent Events (SSE).

Scope: officer
Endpoints:
  - GET  /mcp/officer/sse          (SSE handshake endpoint)
  - POST /mcp/officer/messages     (Session message endpoint)
  - POST /mcp/officer              (Direct Streamable HTTP JSON-RPC endpoint)

Guaranteed Separation: This server exposes strictly departmental officer tools
(queue management, compliance review, approval/rejection decisions, SLA enforcement).
"""

import os
import json
import queue
import secrets
from datetime import datetime, timedelta
from flask import Blueprint, request, Response, jsonify

import db_adapter
from sms_gateway import record_sms

officer_mcp_bp = Blueprint("mcp_officer", __name__)

SSE_SESSIONS = {}

MIN_AGE = 60
BLOCKS = ["Sonari", "Rajapara", "Dhemaji Pathar", "Borgaon", "Namti", "Khelua"]
SLA_DAYS = 15

# ---------------------------------------------------------------------------
# Tool Implementations
# ---------------------------------------------------------------------------

def tool_list_applications(args):
    """Lists applications in the departmental queue with filters and SLA status."""
    status = args.get("status", "PENDING").upper()
    block = args.get("block", "").strip()
    mobile = args.get("mobile", "").strip()
    overdue_only = bool(args.get("overdue_only", False))
    page = max(1, int(args.get("page", 1)))
    limit = min(100, max(1, int(args.get("limit", 25))))
    offset = (page - 1) * limit

    conn = db_adapter.get_connection("localhost", 5432, "sewasetu", "sewasetu", "sewasetu123")
    cur = conn.cursor()

    conditions = []
    params = []

    if status != "ALL":
        conditions.append("status = %s")
        params.append(status)

    if block:
        conditions.append("block = %s")
        params.append(block)

    if mobile:
        conditions.append("mobile = %s")
        params.append(mobile)

    cutoff = datetime.now() - timedelta(days=SLA_DAYS)
    if overdue_only:
        conditions.append("status = 'PENDING'")
        conditions.append("submitted_at < %s")
        params.append(cutoff)

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    # Count total
    cur.execute(f"SELECT count(*) FROM applications {where_clause}", tuple(params))
    total_count = cur.fetchone()[0]

    # Fetch rows
    query = f"""
    SELECT id, application_no, applicant_name, mobile, dob, village, block, status, submitted_at
    FROM applications {where_clause}
    ORDER BY submitted_at ASC
    LIMIT %s OFFSET %s
    """
    fetch_params = list(params) + [limit, offset]
    cur.execute(query, tuple(fetch_params))
    rows = cur.fetchall()
    conn.close()

    items = []
    now = datetime.now()
    for r in rows:
        app_id, no, name, mob, dob, vill, blk, st, sub_at = r
        days_in_q = 0
        is_overdue = False
        if sub_at:
            try:
                sub_dt = datetime.strptime(str(sub_at)[:19], "%Y-%m-%d %H:%M:%S")
                delta = now - sub_dt
                days_in_q = delta.days
                is_overdue = (days_in_q >= SLA_DAYS and st == "PENDING")
            except Exception:
                pass

        items.append({
            "id": app_id,
            "application_no": no,
            "applicant_name": name,
            "mobile": mob,
            "dob": str(dob),
            "village": vill,
            "block": blk,
            "status": st,
            "submitted_at": str(sub_at),
            "days_in_queue": days_in_q,
            "sla_overdue": is_overdue,
            "sla_days_remaining": max(0, SLA_DAYS - days_in_q) if st == "PENDING" else 0
        })

    return {
        "filter": {"status": status, "block": block or "All", "overdue_only": overdue_only},
        "pagination": {"page": page, "limit": limit, "total_records": total_count},
        "applications": items
    }


def tool_get_application_details(args):
    """Retrieves complete dossier and audit history for an application."""
    app_no = args.get("application_no", "").strip()
    app_id = args.get("app_id")

    if not app_no and not app_id:
        return {"error": "Either application_no or app_id is required."}

    conn = db_adapter.get_connection("localhost", 5432, "sewasetu", "sewasetu", "sewasetu123")
    cur = conn.cursor()

    if app_no:
        cur.execute("""
        SELECT id, application_no, applicant_name, mobile, dob, gender, marital_status,
               husband_name, village, block, bank_account, ifsc, doc_path, status,
               submitted_at, decided_at, decided_by
        FROM applications WHERE application_no = %s
        """, (app_no,))
    else:
        cur.execute("""
        SELECT id, application_no, applicant_name, mobile, dob, gender, marital_status,
               husband_name, village, block, bank_account, ifsc, doc_path, status,
               submitted_at, decided_at, decided_by
        FROM applications WHERE id = %s
        """, (app_id,))

    row = cur.fetchone()
    if not row:
        conn.close()
        return {"found": False, "error": "Application not found."}

    (db_id, no, name, mob, dob, gen, mar, hus, vill, blk, acct, ifsc, doc,
     st, sub_at, dec_at, dec_by) = row

    # Fetch audit log
    cur.execute("SELECT at, action, actor, note FROM audit_log WHERE application_id = %s ORDER BY at ASC", (db_id,))
    audit_history = []
    for a in cur.fetchall():
        audit_history.append({
            "timestamp": str(a[0]),
            "action": a[1],
            "actor": a[2],
            "note": a[3] or ""
        })

    conn.close()

    # Calculate days in queue
    days_elapsed = 0
    if sub_at:
        try:
            sub_dt = datetime.strptime(str(sub_at)[:19], "%Y-%m-%d %H:%M:%S")
            days_elapsed = (datetime.now() - sub_dt).days
        except Exception:
            pass

    return {
        "found": True,
        "id": db_id,
        "application_no": no,
        "applicant": {
            "name": name,
            "dob": str(dob),
            "gender": gen,
            "marital_status": mar,
            "husband_name": hus or None,
            "mobile": mob,
            "village": vill,
            "block": blk
        },
        "bank_details": {
            "account_number": acct,
            "ifsc": ifsc
        },
        "document": {
            "doc_path": doc or "Document verified at submission",
            "type": "Age Proof (Aadhaar/Voter/Certificate)"
        },
        "status": st,
        "submitted_at": str(sub_at),
        "decided_at": str(dec_at) if dec_at else None,
        "decided_by": dec_by or None,
        "days_in_queue": days_elapsed,
        "sla_breached": (days_elapsed >= SLA_DAYS and st == "PENDING"),
        "audit_trail": audit_history
    }


def tool_review_application_eligibility(args):
    """Performs an automated compliance audit on an application."""
    details = tool_get_application_details(args)
    if not details.get("found"):
        return details

    app = details["applicant"]
    bank = details["bank_details"]
    st = details["status"]

    checks = []
    all_pass = True

    # 1. Age check
    dob_str = app.get("dob")
    age = 0
    if dob_str:
        try:
            dob_dt = datetime.strptime(str(dob_str)[:10], "%Y-%m-%d").date()
            today = datetime.now().date()
            age = today.year - dob_dt.year - ((today.month, today.day) < (dob_dt.month, dob_dt.day))
            age_ok = age >= MIN_AGE
            checks.append({
                "rule": "Statutory Minimum Age",
                "condition": ">= 60 years",
                "actual": f"{age} years",
                "passed": age_ok
            })
            if not age_ok:
                all_pass = False
        except Exception:
            checks.append({"rule": "Statutory Minimum Age", "passed": False, "actual": "Invalid DOB format"})
            all_pass = False

    # 2. Block check
    blk = app.get("block")
    blk_ok = blk in BLOCKS
    checks.append({
        "rule": "Operational Block",
        "condition": f"One of {', '.join(BLOCKS)}",
        "actual": blk,
        "passed": blk_ok
    })
    if not blk_ok:
        all_pass = False

    # 3. Bank Account Digits
    acct = str(bank.get("account_number") or "").replace(" ", "").replace("-", "")
    acct_ok = (9 <= len(acct) <= 18) and acct.isdigit()
    checks.append({
        "rule": "Bank Account Format",
        "condition": "9 to 18 digits",
        "actual": f"{len(acct)} digits",
        "passed": acct_ok
    })
    if not acct_ok:
        all_pass = False

    # 4. Bank IFSC
    import re
    ifsc = (bank.get("ifsc") or "").strip().upper()
    ifsc_ok = bool(re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", ifsc))
    checks.append({
        "rule": "Bank IFSC Format",
        "condition": "11 characters (4 letters, 0, 6 alphanumeric)",
        "actual": ifsc,
        "passed": ifsc_ok
    })
    if not ifsc_ok:
        all_pass = False

    # 5. SLA Warning
    days_in_q = details.get("days_in_queue", 0)
    sla_warning = days_in_q >= SLA_DAYS
    checks.append({
        "rule": "Statutory SLA (RTPS Act)",
        "condition": f"Processed within {SLA_DAYS} days",
        "actual": f"{days_in_q} days elapsed",
        "passed": not sla_warning
    })

    return {
        "application_no": details["application_no"],
        "applicant_name": app["name"],
        "current_status": st,
        "compliance_scorecard": checks,
        "overall_eligible": all_pass,
        "recommended_action": "APPROVE" if all_pass and st == "PENDING" else ("REJECT" if not all_pass and st == "PENDING" else "ALREADY_DECIDED"),
        "note": "All statutory requirements satisfied." if all_pass else "One or more statutory criteria failed."
    }


def tool_approve_application(args):
    """Approves a PENDING application, logging decision and sending SMS."""
    app_no = args.get("application_no", "").strip()
    app_id = args.get("app_id")
    officer_name = args.get("officer_name", "admin").strip()
    note = args.get("note", "Application approved after statutory verification").strip()

    if not app_no and not app_id:
        return {"success": False, "error": "application_no or app_id is required."}

    conn = db_adapter.get_connection("localhost", 5432, "sewasetu", "sewasetu", "sewasetu123")
    cur = conn.cursor()

    if app_no:
        cur.execute("SELECT id, application_no, mobile, applicant_name, status FROM applications WHERE application_no = %s", (app_no,))
    else:
        cur.execute("SELECT id, application_no, mobile, applicant_name, status FROM applications WHERE id = %s", (app_id,))

    row = cur.fetchone()
    if not row:
        conn.close()
        return {"success": False, "error": "Application not found."}

    db_id, matched_no, mobile, name, current_status = row
    if current_status != "PENDING":
        conn.close()
        return {"success": False, "error": f"Application {matched_no} is currently '{current_status}'. Only PENDING applications can be approved."}

    now = datetime.now()
    actor = f"admin:{officer_name}"

    cur.execute("UPDATE applications SET status = 'APPROVED', decided_at = %s, decided_by = %s WHERE id = %s",
                (now, officer_name, db_id))
    cur.execute("INSERT INTO audit_log (application_id, action, actor, note, at) VALUES (%s, 'APPROVED', %s, %s, %s)",
                (db_id, actor, note, now))
    conn.commit()
    conn.close()

    # Enqueue SMS to beneficiary
    sms_text = f"Sewa Setu: Your Old Age Pension application {matched_no} has been APPROVED. Monthly pension ₹1,000 will be credited to your bank account."
    record_sms(mobile, sms_text)

    return {
        "success": True,
        "application_no": matched_no,
        "applicant_name": name,
        "new_status": "APPROVED",
        "decided_by": officer_name,
        "decided_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "note": note,
        "sms_notification_sent": True
    }


def tool_reject_application(args):
    """Rejects a PENDING application with mandatory statutory reason."""
    app_no = args.get("application_no", "").strip()
    app_id = args.get("app_id")
    reason = args.get("reason", "").strip()
    officer_name = args.get("officer_name", "admin").strip()

    if not reason:
        return {"success": False, "error": "A mandatory statutory reason is required to reject an application."}

    if not app_no and not app_id:
        return {"success": False, "error": "application_no or app_id is required."}

    conn = db_adapter.get_connection("localhost", 5432, "sewasetu", "sewasetu", "sewasetu123")
    cur = conn.cursor()

    if app_no:
        cur.execute("SELECT id, application_no, mobile, applicant_name, status FROM applications WHERE application_no = %s", (app_no,))
    else:
        cur.execute("SELECT id, application_no, mobile, applicant_name, status FROM applications WHERE id = %s", (app_id,))

    row = cur.fetchone()
    if not row:
        conn.close()
        return {"success": False, "error": "Application not found."}

    db_id, matched_no, mobile, name, current_status = row
    if current_status != "PENDING":
        conn.close()
        return {"success": False, "error": f"Application {matched_no} is currently '{current_status}'. Only PENDING applications can be decided."}

    now = datetime.now()
    actor = f"admin:{officer_name}"

    cur.execute("UPDATE applications SET status = 'REJECTED', decided_at = %s, decided_by = %s WHERE id = %s",
                (now, officer_name, db_id))
    cur.execute("INSERT INTO audit_log (application_id, action, actor, note, at) VALUES (%s, 'REJECTED', %s, %s, %s)",
                (db_id, actor, reason, now))
    conn.commit()
    conn.close()

    # Enqueue SMS to applicant
    sms_text = f"Sewa Setu: Your pension application {matched_no} was not approved. Reason: {reason}. You may appeal at your Block Development Office."
    record_sms(mobile, sms_text)

    return {
        "success": True,
        "application_no": matched_no,
        "applicant_name": name,
        "new_status": "REJECTED",
        "decided_by": officer_name,
        "decided_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "reason": reason,
        "sms_notification_sent": True
    }


def tool_get_dashboard_analytics(args):
    """Retrieves high-level summary metrics, SLA status, and block distributions."""
    conn = db_adapter.get_connection("localhost", 5432, "sewasetu", "sewasetu", "sewasetu123")
    cur = conn.cursor()

    cur.execute("SELECT status, count(*) FROM applications GROUP BY status")
    by_status = {r[0]: r[1] for r in cur.fetchall()}

    cutoff = datetime.now() - timedelta(days=SLA_DAYS)
    cur.execute("SELECT count(*) FROM applications WHERE status = 'PENDING' AND submitted_at < %s", (cutoff,))
    overdue_count = cur.fetchone()[0]

    cur.execute("SELECT block, count(*) FROM applications WHERE status = 'PENDING' GROUP BY block ORDER BY count(*) DESC")
    pending_by_block = {r[0]: r[1] for r in cur.fetchall()}

    cur.execute("SELECT count(*) FROM applications")
    total_apps = cur.fetchone()[0]

    conn.close()

    total_decided = by_status.get("APPROVED", 0) + by_status.get("DEEMED_APPROVED", 0) + by_status.get("REJECTED", 0)
    approval_rate = round(((by_status.get("APPROVED", 0) + by_status.get("DEEMED_APPROVED", 0)) / total_decided * 100), 2) if total_decided > 0 else 0

    return {
        "total_applications": total_apps,
        "status_distribution": by_status,
        "statutory_sla_days": SLA_DAYS,
        "pending_overdue_sla_breaches": overdue_count,
        "pending_by_block": pending_by_block,
        "approval_rate_percent": approval_rate,
        "rtps_deemed_approved_count": by_status.get("DEEMED_APPROVED", 0)
    }


def tool_trigger_sla_deemed_approvals(args):
    """Executes the RTPS Act deemed approval process for applications pending > 15 days."""
    conn = db_adapter.get_connection("localhost", 5432, "sewasetu", "sewasetu", "sewasetu123")
    cur = conn.cursor()

    cutoff = datetime.now() - timedelta(days=SLA_DAYS)
    now = datetime.now()

    cur.execute("""
    UPDATE applications
    SET status = 'DEEMED_APPROVED', decided_at = %s, decided_by = 'RTPS-AUTO'
    WHERE status = 'PENDING' AND submitted_at < %s
    RETURNING id, application_no, mobile
    """, (now, cutoff))

    updated_rows = cur.fetchall()

    for app_id, app_no, mobile in updated_rows:
        cur.execute("INSERT INTO audit_log (application_id, action, actor, note, at) "
                    "VALUES (%s, 'DEEMED_APPROVED', 'system:rtps-cron', 'Approved automatically under RTPS Act (15-day SLA)', %s)",
                    (app_id, now))
        sms_text = f"Sewa Setu: your pension application {app_no} stands APPROVED under the RTPS Act statutory SLA."
        record_sms(mobile, sms_text)

    conn.commit()
    conn.close()

    return {
        "success": True,
        "applications_deemed_approved_count": len(updated_rows),
        "processed_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "sample_processed": [r[1] for r in updated_rows[:10]],
        "message": f"Successfully deemed approved {len(updated_rows)} applications under the Right to Public Services Act."
    }


# ---------------------------------------------------------------------------
# MCP Tool Registry (Officer Assistant)
# ---------------------------------------------------------------------------

OFFICER_TOOLS = [
    {
        "name": "list_applications",
        "description": "List applications in the queue with filtering by status (PENDING, APPROVED, REJECTED, etc.), block, or SLA breach.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["PENDING", "APPROVED", "REJECTED", "DEEMED_APPROVED", "ALL"], "default": "PENDING"},
                "block": {"type": "string", "description": "Filter by block"},
                "mobile": {"type": "string", "description": "Filter by 10-digit mobile number"},
                "overdue_only": {"type": "boolean", "description": "Filter applications pending > 15 days SLA"},
                "page": {"type": "integer", "default": 1},
                "limit": {"type": "integer", "default": 25}
            }
        }
    },
    {
        "name": "get_application_details",
        "description": "Fetch full applicant dossier, bank details, age proof document, and complete chronological audit log.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "application_no": {"type": "string", "description": "Application number (e.g. SSP26123456)"},
                "app_id": {"type": "integer", "description": "Database ID of application"}
            }
        }
    },
    {
        "name": "review_application_eligibility",
        "description": "Run an automated rule-verification scorecard on an application: age, block, IFSC format, bank digits, and SLA countdown.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "application_no": {"type": "string", "description": "Application number"},
                "app_id": {"type": "integer", "description": "Database ID"}
            }
        }
    },
    {
        "name": "approve_application",
        "description": "Approve a PENDING application, record officer audit note, and trigger beneficiary SMS notification.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "application_no": {"type": "string", "description": "Application number to approve"},
                "app_id": {"type": "integer", "description": "Database ID"},
                "note": {"type": "string", "description": "Approval remark for audit log"},
                "officer_name": {"type": "string", "default": "admin", "description": "Name or username of officer deciding"}
            }
        }
    },
    {
        "name": "reject_application",
        "description": "Reject a PENDING application with mandatory statutory justification, recording audit log and sending SMS.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "application_no": {"type": "string", "description": "Application number to reject"},
                "app_id": {"type": "integer", "description": "Database ID"},
                "reason": {"type": "string", "description": "Mandatory statutory rejection justification"},
                "officer_name": {"type": "string", "default": "admin", "description": "Name or username of deciding officer"}
            },
            "required": ["reason"]
        }
    },
    {
        "name": "get_dashboard_analytics",
        "description": "Get high-level statistics: status counts, SLA breaches, block distributions, and approval rates.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "block": {"type": "string", "description": "Optional block filter"}
            }
        }
    },
    {
        "name": "trigger_sla_deemed_approvals",
        "description": "Execute statutory deemed-approval process for all applications pending > 15 days under the RTPS Act.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "force": {"type": "boolean", "default": True}
            }
        }
    }
]

TOOL_DISPATCH = {
    "list_applications": tool_list_applications,
    "get_application_details": tool_get_application_details,
    "review_application_eligibility": tool_review_application_eligibility,
    "approve_application": tool_approve_application,
    "reject_application": tool_reject_application,
    "get_dashboard_analytics": tool_get_dashboard_analytics,
    "trigger_sla_deemed_approvals": tool_trigger_sla_deemed_approvals
}


def handle_jsonrpc(req_data):
    """Processes JSON-RPC 2.0 requests for the Block Officer MCP Server."""
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
                    "name": "sewasetu-officer-mcp",
                    "version": "1.0.0",
                    "description": "Sewa Setu Block Officer MCP Server (Scope: officer)"
                }
            }
        }

    elif method == "notifications/initialized":
        return None

    elif method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": OFFICER_TOOLS}
        }

    elif method == "tools/call":
        tool_name = params.get("name")
        tool_args = params.get("arguments", {})
        if tool_name not in TOOL_DISPATCH:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Tool '{tool_name}' not found on Officer MCP server."}
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

@officer_mcp_bp.route("/mcp/officer", methods=["POST"])
def direct_mcp():
    """Streamable HTTP endpoint for direct JSON-RPC 2.0 communication."""
    data = request.get_json(force=True, silent=True) or {}
    resp = handle_jsonrpc(data)
    if resp is None:
        return ("", 204)
    return jsonify(resp)


@officer_mcp_bp.route("/mcp/officer/sse", methods=["GET"])
def sse_handshake():
    """Server-Sent Events stream initialization. Sends endpoint event with session URI."""
    session_id = secrets.token_hex(16)
    q = queue.Queue()
    SSE_SESSIONS[session_id] = q

    def event_stream():
        endpoint_url = f"/mcp/officer/messages?session_id={session_id}"
        yield f"event: endpoint\ndata: {endpoint_url}\n\n"
        while True:
            try:
                msg = q.get(timeout=25)
                yield f"event: message\ndata: {json.dumps(msg)}\n\n"
            except queue.Empty:
                yield ": ping\n\n"

    return Response(event_stream(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive"
    })


@officer_mcp_bp.route("/mcp/officer/messages", methods=["POST"])
def sse_messages():
    """Receives JSON-RPC messages for an active SSE session."""
    session_id = request.args.get("session_id")
    data = request.get_json(force=True, silent=True) or {}
    resp = handle_jsonrpc(data)

    if session_id and session_id in SSE_SESSIONS and resp is not None:
        SSE_SESSIONS[session_id].put(resp)

    if resp is not None:
        return jsonify(resp)
    return ("", 202)

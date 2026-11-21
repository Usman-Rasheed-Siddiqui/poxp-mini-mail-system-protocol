--[[
  poxp.lua
  Wireshark Lua dissector for the Post Office Xchange Protocol (POXP/1.0).

  Install (same file works on Linux and Windows):
    1. In Wireshark: Help -> About Wireshark -> Folders tab -> note the path
       next to "Personal Lua Plugins" (this is the only part that differs
       between OSes; the Lua code itself does not).
         Typical paths:
           Windows : %APPDATA%\Wireshark\plugins\
           Linux   : ~/.local/lib/wireshark/plugins/  (or ~/.wireshark/plugins/
                      on older installs)
    2. Copy this file into that folder.
    3. Restart Wireshark (or Analyze -> Reload Lua Plugins).
    4. Any TCP traffic on the ports registered below will now show up with
       "POXP" in the Protocol column instead of plain "TCP".

  What it dissects:
    - The request/status line (SUBMIT, RELAY, REGISTER, FETCH, or a 3-digit
      ACK code such as "200 OK POXP/1.0"). EVERY POXP line ends with the
      "POXP/1.0" protocol token; a request without it is flagged (the server
      answers such a request with 400 BAD_REQUEST).
    - The status code table (see STATUS_TABLE below): 200 OK, 201 REGISTERED,
      400 BAD_REQUEST, 404 CLIENT_NOT_FOUND, 480 CLIENT_UNAVAILABLE,
      500 SERVER_ERROR, 502 RELAY_ERROR, 503 SERVER_UNAVAILABLE. Each ACK shows
      its meaning, and error codes (>= 400) raise an expert-info warning.
      Note: 503 is raised by the client itself when it cannot reach its own
      server, so it never appears on the wire / in a capture.
    - Every "Key: Value" header line, exposed both as a generic repeated
      field and as named fields for the headers POXP actually uses
      (Sender/Receiver email, Identifier, Priority, Content-Type, Length,
      Attachment-Name/Length, Server id, Client email, IP/Subnet Mask/Gate/DNS).
    - The message body, with attachment envelopes
      (---POXP_ATTACHMENT_BEGIN---/---POXP_ATTACHMENT_END---) recognised and
      shown as a separate attachment field rather than dumped inline.
    - Per-message and per-TCP-segment byte counts, so a capture screenshot can
      show application-layer message length next to the TCP segment length —
      useful for the R5 encapsulation screenshot and the R6 throughput numbers.

  Framing / reassembly notes:
    POXP messages whose header block includes a "Length:" header (SUBMIT,
    RELAY, REGISTER, FETCH requests) are precisely framed: header bytes up to
    the blank-line separator, plus exactly Length bytes of body. This
    dissector uses that header to ask Wireshark's TCP reassembly to wait for
    the rest of a message that spans multiple TCP segments (e.g. a large
    base64 attachment split across many packets) before dissecting it, the
    same guarantee the actual server code gets from reading until the
    client's half-close.

    ACK/status replies (e.g. "200 OK") do not carry a Length header in this
    protocol version. For those this dissector dissects whatever has arrived
    in the current reassembled segment on a best-effort basis; this matches
    the real server/client behaviour, which relies on the TCP half-close
    (shutdown(SHUT_WR)) rather than a length field for those replies. In an
    already-captured pcap this is rarely an issue because Wireshark's TCP
    stream reassembly for "Follow TCP Stream" still shows the complete
    exchange regardless of what this dissector chooses to parse per-segment.
]]

local poxp_proto = Proto("poxp", "Post Office Xchange Protocol")

----------------------------------------------------------------------
-- Fields
----------------------------------------------------------------------

local f_msg_type        = ProtoField.string("poxp.msg_type", "Message Type")           -- REQUEST / ACK
local f_method_or_code  = ProtoField.string("poxp.method_or_code", "Method / Status Code")
local f_version_or_name = ProtoField.string("poxp.version_or_name", "Status Name")
local f_proto_version   = ProtoField.string("poxp.protocol_version", "Protocol Version")
local f_proto_ok        = ProtoField.bool("poxp.protocol_token_ok", "Carries POXP/1.0 token")
local f_status_meaning  = ProtoField.string("poxp.status_meaning", "Status Meaning")
local f_header_line     = ProtoField.string("poxp.header", "Header")                     -- generic, repeats
local f_header_count    = ProtoField.uint32("poxp.header_count", "Header Count")

-- Named headers
local f_sender_email    = ProtoField.string("poxp.sender_email", "Sender Email")
local f_receiver_email  = ProtoField.string("poxp.receiver_email", "Receiver Email")
local f_client_email    = ProtoField.string("poxp.client_email", "Client Email")
local f_identifier      = ProtoField.string("poxp.identifier", "Identifier (UUID)")
local f_priority        = ProtoField.string("poxp.priority", "Priority")
local f_content_type    = ProtoField.string("poxp.content_type", "Content-Type")
local f_date             = ProtoField.string("poxp.date", "Date")
local f_server_id       = ProtoField.string("poxp.server_id", "Server id")
local f_dns             = ProtoField.string("poxp.dns", "DNS")
local f_subnet          = ProtoField.string("poxp.subnet_mask", "Subnet Mask")
local f_gateway         = ProtoField.string("poxp.gateway", "Gateway")
local f_ip              = ProtoField.string("poxp.client_ip", "Client IP")
local f_time_of_ack     = ProtoField.string("poxp.time_of_ack", "Time of Acknowledgement")
local f_error_at        = ProtoField.string("poxp.error_at", "Error at")

-- Body / attachment / length accounting
local f_header_length   = ProtoField.uint32("poxp.header_length", "Header Block Length (bytes)")
local f_declared_length  = ProtoField.uint32("poxp.declared_length", "Declared Body Length (Length header, bytes)")
local f_body             = ProtoField.string("poxp.body", "Body (text, truncated)")
local f_has_attachment   = ProtoField.bool("poxp.has_attachment", "Has Attachment")
local f_attachment_name = ProtoField.string("poxp.attachment_name", "Attachment-Name")
local f_attachment_len  = ProtoField.uint32("poxp.attachment_length", "Attachment-Length (bytes, decoded)")
local f_attachment_b64_len = ProtoField.uint32("poxp.attachment_b64_length", "Attachment base64 payload length (chars, this segment)")

-- Wire-level byte accounting requested for the R5/R6 report sections
local f_segment_bytes    = ProtoField.uint32("poxp.segment_bytes", "TCP Segment Payload Bytes (bytes, after TCP reassembly)")
local f_total_msg_bytes  = ProtoField.uint32("poxp.total_message_bytes", "Total POXP Message Bytes (reassembled)")

poxp_proto.fields = {
    f_msg_type, f_method_or_code, f_version_or_name, f_proto_version, f_proto_ok, f_status_meaning,
    f_header_line, f_header_count,
    f_sender_email, f_receiver_email, f_client_email, f_identifier, f_priority,
    f_content_type, f_date, f_server_id, f_dns, f_subnet, f_gateway, f_ip,
    f_time_of_ack, f_error_at,
    f_header_length, f_declared_length, f_body,
    f_has_attachment, f_attachment_name, f_attachment_len, f_attachment_b64_len,
    f_segment_bytes, f_total_msg_bytes,
}

-- Expert info (shows up in Wireshark's Expert Information window / tree)
local ef_error_status = ProtoExpert.new("poxp.error_status", "POXP error status code",
    expert.group.RESPONSE_CODE, expert.severity.WARN)
local ef_bad_protocol = ProtoExpert.new("poxp.bad_protocol_token",
    "Message is missing the POXP/1.0 protocol token (server replies 400 BAD_REQUEST)",
    expert.group.MALFORMED, expert.severity.ERROR)
poxp_proto.experts = { ef_error_status, ef_bad_protocol }

----------------------------------------------------------------------
-- Helpers
----------------------------------------------------------------------

local POXP_VERSION = "POXP/1.0"

-- Must stay in sync with the STATUS_* constants in poxp_protocol.py.
local STATUS_TABLE = {
    ["200"] = { "OK",                 "Request succeeded / email delivered and fetched by the recipient" },
    ["201"] = { "REGISTERED",         "Client registered with its server" },
    ["400"] = { "BAD_REQUEST",        "Malformed request (missing headers, unknown method or missing POXP/1.0 token)" },
    ["404"] = { "CLIENT_NOT_FOUND",   "Recipient is not known to any server (not in DNS / hosts table)" },
    ["480"] = { "CLIENT_UNAVAILABLE", "Recipient is known but not active: server held the mail 10s, then deleted it" },
    ["500"] = { "SERVER_ERROR",       "Unexpected error inside the server" },
    ["502"] = { "RELAY_ERROR",        "Relay failed: destination server is down/unreachable or unknown" },
    ["503"] = { "SERVER_UNAVAILABLE", "Client could not reach its own server (raised client-side, never on the wire)" },
}

local NAMED_HEADER_FIELDS = {
    ["Sender email"]              = f_sender_email,
    ["Receiver email"]            = f_receiver_email,
    ["Client email"]              = f_client_email,
    ["Identifier"]                = f_identifier,
    ["Priority"]                  = f_priority,
    ["Content-Type"]              = f_content_type,
    ["Date"]                      = f_date,
    ["Server id"]                 = f_server_id,
    ["DNS"]                       = f_dns,
    ["Subnet Mask"]               = f_subnet,
    ["Gate"]                      = f_gateway,
    ["IP"]                        = f_ip,
    ["Time of Acknowledgement"]   = f_time_of_ack,
    ["Error at"]                  = f_error_at,
}

-- Finds the byte offset (0-based, Lua 1-based internally) right after the
-- first blank-line separator ("\n\n") in a plain Lua string. Returns nil if
-- not present yet.
local function find_header_end(s)
    local i, j = string.find(s, "\n\n", 1, true)
    if not i then
        return nil
    end
    return j -- index of the 2nd '\n' of the separator (1-based)
end

local function parse_header_block(header_block)
    local lines = {}
    for line in (header_block .. "\n"):gmatch("([^\n]*)\n") do
        table.insert(lines, line)
    end

    local first_line = lines[1] or ""
    local first_token, rest = first_line:match("^(%S+)%s*(.*)$")
    first_token = first_token or ""
    rest = rest or ""

    local headers = {}
    local header_order = {}
    for i = 2, #lines do
        local line = lines[i]
        local key, val = line:match("^([^:]+):%s*(.*)$")
        if key then
            headers[key] = val
            table.insert(header_order, key)
        end
    end

    local is_ack = (first_token:match("^%d+$") ~= nil)
    return {
        first_token = first_token,
        rest = rest,
        headers = headers,
        header_order = header_order,
        is_ack = is_ack,
    }
end

----------------------------------------------------------------------
-- Main dissector
----------------------------------------------------------------------

function poxp_proto.dissector(buffer, pinfo, tree)
    local buf_len = buffer:len()
    if buf_len == 0 then
        return 0
    end

    local raw = buffer:raw()

    local header_end = find_header_end(raw)
    if not header_end then
        -- Header block (status line + headers + blank line) hasn't fully
        -- arrived yet in this reassembled segment. Ask Wireshark for more.
        pinfo.desegment_len = DESEGMENT_ONE_MORE_SEGMENT
        pinfo.desegment_offset = 0
        return
    end

    local header_block = raw:sub(1, header_end - 2) -- exclude the trailing "\n\n"
    local parsed = parse_header_block(header_block)

    local declared_len = tonumber(parsed.headers["Length"])
    local total_needed = nil
    if declared_len ~= nil then
        total_needed = header_end + declared_len
        if buf_len < total_needed then
            pinfo.desegment_len = total_needed - buf_len
            pinfo.desegment_offset = 0
            return
        end
    end

    -- We have a full header block, and (if a Length header exists) the full
    -- declared body too. Dissect what we have.
    local body_start = header_end + 1
    local body_end = total_needed or buf_len
    local body = raw:sub(body_start, body_end)

    pinfo.cols.protocol = "POXP"

    local subtree = tree:add(poxp_proto, buffer(), "Post Office Xchange Protocol")

    subtree:add(f_msg_type, parsed.is_ack and "ACK" or "REQUEST")
    subtree:add(f_method_or_code, parsed.first_token)

    -- Every POXP status/request line now ends with a "POXP/x.y" token
    -- (e.g. "SUBMIT POXP/1.0", "200 OK POXP/1.0"). Split it into its own
    -- field so ACKs and requests both show a clean protocol-version column
    -- instead of lumping it in with the ACK's status name.
    local status_name = parsed.rest
    local ack_status_name = nil
    local proto_version = parsed.rest:match("(POXP/[%d%.]+)%s*$")
    if proto_version then
        if parsed.is_ack then
            ack_status_name = parsed.rest:sub(1, #parsed.rest - #proto_version):gsub("%s+$", "")
        end
        subtree:add(f_proto_version, proto_version)
    end
    if parsed.is_ack then
        subtree:add(f_version_or_name, ack_status_name or status_name)
        local entry = STATUS_TABLE[parsed.first_token]
        if entry then
            subtree:add(f_status_meaning, entry[2])
        end
        if (tonumber(parsed.first_token) or 0) >= 400 then
            subtree:add_proto_expert_info(ef_error_status,
                "POXP error " .. parsed.first_token .. " " .. (ack_status_name or status_name))
        end
    end

    -- Every POXP message (request, relay or ack) must read "<token> ... POXP/1.0".
    local proto_ok = (proto_version == POXP_VERSION)
    local proto_item = subtree:add(f_proto_ok, proto_ok)
    if not proto_ok then
        proto_item:add_proto_expert_info(ef_bad_protocol)
    end

    local hdr_subtree = subtree:add(poxp_proto, buffer(0, header_end), "Headers")
    hdr_subtree:add(f_header_count, #parsed.header_order)
    for _, key in ipairs(parsed.header_order) do
        local val = parsed.headers[key]
        local field = NAMED_HEADER_FIELDS[key]
        if field then
            -- Known header: show it once, as its clean named field only.
            hdr_subtree:add(field, val)
        else
            -- Unrecognized header: fall back to the generic "Key: Value" line
            -- so nothing is silently dropped from the tree.
            hdr_subtree:add(f_header_line, key .. ": " .. val)
        end
    end
    hdr_subtree:add(f_header_length, header_end)
    if declared_len ~= nil then
        hdr_subtree:add(f_declared_length, declared_len)
    end

    -- Attachment detection (only meaningful on REQUEST messages: SUBMIT/RELAY)
    local att_start_tag = "---POXP_ATTACHMENT_BEGIN---"
    local att_end_tag = "---POXP_ATTACHMENT_END---"
    local has_attachment = false
    local att_start_pos = body:find(att_start_tag, 1, true)
    if att_start_pos then
        has_attachment = true
    end

    local body_subtree = subtree:add(poxp_proto, buffer(body_start - 1, body_end - body_start + 1), "Body")
    body_subtree:add(f_has_attachment, has_attachment)

    if has_attachment then
        if parsed.headers["Attachment-Name"] then
            body_subtree:add(f_attachment_name, parsed.headers["Attachment-Name"])
        end
        if parsed.headers["Attachment-Length"] then
            body_subtree:add(f_attachment_len, tonumber(parsed.headers["Attachment-Length"]) or 0)
        end
        local att_end_pos = body:find(att_end_tag, 1, true)
        if att_end_pos then
            local b64_payload_len = att_end_pos - (att_start_pos + #att_start_tag)
            body_subtree:add(f_attachment_b64_len, b64_payload_len)
        end
        -- Show the plain-text part of the body only (not the base64 blob) to
        -- keep the packet detail readable.
        local plain_part = body:sub(1, att_start_pos - 1)
        body_subtree:add(f_body, plain_part:sub(1, 200))
    else
        body_subtree:add(f_body, body:sub(1, 200))
    end

    -- Byte accounting for the R5 / R6 report sections.
    subtree:add(f_segment_bytes, buf_len)
    subtree:add(f_total_msg_bytes, body_end)

    -- Info column summary line.
    --
    -- Only SUBMIT and RELAY are real sender -> receiver mail transfers (they
    -- carry both a "Sender email"/"Client email" and a "Receiver email"
    -- header), so only those get the "from -> to" arrow. FETCH and REGISTER
    -- are single-party requests (a client checking its own mailbox, or
    -- announcing itself to its server) that never had a counterpart header
    -- to put on the other side of the arrow -- that's why it rendered as
    -- "? -> client_a@poxpmail.com" / "client_a@poxpmail.com -> ?" before.
    -- They get a one-sided line instead, with no "?" in it.
    local info
    local method = parsed.first_token
    if parsed.is_ack then
        local id = parsed.headers["Identifier"] or "?"
        local entry = STATUS_TABLE[method]
        local is_err = (tonumber(method) or 0) >= 400
        info = string.format("POXP %s %s %s  [Identifier=%s, %d bytes]",
            is_err and "ERROR" or "ACK", method, ack_status_name or status_name, id, body_end)
        if entry and is_err then
            info = info .. "  -- " .. entry[2]
        end
    elseif method == "FETCH" then
        local who = parsed.headers["Receiver email"] or parsed.headers["Client email"] or "?"
        info = string.format("POXP FETCH  %s checking mailbox  [%d bytes]", who, body_end)
    elseif method == "REGISTER" then
        local who = parsed.headers["Client email"] or "?"
        info = string.format("POXP REGISTER  %s  [%d bytes]", who, body_end)
    else
        -- SUBMIT / RELAY
        local from = parsed.headers["Sender email"] or parsed.headers["Client email"] or "?"
        local to = parsed.headers["Receiver email"] or "?"
        info = string.format("POXP %s  %s -> %s  [%d bytes%s]",
            method, from, to, body_end,
            has_attachment and (", attachment=" .. (parsed.headers["Attachment-Name"] or "?")) or "")
    end
    if not proto_ok then
        info = "[MISSING POXP/1.0 TOKEN] " .. info
    end
    pinfo.cols.info = info

    return body_end
end

----------------------------------------------------------------------
-- Port registration
----------------------------------------------------------------------
-- Adjust/add ports here to match whatever you actually bind server_node.py
-- to (the architecture guide uses 5025 for Server 1/A and 5026 for Server
-- 2/B; the client-facing local FastAPI ports, e.g. 8000/8001, are plain
-- loopback HTTP and should NOT be added here). demo_errors.py uses 5201/5202,
-- so they are registered too.
local tcp_port_table = DissectorTable.get("tcp.port")
tcp_port_table:add(5025, poxp_proto)
tcp_port_table:add(5026, poxp_proto)
tcp_port_table:add(5201, poxp_proto)
tcp_port_table:add(5202, poxp_proto)
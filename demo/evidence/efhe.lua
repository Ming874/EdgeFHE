-- efhe.lua: Wireshark 4.6 (Lua 5.4) dissector for EdgeFHE frames carried inside TLS 1.3 (research demonstration).
--
-- Two protocols:
--   efhe      the EFHEW001 frame in the decrypted TLS application data: the 256-byte public header and, per
--             ciphertext, c0 and c1 with 3 RNS limbs of 4096 uint32 residues each, with uniformity statistics per limb
--             (layout: v8b_rev5/software/client/wire.hpp:5-34; word = ct*6N + poly*3N + limb*N + coefficient,
--             cloud.cpp:33).
--   efhe_rec  a postdissector that labels every TLS record on the EdgeFHE port with what it carries. The client writes
--             the header in one 256-byte write and the payload in 16,384-byte writes (tls.hpp:120), so each record
--             holds exactly one header or one RNS limb: upload record k (k = 1..24) carries ciphertext (k-1)//6,
--             polynomial ((k-1)%6)//3 and limb (k-1)%3; download record k (k = 1..6) carries polynomial (k-1)//3 and
--             limb (k-1)%3. Without the key log the labels come from the record sizes and order and start with "≈";
--             with the key log they use the decrypted content type.
--
-- Install: copy to %APPDATA%\Wireshark\plugins\, or start Wireshark or tshark with -X lua_script:efhe.lua.
-- Decrypt: Preferences > Protocols > TLS > (Pre)-Master-Secret log filename = tls_keys.log, then Ctrl+R; or open the
-- capture_dsb.pcapng that efhe_seal.py writes. Port: Preferences > Protocols > EFHE (default 18443), or
-- -o efhe.port:N in tshark.

local DEFAULT_PORT = 18443
local HDR = 256
local N = 4096
local LIMB_BYTES = N * 4                -- 16,384
local CT_BYTES = 6 * LIMB_BYTES         -- 98,304 = one CKKS ciphertext: (c0, c1) x 3 RNS limbs
local REC_HEADER = HDR + 17             -- 273: TLS 1.3 record of the header (inner type + 16-byte tag)
local REC_LIMB = LIMB_BYTES + 17        -- 16,401: TLS 1.3 record of one limb
local REC_CLOSE = 2 + 17                -- 19: close_notify
local Q = { 998244353, 1004535809, 469762049 }
local SMALL = 1 << 22                   -- |coefficient| bound of an unencrypted Delta = 2^30 encoding of one slot
local KNOWN = {
  ["c01b69cf3da9fb260dbd4ac15080bd6c1c425d90bfb9015c8bacc16c061fa05c"] = "EdgeFHE 研究用線路設定檔",
  ["de0499f25657351b6a1ca2460181c328a05fd43147a0f01aa51f10f89b9dc2a4"] = "ai4i-failure-logreg-v1",
  ["727f278c3d58c26466adef1885342c63940a796a970eeae4d6368508e67ef611"] = "diabetes-progression-ridge-v1",
  ["6395cb10c6041f22490e801b341d0eda9ff090dec4ebf2d69e2c665aa111356d"] = "diabetes-symptom-logreg-v1",
  ["63cee36f9ac68b8e274dc45b4fa2fee0aeae5c4089b3d18fe51d7cf0c588c106"] = "cdc-diabetes-lr4-v1",
}

local efhe = Proto("efhe", "EdgeFHE CKKS 訊框")
local F = efhe.fields
F.magic  = ProtoField.string("efhe.magic", "標記")
F.ver    = ProtoField.uint8 ("efhe.version", "版本欄位")
F.kind   = ProtoField.uint8 ("efhe.kind", "種類", base.DEC, { [1] = "請求（KV260 → 雲端）", [2] = "回應（雲端 → KV260）" })
F.hlen   = ProtoField.uint32("efhe.header_bytes", "標頭位元組數")
F.feat   = ProtoField.uint32("efhe.features", "特徵數（＝請求中的密文數）")
F.rows   = ProtoField.uint32("efhe.rows", "筆數（裝在 CKKS 槽位中的紀錄數）")
F.layout = ProtoField.uint32("efhe.layout", "版面常數")
F.plen   = ProtoField.uint64("efhe.payload_bytes", "酬載位元組數")
F.gen    = ProtoField.uint64("efhe.generation", "世代值（每次執行隨機產生）", base.HEX)
F.seq    = ProtoField.uint64("efhe.sequence", "序號")
F.prof   = ProtoField.bytes ("efhe.profile_id", "設定檔識別碼")
F.model  = ProtoField.bytes ("efhe.model_id", "模型識別碼（模型檔 SHA-256）")
F.key    = ProtoField.bytes ("efhe.key_id", "公鑰識別碼（SHA-256）")
F.rid    = ProtoField.bytes ("efhe.request_id", "請求識別碼")
F.sess   = ProtoField.bytes ("efhe.session_id", "通道綁定值（TLS 匯出值）")
F.rhash  = ProtoField.bytes ("efhe.request_hash", "請求雜湊（request_sha256）")
F.ct     = ProtoField.bytes ("efhe.ciphertext", "CKKS 密文")
F.limb   = ProtoField.bytes ("efhe.limb", "RNS 分量")
F.mean   = ProtoField.double("efhe.limb.mean_q", "平均餘數 / q（均勻分布：0.500 ± 0.0045）")
F.chi2   = ProtoField.double("efhe.limb.chi2", "16 區間卡方值（均勻分布：15 ± 5.5）")
F.small  = ProtoField.double("efhe.limb.small_frac", "距 0 或 q 在 2^22 以內的比例（均勻分布：0.008–0.018）")
F.canon  = ProtoField.bool  ("efhe.limb.canonical", "所有餘數 < q", base.NONE)
F.first  = ProtoField.string("efhe.limb.first", "前幾個餘數")
local E_hdr  = ProtoExpert.new("efhe.bad_header", "標頭不符合 wire.hpp 的版面", expert.group.MALFORMED, expert.severity.ERROR)
local E_nonc = ProtoExpert.new("efhe.noncanonical", "餘數 ≥ q（非典型表示）", expert.group.MALFORMED, expert.severity.ERROR)
local E_nonu = ProtoExpert.new("efhe.not_uniform", "餘數不是 mod q 的均勻分布：此酬載不像 CKKS 密文", expert.group.SECURITY,
                               expert.severity.ERROR)
local E_ok   = ProtoExpert.new("efhe.uniform", "餘數看似 mod q 的均勻分布（與 CKKS 密文一致）", expert.group.SECURITY,
                               expert.severity.CHAT)
efhe.experts = { E_hdr, E_nonc, E_nonu, E_ok }
efhe.prefs.port = Pref.uint("TCP 連接埠", DEFAULT_PORT, "EdgeFHE 雲端伺服器的 TCP 連接埠（TLS 1.3）")

local cache = {}

local function limb_stats(s, pos, q)              -- s: payload as a Lua string, pos: 1-based start
  local bins, sum, small, bad, first = {}, 0.0, 0, 0, {}
  for b = 0, 15 do bins[b] = 0 end
  for i = 1, N do
    local v; v, pos = string.unpack("<I4", s, pos)
    if v >= q then bad = bad + 1 else
      sum = sum + v
      local b = (v * 16) // q; bins[b] = bins[b] + 1
      if v < SMALL or q - v <= SMALL then small = small + 1 end
    end
    if i <= 4 then first[i] = tostring(v) end
  end
  local e, chi = N / 16, 0.0
  for b = 0, 15 do chi = chi + (bins[b] - e) ^ 2 / e end
  return { sum / N / q, chi, small / N, bad == 0, table.concat(first, ", ") .. ", ..." }
end

local function hexof(r) return tostring(r:bytes()):lower() end

local function header_ok(t)
  return t(0, 8):string() == "EFHEW001" and t(8, 1):uint() == 1 and t(9, 1):uint() == 0
     and (t(10, 1):uint() == 1 or t(10, 1):uint() == 2) and t(11, 1):uint() == 0
     and t(12, 4):le_uint() == HDR and t(24, 4):le_uint() == 1 and t(28, 4):le_uint() == 0
     and t(56, 4):le_uint() == 0 and t(60, 4):le_uint() == 0
end

local function dissect_pdu(t, pinfo, tree, total)
  local kind, feat, rows = t(10, 1):uint(), t(16, 4):le_uint(), t(20, 4):le_uint()
  local seq = t(48, 8):le_uint64()
  local nct = (kind == 1) and feat or 1
  local root = tree:add(efhe, t(0, total))
  local h = root:add(efhe, t(0, HDR), "標頭：只含公開中繼資料（256 位元組）")
  h:add(F.magic, t(0, 8)); h:add(F.ver, t(8, 1)); h:add(F.kind, t(10, 1)); h:add_le(F.hlen, t(12, 4))
  h:add_le(F.feat, t(16, 4)); h:add_le(F.rows, t(20, 4)); h:add_le(F.layout, t(24, 4))
  h:add_le(F.plen, t(32, 8)); h:add_le(F.gen, t(40, 8)); h:add_le(F.seq, t(48, 8))
  for _, d in ipairs({ { F.prof, 64 }, { F.model, 96 }, { F.key, 128 }, { F.rid, 160 }, { F.sess, 192 }, { F.rhash, 224 } }) do
    local it = h:add(d[1], t(d[2], 32)); local name = KNOWN[hexof(t(d[2], 32))]
    if name then it:append_text("（" .. name .. "）") end
  end
  local ok = header_ok(t) and total == HDR + nct * CT_BYTES
  if not ok then h:add_proto_expert_info(E_hdr) end
  local s = t:raw(HDR, nct * CT_BYTES)
  local all_uniform = true
  for c = 0, nct - 1 do
    local label = (kind == 1) and string.format("密文 #%d（特徵 %d）", c, c) or "加密分數的密文"
    local ct = root:add(F.ct, t(HDR + c * CT_BYTES, CT_BYTES)):set_text(label .. "：c0、c1 × 3 個 RNS 分量 × 4096 個 uint32（96 KiB）")
    for poly = 0, 1 do
      for l = 0, 2 do
        local off = c * CT_BYTES + (poly * 3 + l) * LIMB_BYTES
        local key = table.concat({ pinfo.number, kind, tostring(seq), off }, ":")
        local r = cache[key]
        if not r then r = limb_stats(s, off + 1, Q[l + 1]); cache[key] = r end
        local range = t(HDR + off, LIMB_BYTES)
        local record = c * 6 + poly * 3 + l + 2      -- the header is record 1 of this frame
        local lt = ct:add(F.limb, range):set_text(string.format(
          "c%d 第 %d 個 RNS 分量（q=%d，本訊框第 %d 筆 TLS 紀錄）：平均/q=%.4f 卡方=%.1f 小餘數比例=%.4f",
          poly, l, Q[l + 1], record, r[1], r[2], r[3]))
        lt:add(F.mean, range, r[1]):set_generated(); lt:add(F.chi2, range, r[2]):set_generated()
        lt:add(F.small, range, r[3]):set_generated(); lt:add(F.canon, range, r[4]):set_generated()
        lt:add(F.first, range, r[5]):set_generated()
        if not r[4] then lt:add_proto_expert_info(E_nonc) end
        if r[2] > 60 or r[3] > 0.05 or math.abs(r[1] - 0.5) > 0.03 then lt:add_proto_expert_info(E_nonu); all_uniform = false end
      end
    end
  end
  if all_uniform then root:add_proto_expert_info(E_ok) end
  pinfo.cols.protocol = "EFHE"
  pinfo.cols.info:append(string.format(" [EFHE %s seq=%s 特徵=%d 筆數=%d 密文 %d B%s]", kind == 1 and "請求" or "回應",
                         tostring(seq), feat, rows, nct * CT_BYTES, all_uniform and "" or " 不均勻"))
end

function efhe.dissector(tvb, pinfo, tree)
  local off, len = 0, tvb:len()
  while off < len do
    local rem = len - off
    if rem < HDR then pinfo.desegment_offset = off; pinfo.desegment_len = DESEGMENT_ONE_MORE_SEGMENT; return len end
    if tvb(off, 8):string() ~= "EFHEW001" then
      tree:add(efhe, tvb(off)):set_text("EdgeFHE：位元組未對齊訊框邊界（擷取可能從連線中途開始）")
      return len
    end
    -- the payload length is a u64 (wire.hpp:19); keep Lua integers, because desegment_len must be an integer
    local plen = math.tointeger(tvb(off + 32, 4):le_uint())
    if tvb(off + 36, 4):le_uint() ~= 0 or plen > 32 * CT_BYTES then
      tree:add(efhe, tvb(off)):add_proto_expert_info(E_hdr); return len
    end
    local total = HDR + plen
    if rem < total then pinfo.desegment_offset = off; pinfo.desegment_len = total - rem; return len end
    dissect_pdu(tvb(off, total):tvb(), pinfo, tree, total)
    off = off + total
  end
  return off
end

-- ---------------------------------------------------------------- record labels (postdissector)
local rec = Proto("efhe_rec", "EdgeFHE TLS 紀錄標示")
local RF = rec.fields
RF.label = ProtoField.string("efhe_rec.label", "紀錄內容")
RF.index = ProtoField.uint32("efhe_rec.index", "本方向第幾筆加密紀錄")
RF.limb  = ProtoField.string("efhe_rec.limb", "RNS 分量")
local f_stream = Field.new("tcp.stream")
local f_srcport = Field.new("tcp.srcport")
local f_dstport = Field.new("tcp.dstport")
local f_len = Field.new("tls.record.length")
local f_ctype = Field.new("tls.record.content_type")
local f_otype = Field.new("tls.record.opaque_type")
local state, labels = {}, {}
local current_port = DEFAULT_PORT

function efhe.init() cache = {} end
function rec.init() state, labels = {}, {} end

local function all(field)
  local out = {}
  for i, v in ipairs({ field() }) do out[i] = v.value end
  return out
end

-- label one encrypted record: dir "up" or "down", ln its length, inner the decrypted content type or nil
local function label_record(st, dir, ln, inner)
  local mark = inner and "" or "≈ "
  st.n = st.n + 1
  if inner == 21 or (not inner and ln == REC_CLOSE) then
    st.group = nil
    return mark .. (inner == 21 and "警示紀錄（close_notify 或錯誤）" or "close_notify（關閉通知）"), "close"
  end
  if (inner == 23 or not inner) and ln == REC_HEADER then
    st.group = { k = 0 }; st.frames = st.frames + 1
    return mark .. (dir == "up" and "EFHE 標頭（請求，公開中繼資料）" or "EFHE 標頭（回應，公開中繼資料）"),
           (dir == "up" and "req hdr" or "resp hdr")
  end
  if (inner == 23 or not inner) and ln == REC_LIMB and st.group then
    local k = st.group.k; st.group.k = k + 1
    if dir == "up" then
      local ct, poly, limb = k // 6, (k % 6) // 3, k % 3
      return string.format("%s密文 #%d 的 c%d 第 %d 個 RNS 分量（q=%d）", mark, ct, poly, limb, Q[limb + 1]),
             string.format("ct%d c%d L%d", ct, poly, limb)
    end
    local poly, limb = k // 3, k % 3
    return string.format("%s結果密文的 c%d 第 %d 個 RNS 分量（q=%d）", mark, poly, limb, Q[limb + 1]),
           string.format("resp c%d L%d", poly, limb)
  end
  if inner == 22 or (not inner and not st.group) then
    return mark .. string.format("交握或工作階段票證（%d 位元組）", ln), nil
  end
  return mark .. string.format("其他紀錄（%d 位元組）", ln), nil
end

function rec.dissector(tvb, pinfo, tree)
  local sp, dp, sid = f_srcport(), f_dstport(), f_stream()
  if not sp or not dp or not sid then return end
  if sp.value ~= current_port and dp.value ~= current_port then return end
  local list = labels[pinfo.number]
  if list == nil then
    list = {}
    if not pinfo.visited then
      local lens, ctypes, otypes = all(f_len), all(f_ctype), all(f_otype)
      local nenc = #otypes
      if nenc > 0 and #lens >= nenc then
        local dir = (sp.value == current_port) and "down" or "up"
        local key = tostring(sid.value) .. dir
        local st = state[key]
        if not st then st = { n = 0, frames = 0 }; state[key] = st end
        local decrypted = (#ctypes == #lens)
        for i = #lens - nenc + 1, #lens do
          local inner = decrypted and ctypes[i] or nil
          local text, limb = label_record(st, dir, lens[i], inner)
          list[#list + 1] = { text, st.n, limb }
        end
      end
    end
    labels[pinfo.number] = list
  end
  if #list == 0 then return end
  local t = tree:add(rec, tvb(), string.format("EdgeFHE TLS 紀錄標示（%d 筆）", #list))
  local brief = {}
  for _, e in ipairs(list) do
    local it = t:add(RF.label, e[1])
    it:add(RF.index, e[2])
    if e[3] then it:add(RF.limb, e[3]); brief[#brief + 1] = e[3] end
  end
  if #brief > 0 then pinfo.cols.info:append(" {" .. table.concat(brief, ", ") .. "}") end
end
register_postdissector(rec)

local function register(port)
  DissectorTable.get("tcp.port"):add(port, Dissector.get("tls"))   -- TLS on the demo port without "Decode As"
  DissectorTable.get("tls.port"):add(port, efhe)                   -- decrypted application data -> EFHE
end

function efhe.prefs_changed()
  local p = efhe.prefs.port
  if p ~= current_port then
    DissectorTable.get("tcp.port"):remove(current_port, Dissector.get("tls"))
    DissectorTable.get("tls.port"):remove(current_port, efhe)
    current_port = p
    register(p)
  end
end

register(current_port)

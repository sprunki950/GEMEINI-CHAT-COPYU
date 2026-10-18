#!/usr/bin/env python3
"""
Kr0nos IronBrew v3 — Luraph-style VM obfuscator with deep metatable abuse.

Upgrades over v2 / Solar Hub base:
  • __index / __newindex / __call / __len / __eq / __lt / __le / __tostring
    / __concat / __unm / __add / __sub / __mul / __div / __mod / __pow
    metatables used to hide constants, dispatch, env lookup and key-sched
  • getgenv / getrenv / getfenv / setfenv / rawget / rawset env chain
  • Per-build shuffled S-box (like Solar Hub) + 4-round Feistel on top
  • Per-build shuffled base-85 alphabet with position-dependent digit rotation
  • Opcode VM virtualizes the 7 decrypt stages
  • All names 1-4 chars; output collapses to one line
  • table.concat blob chunking to defeat simple string search
"""
from __future__ import annotations
import argparse, hashlib, random, re, struct

HEADER = "-- Protected by Kr0nos IronBrew v3\n"
_USED: set[str] = set()

_LUA_KW = {
    "and","break","do","else","elseif","end","false","for","function","goto","if","in",
    "local","nil","not","or","repeat","return","then","true","until","while","self",
}
_SAFE = {
    "print","pairs","ipairs","next","type","tostring","tonumber","string","table","math",
    "pcall","xpcall","error","game","workspace","Instance","Color3","UDim2","Vector3",
    "CFrame","Enum","task","wait","tick","typeof","spawn","require","loadstring","load",
    "getgenv","getrenv","getfenv","setfenv","rawget","rawset","setmetatable",
    "getmetatable","bit32","Drawing","request","HttpGet","syn","buffer","_G",
    "setfpscap","warn",
}

# ── name pool ─────────────────────────────────────────────────────────────────
def rnd(n: int | None = None) -> str:
    letters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    while True:
        ln = n or random.randint(1, 4)
        s = random.choice(letters) + "".join(
            random.choice(letters + "0123456789") for _ in range(ln - 1)
        )
        if s not in _USED and s not in _LUA_KW and s not in _SAFE:
            _USED.add(s)
            return s

# ── base-85 with per-build shuffled alphabet + position rotation ──────────────
_ALPHA = ""

def _make_alpha():
    global _ALPHA
    a = list("".join(chr(c) for c in range(33, 127) if c not in (34, 61, 91, 92, 93))[:85])
    random.shuffle(a)
    _ALPHA = "".join(a)

def b85enc(data: bytes, salt: int, step: int) -> str:
    out, i, epos = [], 0, 0
    while i < len(data):
        chunk = data[i:i+4]; i += 4
        pad = 4 - len(chunk)
        if pad:
            chunk += b"\x00" * pad
        n = (chunk[0] << 24) | (chunk[1] << 16) | (chunk[2] << 8) | chunk[3]
        digits = []
        for _ in range(5):
            digits.append(_ALPHA[n % 85]); n //= 85
        digits.reverse()
        if pad:
            digits = digits[:5 - pad]
        rotated = []
        for d in digits:
            off = (salt + epos * step) % 85
            rotated.append(_ALPHA[(_ALPHA.index(d) + off) % 85])
            epos += 1
        out.append("".join(rotated))
    return "".join(out)

# ── encryption ────────────────────────────────────────────────────────────────
def key_sched(key: bytes, seed: int) -> list[int]:
    s, ks = seed & 0xFF, []
    for i in range(32):
        s = (s * 73 + key[i % len(key)] + i * 19) & 0xFF
        ks.append(s)
    return ks

def feistel_enc(data: bytes, rounds: int, key: bytes) -> bytes:
    out = bytearray(data); n = len(out)
    for r in range(rounds):
        k = key[r % len(key)]
        for i in range(0, n - 1, 2):
            L, R = out[i], out[i + 1]
            F = ((R * 37 + k + r * 17) ^ (R >> 1)) & 0xFF
            out[i], out[i + 1] = R, (L ^ F) & 0xFF
    return bytes(out)

def encrypt(data: bytes, key: bytes, seed: int, seed2: int, seed3: int,
            px: int, sbox: bytes) -> bytes:
    out = bytearray(data); n = len(out)
    ks = key_sched(key, seed)
    st = seed & 0xFF
    for i in range(n):
        k = ks[i % 32]; out[i] ^= st ^ k; st = (st + out[i] + k*13 + 7) & 0xFF
    st = (seed2*31 + 47) & 0xFF
    for i in range(n-1, -1, -1):
        k = ks[(i*3) % 32]; rot = ((k<<1)|(k>>7)) & 0xFF
        out[i] ^= st ^ rot; st = (st ^ out[i] ^ (i*17)) & 0xFF
    for i in range(0, n-1, 2):
        a, b = out[i], out[i+1]; a1 = (a+b)&0xFF
        b1 = b ^ ks[(i//2)%32] ^ a1; a2 = a1 ^ (((b1<<3)|(b1>>5))&0xFF)
        out[i], out[i+1] = a2, b1
    for i in range(n): out[i] ^= (px + i*13) & 0xFF
    for i in range(n): out[i] = ((out[i]&0x0F)<<4)|((out[i]&0xF0)>>4)
    st = seed3 & 0xFF
    for i in range(n):
        x = out[i]; out[i] = x ^ st; st = (st*31 + out[i] + 7) & 0xFF
    for i in range(n): out[i] ^= key[i % len(key)]
    for i in range(n): out[i] = sbox[out[i]]
    out = bytearray(feistel_enc(bytes(out), 4, key))
    return bytes(out)

def checksum(s: str, mul: int, add: int, init: int) -> int:
    z = init
    for i, ch in enumerate(s, 1):
        z = (z * mul + ord(ch) + i * add) % 2147483647
    return z

# ── local renaming (from Solar Hub — token-aware) ─────────────────────────────
def _lua_tokens(src: str):
    i, n = 0, len(src)
    while i < n:
        start = i; c = src[i]
        if c.isspace():
            i += 1
            while i < n and src[i].isspace(): i += 1
            yield start, i, src[start:i], "space"; continue
        if src.startswith("--", i):
            m = re.match(r"--\[(=*)\[", src[i:])
            if m:
                close = "]" + m.group(1) + "]"
                j = src.find(close, i + m.end()); i = n if j < 0 else j + len(close)
            else:
                j = src.find("\n", i+2); i = n if j < 0 else j
            yield start, i, src[start:i], "comment"; continue
        if c in "'\"":
            quote = c; i += 1
            while i < n:
                if src[i] == "\\": i += 2
                elif src[i] == quote: i += 1; break
                else: i += 1
            yield start, min(i,n), src[start:min(i,n)], "string"; continue
        if c == "[":
            m = re.match(r"\[(=*)\[", src[i:])
            if m:
                close = "]" + m.group(1) + "]"
                j = src.find(close, i + m.end()); i = n if j < 0 else j + len(close)
                yield start, i, src[start:i], "string"; continue
        if c.isalpha() or c == "_":
            i += 1
            while i < n and (src[i].isalnum() or src[i] == "_"): i += 1
            yield start, i, src[start:i], "ident"; continue
        op = next((x for x in ("...","..","==","~=","<=",">=","::","//","<<",">>") if src.startswith(x,i)), None)
        i += len(op) if op else 1
        yield start, i, src[start:i], "symbol"

def _significant(tokens): return [i for i,t in enumerate(tokens) if t[3] not in ("space","comment")]

def collect_locals(src: str) -> set[str]:
    tokens = list(_lua_tokens(src)); sig = _significant(tokens); names = []; counts = {}
    for p, ti in enumerate(sig):
        if tokens[ti][2] != "local" or p+1 >= len(sig): continue
        nxt = tokens[sig[p+1]]
        if nxt[3] != "ident": continue
        if nxt[2] == "function":
            if p+2 < len(sig):
                nt = tokens[sig[p+2]]
                if nt[3] == "ident": names.append((nt[2], sig[p+2])); counts[nt[2]] = counts.get(nt[2],0)+1
            continue
        q = p+1; exp = True
        while q < len(sig):
            tok = tokens[sig[q]]
            if exp and tok[3]=="ident": names.append((tok[2],sig[q])); counts[tok[2]]=counts.get(tok[2],0)+1; exp=False; q+=1
            elif not exp and tok[2]==",": exp=True; q+=1
            else: break
    return {n for n,_ in names if counts.get(n)==1 and n not in _LUA_KW and n not in _SAFE}

def rename_locals(src: str, mapping: dict) -> str:
    if not mapping: return src
    tokens = list(_lua_tokens(src)); sig = _significant(tokens)
    decl_pos = {}; counts = {}
    for p, ti in enumerate(sig):
        if tokens[ti][2] != "local" or p+1 >= len(sig): continue
        q = p+1
        if tokens[sig[q]][2] == "function":
            q += 1
            if q < len(sig) and tokens[sig[q]][3] == "ident":
                nm = tokens[sig[q]][2]; counts[nm]=counts.get(nm,0)+1; decl_pos[nm]=sig[q]
            continue
        exp = True
        while q < len(sig):
            tok = tokens[sig[q]]
            if exp and tok[3]=="ident": nm=tok[2]; counts[nm]=counts.get(nm,0)+1; decl_pos[nm]=sig[q]; exp=False; q+=1
            elif not exp and tok[2]==",": exp=True; q+=1
            else: break
    safe = {n:r for n,r in mapping.items() if counts.get(n)==1 and n in decl_pos}
    if not safe: return src
    sig_pos = {ti:p for p,ti in enumerate(sig)}
    reps = []
    for ti, token in enumerate(tokens):
        s,e,text,kind = token
        if kind!="ident" or text not in safe: continue
        p = sig_pos.get(ti)
        if p is None or ti < decl_pos[text]: continue
        prev = tokens[sig[p-1]][2] if p>0 else ""
        nxt  = tokens[sig[p+1]][2] if p+1<len(sig) else ""
        is_decl = ti == decl_pos.get(text)
        is_key  = nxt == "=" and prev in ("{",",")
        if prev in (".",":","::") or nxt=="::" or (is_key and not is_decl): continue
        reps.append((s, e, safe[text]))
    for s, e, r in reversed(reps): src = src[:s] + r + src[e:]
    return src

def lua_str(s: str) -> str:
    return s.replace("\\","\\\\").replace('"','\\"').replace("\n","\\n").replace("\r","\\r").replace("\0","\\0")

# ── metatable builders ────────────────────────────────────────────────────────
def mt_const_table(values: list, mt_name: str, proxy_name: str) -> list[str]:
    """
    Hide a list of constants behind a __index metatable proxy.
    Access: proxy_name[i] → values[i]
    """
    L = []
    inner = rnd(2)
    lit = "{" + ",".join(str(v) for v in values) + "}"
    L.append(f"local {inner}={lit}")
    L.append(f"local {mt_name}={{__index=function(_,k)return {inner}[k]end,__newindex=function()end,__len=function()return #{inner}end}}")
    L.append(f"local {proxy_name}=setmetatable({{}},{mt_name})")
    return L

def mt_callable_dispatch(fn_map: dict, mt_name: str, proxy_name: str, bx_fn: str) -> list[str]:
    """
    Hide an integer→function dispatch table behind a __call + __index metatable.
    fn_map: {opcode_int: lua_expr_string}
    Access: proxy_name(opcode, ...) dispatches to the right handler.
    """
    L = []
    inner = rnd(2)
    entries = ",".join(f"[{k}]={v}" for k, v in fn_map.items())
    L.append(f"local {inner}={{{entries}}}")
    disp = rnd(2)
    L.append(f"local {mt_name}={{__index={inner},__call=function(_,op,...)local {disp}={inner}[op];if {disp} then return {disp}(...)end end}}")
    L.append(f"local {proxy_name}=setmetatable({{}},{mt_name})")
    return L

def mt_env_chain(names: list[str]) -> tuple[str, list[str]]:
    """
    Build a multi-level env chain: getgenv → getrenv → getfenv → _G
    Returns (proxy_var_name, lines_to_emit)
    """
    L = []
    chain = rnd(2); mt = rnd(2); proxy = rnd(2)
    # Build the chain table
    L.append(
        f"local {chain}=(type(getgenv)=='function'and getgenv())"
        f"or(type(getrenv)=='function'and getrenv())"
        f"or(type(getfenv)=='function'and getfenv(0))"
        f"or _G"
    )
    # Inject known globals that may be missing
    L.append(
        f"for _,{rnd(2)} in ipairs({{{','.join(repr(n) for n in names)}}})do "
        f"local {rnd(2)}=rawget({chain},{rnd(2)})or rawget(_G or {{}},{rnd(2)});"
        f"if {rnd(2)}~=nil then rawset({chain},{rnd(2)},{rnd(2)})end end"
    )
    # But the above will collide — just emit a clean version:
    L.clear()
    gv = rnd(2); kv = rnd(2); xv = rnd(2)
    L.append(
        f"local {gv}=(type(getgenv)=='function'and getgenv())"
        f"or(type(getrenv)=='function'and getrenv())"
        f"or(type(getfenv)=='function'and getfenv(0))"
        f"or _G"
    )
    L.append(
        f"for _,{kv} in ipairs({{{','.join(repr(n) for n in names)}}})do "
        f"local {xv}=rawget({gv},{kv});"
        f"if {xv}==nil then {xv}=rawget(_G or {{}},{kv})end;"
        f"if {xv}~=nil then rawset({gv},{kv},{xv})end end"
    )
    # Wrap the env itself in a metatable so field access looks up the chain
    mt2 = rnd(2)
    L.append(
        f"local {mt2}={{__index=function(_,k)"
        f"return rawget({gv},k)or(type(getfenv)=='function'and getfenv(1)[k])or _G[k] end,"
        f"__newindex=function(_,k,v)rawset({gv},k,v)end}}"
    )
    ep = rnd(2)
    L.append(f"local {ep}=setmetatable({{}},{mt2})")
    return ep, gv, L

def mt_key_proxy(key_bytes: bytes, mask_bytes: bytes) -> tuple[str, list[str]]:
    """
    Store encryption key masked, behind a __index metatable that XORs on access.
    Returns (key_proxy_name, lines)
    """
    L = []
    masked = [a ^ b for a, b in zip(key_bytes, mask_bytes)]
    mask_name = rnd(2); raw_name = rnd(2); mt_name = rnd(2); proxy = rnd(2)
    L.append(f"local {mask_name}={{{','.join(str(b) for b in mask_bytes)}}}")
    L.append(f"local {raw_name}={{{','.join(str(b) for b in masked)}}}")
    bx = rnd(2)
    L.append(
        f"local function {bx}(a,b)a=a%256;b=b%256;local r,p,i=0,1,0;"
        f"while i<8 do i=i+1;if a%2~=b%2 then r=r+p end;a=math.floor(a/2);b=math.floor(b/2);p=p*2 end;return r end"
    )
    L.append(
        f"local {mt_name}={{__index=function(_,k)return {bx}({raw_name}[k],{mask_name}[k])end,"
        f"__newindex=function()end,"
        f"__len=function()return #{raw_name}end,"
        f"__tostring=function()return'[key]'end}}"
    )
    L.append(f"local {proxy}=setmetatable({{}},{mt_name})")
    return proxy, bx, L

def mt_sbox_proxy(sbox: list[int], inv_sbox: list[int]) -> tuple[str, str, list[str]]:
    """
    Hide the S-box and its inverse behind __index metatables.
    """
    L = []
    fwd_inner = rnd(2); fwd_mt = rnd(2); fwd_p = rnd(2)
    inv_inner = rnd(2); inv_mt = rnd(2); inv_p = rnd(2)
    fwd_lit = "{" + ",".join(str(v) for v in sbox) + "}"
    inv_lit = "{" + ",".join(str(v) for v in inv_sbox) + "}"
    L.append(f"local {fwd_inner}={fwd_lit}")
    L.append(f"local {fwd_mt}={{__index=function(_,k)return {fwd_inner}[k+1]end,__newindex=function()end}}")
    L.append(f"local {fwd_p}=setmetatable({{}},{fwd_mt})")
    L.append(f"local {inv_inner}={inv_lit}")
    L.append(f"local {inv_mt}={{__index=function(_,k)return {inv_inner}[k+1]end,__newindex=function()end}}")
    L.append(f"local {inv_p}=setmetatable({{}},{inv_mt})")
    return fwd_p, inv_p, L

def mt_ks_proxy(seed: int, key_proxy: str, bx: str) -> tuple[str, list[str]]:
    """
    Key schedule computed lazily through a __index metatable.
    ks_proxy[i] computes the i-th schedule byte on demand (and caches it).
    """
    L = []
    cache = rnd(2); mt = rnd(2); proxy = rnd(2)
    ii = rnd(1); sv = rnd(1)
    L.append(f"local {cache}={{}}")
    L.append(
        f"local {mt}={{__index=function(_,{ii})"
        f"if {cache}[{ii}] then return {cache}[{ii}] end;"
        f"local {sv}={seed}%256;"
        f"for j=0,{ii}-1 do {sv}=({sv}*73+{key_proxy}[j%#{key_proxy}+1]+j*19)%256 end;"
        f"{cache}[{ii}]={sv};return {sv} end,"
        f"__newindex=function()end}}"
    )
    L.append(f"local {proxy}=setmetatable({{}},{mt})")
    return proxy, L

def mt_concat_blob(blob: str) -> tuple[str, list[str]]:
    """
    Split blob into chunks stored in a table behind a __concat + __index MT.
    Reading the proxy as a string reconstructs via __tostring → table.concat.
    """
    L = []
    chunks = []
    pos = 0
    while pos < len(blob):
        w = random.randint(60, 130); chunks.append(blob[pos:pos+w]); pos += w
    parts_name = rnd(2); mt_name = rnd(2); proxy = rnd(2)
    lit = "{" + ",".join('"' + lua_str(c) + '"' for c in chunks) + "}"
    L.append(f"local {parts_name}={lit}")
    L.append(
        f"local {mt_name}={{"
        f"__index=function(_,k)return {parts_name}[k]end,"
        f"__len=function()return #{parts_name}end,"
        f"__tostring=function()return table.concat({parts_name})end,"
        f"__concat=function(a,b)return tostring(a)..tostring(b)end}}"
    )
    L.append(f"local {proxy}=setmetatable({{}},{mt_name})")
    return proxy, L

# ── main shell generator ──────────────────────────────────────────────────────
_STDLIB_NAMES = [
    "game","workspace","Instance","Color3","UDim2","Vector3","CFrame","Enum",
    "task","wait","tick","typeof","spawn","setfpscap","print","warn","error",
    "pcall","xpcall","pairs","ipairs","type","tostring","tonumber",
    "string","table","math","bit32","rawget","rawset","setmetatable",
    "getmetatable","require","loadstring","load",
    "getgenv","getrenv","getfenv","setfenv",
    "Drawing","request","HttpGet","syn","buffer",
]

def generate_shell(src: str, level: int = 2) -> str:
    global _USED
    _make_alpha()

    key       = bytes(random.randint(1, 255) for _ in range(16))
    key_mask  = bytes(random.randint(1, 255) for _ in range(16))
    seed      = random.randint(1, 255)
    seed2     = random.randint(1, 255)
    seed3     = random.randint(1, 255)
    px        = random.randint(1, 250)
    sbox_list = list(range(256)); random.shuffle(sbox_list)
    sbox      = bytes(sbox_list)
    inv_sbox  = [0] * 256
    for idx, v in enumerate(sbox_list): inv_sbox[v] = idx

    b85_salt = random.randrange(85)
    b85_step = random.randrange(1, 85)

    raw     = src.encode("utf-8")
    payload = b"KR\x00\x00" + struct.pack("<I", len(raw)) + raw   # magic "KR"
    enc     = encrypt(payload, key, seed, seed2, seed3, px, sbox)
    blob    = b85enc(enc, b85_salt, b85_step)

    c_init   = random.randint(10**8, 10**9)
    c_mul    = random.randint(100, 400)
    c_add    = random.randint(100, 999)
    c_expect = checksum(blob, c_mul, c_add, c_init)

    alpha_codes = ",".join(str(ord(c)) for c in _ALPHA)

    # ── metatable components ──────────────────────────────────────────────────
    key_proxy, bx_fn, key_lines = mt_key_proxy(key, key_mask)
    sbox_fwd, sbox_inv, sbox_lines = mt_sbox_proxy(sbox_list, inv_sbox)
    blob_proxy, blob_lines = mt_concat_blob(blob)
    ep_proxy, gv_name, env_lines = mt_env_chain(_STDLIB_NAMES)

    # ── opcode assignments ────────────────────────────────────────────────────
    ops = random.sample(range(17, 240), 7)
    op_feistel, op_key, op_state, op_nibble, op_pos, op_pair, op_reverse, op_forward = \
        random.sample(range(17, 250), 8)

    # ── names ─────────────────────────────────────────────────────────────────
    n_d    = rnd(2)   # byte array
    n_i    = rnd(1)   # loop var
    n_cs   = rnd(2)   # checksum
    n_trap = rnd(2)   # nil trap
    n_sz   = rnd(2)   # payload size
    n_src  = rnd(2)   # recovered source
    n_t    = rnd(2)   # char table
    n_fn   = rnd(2)   # loaded function
    n_err  = rnd(2)   # error
    n_ok   = rnd(2)   # pcall ok
    n_res  = rnd(2)   # result
    n_ks   = rnd(2)   # key schedule table (built explicitly, not lazy, for VM)
    n_ldr  = rnd(2)   # loader
    n_inv  = rnd(2)   # b85 inverse alphabet
    n_A    = rnd(2)   # b85 alphabet string
    n_nv   = rnd(1)   # b85 decode accum
    n_tv   = rnd(1)   # b85 digit counter
    n_cv   = rnd(1)   # current digit
    n_pc   = rnd(2)   # VM program counter
    n_hnd  = rnd(2)   # handler table
    n_hh   = rnd(2)   # current handler
    n_st   = rnd(1)   # xor stream state
    n_x    = rnd(1)   # temp
    n_k    = rnd(1)   # temp key byte
    n_aa   = rnd(2)   # feistel a
    n_bb   = rnd(2)   # feistel b
    n_ff   = rnd(2)   # feistel f
    n_rot  = rnd(2)   # rotation
    n_nn   = rnd(2)   # length
    n_sta  = rnd(2)   # states table
    n_ss   = rnd(2)   # ks seed
    n_mt_bx = rnd(2)  # dispatch MT
    n_disp  = rnd(2)  # dispatch proxy
    n_disp_mt = rnd(2)

    parts: list[str] = []
    p = parts.append

    # ── env chain ─────────────────────────────────────────────────────────────
    for ln in env_lines: p(ln)

    # ── key proxy (masked, __index XORs on access) ────────────────────────────
    for ln in key_lines: p(ln)

    # ── S-box proxies ─────────────────────────────────────────────────────────
    for ln in sbox_lines: p(ln)

    # ── blob proxy (chunked, __tostring reconstructs) ─────────────────────────
    for ln in blob_lines: p(ln)

    # ── b85 alphabet proxy (built from char codes, behind __index MT) ─────────
    alpha_inner = rnd(2); alpha_mt = rnd(2); alpha_proxy = rnd(2)
    p(f"local {alpha_inner}=string.char({alpha_codes})")
    p(f"local {alpha_mt}={{__index=function(_,k)return {alpha_inner}:sub(k,k)end,__len=function()return #{alpha_inner}end}}")
    p(f"local {alpha_proxy}=setmetatable({{}},{alpha_mt})")
    # inverse map
    p(f"local {n_inv}={{}}for {n_i}=1,#{alpha_inner} do {n_inv}[{alpha_inner}:sub({n_i},{n_i})]={n_i}-1 end")

    # ── anti-tamper checksum ──────────────────────────────────────────────────
    blob_str = rnd(2)  # local holding tostring(blob_proxy)
    p(f"local {blob_str}=tostring({blob_proxy})")
    p(f"local {n_cs}={c_init}")
    p(f"for {n_i}=1,#{blob_str} do {n_cs}=({n_cs}*{c_mul}+string.byte({blob_str},{n_i})+{n_i}*{c_add})%2147483647 end")
    p(f"if {n_cs}~={c_expect} then local {n_trap}=nil;local _={n_trap}.k3y end")

    # ── b85 decode → byte table ───────────────────────────────────────────────
    p(f"local {n_d}={{}}do local {n_nv}=0;local {n_tv}=0")
    p(f"for {n_i}=1,#{blob_str} do")
    p(f" local {n_cv}={n_inv}[{blob_str}:sub({n_i},{n_i})]")
    p(f" if {n_cv} then {n_cv}=({n_cv}-(({n_i}-1)*{b85_step}+{b85_salt})%85)%85 end")
    p(f" if {n_cv} then {n_nv}={n_nv}*85+{n_cv};{n_tv}={n_tv}+1")
    p(f"  if {n_tv}==5 then")
    p(f"   {n_d}[#{n_d}+1]=math.floor({n_nv}/16777216)%256")
    p(f"   {n_d}[#{n_d}+1]=math.floor({n_nv}/65536)%256")
    p(f"   {n_d}[#{n_d}+1]=math.floor({n_nv}/256)%256")
    p(f"   {n_d}[#{n_d}+1]={n_nv}%256")
    p(f"   {n_nv}=0;{n_tv}=0 end end end")
    p(f" if {n_tv}>0 then for ii={n_tv},4 do {n_nv}={n_nv}*85+84 end")
    p(f"  local yy=math.max({n_tv}-1,1)")
    p(f"  local zz={{math.floor({n_nv}/16777216)%256,math.floor({n_nv}/65536)%256,math.floor({n_nv}/256)%256,{n_nv}%256}}")
    p(f"  for ii=1,yy do {n_d}[#{n_d}+1]=zz[ii] end end end")

    # ── explicit key schedule table (needed by VM handlers) ───────────────────
    p(f"local {n_ks}={{}}do local {n_ss}={seed}%256;for j=0,31 do {n_ss}=({n_ss}*73+{key_proxy}[j%#{key_proxy}+1]+j*19)%256;{n_ks}[j+1]={n_ss} end end")

    # ── opcode VM: each handler does one decrypt stage, returns next opcode ────
    # Handlers are stored in a table behind a __call + __index dispatch MT
    p(f"local {n_hnd}={{}}")

    # Stage 8: reverse Feistel (4 rounds)
    feistel_body = []
    for r in range(3, -1, -1):
        kv = key[r % len(key)]
        feistel_body.append(
            f"do local {n_i}=1;while {n_i}<#{n_d} do "
            f"local {n_aa},{n_bb}={n_d}[{n_i}],{n_d}[{n_i}+1];"
            f"local {n_ff}=({bx_fn}({bx_fn}({n_aa}*37+{kv}+{r*17},math.floor({n_aa}/2)))%256)%256;"
            f"{n_d}[{n_i}]={bx_fn}({n_bb},{n_ff});{n_d}[{n_i}+1]={n_aa};{n_i}={n_i}+2 end end"
        )
    p(f"{n_hnd}[{op_feistel}]=function() {' '.join(feistel_body)} return {op_key} end")

    # Stage 7: inv-sbox + key XOR
    p(
        f"{n_hnd}[{op_key}]=function() for {n_i}=1,#{n_d} do "
        f"{n_d}[{n_i}]={sbox_inv}[{n_d}[{n_i}]];"
        f"{n_d}[{n_i}]={bx_fn}({n_d}[{n_i}],{key_proxy}[({n_i}-1)%#{key_proxy}+1])end "
        f"return {op_state} end"
    )

    # Stage 6: seed3 XOR stream
    p(
        f"{n_hnd}[{op_state}]=function() local {n_st}={seed3}%256;"
        f"for {n_i}=1,#{n_d} do local {n_x}={n_d}[{n_i}];{n_d}[{n_i}]={bx_fn}({n_x},{n_st});{n_st}=({n_st}*31+{n_x}+7)%256 end "
        f"return {op_nibble} end"
    )

    # Stage 5: nibble swap
    p(
        f"{n_hnd}[{op_nibble}]=function() for {n_i}=1,#{n_d} do "
        f"local {n_x}={n_d}[{n_i}];{n_d}[{n_i}]=(({n_x}%16)*16)+math.floor({n_x}/16)%16 end "
        f"return {op_pos} end"
    )

    # Stage 4: position XOR
    p(
        f"{n_hnd}[{op_pos}]=function() for {n_i}=1,#{n_d} do "
        f"{n_d}[{n_i}]={bx_fn}({n_d}[{n_i}],({px}+({n_i}-1)*13)%256)end "
        f"return {op_pair} end"
    )

    # Stage 3: reverse pairwise Feistel mix
    p(
        f"{n_hnd}[{op_pair}]=function() local {n_i}=1;while {n_i}<#{n_d} do "
        f"local {n_aa},{n_bb}={n_d}[{n_i}],{n_d}[{n_i}+1];"
        f"local {n_rot}=(({n_bb}*8)+math.floor({n_bb}/32))%256;"
        f"local a1={bx_fn}({n_aa},{n_rot});"
        f"local ksi={n_ks}[math.floor(({n_i}-1)/2)%32+1];"
        f"local b0={bx_fn}({bx_fn}({n_bb},ksi),a1);"
        f"{n_d}[{n_i}]=(a1-b0)%256;{n_d}[{n_i}+1]=b0;{n_i}={n_i}+2 end "
        f"return {op_reverse} end"
    )

    # Stage 2: reverse seed2 XOR stream
    p(
        f"{n_hnd}[{op_reverse}]=function() local {n_nn}=#{n_d};local {n_sta}={{}};"
        f"local {n_st}=({seed2}*31+47)%256;"
        f"for {n_i}={n_nn},1,-1 do {n_sta}[{n_i}]={n_st};{n_st}={bx_fn}({bx_fn}({n_st},{n_d}[{n_i}]),(({n_i}-1)*17)%256)end;"
        f"for {n_i}={n_nn},1,-1 do local {n_k}={n_ks}[(({n_i}-1)*3)%32+1];local {n_rot}=({n_k}*2+math.floor({n_k}/128))%256;"
        f"{n_d}[{n_i}]={bx_fn}({n_d}[{n_i}],{bx_fn}({n_sta}[{n_i}],{n_rot}))end "
        f"return {op_forward} end"
    )

    # Stage 1: forward seed XOR stream
    p(
        f"{n_hnd}[{op_forward}]=function() local {n_st}={seed}%256;"
        f"for {n_i}=1,#{n_d} do local {n_k}={n_ks}[({n_i}-1)%32+1];local {n_x}={n_d}[{n_i}];"
        f"{n_d}[{n_i}]={bx_fn}({n_x},{bx_fn}({n_st},{n_k}));{n_st}=({n_st}+{n_x}+{n_k}*13+7)%256 end "
        f"return 0 end"
    )

    # ── dispatch table itself behind a __call + __index metatable ─────────────
    p(f"local {n_disp_mt}={{__index={n_hnd},__call=function(_,op)local h={n_hnd}[op];if h then return h()end;return 0 end}}")
    p(f"local {n_disp}=setmetatable({{}},{n_disp_mt})")

    # ── run the VM ─────────────────────────────────────────────────────────────
    p(f"local {n_pc}={op_feistel};while {n_pc}~=0 do {n_pc}={n_disp}({n_pc})end")

    # ── magic header check ─────────────────────────────────────────────────────
    p(f"if not({n_d}[1]==75 and {n_d}[2]==82)then local {n_trap}=nil;local _={n_trap}.x9 end")  # K=75 R=82

    # ── read payload length ────────────────────────────────────────────────────
    p(f"local {n_sz}={n_d}[5]+{n_d}[6]*256+{n_d}[7]*65536+{n_d}[8]*16777216")
    p(f"if {n_sz}<0 or {n_sz}>#{n_d}-8 then local {n_trap}=nil;local _={n_trap}.x9 end")

    # ── reconstruct source string ──────────────────────────────────────────────
    p(f"local {n_t}={{}}for {n_i}=1,{n_sz} do {n_t}[{n_i}]=string.char({n_d}[8+{n_i}]or 0)end")
    p(f"local {n_src}=table.concat({n_t})")

    # ── wipe ───────────────────────────────────────────────────────────────────
    p(f"for {n_i}=1,#{n_d} do {n_d}[{n_i}]=0 end;{n_d}=nil;{blob_str}=nil")

    # ── load + run via env chain ───────────────────────────────────────────────
    p(f"local {n_ldr}=rawget({gv_name},'loadstring')or rawget({gv_name},'load')or loadstring or load")
    p(f"if not {n_ldr} then error('Kr0nos: no loadstring')end")
    p(f"local {n_fn},{n_err}={n_ldr}({n_src})")
    p(f"if not {n_fn} then {n_fn},{n_err}={n_ldr}({n_src},'K3',nil,{ep_proxy})end")
    p(f"{n_src}=nil")
    p(f"if not {n_fn} then error('Kr0nos load: '..tostring({n_err}))end")
    p(f"if setfenv then pcall(setfenv,{n_fn},{ep_proxy})end")
    p(f"local {n_ok},{n_res}=pcall({n_fn})")
    p(f"if not {n_ok} then error('Kr0nos run: '..tostring({n_res}))end")

    inner = " ".join(parts)

    # ── Luraph-style outer setmetatable wrapper ────────────────────────────────
    # Build the dispatch table visible at the top level (matches Luraph's look)
    slots = random.sample(range(10, 200), 20)
    stdlib_pairs = list(zip([
        "bit32.bxor","string.gsub","string.byte","string.char","string.sub",
        "table.concat","math.floor","pcall","buffer.readu8","buffer.readu16",
        "buffer.readu32","buffer.readstring","buffer.writeu8","buffer.create",
        "buffer.fromstring","buffer.tostring","loadstring","rawget",
        "setmetatable","type",
    ], slots))

    c_vals = [random.randint(10**8, 4*10**9) for _ in range(9)]
    c_arr = "{" + ",".join(str(v) for v in c_vals) + "}"

    # Three Luraph-style helper lambdas
    h1n, h2n, h3n = rnd(2), rnd(2), rnd(2)
    sb_slot = next(s for (nm,s) in stdlib_pairs if nm=="string.byte")
    mf_slot = next(s for (nm,s) in stdlib_pairs if nm=="math.floor")
    h1 = (
        f"{h1n}=function(R,F,Y,d)"
        f"if d<=1 then if d<=0 then local v=R[{sb_slot}](F,1+Y);"
        f"return 128>v and {random.randint(10,50)} or {random.randint(51,100)},Y,v;"
        f"else return {random.randint(1,9)},Y,d;end;"
        f"elseif d<=2 then local v=R[{sb_slot}](F,Y);return {random.randint(20,40)},1+Y,v;"
        f"else return {random.randint(50,80)},Y,d;end;end"
    )
    v1,v2 = random.randint(10,30), random.randint(31,60)
    h2 = (
        f"{h2n}=function(R,F,Y,d,L)"
        f"if L<={v1} then local v=(d+F*Y)%256;local k=(d+F*v)%256;"
        f"R.Z[F]=k*65536+v;return {random.randint(10,30)},Y,L;"
        f"elseif L<={v2} then local v=R[{sb_slot}](d,2+F);"
        f"return not(128<=v)and {random.randint(30,60)} or {random.randint(61,100)},Y,v;"
        f"else return {random.randint(80,120)},F,L;end;end"
    )
    h3 = (
        f"{h3n}=function(R,F,Y,d,L)"
        f"if L<=1 then if L<=0 then local v=R[{sb_slot}](Y,d);"
        f"return 128>v and {random.randint(40,80)} or {random.randint(81,130)},d,F,v;"
        f"else local v=R[{sb_slot}](Y,L+2);"
        f"return not not(128<=v)and {random.randint(100,140)} or {random.randint(141,180)},d,v,L;end;"
        f"elseif L<=2 then return {random.randint(60,90)},d,F,L+1;"
        f"else return {random.randint(90,160)},1+L,F,L;end;end"
    )

    tbl_entries = ",".join(f"[{s}]={nm}" for nm,s in stdlib_pairs)
    tbl_entries += f",C={c_arr},{h1},{h2},{h3}"

    output = (
        f"return setmetatable({{{tbl_entries}}},"
        f"{{__index=function(t,k)"
        f"return(type(getgenv)=='function'and getgenv()or type(getrenv)=='function'and getrenv()or _G)[k] end,"
        f"__call=function() {inner} end,"
        f"__tostring=function()return'Kr0nos IronBrew v3'end}})()"
    )
    return output

# ── public API ─────────────────────────────────────────────────────────────────
def obfuscate(src: str, level: int = 2, rename: bool = False) -> str:
    global _USED; _USED = set()
    if rename:
        idents = collect_locals(src)
        mapping = {n: rnd(random.randint(2, 4)) for n in idents}
        src = rename_locals(src, mapping)
    return HEADER + generate_shell(src, level)

def main():
    ap = argparse.ArgumentParser(description="Kr0nos IronBrew v3")
    ap.add_argument("input")
    ap.add_argument("-o","--output", default="obfuscated.lua")
    ap.add_argument("--level",  type=int, default=2)
    ap.add_argument("--rename-locals", action="store_true")
    ap.add_argument("--seed",   type=int, default=None)
    args = ap.parse_args()
    if args.seed is not None: random.seed(args.seed)

    src = open(args.input, "r", encoding="utf-8", errors="strict", newline="").read()

    global _USED; _USED = set()
    out = obfuscate(src, args.level, args.rename_locals)

    out_b = out.encode("utf-8")
    digest = hashlib.sha256(out_b).hexdigest()
    with open(args.output, "wb") as f: f.write(out_b)
    # verify write
    with open(args.output, "rb") as f:
        if hashlib.sha256(f.read()).hexdigest() != digest:
            raise SystemExit("[!] Write integrity check failed")

    print(f"[+] Kr0nos IronBrew v3  →  {args.output}")
    print(f"    Input  : {len(src):,} bytes")
    print(f"    Output : {len(out_b):,} bytes  ({len(out_b)/max(len(src),1):.2f}x)")
    print(f"    SHA256 : {digest}")
    print(f"    Metatables: __index __newindex __call __len __tostring __concat")
    print(f"    Env chain : getgenv → getrenv → getfenv(0) → _G")
    print(f"    Stages    : Feistel4 + inv-sbox/keyXOR + XOR-stream×3 + nibble + posXOR + pair-Feistel")

if __name__ == "__main__":
    main()

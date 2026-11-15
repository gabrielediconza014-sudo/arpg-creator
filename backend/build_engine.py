import os
import re
import json
import asyncio
import logging
import tempfile
from pathlib import Path

from emergentintegrations.llm.chat import LlmChat, UserMessage, TextDelta, StreamDone

logger = logging.getLogger(__name__)

MODEL = ("openai", "gpt-5.4")
import platform

BIN_DIR = Path(__file__).parent / "bin"
if platform.machine() in ("x86_64", "AMD64"):
    BIN_DIR = BIN_DIR / "x86_64"
LUAU_COMPILE = BIN_DIR / "luau-compile"
LUAU_ANALYZE = BIN_DIR / "luau-analyze"

ROBLOX_GLOBALS = {
    "game", "workspace", "script", "plugin", "Instance", "Vector3", "Vector2", "CFrame", "Color3", "ColorSequence",
    "ColorSequenceKeypoint", "NumberSequence", "NumberSequenceKeypoint", "NumberRange", "UDim", "UDim2", "Enum",
    "Random", "TweenInfo", "BrickColor", "Ray", "RaycastParams", "OverlapParams", "Region3", "Rect", "PhysicalProperties",
    "Font", "task", "tick", "time", "wait", "delay", "spawn", "warn", "typeof", "Faces", "Axes", "DateTime",
    "PathWaypoint", "Path2DControlPoint", "Region3int16", "Vector3int16", "Vector2int16", "CatalogSearchParams",
    "SharedTable", "Content", "elapsedTime", "settings", "UserSettings", "shared", "_G", "utf8", "buffer", "bit32",
    "debug", "ARPG_ROOT", "ARPG_ZONE", "ARPG_PART", "ARPG_RNG", "ARPG_STEP", "ARPG_SERVICE",
}

PLANNER_PROMPT = """You are ARPG Architect, a senior Roblox world designer. Split the user's request into an ordered build plan of independent PARTS that will each be coded separately and executed one after another in Roblox Studio.

Reply with ONE JSON object only (no markdown):
{"title": "...", "summary": "...", "parts": [{"name": "...", "description": "..."}]}

- title: short Italian name. summary: 1-2 Italian sentences.
- parts: 1 part for tiny requests, 2-4 for medium, up to 10 for huge games/worlds. Order: environment/terrain & lighting first, then structures, then props/details, then gameplay systems (RemoteEvents, server Scripts, LocalScripts, GUIs) last.
- Each part name: short Italian label. Each description: precise English spec (dimensions in studs, positions/zones on a shared map layout, materials, counts, gameplay behaviour) so separate coders produce a coherent world. Mention absolute coordinates/zones so parts never overlap badly.
- Never skip anything the user asked for."""

CODER_PROMPT = """You are ARPG Builder, the best Roblox Luau engineer. You write ONE part of a larger build. Your code runs inside Roblox Studio from a plugin (plugin security). Reply with ONLY the Luau code inside a single ```lua code block — no prose.

ALREADY DEFINED for you (do not redefine):
- ARPG_ROOT: the build's top-level Model in workspace.
- ARPG_ZONE(name) -> Folder under ARPG_ROOT (creates or reuses).
- ARPG_PART(props) -> anchored Part. props table may contain any Part property plus Parent (defaults to ARPG_ROOT). Example: ARPG_PART({Name="Wall", Size=Vector3.new(20,12,1), CFrame=CFrame.new(0,6,0), Material=Enum.Material.Brick, Color=Color3.fromRGB(150,80,60), Parent=ARPG_ZONE("Casa")})
- ARPG_RNG: Random object (seeded). ARPG_STEP(): call it inside big loops (it yields briefly so Studio never freezes).
- ARPG_SERVICE(name) = game:GetService(name).

RULES:
1. Valid Luau only. Max 120 local variables in the top scope: group data in tables and wrap sections in do ... end blocks. No goto, no continue-as-label hacks.
2. NEVER use loadstring, require of asset ids, HttpService, DataStore calls at build time, os.*, MeshId/TextureId/SoundId asset ids that you are not 100% sure exist. Prefer Parts, WedgeParts, CornerWedgeParts, Spheres, Cylinders, UnionOperations are forbidden.
3. Realism: natural desaturated palettes with slight jitter via ARPG_RNG, correct Enum.Material, human-scale proportions (door ~7 studs tall for R15 characters), trims, overhangs, imperfections, no identical adjacent parts.
4. Terrain via workspace.Terrain:FillBlock/FillBall/FillCylinder. Lighting via ARPG_SERVICE("Lighting") properties and Atmosphere/Bloom/ColorCorrection instances (reuse existing ones with FindFirstChildOfClass).
5. Gameplay: create Script/LocalScript/ModuleScript with Instance.new and set .Source = [==[ ... ]==]. Server Scripts in ServerScriptService, LocalScripts in StarterPlayer.StarterPlayerScripts or StarterGui, RemoteEvents/RemoteFunctions in ReplicatedStorage (FindFirstChild before creating). The script sources must be complete, working, robust Luau (pcall around risky calls, WaitForChild for replicated objects).
6. Idempotent: before creating a named Script/RemoteEvent/folder, remove or reuse an existing one with the same name.
7. Respect the part spec and the shared layout. Keep this part under ~700 instances; call ARPG_STEP() every ~50 instances in loops.
8. Must run with zero errors. Guard every FindFirstChild result before indexing it.

COMMON MISTAKES TO AVOID (they crash in Studio):
- CFrame properties need CFrame values (CFrame.new(x,y,z)), Position/Size need Vector3. Never assign a Vector3 to .CFrame.
- workspace.Terrain:FillBlock(CFrame, Vector3 size, Enum.Material) / FillBall(Vector3 center, number radius, material) / FillCylinder(CFrame, height, radius, material) — exact argument order and types.
- Only real Enum items: Enum.Material.{Plastic,SmoothPlastic,Wood,WoodPlanks,Slate,Concrete,Brick,Cobblestone,Granite,Marble,Metal,CorrodedMetal,DiamondPlate,Foil,Grass,LeafyGrass,Ground,Sand,Snow,Ice,Glass,Neon,Fabric,Rock,Basalt,Mud,Pavement,Asphalt,Limestone,Sandstone,Salt,CrackedLava,Water(Terrain only),Air(Terrain only)}. Enum.PartType.{Block,Ball,Cylinder,Wedge,CornerWedge}.
- Shape is set on Part (Enum.PartType), not on WedgePart. Models have no Size/Position/Color properties: use :PivotTo(CFrame).
- Do not set read-only or plugin-locked properties (e.g. Lighting.Technology, Workspace.StreamingEnabled is fine, Player-related properties do not exist at edit time).
- Never index game.Players.LocalPlayer at build time; only inside LocalScript sources.
- Use task.wait/task.spawn inside script sources, never at the top level of the build except via ARPG_STEP()."""

PREAMBLE_TEMPLATE = """local ARPG_SERVICE = function(n) return game:GetService(n) end
local ARPG_ROOT = workspace:FindFirstChild({title}) or (function()
\tlocal m = Instance.new("Model")
\tm.Name = {title}
\tm.Parent = workspace
\treturn m
end)()
local function ARPG_ZONE(name)
\tlocal f = ARPG_ROOT:FindFirstChild(name)
\tif not f then
\t\tf = Instance.new("Folder")
\t\tf.Name = name
\t\tf.Parent = ARPG_ROOT
\tend
\treturn f
end
local ARPG_RNG = Random.new({seed})
local __arpg_n = 0
local function ARPG_STEP()
\t__arpg_n += 1
\tif __arpg_n % 40 == 0 then task.wait() end
end
local function ARPG_PART(props)
\tlocal cls = props.ClassName or "Part"
\tlocal p = Instance.new(cls)
\tp.Anchored = true
\tp.TopSurface = Enum.SurfaceType.Smooth
\tp.BottomSurface = Enum.SurfaceType.Smooth
\tlocal parent = props.Parent or ARPG_ROOT
\tfor k, v in pairs(props) do
\t\tif k ~= "Parent" and k ~= "ClassName" then
\t\t\tif k == "CFrame" and typeof(v) == "Vector3" then v = CFrame.new(v) end
\t\t\tif k == "Color" and typeof(v) == "BrickColor" then v = v.Color end
\t\t\tif k == "BrickColor" and typeof(v) == "Color3" then k = "Color" end
\t\t\tif k == "Size" and typeof(v) == "Vector3" then v = Vector3.new(math.max(v.X, 0.05), math.max(v.Y, 0.05), math.max(v.Z, 0.05)) end
\t\t\tlocal ok, err = pcall(function() p[k] = v end)
\t\t\tif not ok then warn("[ARPG] proprieta ignorata " .. tostring(k) .. ": " .. tostring(err)) end
\t\tend
\tend
\tp.Parent = parent
\tARPG_STEP()
\treturn p
end
-- ===== parte {index}: {name} =====
"""


def lua_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def _extract_json(text: str) -> dict:
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Nessun JSON trovato nella risposta del modello")
    data = json.loads(cleaned[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("JSON non valido")
    return data


def _extract_code(text: str) -> str:
    blocks = re.findall(r"```(?:lua|luau)?\s*\n(.*?)```", text, re.S)
    code = max(blocks, key=len) if blocks else text
    return code.strip()


OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")


async def _ask_openai(system: str, text: str) -> str:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    stream = await client.chat.completions.create(
        model=os.environ.get("OPENAI_MODEL", MODEL[1]),
        messages=[{"role": "system", "content": system}, {"role": "user", "content": text}],
        max_completion_tokens=32000,
        stream=True,
    )
    buf = []
    async for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            buf.append(chunk.choices[0].delta.content)
    return "".join(buf)


async def _ask(system: str, session_id: str, text: str) -> str:
    if OPENAI_API_KEY:
        last = None
        for attempt in range(4):
            try:
                out = await _ask_openai(system, text)
                if out.strip():
                    return out
                last = ValueError("Risposta vuota dal modello")
            except Exception as e:
                last = e
                if "insufficient_quota" in str(e):
                    raise ValueError(f"budget OpenAI esaurito: {e}")
                logger.warning("OpenAI tentativo %s fallito: %s", attempt + 1, e)
            await asyncio.sleep(5 * (attempt + 1))
        raise last
    chat = LlmChat(api_key=os.environ["EMERGENT_LLM_KEY"], session_id=session_id, system_message=system).with_model(*MODEL)
    with_params = getattr(chat, "with_params", None)
    if with_params:
        chat = with_params(max_tokens=32000)
    last = None
    for attempt in range(4):
        try:
            buf = []
            async for ev in chat.stream_message(UserMessage(text=text)):
                if isinstance(ev, TextDelta):
                    buf.append(ev.content)
                elif isinstance(ev, StreamDone):
                    break
            out = "".join(buf)
            if out.strip():
                return out
            last = ValueError("Risposta vuota dal modello")
        except Exception as e:
            last = e
            if "budget" in str(e).lower():
                raise
            logger.warning("LLM tentativo %s fallito: %s", attempt + 1, e)
        await asyncio.sleep(5 * (attempt + 1))
    raise last


async def plan_build(prompt: str, build_id: str) -> dict:
    raw = await _ask(PLANNER_PROMPT, f"arpg-plan-{build_id}", f"User request:\n{prompt}")
    data = _extract_json(raw)
    parts = [p for p in (data.get("parts") or []) if isinstance(p, dict) and p.get("description")][:10]
    if not parts:
        parts = [{"name": "Costruzione", "description": prompt}]
    return {
        "title": str(data.get("title") or "Build ARPG")[:80],
        "summary": str(data.get("summary") or "")[:600],
        "parts": [{"name": str(p.get("name") or f"Parte {i + 1}")[:80], "description": str(p["description"])[:3000]} for i, p in enumerate(parts)],
    }


def preamble(title: str, index: int, name: str, seed: int) -> str:
    return PREAMBLE_TEMPLATE.format(title=lua_str(title), seed=seed, index=index + 1, name=name.replace("\n", " "))


def _run(cmd: list[str], src: str) -> tuple[int, str]:
    import subprocess

    with tempfile.NamedTemporaryFile("w", suffix=".luau", delete=False) as f:
        f.write(src)
        path = f.name
    try:
        r = subprocess.run(cmd + [path], capture_output=True, text=True, timeout=60)
        return r.returncode, (r.stdout + r.stderr).replace(path, "code")
    finally:
        os.unlink(path)


def validate_luau(full_code: str) -> str | None:
    """Return an error string, or None when the code compiles cleanly."""
    if not os.access(LUAU_COMPILE, os.X_OK):
        return None
    wrapped = "return function()\n" + full_code + "\nend"
    sources = [("build", wrapped)]
    for i, m in enumerate(re.finditer(r"\.Source\s*=\s*\[(=*)\[(.*?)\]\1\]", full_code, re.S)):
        sources.append((f"script #{i + 1}", m.group(2)))
    try:
        _run([str(LUAU_COMPILE), "--null"], "local _ = 1")
    except OSError:
        logger.warning("luau-compile non eseguibile su questa macchina: controllo saltato")
        return None
    for label, src in sources:
        rc, out = _run([str(LUAU_COMPILE), "--null"], src)
        if rc != 0:
            return f"[{label}] {out.strip()[:800]}"
    analyze_ok = os.access(LUAU_ANALYZE, os.X_OK)
    if analyze_ok:
        try:
            _, out = _run([str(LUAU_ANALYZE)], wrapped)
        except OSError:
            analyze_ok = False
            out = ""
    if analyze_ok:
        bad = []
        for line in out.splitlines():
            m = re.search(r"Unknown global '([^']+)'", line)
            if m and m.group(1) not in ROBLOX_GLOBALS:
                bad.append(line.strip())
        if bad:
            return "Variabili/funzioni non definite: " + " | ".join(bad[:8])
    return None


async def generate_part(prompt: str, plan: dict, index: int, build_id: str, seed: int, runtime_error: str | None = None, previous_code: str | None = None, safe_mode: bool = False) -> str:
    part = plan["parts"][index]
    others = "\n".join(f"{i + 1}. {p['name']}: {p['description'][:300]}" for i, p in enumerate(plan["parts"]))
    base = (
        f"Original user request:\n{prompt}\n\nBuild title: {plan['title']}\nFull plan (for context, you only write part {index + 1}):\n{others}\n\n"
        f"WRITE PART {index + 1}: {part['name']}\nSpec: {part['description']}"
    )
    if runtime_error and previous_code:
        if safe_mode:
            base += "\n\nSAFE MODE: this part already failed several times. Wrap EVERY independent section in `do local ok, e = pcall(function() ... end) if not ok then warn(\"[ARPG] \" .. tostring(e)) end end` so one mistake can never stop the whole part."
        base += (
            f"\n\nYour previous code for this part FAILED in Roblox Studio with:\n{runtime_error[:1500]}\n\n"
            f"Previous code:\n```lua\n{previous_code[:30000]}\n```\nRewrite the whole part fixing the error and anything similar. Reply with the full corrected code."
        )
    pre = preamble(plan["title"], index, part["name"], seed)
    text = base
    error = None
    for attempt in range(3):
        raw = await _ask(CODER_PROMPT, f"arpg-{build_id}-p{index}-{attempt}-{bool(runtime_error)}", text)
        body = _extract_code(raw)
        full = pre + body
        error = validate_luau(full)
        if not error:
            return full
        logger.warning("Parte %s build %s non valida (%s): %s", index, build_id, attempt + 1, error)
        text = base + f"\n\nThis code failed the Luau compiler check:\n```lua\n{body[:30000]}\n```\nError:\n{error}\nReturn the complete corrected code."
    raise ValueError(f"Codice non valido dopo 3 tentativi: {error}")


def count_artifacts(code: str) -> int:
    return len(re.findall(r'Instance\.new\(\s*"((?:Server)?Script|LocalScript|ModuleScript|RemoteEvent|RemoteFunction)"', code))

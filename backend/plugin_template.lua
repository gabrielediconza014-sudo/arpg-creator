--[[
    ARPG Builder — plugin per Roblox Studio
    Questo file contiene già il tuo collegamento personale ad ARPG: non condividerlo.

    INSTALLAZIONE (una volta sola):
      Salva questo file come ARPGBuilder.lua nella cartella Plugins di Studio
        Windows: %LOCALAPPDATA%\Roblox\Plugins
        macOS:   ~/Documents/Roblox/Plugins
      Riavvia Studio. Il plugin si collega da solo. La prima volta Studio chiede
      il permesso di contattare il sito ARPG: premi "Allow".
]]

local HttpService = game:GetService("HttpService")
local ChangeHistoryService = game:GetService("ChangeHistoryService")
local Selection = game:GetService("Selection")
local ServerStorage = game:GetService("ServerStorage")

local EMBED_URL = "__ARPG_API_URL__"
local EMBED_KEY = "__ARPG_STUDIO_KEY__"
local VERSION = "2.0"
local POLL_SECONDS = 3

local function embedded(v)
    return v ~= "" and not string.find(v, "__ARPG", 1, true)
end

local apiUrl = embedded(EMBED_URL) and EMBED_URL or tostring(plugin:GetSetting("apiUrl") or "")
local studioKey = embedded(EMBED_KEY) and EMBED_KEY or tostring(plugin:GetSetting("studioKey") or "")

-- ================= UI =================
local C = {
    bg = Color3.fromRGB(255, 249, 244), ink = Color3.fromRGB(30, 30, 47), muted = Color3.fromRGB(90, 90, 114),
    orange = Color3.fromRGB(255, 87, 34), cyan = Color3.fromRGB(0, 188, 212), green = Color3.fromRGB(16, 185, 129),
    amber = Color3.fromRGB(245, 158, 11), red = Color3.fromRGB(220, 60, 50), white = Color3.new(1, 1, 1),
}

local toolbar = plugin:CreateToolbar("ARPG")
local toggleButton = toolbar:CreateButton("ARPG Builder", "Apri il pannello ARPG", "rbxasset://textures/StudioToolbox/AssetPreview/hammer.png")
local widget = plugin:CreateDockWidgetPluginGui("ARPGBuilder", DockWidgetPluginGuiInfo.new(Enum.InitialDockState.Right, true, false, 360, 420, 300, 300))
widget.Title = "ARPG Builder"
toggleButton.Click:Connect(function() widget.Enabled = not widget.Enabled end)
widget:GetPropertyChangedSignal("Enabled"):Connect(function() toggleButton:SetActive(widget.Enabled) end)

local function new(cls, props, parent)
    local i = Instance.new(cls)
    for k, v in pairs(props) do i[k] = v end
    i.Parent = parent
    return i
end
local function round(inst, r) new("UICorner", { CornerRadius = UDim.new(0, r or 10) }, inst) end

local root = new("Frame", { Size = UDim2.fromScale(1, 1), BackgroundColor3 = C.bg, BorderSizePixel = 0 }, widget)
new("UIPadding", { PaddingTop = UDim.new(0, 12), PaddingBottom = UDim.new(0, 12), PaddingLeft = UDim.new(0, 12), PaddingRight = UDim.new(0, 12) }, root)
new("UIListLayout", { Padding = UDim.new(0, 10), SortOrder = Enum.SortOrder.LayoutOrder }, root)

new("TextLabel", {
    Size = UDim2.new(1, 0, 0, 30), BackgroundTransparency = 1, Font = Enum.Font.FredokaOne, TextSize = 26,
    TextXAlignment = Enum.TextXAlignment.Left, TextColor3 = C.orange, RichText = true, LayoutOrder = 1,
    Text = 'ARPG<font color="#00BCD4">.</font> <font size="14" color="#5A5A72">Builder v' .. VERSION .. "</font>",
}, root)

local statusCard = new("Frame", { Size = UDim2.new(1, 0, 0, 44), BackgroundColor3 = C.white, LayoutOrder = 2 }, root)
round(statusCard, 12)
new("UIStroke", { Color = Color3.fromRGB(230, 225, 220), Thickness = 2 }, statusCard)
local dot = new("Frame", { Size = UDim2.fromOffset(12, 12), Position = UDim2.new(0, 14, 0.5, -6), BackgroundColor3 = C.amber }, statusCard)
round(dot, 6)
local statusLabel = new("TextLabel", {
    Size = UDim2.new(1, -40, 1, 0), Position = UDim2.fromOffset(34, 0), BackgroundTransparency = 1, Font = Enum.Font.GothamBold,
    TextSize = 14, TextXAlignment = Enum.TextXAlignment.Left, TextColor3 = C.ink, Text = "Mi collego ad ARPG…", TextTruncate = Enum.TextTruncate.AtEnd,
}, statusCard)

local jobLabel = new("TextLabel", {
    Size = UDim2.new(1, 0, 0, 36), BackgroundTransparency = 1, Font = Enum.Font.GothamMedium, TextSize = 13, TextWrapped = true,
    TextXAlignment = Enum.TextXAlignment.Left, TextColor3 = C.muted, LayoutOrder = 3,
    Text = "Scrivi cosa vuoi costruire sul sito ARPG: qui apparirà da solo.",
}, root)

local barBg = new("Frame", { Size = UDim2.new(1, 0, 0, 14), BackgroundColor3 = Color3.fromRGB(235, 230, 225), LayoutOrder = 4, Visible = false }, root)
round(barBg, 7)
local barFill = new("Frame", { Size = UDim2.fromScale(0, 1), BackgroundColor3 = C.orange }, barBg)
round(barFill, 7)

local setup = new("Frame", { Size = UDim2.new(1, 0, 0, 120), BackgroundTransparency = 1, LayoutOrder = 5, Visible = false }, root)
new("UIListLayout", { Padding = UDim.new(0, 6), SortOrder = Enum.SortOrder.LayoutOrder }, setup)
local function input(ph, txt, order)
    local b = new("TextBox", {
        Size = UDim2.new(1, 0, 0, 30), BackgroundColor3 = C.white, Font = Enum.Font.Gotham, TextSize = 12, PlaceholderText = ph,
        Text = txt, TextColor3 = C.ink, ClearTextOnFocus = false, LayoutOrder = order, TextXAlignment = Enum.TextXAlignment.Left,
    }, setup)
    round(b, 8)
    new("UIPadding", { PaddingLeft = UDim.new(0, 8) }, b)
    return b
end
local urlBox = input("URL ARPG", apiUrl, 1)
local keyBox = input("Studio Key", studioKey, 2)
local saveBtn = new("TextButton", {
    Size = UDim2.new(1, 0, 0, 32), BackgroundColor3 = C.orange, Font = Enum.Font.GothamBold, TextSize = 14, TextColor3 = C.white, Text = "Salva e collega", LayoutOrder = 3,
}, setup)
round(saveBtn, 16)

local logFrame = new("ScrollingFrame", {
    Size = UDim2.new(1, 0, 1, -170), BackgroundColor3 = C.ink, BorderSizePixel = 0, LayoutOrder = 6, ScrollBarThickness = 4,
    AutomaticCanvasSize = Enum.AutomaticSize.Y, CanvasSize = UDim2.new(),
}, root)
round(logFrame, 10)
new("UIPadding", { PaddingTop = UDim.new(0, 6), PaddingLeft = UDim.new(0, 8), PaddingRight = UDim.new(0, 8), PaddingBottom = UDim.new(0, 6) }, logFrame)
local logText = new("TextLabel", {
    Size = UDim2.new(1, 0, 0, 0), AutomaticSize = Enum.AutomaticSize.Y, BackgroundTransparency = 1, Font = Enum.Font.Code, TextSize = 12,
    TextXAlignment = Enum.TextXAlignment.Left, TextYAlignment = Enum.TextYAlignment.Top, TextColor3 = Color3.fromRGB(180, 255, 220), TextWrapped = true, Text = "",
}, logFrame)

local lines = {}
local function log(line)
    table.insert(lines, "[" .. os.date("%H:%M:%S") .. "] " .. line)
    while #lines > 60 do table.remove(lines, 1) end
    logText.Text = table.concat(lines, "\n")
    task.defer(function() logFrame.CanvasPosition = Vector2.new(0, logFrame.AbsoluteCanvasSize.Y) end)
    print("[ARPG] " .. line)
end

local function setStatus(kind, text)
    dot.BackgroundColor3 = (kind == "ok" and C.green) or (kind == "busy" and C.cyan) or (kind == "err" and C.red) or C.amber
    statusLabel.Text = text
end

local function setProgress(done, total)
    barBg.Visible = total > 0
    barFill.Size = UDim2.fromScale(total > 0 and math.clamp(done / total, 0, 1) or 0, 1)
end

-- ================= HTTP =================
local function request(method, path, body)
    local ok, res = pcall(function()
        return HttpService:RequestAsync({
            Url = apiUrl .. path, Method = method,
            Headers = { ["X-Studio-Key"] = studioKey, ["Content-Type"] = "application/json" },
            Body = body and HttpService:JSONEncode(body) or nil,
        })
    end)
    if not ok then return false, nil, tostring(res) end
    if not res.Success then return false, nil, "HTTP " .. tostring(res.StatusCode) .. ": " .. string.sub(tostring(res.Body), 1, 160) end
    local okD, data = pcall(function() return HttpService:JSONDecode(res.Body) end)
    if not okD then return false, nil, "Risposta non valida" end
    return true, data, nil
end

local function report(job, success, message)
    for _ = 1, 5 do
        local ok, data = request("POST", "/api/studio/complete", {
            build_id = job.build_id, part_index = job.part_index, success = success, log = string.sub(tostring(message or "OK"), 1, 1900),
        })
        if ok then return data end
        task.wait(2)
    end
    return nil
end

-- ================= ESECUZIONE =================
local function focusOn(title)
    local model = workspace:FindFirstChild(title)
    if not model or not model:IsA("Model") then return end
    Selection:Set({ model })
    local okB, cf, size = pcall(function() return model:GetBoundingBox() end)
    local cam = workspace.CurrentCamera
    if okB and cam and size.Magnitude > 0 and size.Magnitude < 1e5 then
        local d = math.max(size.Magnitude * 0.75, 30)
        cam.CFrame = CFrame.lookAt(cf.Position + Vector3.new(d * 0.7, d * 0.55, d * 0.7), cf.Position)
        cam.Focus = cf
    end
end

local function runCode(code)
    local holder = Instance.new("ModuleScript")
    holder.Name = "ARPG_Run_" .. HttpService:GenerateGUID(false)
    holder.Source = "return function()\n" .. code .. "\nend"
    holder.Parent = ServerStorage
    local okReq, fn = pcall(require, holder)
    holder:Destroy()
    if not okReq then return false, "Errore di caricamento: " .. tostring(fn) end
    if type(fn) ~= "function" then return false, "Il codice non ha restituito una funzione" end
    local trace
    local ok = xpcall(fn, function(err)
        trace = tostring(err) .. "\n" .. debug.traceback("", 2)
        return err
    end)
    return ok, trace
end

local function beginHistory(name)
    local ok, id = pcall(function() return ChangeHistoryService:TryBeginRecording(name) end)
    if ok and id then return id end
    pcall(function() ChangeHistoryService:SetWaypoint("Prima di " .. name) end)
    return nil
end

local function endHistory(id, name, commit)
    if id then
        pcall(function()
            ChangeHistoryService:FinishRecording(id, commit and Enum.FinishRecordingOperation.Commit or Enum.FinishRecordingOperation.Cancel)
        end)
    else
        pcall(function() ChangeHistoryService:SetWaypoint("Dopo " .. name) end)
    end
end

local function executeJob(job)
    local label = string.format("%s — parte %d/%d: %s", tostring(job.title), job.part_index + 1, job.part_total, tostring(job.part_name))
    setStatus("busy", "Sto costruendo…")
    jobLabel.Text = label
    setProgress(job.part_index, job.part_total)
    log("Costruisco " .. label)
    local hid = beginHistory("ARPG " .. tostring(job.title))
    local ok, err = runCode(tostring(job.code or ""))
    endHistory(hid, "ARPG " .. tostring(job.title), ok)
    if ok then
        setProgress(job.part_index + 1, job.part_total)
        log("Parte completata ✓")
        report(job, true, "OK")
        if job.part_index + 1 >= job.part_total then
            log("Costruzione finita! Ctrl+Z per annullare.")
            jobLabel.Text = "✓ " .. tostring(job.title) .. " costruita!"
            focusOn(job.title)
        end
    else
        log("Errore, ARPG lo corregge da solo: " .. string.sub(tostring(err), 1, 200))
        local res = report(job, false, err)
        if res and res.repairing then
            jobLabel.Text = "Correggo automaticamente la parte " .. (job.part_index + 1) .. "…"
        else
            jobLabel.Text = "Costruzione non riuscita: riprova dal sito ARPG."
        end
    end
    setStatus("ok", "Collegato ad ARPG")
end

-- ================= LOOP =================
local lastOk = nil
local function pollOnce()
    if apiUrl == "" or studioKey == "" then
        setup.Visible = true
        setStatus("wait", "Inserisci URL e Studio Key")
        return false
    end
    local place = HttpService:UrlEncode(string.sub(game.Name or "", 1, 100))
    local ok, data, err = request("GET", "/api/studio/poll?place=" .. place)
    if not ok then
        setStatus("err", "Non collegato — riprovo…")
        if lastOk ~= false then log("Connessione fallita: " .. tostring(err)) end
        if string.find(tostring(err), "401", 1, true) then setup.Visible = true end
        lastOk = false
        return false
    end
    if lastOk ~= true then log("Collegato ad ARPG ✓") end
    lastOk = true
    setup.Visible = false
    if data.locked then
        setStatus("wait", "Collegato — account da attivare")
        jobLabel.Text = tostring(data.locked)
        return false
    end
    setStatus("ok", "Collegato ad ARPG")
    if data.job then
        executeJob(data.job)
        return true
    end
    return false
end

saveBtn.Activated:Connect(function()
    apiUrl = (urlBox.Text:gsub("%s+", "")):gsub("/+$", "")
    studioKey = keyBox.Text:gsub("%s+", "")
    plugin:SetSetting("apiUrl", apiUrl)
    plugin:SetSetting("studioKey", studioKey)
    lastOk = nil
    log("Impostazioni salvate")
end)

log("ARPG Builder v" .. VERSION .. " avviato")
task.spawn(function()
    while true do
        local okLoop, busy = pcall(pollOnce)
        if not okLoop then log("Errore interno: " .. tostring(busy)) end
        if not (okLoop and busy) then task.wait(POLL_SECONDS) end
    end
end)

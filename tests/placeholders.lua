-- Run from the repository root with Lua 5.4 or nvim --headless -l.
package.path = "./lua/?.lua;./lua/?/init.lua;" .. package.path

local function check(ui_send, tmux)
    for _, name in ipairs({
        "pyrepl.config",
        "pyrepl.image",
        "pyrepl.placeholders",
        "pyrepl.providers.placeholders",
    }) do
        package.loaded[name] = nil
    end

    local sent = {}
    local function send(sequence)
        sent[#sent + 1] = sequence
    end
    _G.vim = {
        env = { TERM_PROGRAM = "ghostty", TMUX = tmux and "/tmp/tmux-test" or nil },
        o = { termguicolors = true },
        v = { stderr = 2 },
        api = {
            nvim_create_namespace = function() return 1 end,
            nvim_create_augroup = function() return 1 end,
            nvim_create_autocmd = function() return 1 end,
            nvim_del_autocmd = function() end,
            nvim_chan_send = function(channel, sequence)
                assert(not ui_send, "stderr used despite UI API being available")
                assert(channel == 2)
                send(sequence)
            end,
        },
        deepcopy = function(value) return value end,
        wait = function() end,
    }
    if ui_send then
        vim.api.nvim_ui_send = send
    end

    local provider = require("pyrepl.config").get_image_provider()
    assert(type(provider.render_inline) == "function", "configured provider cannot render inline")
    assert(provider == require("pyrepl.placeholders"), "provider implementations diverged")

    -- Exercise the actual console endpoint, including history insertion.
    -- No floating-window APIs are mocked: accidentally falling back must fail.
    local payload = string.rep("AAAA", 2500)
    assert(require("pyrepl.image").console_endpoint(payload, {
        image_id = 0x123456, cols = 20, rows = 10,
    }) == true)

    local graphics = {}
    for _, sequence in ipairs(sent) do
        if tmux then
            assert(sequence:sub(1, 7) == "\27Ptmux;")
            sequence = sequence:sub(8, -3):gsub("\27\27", "\27")
        end
        if sequence:sub(1, 3) == "\27_G" then
            assert(sequence:sub(-2) == "\27\\")
            graphics[#graphics + 1] = sequence:sub(4, -3)
        end
    end
    assert(#graphics == 4, "expected three upload chunks and one placement")
    assert(graphics[1]:match("^a=t,f=100,t=d,i=1193046,q=2,m=1;"))
    assert(graphics[2]:match("^m=1;"))
    assert(graphics[3]:match("^m=0;"))
    assert(graphics[4] == "a=p,U=1,i=1193046,c=20,r=10,C=1,q=2")
    local chunks = {}
    for index = 1, 3 do
        chunks[index] = graphics[index]:match(";(.*)$")
        assert(#chunks[index] <= 4096)
    end
    assert(table.concat(chunks) == payload, "upload corrupted the image payload")

    local before = #sent
    vim.o.termguicolors = false
    assert(provider.render_inline(payload, 257, 20, 10) == false)
    vim.o.termguicolors = true
    vim.env.TERM_PROGRAM = "unknown"
    assert(provider.render_inline(payload, 257, 20, 10) == false)
    assert(#sent == before, "unsupported inline display sent image data")
end

check(true, false)
check(false, false)
check(true, true)
check(false, true)
print("PASS: provider routing, console endpoint, chunked uploads, placement, stderr fallback, tmux")

# solidworks-automation

An [MCP](https://modelcontextprotocol.io) server that lets AI agents (Claude, ChatGPT, Cursor, and other MCP clients)
design parts and assemblies in **SolidWorks** on your computer. Ask for "a 100 × 60 × 10 mm aluminium plate with four
M8 clearance holes and 2 mm fillets", and the agent sketches, extrudes, cuts, fillets, checks the result and exports it.

Inspired by [dshworks/dsh-solidworks](https://github.com/dshworks/dsh-solidworks), written in Python.

## How it works

```
 Agent (Claude / ChatGPT / ...)
        │  MCP (stdio or HTTP)
        ▼
 solidworks-automation  ──────────── Windows: talks to SolidWorks directly over COM
        │
        └── macOS: HTTP + token ──▶  solidworks-automation bridge   (inside a Windows VM)
                                              │ COM
                                              ▼
                                          SolidWorks
```

SolidWorks only runs on Windows. On a Mac it runs in a Windows VM (Parallels, VMware Fusion, ...). The MCP server runs on
the Mac and forwards each operation to a small bridge inside the VM.

### Asking permission

The server never starts SolidWorks silently. When an agent calls `sw_connect` and SolidWorks is not running, the user
sees a permission prompt (an MCP *elicitation*) in their agent app and must approve it. If SolidWorks is already
running, the server attaches to it without a prompt.

If a client cannot show prompts, `sw_connect` refuses and tells the agent to ask the user in chat. Only after the user
agrees does the agent call it again with `user_approved=true`. Most clients also ask the user before each tool call.

Overwriting files (`overwrite=true`) and discarding unsaved changes also show a prompt.

## Requirements

- SolidWorks 2016 or later on Windows (or in a Windows VM)
- Python 3.12+ and [uv](https://docs.astral.sh/uv/)

## Setup: Windows (same PC as SolidWorks)

```powershell
git clone <this repo> C:\tools\solidworks_automation
cd C:\tools\solidworks_automation
uv sync
```

**Claude Desktop**: add to `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "solidworks": {
      "command": "uv",
      "args": ["--directory", "C:\\tools\\solidworks_automation", "run", "solidworks-automation"]
    }
  }
}
```

**Claude Code**:

```powershell
claude mcp add solidworks -- uv --directory C:\tools\solidworks_automation run solidworks-automation
```

**Clients that connect over HTTP** (e.g. ChatGPT connectors): run
`uv run solidworks-automation serve --transport streamable-http` and expose it to the client as that client requires.

## Setup: macOS (SolidWorks in a Windows VM)

1. **In the Windows VM**, signed in as the user who runs SolidWorks:

   ```powershell
   uv sync
   uv run solidworks-automation bridge --host 0.0.0.0 --port 8765
   ```

   The bridge prints a token. Keep it; it is the shared secret. To use a fixed token, set `SW_BRIDGE_TOKEN` before
   starting the bridge.

2. **On the Mac**, clone the repo, run `uv sync`, and configure your client with the VM's address and the token:

   ```json
   {
     "mcpServers": {
       "solidworks": {
         "command": "uv",
         "args": ["--directory", "/Users/you/solidworks_automation", "run", "solidworks-automation"],
         "env": {
           "SW_BRIDGE_URL": "http://10.211.55.3:8765",
           "SW_BRIDGE_TOKEN": "<token printed by the bridge>"
         }
       }
     }
   }
   ```

Bridge traffic is plain HTTP authenticated by the token. Use the VM's host-only/shared network, or tunnel it over SSH.
Do not expose the bridge to the internet.

## Tools

| Tool | What it does |
|---|---|
| `sw_status` | Installed/running, release, open documents. Call first. |
| `sw_connect` | Attach to SolidWorks, or start it **after the user approves**. |
| `sw_new_document`, `sw_open`, `sw_close` | Create, open, close parts/assemblies. |
| `sw_create_sketch` | Lines, polylines, rectangles, circles, arcs, polygons, centerlines on a plane or face. |
| `sw_extrude`, `sw_cut_extrude`, `sw_revolve` | Solid features (blind, mid-plane, through-all, draft). |
| `sw_create_plane` | Offset reference planes. |
| `sw_fillet`, `sw_chamfer`, `sw_shell` | Edge and body finishing. Edges picked by model point or `"all"`. |
| `sw_set_material`, `sw_set_dimension`, `sw_set_suppression`, `sw_delete_feature`, `sw_undo` | Edit the model. |
| `sw_list_features`, `sw_rebuild`, `sw_mass_properties` | Inspect the feature tree, errors, mass, bounding box. |
| `sw_screenshot` | Image of the model from a standard view, so the agent can check its work. |
| `sw_save`, `sw_export` | Native save; STEP, IGES, STL, Parasolid, 3MF, PDF, DXF/DWG, PNG/JPG export. |
| `sw_insert_component`, `sw_add_mate`, `sw_list_components` | Build assemblies. |

All lengths are millimetres and all angles are degrees. Model space is Y-up, with the Front plane at Z = 0.

## Safety model

- **Starting SolidWorks needs the user's approval.** The server never closes SolidWorks or answers its dialogs.
- **Edits stay in memory** until `sw_save` / `sw_export` is called.
- **Writes are confined.** Files go to `~/Documents/SolidWorksMCP` (override with `SW_MCP_OUTPUT_DIR`). Other folders
  must be listed in `SW_MCP_ALLOWED_DIRS` (separated by `;` on Windows, `:` elsewhere). Existing files are replaced
  only with `overwrite=true`.
- **Deletion is limited** to features the server created in the current session. Other features can only be suppressed.
- **No arbitrary code.** There is no macro or "call any API" tool. The bridge accepts only the fixed list of operations
  in `operations.OPERATIONS`.

## Limitations

- No patterns, lofts, sweeps, Hole Wizard, sheet metal or drawings yet.
- Sketches have no dimensions or relations; geometry is placed at exact coordinates.
- Sketching on a face uses that face's own sketch axes; prefer standard or offset planes.
- **Not yet verified against a live SolidWorks install.** The COM calls follow the SolidWorks API documentation, but
  the automated tests use a fake backend. Run `examples/live_smoke.py` on a SolidWorks machine to verify, and please
  report anything that fails.

## Development

```powershell
uv sync
uv run pytest                          # server, permission flow, bridge, path policy (no SolidWorks needed)
uv run python examples/live_smoke.py   # end-to-end against a real SolidWorks
uvx ruff check src tests examples
```

Layout (`src/solidworks_automation/`):

- `server.py`: MCP tools, input schemas, permission prompts
- `backend.py`: local (COM thread) vs. remote (bridge) execution
- `operations.py`: every SolidWorks API call; the only module that touches SolidWorks
- `com.py`: late-bound `IDispatch` helpers, launch/attach, the COM thread
- `bridge.py`: token-authenticated HTTP bridge for the VM setup
- `safety.py`: file-write policy

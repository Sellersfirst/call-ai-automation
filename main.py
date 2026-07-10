import logging, time
from fastapi import FastAPI, Header, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware

from core.rate_limiter import rate_limiter
from config.database import init_db, get_row_limit, update_row_limit
from api.fus_bot_new_lead import Router as LeadRouter
from api.fus_bot_call_end import Router as CallEndRouter
from api.fus_bot_post_call import Router as PostCallRouter
from api.alab_sheets_bot import Router as AlabSheetsRouter
from api.sf_sheets_bot import Router as SFSheetsRouter
from api.count import router as SheetsstatsRouter
from api.call_analytics import router as CallAnalyticsRouter
from core.security import verify_admin
from api.sheets import router as SheetsRouter
from api.smrt_webhook import router as smrt_router
from api.ghl_webhook import router as GHLRouter
from api.prompts import router as PromptsRouter
from api.agent_memory_webhook import router as AgentMemoryRouter
from api.conversation import router as ConversationRouter
from core.celery_app import run_scheduler


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("app")

app = FastAPI(title="Lead Automation System")

# ── CORS must be added first, before any other middleware ─────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Request logging middleware ────────────────────────────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    duration = round(time.time() - start_time, 3)
    logger.info(f"{request.method} {request.url} - {duration}s")
    return response


@app.on_event("startup")
def startup_event():
    init_db()
    logger.info("Database initialized.")


class ConfigUpdate(BaseModel):
    num_rows: int


# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(LeadRouter,          prefix="/api/leads",        tags=["Lead Processing"])
app.include_router(CallEndRouter,       prefix="/api/callback",     tags=["Call Analysis"])
app.include_router(PostCallRouter,      prefix="/api/postcall",     tags=["Post-Call Logging"])
app.include_router(AlabSheetsRouter,    prefix="/api/alab-sheets",  tags=["ALab Sheets Bot"])
app.include_router(SFSheetsRouter,      prefix="/api/sf-sheets",    tags=["SF Sheets Bot"])
app.include_router(SheetsRouter,        prefix="/api",              tags=["Sheets"])
app.include_router(SheetsstatsRouter,   prefix="/api",              tags=["Sheet Stats"])
app.include_router(CallAnalyticsRouter, prefix="/api",              tags=["Analytics"])
app.include_router(smrt_router)
app.include_router(GHLRouter)
app.include_router(PromptsRouter,       prefix="/api",              tags=["Prompts"])
app.include_router(AgentMemoryRouter,   prefix="/api/agent-memory", tags=["Agent Memory"])
app.include_router(ConversationRouter,  prefix="/api",              tags=["Conversation"])

@app.get("/test-scheduler")
def test_scheduler():
    run_scheduler.delay()
    return {"message": "Scheduler triggered manually"}


@app.get("/config", dependencies=[Depends(rate_limiter)])
async def view_config(_: str = Depends(verify_admin)):
    return {"num_rows": get_row_limit()}

@app.post("/config", dependencies=[Depends(rate_limiter)])
async def update_config(data: ConfigUpdate, _: str = Depends(verify_admin)):
    update_row_limit(data.num_rows)
    return {"message": f"Limit updated to {data.num_rows}"}

@app.get("/", response_class=HTMLResponse)
async def simple_ui():
    current_limit = get_row_limit()

    return f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Lead Automation | Control Panel</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&display=swap" rel="stylesheet">
        <style>
            body {{ font-family: 'Inter', sans-serif; }}
        </style>
    </head>
    <body class="bg-gray-50 flex items-center justify-center min-h-screen">
        <div class="max-w-md w-full bg-white shadow-xl rounded-2xl p-8 border border-gray-100">
            <div class="text-center mb-8">
                <div class="inline-flex items-center justify-center w-12 h-12 bg-indigo-100 text-indigo-600 rounded-lg mb-4">
                    <svg xmlns="http://www.w3.org/2000/svg" class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                    </svg>
                </div>
                <h2 class="text-2xl font-bold text-gray-800">Lead Bot Settings</h2>
                <p class="text-gray-500 mt-2">Adjust processing limits and system parameters</p>
            </div>

            <div class="bg-indigo-50 rounded-lg p-4 mb-6 flex justify-between items-center">
                <span class="text-sm font-medium text-indigo-700">Current Batch Size</span>
                <span class="px-3 py-1 bg-white text-indigo-700 rounded-full text-sm font-bold shadow-sm">
                    {current_limit} Rows
                </span>
            </div>

            <div class="space-y-4">
                <div>
                    <label class="block text-xs font-semibold text-gray-400 uppercase tracking-wider mb-1 ml-1">Admin Security</label>
                    <input type="password" id="pw" 
                           class="w-full px-4 py-3 bg-gray-50 border border-gray-200 rounded-xl focus:ring-2 focus:ring-indigo-500 focus:outline-none transition-all" 
                           placeholder="Enter Admin Key">
                </div>

                <div>
                    <label class="block text-xs font-semibold text-gray-400 uppercase tracking-wider mb-1 ml-1">New Batch Limit</label>
                    <input type="number" id="rows" 
                           class="w-full px-4 py-3 bg-gray-50 border border-gray-200 rounded-xl focus:ring-2 focus:ring-indigo-500 focus:outline-none transition-all" 
                           placeholder="e.g. 5">
                </div>

                <button onclick="save()" id="saveBtn"
                        class="w-full bg-indigo-600 hover:bg-indigo-700 text-white font-semibold py-3 rounded-xl shadow-lg shadow-indigo-200 transition-all active:scale-95 flex items-center justify-center">
                    Update Configuration
                </button>
            </div>

            <div id="msg" class="mt-4 text-center text-sm hidden"></div>
        </div>

        <script>
            async function save() {{
                const btn = document.getElementById('saveBtn');
                const msg = document.getElementById('msg');
                const key = document.getElementById('pw').value;
                const val = document.getElementById('rows').value;

                if (!key || !val) {{
                    alert("Please fill in both fields");
                    return;
                }}

                btn.disabled = true;
                btn.innerText = "Updating...";
                btn.classList.add('opacity-50');

                try {{
                    const res = await fetch('/config', {{
                        method: 'POST',
                        headers: {{
                            'x-api-key': key,
                            'Content-Type': 'application/json'
                        }},
                        body: JSON.stringify({{num_rows: parseInt(val)}})
                    }});

                    const out = await res.json();

                    if (res.ok) {{
                        msg.className = "mt-4 text-center text-sm text-green-600 font-medium";
                        msg.innerText = "✓ Success: " + out.message;
                        msg.classList.remove('hidden');
                        setTimeout(() => location.reload(), 1500);
                    }} else {{
                        throw new Error(out.detail || "Unauthorized access");
                    }}
                }} catch (err) {{
                    msg.className = "mt-4 text-center text-sm text-red-500 font-medium";
                    msg.innerText = "✕ Error: " + err.message;
                    msg.classList.remove('hidden');
                    btn.disabled = false;
                    btn.innerText = "Update Configuration";
                    btn.classList.remove('opacity-50');
                }}
            }}
        </script>
    </body>
    </html>
    """
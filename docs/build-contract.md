# Internal build contract

Single-owner, self-hosted Python 3.12+ FastAPI application with SQLite WAL, a separate durable worker, and vanilla HTML/CSS/JS served same-origin. No demo success or fake metrics. Empty repo, no existing customer brand; use an original restrained green/ink operator workbench. Do not copy Tin proprietary prompts or assets.

## HTTP interface (all /api authenticated except login)
- POST /api/login {password} -> {ok:true,csrf_token}; sets HttpOnly session cookie.
- GET /api/session -> {csrf_token}; POST /api/logout.
- All mutations use X-CSRF-Token from session. Cookies same-origin.
- GET /api/overview -> {project:{name,goal,autonomy},counts:{tasks,running,approvals,completed},worker:{online,last_seen},provider:{configured,model},recent_events:[]}
- GET /api/agents -> [{id,name,description,skills:[string]}]
- GET /api/tasks -> [{id,title,prompt,agent,status,created_at,updated_at,result,error,steps}]
- POST /api/tasks {title,prompt,agent='strategist',start=false} -> task; GET /api/tasks/{id} -> {...task,events:[],approvals:[],artifacts:[]}
- POST /api/tasks/{id}/run, POST /api/tasks/{id}/cancel -> task
- GET /api/approvals -> [{id,task_id,tool,arguments:object,status,created_at}]
- POST /api/approvals/{id}/decision {decision:'approve'|'reject'} -> approval
- GET /api/memory -> [{key,content,updated_at}]; PUT /api/memory/{key} {content}
- GET /api/schedules -> [{id,name,prompt,agent,interval_minutes,enabled,next_run_at}]
- POST /api/schedules {name,prompt,agent,interval_minutes}; PATCH /api/schedules/{id} {enabled:boolean}
- GET /api/integrations -> [{id,name,configured,description}]
- GET /api/events -> [{id,task_id,kind,message,created_at}]
- GET /api/settings -> {name,goal,autonomy:'manual'|'supervised'|'autonomous'}
- PATCH /api/settings {name,goal,autonomy}; GET /healthz public; GET /api/readiness authenticated.
- GET /api/artifacts/{id} downloads attachment. Task status: draft,queued,running,waiting_approval,done,failed,cancelled.
- JSON errors {detail:string}. Poll active views every 5 seconds, no secret storage in browser.

## Tools module contract
agent4good/catalog.py: AGENTS list above; agent_prompt(agent_id)->str.
agent4good/tools.py: ToolRegistry(settings), .definitions()->list Responses function schema, .requires_approval(name,args,autonomy)->bool, .execute(name,args)-> JSON-serializable object, .integrations()->list. .execute independent of DB. Engine implements memory_read, memory_write, artifact_write itself, not registry. Registry tools must be allowlisted, parameter-validated, bounded, never shell arbitrary code. External mutations always require approval; read-only require approval in manual mode. Paths, hosts and repositories server-constrained. Secrets from settings attributes only.
agent4good/provider.py: ResponsesProvider(settings), .respond(instructions:str, items:list, tools:list)->{output:list,output_text:str,usage:object}. Synchronous using httpx; OpenAI fixed HTTPS endpoint. Require settings.openai_api_key; settings.model; settings.max_output_tokens. No automatic charge-incurring calls in build verification.
Settings attributes: data_dir:Path, admin_password:str, session_secret:str, secure_cookies:bool, allowed_hosts:list[str], public_origin:str, openai_api_key:str, model:str, max_output_tokens:int, max_steps:int, github_token:str, github_repo:str, resend_api_key:str, mail_from:str, search_api_key:str, allowed_read_hosts:list[str], worker_poll_seconds:float.
Tools implement genuine GitHub read/create issue/create branch/read file/propose file (branch only), web search via Brave if configured, allowlisted public HTTPS fetch with DNS/private IP protection, send email via Resend with exact approval. Unsupported provider categories explicitly 'not implemented', not fake connect toggles. Image generation and third-party adapters can be documented extension boundaries if not implemented.

## Ownership
Root owns app.py, db.py, config.py, engine.py, worker.py, security.py, tests/test_app.py, tests/test_engine.py, infra/docs README.
UI specialist owns only agent4good/static/* and UI test if desired.
Tools specialist owns only catalog.py, tools.py, provider.py, tests/test_tools.py, tests/test_provider.py, docs/capabilities.md.

const el=id=>document.getElementById(id);
function inAppConfirm({title="Approval required",message,confirmLabel="Continue"}){
 return new Promise(resolve=>{
  const overlay=document.createElement("div");overlay.className="so-approval-overlay";overlay.setAttribute("role","presentation");
  const panel=document.createElement("section");panel.className="so-approval-dialog";panel.setAttribute("role","dialog");panel.setAttribute("aria-modal","true");panel.setAttribute("aria-labelledby","so-approval-title");
  const heading=document.createElement("h3");heading.id="so-approval-title";heading.textContent=title;
  const copy=document.createElement("p");copy.textContent=message;
  const actions=document.createElement("div");actions.className="so-approval-actions";
  const cancel=document.createElement("button");cancel.type="button";cancel.textContent="Cancel";
  const approve=document.createElement("button");approve.type="button";approve.className="primary";approve.textContent=confirmLabel;
  actions.append(cancel,approve);panel.append(heading,copy,actions);overlay.append(panel);document.body.append(overlay);
  const finish=value=>{overlay.remove();document.removeEventListener("keydown",onKey);resolve(value);};
  const onKey=event=>{if(event.key==="Escape")finish(false);};
  cancel.onclick=()=>finish(false);approve.onclick=()=>finish(true);overlay.addEventListener("click",event=>{if(event.target===overlay)finish(false);});
  document.addEventListener("keydown",onKey);approve.focus();
 });
}
function fitMessageComposer(){
 const field=el("message");if(!field)return;
 field.style.setProperty("height","auto","important");field.style.setProperty("height",Math.min(120,Math.max(40,field.scrollHeight))+"px","important");
}
let base="",token="",workspaces=[],active="",conversation=null,routerData=null,callActive=false,voiceBusy=false,apiConnected=false,listenTimer=null,voiceWaitTimer=null,callSession=0,pendingImages=[],cloudSessionApproval="",voiceCapabilities=null;
let micStream=null,micRecorder=null,micAudioContext=null,micAnalyser=null,micFrame=null,micChunks=[],micDiscard=false,micFinalizing=false,micSpeechStartedAt=0,micLastVoiceAt=0,micNoiseFloor=0.003,micCalibrationStartedAt=0,micAboveCount=0,voiceTranscriptionController=null;
const prefs={sound:true,noticeSeconds:10,voiceRate:1.14,voiceLanguage:"en-GB",voiceName:""};
try{Object.assign(prefs,JSON.parse(localStorage.getItem("sovereign-ui-preferences")||"{}"));}catch(e){}
if([0.96,1.08].includes(Number(prefs.voiceRate)))prefs.voiceRate=1.14;
function savePrefs(){localStorage.setItem("sovereign-ui-preferences",JSON.stringify(prefs));}
let audioContext=null;

let activeAudio=null,activeAudioUrl=null,voiceRequestController=null,activeUtterance=null,lastSpokenText="";

let liveRecognition=null,liveInterimText="",liveFinalizing=false,voiceCloudConsentSession=0;

function stopStreamingRecognition(){
 const recognition=liveRecognition;
 liveRecognition=null;
 liveInterimText="";
 if(recognition){
  recognition.onresult=null;
  recognition.onerror=null;
  recognition.onend=null;
  recognition.onstart=null;
  try{recognition.stop();}catch(error){}
 }
}

function stopVoicePlayback(){
    const controller=voiceRequestController;
    voiceRequestController=null;
    if(controller)controller.abort();

    if(activeUtterance){
        activeUtterance.onstart=null;
        activeUtterance.onend=null;
        activeUtterance.onerror=null;
        activeUtterance=null;
    }
    if(window.speechSynthesis)window.speechSynthesis.cancel();

    if(activeAudio){
        const audio=activeAudio;
        activeAudio=null;
        audio.onended=null;
        audio.onerror=null;
        audio.pause();
        audio.removeAttribute("src");
        audio.load();
    }

    if(activeAudioUrl){
        URL.revokeObjectURL(activeAudioUrl);
        activeAudioUrl=null;
    }
}

function notificationSound(){if(!prefs.sound)return;try{const Ctx=window.AudioContext||window.webkitAudioContext;if(!Ctx)return;audioContext=audioContext||new Ctx();if(audioContext.state==="suspended")audioContext.resume();const now=audioContext.currentTime;[660,880].forEach((hz,i)=>{const osc=audioContext.createOscillator(),gain=audioContext.createGain();osc.type="sine";osc.frequency.value=hz;gain.gain.setValueAtTime(0.0001,now+i*.13);gain.gain.exponentialRampToValueAtTime(.075,now+i*.13+.025);gain.gain.exponentialRampToValueAtTime(.0001,now+i*.13+.18);osc.connect(gain);gain.connect(audioContext.destination);osc.start(now+i*.13);osc.stop(now+i*.13+.2);});}catch(e){/* audio is optional */}}
const providers=["Groq","OpenRouter Free","Cloudflare Workers AI","Z.AI","SambaNova","NVIDIA NIM","Ollama","Mistral API (paid opt-in)"];
const titles={agent:["Agent workspace","Turn goals into plans, review permissions and track verified work."],overview:["Command centre","Your intelligence. Your devices. Your control."],chat:["Workspace intelligence","A private intelligence workspace."],call:["Call your agent","Talk naturally. Hear Sovereign respond."],models:["Model health","Know which intelligence is ready and why."],iot:["IoT operations","Observe connected systems safely."],approvals:["Approval centre","Your permission is required for consequential actions."],settings:["Settings","Configure your local intelligence connection."]};
function notice(msg,kind="info"){const stack=el("notification-stack");if(!stack)return;const card=document.createElement("article");card.className="toast toast-"+(kind==="error"?"error":kind==="warning"?"warning":"info");const icon=document.createElement("span");icon.className="toast-icon";icon.textContent=kind==="error"?"!":kind==="warning"?"△":"✦";const copy=document.createElement("div");copy.className="toast-copy";const title=document.createElement("b");title.textContent=kind==="error"?"Needs attention":kind==="warning"?"System notice":"SovereignOS";const body=document.createElement("p");body.textContent=msg;copy.append(title,body);const close=document.createElement("button");close.className="toast-close";close.type="button";close.setAttribute("aria-label","Dismiss notification");close.textContent="×";close.onclick=()=>card.remove();card.append(icon,copy,close);stack.append(card);notificationSound();const duration=Math.max(6,Number(prefs.noticeSeconds)||10)*1000;window.setTimeout(()=>{card.classList.add("toast-leaving");window.setTimeout(()=>card.remove(),350);},kind==="error"?Math.max(duration,16000):duration);while(stack.children.length>5)stack.firstElementChild.remove();}
function setBusy(button,busy,label){if(!button)return;button.disabled=busy;if(busy){button.dataset.label=button.textContent;button.textContent=label||"Working…";}else if(button.dataset.label){button.textContent=button.dataset.label;delete button.dataset.label;}}
async function api(path,options={}){if(!base||!token)throw Error("The local SovereignOS session has not been established. Restart the desktop launcher and reconnect.");const {timeoutMs=45000,...request}=options;const signal=request.signal||AbortSignal.timeout(timeoutMs);const r=await fetch(base.replace(/\/$/,"")+path,{...request,signal,headers:{"Content-Type":"application/json","Authorization":"Bearer "+token,...request.headers}});const d=await r.json().catch(()=>({detail:"Invalid response"}));if(!r.ok)throw Error(d.detail||("HTTP "+r.status));return d;}
function setCoreState(state){document.body.dataset.coreState=state;document.querySelectorAll(".orb-wrap,.chat-welcome,.call-shell").forEach(node=>{node.dataset.coreState=state;});const indicator=el("chat-state");if(indicator){const labels={idle:"CORE READY",thinking:"PROCESSING",speaking:"RESPONDING",listening:"LISTENING",error:"ATTENTION REQUIRED",connecting:"CONNECTING"};indicator.textContent=labels[state]||"CORE READY";indicator.dataset.state=state;}}
function view(name){document.querySelectorAll(".view").forEach(x=>x.classList.add("hidden"));el("view-"+name).classList.remove("hidden");document.querySelectorAll(".nav").forEach(x=>x.classList.toggle("active",x.dataset.view===name));el("title").textContent=titles[name][0];el("header-sub").textContent=titles[name][1];if(name==="call")updateCallSupport();if(name==="agent")loadAgentTasks();if(name==="approvals")approvals().catch(error=>notice(error.message,"error"));if(name==="settings")bindPreferences();}
document.querySelectorAll(".nav").forEach(b=>b.onclick=()=>view(b.dataset.view));
document.querySelectorAll("[data-go]").forEach(b=>b.onclick=()=>view(b.dataset.go));
function renderWorkspaces(){const s=el("workspace");s.replaceChildren();workspaces.forEach(w=>{const o=document.createElement("option");o.value=w.id;o.textContent=w.name;s.append(o)});s.value=active;const w=workspaces.find(x=>x.id===active);el("active").textContent=w?.name||"Not connected";el("chat-title").textContent=w?.name==="SmartFlow BW"?"Thothi · SmartFlow BW":"SovereignOS · "+(w?.name||"Workspace");if(el("intel-presence"))el("intel-presence").textContent=w?w.purpose:"No active workspace selected";el("workspace-list").replaceChildren(...workspaces.map(w=>{const d=document.createElement("div");d.className="workspace-row";const b=document.createElement("b");b.textContent=w.name;const p=document.createElement("small");p.textContent=w.purpose;d.append(b,p);return d;}));}
async function autoConnectLocal(){
 const feedback=el("settings-feedback");
 const preferred=Number(new URLSearchParams(location.search).get("apiPort"))||8765;
 const ports=[...Array(20)].map((_,i)=>preferred+i);
 let lastError="No local SovereignOS API answered the bootstrap request.";
 for(let attempt=0;attempt<4;attempt++){
  for(const port of ports){
   try{
    const response=await fetch("http://127.0.0.1:"+port+"/api/v1/bootstrap",{cache:"no-store",signal:AbortSignal.timeout(900)});
    if(!response.ok){lastError="API port "+port+" returned HTTP "+response.status+" from bootstrap.";continue;}
    const session=await response.json();
    if(!session.api_url||!session.token){lastError="The local API answered without a valid desktop session.";continue;}
    base=session.api_url;token=session.token;
    await connect();
    return;
   }catch(error){lastError=error?.message||String(error);}
  }
  if(feedback)feedback.textContent="Searching for the local intelligence core… (attempt "+(attempt+1)+"/12)";
  await new Promise(resolve=>setTimeout(resolve,500));
 }
 if(feedback)feedback.textContent="Could not discover the local API. "+lastError+" Confirm the desktop engine is running, then select Reconnect local engine. No URL or token entry is needed."; 
}
async function connect(){
 const button=el("save");
 apiConnected=false;updateCallSupport();
 setBusy(button,true,"Connecting…");
 el("settings-feedback").textContent="Checking API and authenticating…";
 try{
  if(!base||!token)throw Error("Automatic local session is missing. Confirm the SovereignOS desktop engine is running.");
  const h=await fetch(base.replace(/\/$/,"")+"/api/v1/health",{cache:"no-store",signal:AbortSignal.timeout(2500)});
  if(!h.ok)throw Error("API health check failed (HTTP "+h.status+")");
  const ws=await api("/api/v1/workspaces");
  workspaces=ws.workspaces;
  active=workspaces.some(w=>w.id===active)?active:(workspaces[0]?.id||"");
  renderWorkspaces();
  apiConnected=true;
  updateCallSupport();
  el("dot").classList.add("online");
  el("connection").textContent="API connected · loading services";
  el("connect").textContent="API connected ✓";
  el("footer").textContent="API authenticated";
  el("mode").textContent="LIVE API";
  el("settings-feedback").textContent="API authenticated successfully. Loading workspace services…";
  notice("SovereignOS API authenticated. Loading workspace services…");
  const results=await Promise.allSettled([models(),devices(),approvals(),conversations(),voiceStatus()]);
  const failed=results.filter(x=>x.status==="rejected");
  el("connection").textContent=routerData?.providers?.length?"API connected · models configured":"API connected · no model configured";
  if(failed.length){
   const msg=failed.length+" optional service panel(s) could not load. The API connection is still active.";
   el("settings-feedback").textContent=msg;
   notice(msg,"error");
  }else{
   el("settings-feedback").textContent="Connected. Workspace, models, devices and approvals loaded.";
   notice("SovereignOS API connected. All panels loaded.");
  }
 }catch(e){
  apiConnected=false;updateCallSupport();
  el("dot").classList.remove("online");
  el("connection").textContent="API disconnected";
  el("connect").textContent="Connect API";
  el("footer").textContent="Connection failed";
  el("mode").textContent="OFFLINE";
  el("settings-feedback").textContent=e.message;
  notice(e.message,"error");
 }finally{setBusy(button,false);}
}
async function voiceStatus(){try{voiceCapabilities=await api("/api/v1/voice/status");}catch(error){voiceCapabilities={available:false,reason:error.message||"Voice readiness could not be checked."};}updateCallSupport();return voiceCapabilities;}
async function conversations(){const d=await api("/api/v1/workspaces/"+encodeURIComponent(active)+"/conversations");const box=el("messages");box.replaceChildren();if(!d.conversations.length){conversation=null;box.textContent="No conversations in this workspace yet. Start a new conversation below.";return;}const current=d.conversations.find(c=>c.id===conversation);const chosen=current||d.conversations[0];const history=async c=>{conversation=c.id;box.replaceChildren();const result=await api("/api/v1/workspaces/"+encodeURIComponent(active)+"/conversations/"+encodeURIComponent(c.id)+"/messages");result.messages.forEach(m=>addBubble(m.role,m.content));};const rail=document.createElement("div");rail.className="conversation-history-rail";d.conversations.forEach(c=>{const b=document.createElement("button");b.className="history-item";b.textContent=c.title;b.classList.toggle("history-active",c.id===chosen.id);b.onclick=()=>history(c).catch(err=>notice(err.message,"error"));rail.append(b);});box.append(rail);await history(chosen);}
async function models(){try{const d=await api("/api/v1/models/health");routerData=d;const list=el("model-list");list.replaceChildren();const configured=d.providers;const ready=configured.filter(p=>p.status==="ready").length;const connected=configured.filter(p=>p.status==="connected").length;const activeModel=d.providers.find(p=>p.active);el("models-count").textContent=activeModel?"1 ACTIVE":"0 ACTIVE";el("overview-model-detail").textContent=activeModel?activeModel.provider+" · "+activeModel.model:configured.length+" connected · choose a model to activate";el("model-summary").textContent=(activeModel?"Active: "+activeModel.provider+" · "+activeModel.model:"No active selection")+" · "+ready+" ready · "+connected+" connected";el("model-badge").textContent=activeModel?activeModel.provider+" · "+activeModel.model:"SELECT A MODEL";el("model-ready-label").textContent=configured.length?configured.length+" model"+(configured.length===1?"":"s")+" configured":"No model provider configured";el("model-ready-detail").textContent="Connected means credentials and a client are configured. Ready means a successful request has been completed in this API session.";el("router-state").textContent=configured.length?"ROUTING AVAILABLE":"AWAITING MODEL";el("router-state").className="status-tag "+(configured.length?"status-safe":"status-warn");const select=el("model-select");select.replaceChildren();d.providers.forEach(p=>{const o=document.createElement("option");o.value=JSON.stringify({provider:p.provider,model:p.model});o.textContent=p.provider+" · "+p.model+(p.active?" · ACTIVE":"");if(p.active)o.selected=true;select.append(o);});providers.forEach(n=>{const p=d.providers.find(x=>x.provider.toLowerCase().includes(n.toLowerCase().split(" ")[0]));const c=document.createElement("article");c.className="model-card "+(p?"model-ready":"model-off");const top=document.createElement("div");top.className="model-card-top";const b=document.createElement("b");b.textContent=n;const badge=document.createElement("span");badge.className="status-tag "+(p?.status==="ready"?"status-safe":p?.status==="connected"?"status-idle":p?.status==="cooldown"?"status-warn":"status-idle");badge.textContent=p?(p.status==="ready"?"READY":p.status==="connected"?"CONNECTED":p.status==="cooldown"?"COOLDOWN":"UNAVAILABLE"):"NOT CONFIGURED";top.append(b,badge);const detail=document.createElement("p");detail.textContent=p?p.model+" · "+p.runtime:"No provider credentials loaded by the API";const stats=document.createElement("div");stats.className="model-stats";stats.textContent=p?(p.successes+" successful · "+p.failures+" failed"+(p.active?" · active selection":"")):"Add at least one provider credential to the project's local .env file, then restart the API.";c.append(top,detail,stats);list.append(c);});el("model-activate").disabled=!d.providers.length;updateCallSupport();await loadSpecialists();const warning=el("model-warnings");if(d.warnings?.length){warning.textContent=d.warnings.join(" ");warning.classList.remove("hidden");}else warning.classList.add("hidden");return d;}catch(e){notice("Could not read model health: "+e.message,"error");throw e;}}
async function loadSpecialists(){
 const list=el("specialist-list"),run=el("run-group-chat");if(!list||!run)return;
 try{
  const data=await api("/api/v1/models/specialists");list.replaceChildren();
  if(!data.specialists.length){const empty=document.createElement("p");empty.textContent="No configured model candidates yet. Add provider credentials in the local API environment and restart SovereignOS.";list.append(empty);run.disabled=true;return;}
  data.specialists.forEach(sp=>{const label=document.createElement("label");label.className="specialist-option";const input=document.createElement("input");input.type="checkbox";input.value=sp.id;input.checked=list.querySelectorAll("input:checked").length<Math.min(2,data.specialists.length);const copy=document.createElement("span");const name=document.createElement("b");name.textContent=sp.title;const detail=document.createElement("small");detail.textContent=sp.provider+" · "+sp.model+" · "+sp.status;copy.append(name,detail);label.append(input,copy);list.append(label);input.onchange=()=>{const checked=list.querySelectorAll("input:checked");if(checked.length>4){input.checked=false;notice("A group can include up to four Specialists.","warning");}run.disabled=list.querySelectorAll("input:checked").length<2;};});
  run.disabled=list.querySelectorAll("input:checked").length<2;
 }catch(error){list.textContent="Specialist roster unavailable: "+error.message;run.disabled=true;}
}
async function runSpecialistGroup(){
 const mission=el("group-mission").value.trim(),selected=[...el("specialist-list").querySelectorAll("input:checked")].map(x=>x.value),button=el("run-group-chat"),output=el("group-chat-output");
 if(!mission||selected.length<2)return;
 setBusy(button,true,"Specialists are working…");output.replaceChildren();
 const status=document.createElement("p");status.textContent="Convening "+selected.length+" Specialists. Each will receive the shared workspace conversation.";output.append(status);
 try{
  const data=await api("/api/v1/workspaces/"+encodeURIComponent(active)+"/group-chat",{method:"POST",body:JSON.stringify({message:mission,conversation_id:conversation,specialist_ids:selected})});
  conversation=data.conversation_id;output.replaceChildren();
  data.responses.forEach(item=>{const card=document.createElement("article");card.className="specialist-response";const heading=document.createElement("b");heading.textContent=item.specialist+" · "+item.provider;const body=document.createElement("p");body.textContent=item.reply;card.append(heading,body);output.append(card);});
  notice("Specialist collective completed. All contributions were saved to this workspace conversation.");
 }catch(error){output.textContent="Group session failed: "+error.message;notice(error.message,"error");}
 finally{setBusy(button,false);}
}
async function activateModel(){try{const choice=JSON.parse(el("model-select").value);await api("/api/v1/models/select",{method:"POST",body:JSON.stringify(choice)});await models();notice("Manual model selection updated: "+choice.provider+" · "+choice.model+".");}catch(e){notice("Model switch failed: "+e.message,"error");}}
async function devices(){const d=await api("/api/v1/iot/devices");el("devices-count").textContent=d.simulation?d.devices.length+" simulated":"Connected";const list=el("device-list");list.replaceChildren();d.devices.forEach(x=>{const c=document.createElement("article");c.className="device-card";const b=document.createElement("b");b.textContent=x.name;const s=document.createElement("span");s.className="status-tag status-warn";s.textContent="SIMULATION";const p=document.createElement("p");p.textContent=x.value+" "+x.unit+" · "+x.kind;c.append(b,s,p);list.append(c);});}
async function approvals(){
 const d=await api("/api/v1/approvals"+(active?"?workspace_id="+encodeURIComponent(active):""));
 const count=d.approvals.length;el("approval-count").textContent=count;
 el("approval-heading").textContent=count?count+" action"+(count===1?"":"s")+" need your review":"Your approval centre is clear.";
 el("approval-subtitle").textContent=count?"Review each proposed step. Approval records the decision but does not run the action.":"No pending actions in this workspace.";
 const list=el("approval-list");list.replaceChildren();
 if(!count){const empty=document.createElement("div");empty.className="approval-empty";const symbol=document.createElement("div");symbol.className="empty-symbol";symbol.textContent="◇";const heading=document.createElement("strong");heading.textContent="All quiet for now";const copy=document.createElement("p");copy.textContent="Sensitive plan steps will appear here with their workspace, purpose, risk and requested permissions.";empty.append(symbol,heading,copy);list.append(empty);return;}
 d.approvals.forEach(a=>{
  const card=document.createElement("article");card.className="approval-item";
  const heading=document.createElement("h3");heading.textContent=a.task_title;
  const scope=document.createElement("small");scope.textContent=a.workspace_name+" · Step "+a.step_number+" · "+a.permission_level.replaceAll("_"," ").toUpperCase();
  const action=document.createElement("p");action.textContent=a.action;
  const description=document.createElement("p");description.textContent=a.task_description;
  const details=a.requested_details||{};
  let preview=null;
  if(a.tool_name==="workspace.create_note"){
   preview=document.createElement("pre");preview.className="approval-tool-preview";
   preview.textContent="New local file: "+(details.filename||"unnamed.md")+"\n\n"+(details.content||"");
  }else if(a.tool_name==="workspace.read_file"){
   preview=document.createElement("pre");preview.className="approval-tool-preview";
   preview.textContent="Read-only file scope: "+(details.path||"unspecified");
  }
  const actions=document.createElement("div");actions.className="approval-actions";
  const reject=document.createElement("button");reject.type="button";reject.className="danger";reject.textContent="Reject";
  const approve=document.createElement("button");approve.type="button";approve.className="primary";approve.textContent="Approve step";
  reject.onclick=()=>decideApproval(a,false,reject);approve.onclick=()=>decideApproval(a,true,approve);
  actions.append(reject,approve);card.append(heading,scope,action,description);if(preview)card.append(preview);card.append(actions);list.append(card);
 });
}
async function decideApproval(item,approved,button){
 setBusy(button,true,approved?"Approving…":"Rejecting…");
 try{
  await api("/api/v1/workspaces/"+encodeURIComponent(item.workspace_id)+"/agent/tasks/"+encodeURIComponent(item.problem_id)+"/steps/"+encodeURIComponent(item.step_id)+"/decision",{method:"POST",body:JSON.stringify({approved,note:"Decision made in SovereignOS Approval Centre."})});
  notice(approved?"Step approved. No action has been executed.":"Step rejected and blocked.");
  await approvals();await loadAgentTasks();
 }catch(error){notice("Approval decision failed: "+error.message,"error");setBusy(button,false);}
}
async function loadAgentTasks(){
 const list=el("agent-task-list");if(!list||!active)return;
 try{
  const data=await api("/api/v1/workspaces/"+encodeURIComponent(active)+"/agent/tasks");list.replaceChildren();
  if(!data.tasks.length){const empty=document.createElement("p");empty.className="muted";empty.textContent="No agent plans in this workspace yet. Give SovereignOS a goal above to create the first plan.";list.append(empty);return;}
  data.tasks.forEach(entry=>{
   const card=document.createElement("article");card.className="agent-task-card";
   const heading=document.createElement("h3");heading.textContent=entry.task.title;
   const status=document.createElement("span");status.className="status-tag "+(entry.task.status==="completed"?"status-safe":entry.task.status==="awaiting_approval"?"status-warn":"status-idle");status.textContent=entry.task.status.replaceAll("_"," ").toUpperCase();
   const description=document.createElement("p");description.textContent=entry.task.description;
   const specs=entry.tool_specs||[];
   const steps=document.createElement("ol");steps.className="agent-step-list";
   entry.steps.forEach(step=>{
    const item=document.createElement("li");const action=document.createElement("span");action.textContent=step.action;
    const spec=specs.find(value=>value.step_id===step.id);
    const meta=document.createElement("small");meta.textContent=step.permission_level.replaceAll("_"," ")+" · "+step.status.replaceAll("_"," ")+(spec?" · "+spec.tool_name:"");
    item.append(action,meta);steps.append(item);
   });
   card.append(heading,status,description,steps);
   const runnable=specs.some(spec=>{
    const step=entry.steps.find(value=>value.id===spec.step_id);
    return step&&(step.status==="approved"||(step.status==="planned"&&step.permission_level==="read_only"));
   });
   if(runnable&&!["blocked","failed","completed","cancelled"].includes(entry.task.status)){
    const run=document.createElement("button");run.type="button";run.className="primary";run.textContent="Run safe steps ↗";
    run.onclick=()=>runAgentStep(entry.task.id,run);card.append(run);
   }
   if(!["completed","cancelled"].includes(entry.task.status)){
    const cancel=document.createElement("button");cancel.type="button";cancel.className="agent-cancel";cancel.textContent="Cancel task";
    cancel.onclick=()=>cancelAgentTask(entry.task.id,cancel);card.append(cancel);
   }
   list.append(card);
  });
 }catch(error){list.textContent="Agent task list unavailable: "+error.message;}
}
async function runAgentStep(taskId,button){
 const feedback=el("agent-plan-feedback"),output=el("agent-tool-result");
 setBusy(button,true,"Running safe steps…");
 try{
  const result=await api("/api/v1/workspaces/"+encodeURIComponent(active)+"/agent/tasks/"+encodeURIComponent(taskId)+"/run-safe",{method:"POST",body:JSON.stringify({}),timeoutMs:60000});
  const results=result.results||[];
  const summaries=results.map(item=>{
   const tool=item.tool_result||{};
   if(tool.tool==="workspace.list_files")return "Listed "+tool.count+" files";
   if(tool.tool==="workspace.read_file")return "Read "+tool.path+" ("+tool.bytes+" bytes)";
   if(tool.tool==="workspace.create_note")return "Created "+tool.path+" ("+tool.bytes+" bytes)";
   return tool.tool||"Tool step";
  });
  feedback.textContent=result.executed_count+" safe step(s) executed and verified. "+result.stop_reason+" Task status: "+result.task_status.replaceAll("_"," ")+". ";
  output.textContent=results.length?results.map((item,index)=>"STEP "+(index+1)+" · "+(summaries[index]||"Verified")+"\n"+JSON.stringify(item.tool_result||{},null,2)).join("\n\n"):"No safe tool steps were ready."; 
  output.classList.remove("hidden");
  await loadAgentTasks();await approvals();
  notice(result.stopped_at_approval?"Safe steps verified. The next consequential action is waiting in Approvals.":"Agent safe-step batch completed.");
 }catch(error){
  feedback.textContent="Agent execution stopped: "+error.message;
  notice("Agent tool could not complete: "+error.message,"error");
  setBusy(button,false);
 }
}
async function cancelAgentTask(taskId,button){
 setBusy(button,true,"Cancelling…");
 try{
  await api("/api/v1/workspaces/"+encodeURIComponent(active)+"/agent/tasks/"+encodeURIComponent(taskId)+"/cancel",{method:"POST"});
  el("agent-plan-feedback").textContent="Task cancelled. Pending approvals and future tool steps are closed.";
  await loadAgentTasks();await approvals();
  notice("Agent task cancelled. No further steps can run.");
 }catch(error){notice("Task could not be cancelled: "+error.message,"error");setBusy(button,false);}
}
async function createAgentPlan(){
 const field=el("agent-goal"),button=el("agent-plan-button"),feedback=el("agent-plan-feedback"),goal=field.value.trim();
 if(goal.length<10){feedback.textContent="Describe the goal in at least 10 characters.";return;}
 if(!apiConnected||!active){feedback.textContent="Connect the local API and choose a workspace first.";return;}
 if(!routerData?.providers?.length){feedback.textContent="No model is configured. Check Model health first.";return;}
 const eligibleCloud=(routerData.providers||[]).filter(p=>p.runtime==="cloud");
 const approvalKey=active+"|agent-planner|"+eligibleCloud.map(p=>p.provider+"|"+p.model).sort().join(";");
 if(eligibleCloud.length&&cloudSessionApproval!==approvalKey){
  if(!await inAppConfirm({title:"External AI planning",message:"Your goal and workspace name may be sent to these configured cloud models for planning: "+eligibleCloud.map(p=>p.provider+" · "+p.model).join(", ")+". This includes every provider in your configured model order. Do not include passwords, identity documents, financial records or confidential material. Allow cloud planning for this workspace session?",confirmLabel:"Allow cloud planning"}))return;
  cloudSessionApproval=approvalKey;
 }
 setBusy(button,true,"Planning…");feedback.textContent="Sovereign is structuring the goal into a workspace-specific plan. Nothing is being executed.";
 try{
  const allowCloudProcessing=eligibleCloud.length>0&&cloudSessionApproval===approvalKey;
  const result=await api("/api/v1/workspaces/"+encodeURIComponent(active)+"/agent/plan",{method:"POST",body:JSON.stringify({goal,allow_cloud_processing:allowCloudProcessing}),timeoutMs:90000});
  field.value="";feedback.textContent="Plan saved. "+result.steps.length+" steps created. No tools or external actions were executed.";
  await loadAgentTasks();await approvals();
  notice("Agent plan saved to "+(workspaces.find(w=>w.id===active)?.name||"the active workspace")+".");
 }catch(error){feedback.textContent="Plan creation failed: "+error.message;notice("Agent planner failed: "+error.message,"error");}
 finally{setBusy(button,false);}
}
function cleanAssistantText(value){return String(value??"").replace(/\r\n?/g,"\n").replace(/\u00a0/g," ").replace(/^[\x60]{3}[\w-]*\s*$/gm,"").replace(/!\[([^\]]*)\]\([^)]+\)/g,"$1").replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g,"$1 ($2)").replace(/[\x60]([^\x60]+)[\x60]/g,"$1").replace(/^\s{0,3}#{1,6}\s+/gm,"").replace(/^\s*>\s?/gm,"").replace(/^\s*[-*+]\s+/gm,"• ").replace(/^\s*[-*_]{3,}\s*$/gm,"").replace(/\*\*(.*?)\*\*/g,"$1").replace(/__(.*?)__/g,"$1").replace(/(?<!\w)\*(.*?)\*(?!\w)/g,"$1").replace(/(?<!\w)_(.*?)_(?!\w)/g,"$1").replace(/[ \t]+\n/g,"\n").replace(/\n{3,}/g,"\n\n").trim();}
function speechText(value){let text=cleanAssistantText(value);const urls=[];text=text.replace(/\b(?:https?:\/\/|www\.)[^\s<>]+/gi,match=>{const url=match.replace(/[),.;!?]+$/,"");urls.push(url);return " URLTOKEN"+(urls.length-1)+" ";});text=text.replace(/[#*_~>|{}\[\]]/g," ").replace(/[•▪◦]/g," ").replace(/\s*[-–—]{2,}\s*/g,". ").replace(/\s+/g," ").trim();return text.replace(/URLTOKEN(\d+)/g,(_,index)=>urls[Number(index)]||"");}
function addBubble(role,text,images=[]){const box=el("messages");const welcome=box.querySelector(".chat-welcome");if(welcome)welcome.remove();if(box.childNodes.length===1&&box.firstChild.nodeType===3&&/No conversations|Connect the API/.test(box.textContent))box.replaceChildren();const b=document.createElement("div");b.className="bubble "+(role==="user"?"user":"assistant");if(role==="assistant"){const source=String(text??"");const pattern=/!\[([^\]]*)\]\((https?:\/\/[^\s)]+)\)/g;let cursor=0,match;while((match=pattern.exec(source))){const before=source.slice(cursor,match.index);if(before.trim()){const p=document.createElement("p");p.textContent=cleanAssistantText(before);b.append(p);}const img=document.createElement("img");img.className="assistant-image";img.src=match[2];img.alt=match[1]||"Sovereign illustration";img.loading="lazy";img.referrerPolicy="no-referrer";b.append(img);cursor=pattern.lastIndex;}const tail=source.slice(cursor);if(tail.trim()){const p=document.createElement("p");p.textContent=cleanAssistantText(tail);b.append(p);}if(!b.childNodes.length)b.textContent=cleanAssistantText(source);}else{b.textContent=text;images.forEach(src=>{const img=document.createElement("img");img.className="assistant-image";img.src=src;img.alt="User attached image";img.loading="lazy";b.append(img);});}box.append(b);box.scrollTop=box.scrollHeight;return b;}

function createLiveSpeechQueue(session){
 const queue=[];let active=false,finished=false,stopped=false;
 const pump=()=>{
  if(stopped||active)return;
  const phrase=queue.shift();
  if(!phrase){if(finished){voiceBusy=false;if(callActive&&session===callSession)listenAgain(160);else setCallState("idle","STANDING BY","Your AI is ready when you are.","Response complete.");}return;}
  const voices=window.speechSynthesis?.getVoices?.()||[];
  const selected=voices.find(item=>item.name===prefs.voiceName)||voices.find(item=>item.lang===prefs.voiceLanguage)||voices.find(item=>item.lang?.startsWith((prefs.voiceLanguage||"en-GB").split("-")[0]));
  const utterance=new SpeechSynthesisUtterance(phrase);utterance.voice=selected||null;utterance.lang=prefs.voiceLanguage||"en-GB";utterance.rate=Math.min(1.2,Math.max(.75,Number(prefs.voiceRate)||1.14));
  utterance.onstart=()=>{if(session===callSession)setCallState("speaking","SPEAKING","Sovereign is responding.","Speaking while the rest of the answer is generated.");};
  utterance.onend=()=>{active=false;pump();};
  utterance.onerror=()=>{active=false;stopped=true;voiceBusy=false;appendTranscript("SYSTEM","Live browser speech stopped. The complete response remains visible.");if(callActive&&session===callSession)listenAgain(500);};
  active=true;try{window.speechSynthesis.speak(utterance);}catch(error){active=false;stopped=true;voiceBusy=false;appendTranscript("SYSTEM","Live speech could not start: "+error.message);}
 };
 return {enqueue(phrase){if(phrase&&!stopped){queue.push(phrase);pump();}},finish(){finished=true;pump();},stop(){stopped=true;queue.length=0;}};
}
function takeSpeechPhrases(buffer,flush=false){
 const phrases=[];let rest=buffer;
 while(rest){
  const match=rest.match(/^([\s\S]*?[.!?](?:["')\]]*)\s+)/);
  if(match){phrases.push(match[1].trim());rest=rest.slice(match[0].length);continue;}
  if(rest.length>180){const cut=rest.lastIndexOf(" ",180);if(cut>70){phrases.push(rest.slice(0,cut).trim());rest=rest.slice(cut+1);continue;}}
  break;
 }
 if(flush&&rest.trim())phrases.push(rest.trim());
 return {phrases,rest:flush?"":rest};
}
async function sendMessage(msg,{voice=false,voiceSession=callSession,images=[],onDelta=null}={}){
 if(!active)throw Error("Choose a workspace first.");
 setCoreState("thinking");const requestStarted=performance.now();
 if(images.length){
  const d=await api("/api/v1/workspaces/"+encodeURIComponent(active)+"/chat",{method:"POST",body:JSON.stringify({message:msg,conversation_id:conversation,voice_mode:voice,images})});
  conversation=d.conversation_id;el("model-badge").textContent=d.provider+" · "+d.model+" · "+(d.runtime==="local"?"LOCAL":"CLOUD");
  if(voice){appendTranscript("SOVEREIGN",d.reply);if(callActive&&voiceSession===callSession)speak(d.reply,voiceSession);else setCoreState("idle");}else setCoreState("idle");return d.reply;
 }
 let response;
 try{response=await fetch(base.replace(/\/$/,"")+"/api/v1/workspaces/"+encodeURIComponent(active)+"/chat/stream",{
  method:"POST",headers:{"Content-Type":"application/json","Authorization":"Bearer "+token,"Accept":"text/event-stream"},
  body:JSON.stringify({message:msg,conversation_id:conversation,voice_mode:voice,images:[]}),signal:AbortSignal.timeout(120000)
 });}catch(error){setCoreState("error");throw error;}
 if(!response.ok){let detail="HTTP "+response.status;try{const data=await response.json();detail=data.detail||detail;}catch(error){}setCoreState("error");throw new Error(detail);}
 if(!response.body){setCoreState("error");throw new Error("This browser does not support streamed responses.");}
 const startedAt=performance.now();let fullReply="",buffer="",done=null,transcriptBody=null,speech=null,speechBuffer="";
 if(voice){
  stopMicrophone(true);stopVoicePlayback();voiceBusy=true;
  const transcript=appendTranscript("SOVEREIGN","");transcriptBody=transcript.querySelector("p");
  if(window.speechSynthesis&&typeof SpeechSynthesisUtterance!=="undefined")speech=createLiveSpeechQueue(voiceSession);
  setCallState("thinking","THINKING","Sovereign is listening and responding.","Answer text and speech will arrive incrementally.");
 }
 const handleFrame=frame=>{
  if(!frame.trim())return;let eventName="",dataLines=[];
  for(const line of frame.split(/\r?\n/)){if(line.startsWith("event:"))eventName=line.slice(6).trim();else if(line.startsWith("data:"))dataLines.push(line.slice(5).trimStart());}
  if(!dataLines.length)return;let data;try{data=JSON.parse(dataLines.join("\n"));}catch(error){throw new Error("The streaming API sent an invalid event.");}
  if(eventName==="start")conversation=data.conversation_id;
  else if(eventName==="delta"){
   const delta=String(data.text||"");if(!delta)return;fullReply+=delta;if(onDelta)onDelta(delta,fullReply);
   if(transcriptBody){transcriptBody.textContent=fullReply;el("call-transcript").scrollTop=el("call-transcript").scrollHeight;}
   if(speech){speechBuffer+=delta;const split=takeSpeechPhrases(speechBuffer);speechBuffer=split.rest;split.phrases.map(speechText).filter(Boolean).forEach(phrase=>speech.enqueue(phrase));}
  }else if(eventName==="done")done=data;else if(eventName==="error")throw new Error(data.message||"The streamed response failed.");
 };
 try{
  const reader=response.body.getReader(),decoder=new TextDecoder();
  while(true){const {value,done:ended}=await reader.read();if(ended)break;buffer+=decoder.decode(value,{stream:true});const frames=buffer.split(/\r?\n\r?\n/);buffer=frames.pop()||"";for(const frame of frames)handleFrame(frame);}
  buffer+=decoder.decode();if(buffer.trim())handleFrame(buffer);
  if(!done)throw new Error("The stream ended before the completion event.");if(!fullReply.trim())throw new Error("The model returned no text.");
  const requestMs=Math.round(performance.now()-requestStarted),modelMs=Number(done.model_latency_ms)||Math.round(performance.now()-startedAt);
  el("model-badge").textContent=done.provider+" · "+done.model+" · "+(done.runtime==="local"?"LOCAL":"CLOUD");
  el("model-badge").title="First streamed text: "+Math.round(startedAt-requestStarted)+" ms · Model completion: "+modelMs+" ms · Total: "+requestMs+" ms";
  if(done.fallback_used)notice("Model availability notice: Sovereign used your configured fallback order and selected "+done.provider+" · "+done.model+".","warning");
  if(voice){if(speech){const finalSplit=takeSpeechPhrases(speechBuffer,true);finalSplit.phrases.map(speechText).filter(Boolean).forEach(phrase=>speech.enqueue(phrase));speech.finish();}else if(callActive&&voiceSession===callSession)speak(fullReply,voiceSession);else voiceBusy=false;}else setCoreState("idle");
  return fullReply;
 }catch(error){if(speech)speech.stop();if(voice){voiceBusy=false;if(callActive&&voiceSession===callSession)listenAgain(500);}setCoreState("error");throw error;}
}
el("workspace").onchange=async e=>{active=e.target.value;conversation=null;renderWorkspaces();updateCallSupport();try{await Promise.all([conversations(),approvals(),loadAgentTasks()]);}catch(err){notice(err.message,"error");}};
el("connect").onclick=()=>{if(base&&token)connect();else view("settings");};
el("save").onclick=autoConnectLocal;
function populateVoiceOptions(){const select=el("voice-choice");if(!select||!window.speechSynthesis)return;const current=prefs.voiceName;select.replaceChildren();const automatic=document.createElement("option");automatic.value="";automatic.textContent="Automatic · recommended";select.append(automatic);window.speechSynthesis.getVoices().forEach(v=>{const o=document.createElement("option");o.value=v.name;o.textContent=v.name+" · "+v.lang;select.append(o);});select.value=current;}
function bindPreferences(){const sound=el("notification-sound"),duration=el("notification-duration"),rate=el("voice-rate"),language=el("voice-language"),voice=el("voice-choice");if(sound){sound.checked=!!prefs.sound;sound.onchange=()=>{prefs.sound=sound.checked;savePrefs();if(prefs.sound)notificationSound();};}if(duration){duration.value=String(prefs.noticeSeconds);duration.onchange=()=>{prefs.noticeSeconds=Number(duration.value)||10;savePrefs();};}if(rate){rate.value=String(prefs.voiceRate);el("voice-rate-value").textContent=Number(prefs.voiceRate).toFixed(2)+"×";rate.oninput=()=>{prefs.voiceRate=Number(rate.value);el("voice-rate-value").textContent=prefs.voiceRate.toFixed(2)+"×";savePrefs();};}if(language){language.value=prefs.voiceLanguage;language.onchange=()=>{prefs.voiceLanguage=language.value;savePrefs();populateVoiceOptions();};}if(voice){populateVoiceOptions();voice.onchange=()=>{prefs.voiceName=voice.value;savePrefs();};}}
bindPreferences();if(window.speechSynthesis)window.speechSynthesis.onvoiceschanged=populateVoiceOptions;
document.querySelectorAll("[data-prompt]").forEach(b=>b.onclick=()=>{const field=el("message");field.value=b.dataset.prompt;field.dispatchEvent(new Event("input",{bubbles:true}));field.focus();});
el("model-activate").onclick=activateModel;el("agent-plan-button").onclick=createAgentPlan;
el("run-group-chat").onclick=runSpecialistGroup;
el("refresh-models").onclick=async()=>{setBusy(el("refresh-models"),true,"Refreshing…");try{await models();notice("Model health refreshed.");}finally{setBusy(el("refresh-models"),false);}};
el("chatform").onsubmit=async e=>{e.preventDefault();const msg=el("message").value.trim()||(pendingImages.length?"Please examine the attached image(s).":"");if(!msg)return;const images=pendingImages.slice();const selected=routerData?.providers?.find(p=>p.active);const cloudProviders=(routerData?.providers||[]).filter(p=>p.runtime==="cloud");const providerName=selected?(selected.provider+" · "+selected.model):"the selected AI provider";const cloudNames=cloudProviders.map(p=>p.provider+" · "+p.model).join(", ");const approvalKey=active+"|"+cloudProviders.map(p=>p.provider+"|"+p.model).sort().join(";");if(cloudProviders.length&&cloudSessionApproval!==approvalKey){if(!await inAppConfirm({title:"External AI processing",message:"This workspace request may be sent outside this computer to a configured cloud AI model: "+cloudNames+". The router may use a different model from the one manually selected, including for image understanding or availability fallback. Text and attached images may be processed under the provider’s terms. Do not send passwords, identity documents, financial records or confidential material. Allow cloud processing for this workspace session?",confirmLabel:"Allow cloud processing"}))return;cloudSessionApproval=approvalKey;}if(images.length&&!await inAppConfirm({title:"Image privacy check",message:"The attached images will be sent to the vision-capable model selected by SovereignOS. It may differ from your manually selected chat model. Continue only if you are authorised to share these images and they contain no sensitive information.",confirmLabel:"Share images"}))return;addBubble("user",msg,pendingImages);el("message").value="";fitMessageComposer();const button=el("send-button");setBusy(button,true,"Thinking…");setCoreState("thinking");const pending=addBubble("assistant","Sovereign is thinking…");pending.classList.add("thinking-bubble");try{const reply=await sendMessage(msg,{images,onDelta:(_delta,full)=>{pending.textContent=full;pending.classList.remove("thinking-bubble");}});pending.remove();addBubble("assistant",reply);pendingImages=[];renderAttachmentPreview();}catch(err){pending.textContent="I couldn't complete that request: "+err.message;pending.classList.remove("thinking-bubble");notice(err.message,"error");setCoreState("error");}finally{setBusy(button,false);}};
function renderAttachmentPreview(){const box=el("attachment-preview");if(!box)return;box.replaceChildren();box.classList.toggle("hidden",!pendingImages.length);pendingImages.forEach((src,index)=>{const item=document.createElement("div");item.className="attachment-thumb";const img=document.createElement("img");img.src=src;img.alt="Image attachment "+(index+1);const remove=document.createElement("button");remove.type="button";remove.textContent="×";remove.setAttribute("aria-label","Remove image");remove.onclick=()=>{pendingImages.splice(index,1);renderAttachmentPreview();};item.append(img,remove);box.append(item);});}
el("attach-image").onclick=()=>el("image-input").click();
el("image-input").onchange=async event=>{const files=Array.from(event.target.files||[]).slice(0,4-pendingImages.length);for(const file of files){if(!["image/png","image/jpeg","image/webp"].includes(file.type)||file.size>4*1024*1024){notice("Choose PNG, JPEG or WebP images up to 4 MB each.","warning");continue;}const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result));reader.onerror=reject;reader.readAsDataURL(file);});pendingImages.push(data);}event.target.value="";renderAttachmentPreview();};
el("message").addEventListener("keydown",event=>{if(event.key==="Enter"&&!event.shiftKey&&!event.isComposing){event.preventDefault();el("chatform").requestSubmit();}});
el("message").addEventListener("input",fitMessageComposer);
function updateCallSupport(){
 const supported=!!(navigator.mediaDevices?.getUserMedia&&window.MediaRecorder&&(window.AudioContext||window.webkitAudioContext));
 const ready=apiConnected&&!!active;
 const modelReady=!!routerData?.providers?.length;
 const voiceReady=!!voiceCapabilities?.available&& (!!window.speechSynthesis||voiceCapabilities?.speech_output_available===true);
 el("call-start").disabled=!supported||!ready||!modelReady||!voiceReady||callActive;
 if(!supported)el("call-description").textContent="Microphone capture is unavailable in this browser. Open SovereignOS in Microsoft Edge and allow microphone access for localhost.";
 else if(!apiConnected)el("call-description").textContent="Connect the SovereignOS API in Settings first.";
 else if(!active)el("call-description").textContent="Choose a workspace before starting a voice session.";
 else if(!modelReady)el("call-description").textContent="No AI model is configured. Check Model health.";
 else if(!voiceCapabilities)el("call-description").textContent="Checking microphone and transcription services…";
 else if(!voiceReady)el("call-description").textContent=(voiceCapabilities.reason||"Voice services are unavailable.")+" You can still message Sovereign using the text box below.";
 else el("call-description").textContent="Voice link ready · "+(workspaces.find(w=>w.id===active)?.name||"your active workspace")+". Speak naturally at normal volume.";
}function appendTranscript(who,text){const empty=el("call-transcript").querySelector(".transcript-empty");if(empty)empty.remove();const item=document.createElement("div");item.className="transcript-line";const label=document.createElement("b");label.textContent=who;const body=document.createElement("p");body.textContent=text;item.append(label,body);el("call-transcript").append(item);el("call-transcript").scrollTop=el("call-transcript").scrollHeight;return item;}
function setCallState(state,label,title,description){el("call-shell").dataset.state=state;el("call-status").textContent=label;el("call-status").className="status-tag "+(["listening","thinking","speaking"].includes(state)?"status-live":state==="error"?"status-warn":"status-idle");el("voice-orb").classList.toggle("voice-active",["listening","thinking","speaking"].includes(state));const visual=el("voice-state-indicator");if(visual){visual.dataset.state=state;el("voice-state-label").textContent=label;el("voice-state-detail").textContent=description;}setCoreState(state);el("call-title").textContent=title;el("call-description").textContent=description;}
function clearListenTimer(){if(listenTimer!==null){window.clearTimeout(listenTimer);listenTimer=null;}}
async function speak(text,session=callSession,preferLocal=true,allowOutsideCall=false){
    stopMicrophone(true);
    stopVoicePlayback();
    voiceBusy=true;
    setCallState("preparing","PREPARING AUDIO","Your response is ready.","Preparing speech output.");

    const spoken=speechText(text);
    if(!spoken){
        voiceBusy=false;
        if(callActive)listenAgain(250);
        return;
    }

    lastSpokenText=spoken;
    const replayButton=el("call-replay");
    if(replayButton)replayButton.disabled=false;

    if(preferLocal&&window.speechSynthesis&&typeof SpeechSynthesisUtterance!=="undefined"){
        const voices=window.speechSynthesis.getVoices();
        const selected=voices.find(item=>item.name===prefs.voiceName)||
            voices.find(item=>item.lang===prefs.voiceLanguage)||
            voices.find(item=>item.lang?.startsWith((prefs.voiceLanguage||"en-GB").split("-")[0]));
        const utterance=new SpeechSynthesisUtterance(spoken);
        utterance.voice=selected||null;
        utterance.lang=prefs.voiceLanguage||"en-GB";
        utterance.rate=Math.min(1.2,Math.max(.75,Number(prefs.voiceRate)||1.14));
        activeUtterance=utterance;
        utterance.onstart=()=>{
            if(session===callSession&&activeUtterance===utterance)
                setCallState("speaking","SPEAKING","Your answer is here.","Sovereign is speaking using your browser voice.");
        };
        utterance.onend=()=>{
            if(session!==callSession||activeUtterance!==utterance)return;
            activeUtterance=null;voiceBusy=false;
            if(callActive)listenAgain(160);
            else setCallState("idle","STANDING BY","Your AI is ready when you are.","Replay complete.");
        };
        utterance.onerror=()=>{
            if(session!==callSession||activeUtterance!==utterance)return;
            activeUtterance=null;voiceBusy=false;
            if(voiceCapabilities?.speech_output_available){
                appendTranscript("SYSTEM","Browser speech output failed. Switching to SovereignOS Edge TTS.");
                speak(text,session,false,allowOutsideCall);
            }else{
                appendTranscript("SYSTEM","Browser speech output failed. The response remains available as text.");
                if(callActive)listenAgain(500);else setCoreState("error");
            }
        };
        try{
            window.speechSynthesis.speak(utterance);
            return;
        }catch(error){
            activeUtterance=null;
            voiceBusy=false;
            if(voiceCapabilities?.speech_output_available){
                appendTranscript("SYSTEM","Browser speech output could not start. Switching to SovereignOS Edge TTS.");
                return speak(text,session,false,allowOutsideCall);
            }
            appendTranscript("SYSTEM","Browser speech output could not start. The response remains available as text.");
            if(callActive)listenAgain(500);
            return;
        }
    }

    const controller=new AbortController();
    voiceRequestController=controller;

    try{
        const response=await fetch(
            base.replace(/\/$/,"")+"/api/v1/voice/synthesize",
            {
                method:"POST",
                headers:{
                    "Content-Type":"application/json",
                    "Authorization":"Bearer "+token
                },
                body:JSON.stringify({
                    text:spoken,
                    rate:Math.min(1.2,Math.max(.75,Number(prefs.voiceRate)||1.14))
                }),
                signal:AbortSignal.any([controller.signal,AbortSignal.timeout(30000)])
            }
        );

        if(!response.ok){
            let detail="HTTP "+response.status;
            try{
                const data=await response.json();
                if(data.detail)detail=data.detail;
            }catch(error){}
            throw new Error(detail);
        }

        const blob=await response.blob();
        if(!blob.size)throw new Error("The speech service returned empty audio.");
        if(session!==callSession||(!callActive&&!allowOutsideCall))return;

        const url=URL.createObjectURL(blob);
        activeAudioUrl=url;
        const audio=new Audio(url);
        activeAudio=audio;

        audio.onplay=()=>{
            if(session===callSession){
                setCallState("speaking","SPEAKING","Your answer is here.","Sovereign is speaking. Use Interrupt audio at any time.");
            }
        };

        audio.onended=()=>{
            if(session!==callSession)return;
            stopVoicePlayback();
            voiceBusy=false;
            if(callActive)listenAgain(160);
            else setCallState("idle","STANDING BY","Your AI is ready when you are.","Replay complete.");
        };

        audio.onerror=()=>{
            if(session!==callSession)return;
            stopVoicePlayback();
            voiceBusy=false;
            appendTranscript("SYSTEM","Voice playback failed. The response remains available as text.");
            if(callActive)listenAgain(500);
            else setCoreState("error");
        };

        await audio.play();
    }catch(error){
        if(session!==callSession)return;
        if(error.name==="AbortError")return;
        stopVoicePlayback();
        voiceBusy=false;
        appendTranscript("SYSTEM","Voice output issue: "+(error.message||"unknown error"));
        if(callActive)listenAgain(500);
        else setCoreState("error");
    }
}

function stopMicrophone(discard=true){
 stopStreamingRecognition();
 micDiscard=discard;
 if(micFrame!==null){cancelAnimationFrame(micFrame);micFrame=null;}
 if(micRecorder&&micRecorder.state!=="inactive"){
  try{micRecorder.stop();}catch(error){}
 }
 micRecorder=null;
 if(micStream){
  micStream.getTracks().forEach(track=>{try{track.stop();}catch(error){}});
  micStream=null;
 }
 if(micAudioContext){
  const context=micAudioContext;
  micAudioContext=null;
  try{context.close();}catch(error){}
 }
 micAnalyser=null;
 micChunks=[];
 micFinalizing=false;
 micAboveCount=0;
}

async function transcribeVoiceBlob(blob,session){
 if(!blob||!blob.size){
  voiceBusy=false;
  appendTranscript("SYSTEM","No audio was captured. Please try speaking again.");
  if(callActive&&session===callSession)listenAgain(500);
  return;
 }
 const controller=new AbortController();
 voiceTranscriptionController=controller;
 const started=performance.now();
 try{
  const mediaType=(blob.type||"audio/webm").split(";")[0];
  const response=await fetch(base.replace(/\/$/,"")+"/api/v1/voice/transcribe",{
   method:"POST",
   headers:{
    "Content-Type":mediaType,
    "Authorization":"Bearer "+token,
    "X-Sovereign-Language":prefs.voiceLanguage||"en-GB",
    "X-Sovereign-Cloud-Consent":voiceCloudConsentSession===session?"explicit-session":""
   },
   body:blob,
   signal:AbortSignal.any([controller.signal,AbortSignal.timeout(25000)])
  });
  const data=await response.json().catch(()=>({detail:"Invalid transcription response"}));
  if(!response.ok)throw new Error(data.detail||("HTTP "+response.status));
  if(session!==callSession||!callActive)return;
  const transcript=(data.transcript||"").trim();
  if(!transcript)throw new Error("No speech was detected. Please try again.");
  const elapsed=Math.round(performance.now()-started);
  appendTranscript("YOU",transcript);
  setCallState("thinking","THINKING","Thinking…","Transcription "+(data.processing_ms||elapsed)+" ms · sending to "+(workspaces.find(w=>w.id===active)?.name||"your workspace")+".");
  const waitStarted=performance.now();
  clearInterval(voiceWaitTimer);
  voiceWaitTimer=window.setInterval(()=>{
   if(session!==callSession)return;
   const seconds=Math.floor((performance.now()-waitStarted)/1000);
   setCallState("thinking","THINKING","Thinking…","Your message is with "+(routerData?.providers?.find(p=>p.active)?.provider||"your selected model")+" · "+seconds+"s");
  },500);
  await sendMessage(transcript,{voice:true,voiceSession:session});
 }catch(error){
  if(session!==callSession||error.name==="AbortError")return;
  voiceBusy=false;
  appendTranscript("SYSTEM","Voice input issue: "+(error.message||"transcription failed"));
  const permanent=/FFmpeg|audio conversion engine is unavailable|SpeechRecognition package|Speech recognition service is unavailable|timed out|timeout|HTTP 5\d\d|Internal Server Error|NetworkError|Failed to fetch|network|could not be decoded|Unsupported audio format|exceeds the 8 MB/i.test(error.message||"");
  if(permanent){
   callActive=false;
   stopMicrophone(true);
   setCallState("error","VOICE SERVICE ISSUE","Voice input needs attention.",(error.message||"The voice service failed.")+" You can continue by typing below.");
   el("call-interrupt").disabled=true;
   el("call-end").disabled=true;
   el("call-start").disabled=!(navigator.mediaDevices?.getUserMedia&&window.MediaRecorder&&(window.AudioContext||window.webkitAudioContext)&&apiConnected&&!!active&&!!routerData?.providers?.length&&voiceCapabilities?.available&&(window.speechSynthesis||voiceCapabilities?.speech_output_available));
  }else{
   setCallState("error","VOICE INPUT ISSUE","I couldn't hear that clearly.",error.message||"Please try again.");
   if(callActive)listenAgain(900);
  }
 }finally{
  clearInterval(voiceWaitTimer);
  voiceWaitTimer=null;
  if(voiceTranscriptionController===controller)voiceTranscriptionController=null;
 }
}

async function startBatchRecognition(session){
 if(!callActive||session!==callSession||voiceBusy||micStream)return;
 if(!navigator.mediaDevices?.getUserMedia||!window.MediaRecorder){
  setCallState("error","MICROPHONE UNAVAILABLE","I can't open the microphone.","Use Microsoft Edge and allow microphone access for localhost.");
  return;
 }
 try{
  micDiscard=true;
  let stream=await navigator.mediaDevices.getUserMedia({
   audio:{
    channelCount:1,
    echoCancellation:true,
    noiseSuppression:true,
    autoGainControl:true
   }
  });

  // Prefer a real microphone over Stereo Mix when Edge exposes device labels.
  try{
   const devices=await navigator.mediaDevices.enumerateDevices();
   const inputs=devices.filter(d=>d.kind==="audioinput"&&d.deviceId&&
    !/stereo mix|loopback/i.test(d.label));
   const preferred=inputs.find(d=>/realtek/i.test(d.label)&&/microphone|mic/i.test(d.label))||
    inputs.find(d=>/microphone|mic in/i.test(d.label));
   const currentId=stream.getAudioTracks()[0]?.getSettings?.().deviceId;
   if(preferred&&preferred.deviceId!==currentId){
    try{
     const selected=await navigator.mediaDevices.getUserMedia({
      audio:{
       deviceId:{exact:preferred.deviceId},
       channelCount:1,
       echoCancellation:true,
       noiseSuppression:true,
       autoGainControl:true
      }
     });
     stream.getTracks().forEach(track=>track.stop());
     stream=selected;
    }catch(selectionError){
     console.warn("Preferred microphone unavailable; retaining browser-selected input.",selectionError);
    }
   }
  }catch(deviceError){
   console.warn("Microphone enumeration unavailable; retaining browser-selected input.",deviceError);
  }

  if(!callActive||session!==callSession){
   stream.getTracks().forEach(track=>track.stop());
   return;
  }

  micStream=stream;
  micDiscard=false;
  const track=stream.getAudioTracks()[0];
  const trackSettings=track?.getSettings?.()||{};
  const micLabel=track?.label||"Browser-selected microphone";

  const AudioContextClass=window.AudioContext||window.webkitAudioContext;
  micAudioContext=new AudioContextClass();
  await micAudioContext.resume();
  if(!callActive||session!==callSession)return;

  const source=micAudioContext.createMediaStreamSource(stream);
  micAnalyser=micAudioContext.createAnalyser();
  micAnalyser.fftSize=1024;
  source.connect(micAnalyser);

  micNoiseFloor=0.0015;
  micCalibrationStartedAt=performance.now();
  micSpeechStartedAt=0;
  micLastVoiceAt=0;
  micAboveCount=0;
  micFinalizing=false;
  micChunks=[];
  micRecorder=null;

  let lastMeterUpdate=0;

  const mimeType=MediaRecorder.isTypeSupported("audio/webm;codecs=opus")?"audio/webm;codecs=opus":
   (MediaRecorder.isTypeSupported("audio/webm")?"audio/webm":"");

  micRecorder=mimeType?new MediaRecorder(stream,{mimeType}):new MediaRecorder(stream);

  micRecorder.ondataavailable=event=>{
   // Preserve every timeslice in order; dropping WebM chunks can corrupt the recording.
   if(event.data?.size&&!micDiscard)micChunks.push(event.data);
  };

  micRecorder.onerror=()=>{
   if(session!==callSession)return;
   voiceBusy=false;
   stopMicrophone(true);
   appendTranscript("SYSTEM","Microphone recording encountered an error.");
   if(callActive)listenAgain(700);
  };

  micRecorder.onstop=()=>{
   const shouldDiscard=micDiscard;
   const chunks=micChunks.slice();
   const type=micRecorder?.mimeType||chunks.find(c=>c.type)?.type||"audio/webm";
   micRecorder=null;
   if(shouldDiscard||session!==callSession||!callActive)return;
   const blob=new Blob(chunks,{type});
   stopMicrophone(true);
   voiceBusy=true;
   setCallState("thinking","TRANSCRIBING","One moment","Converting your speech into text.");
   transcribeVoiceBlob(blob,session);
  };

  micRecorder.start(200);

  const gainText=trackSettings.autoGainControl===false?
   "Automatic microphone gain unavailable":"Automatic microphone gain enabled";
  setCallState("listening","LISTENING","I'm listening",gainText+" · "+micLabel);

  const samples=new Float32Array(micAnalyser.fftSize);

  const finishUtterance=()=>{
   if(micFinalizing||!micRecorder||micRecorder.state==="inactive")return;
   micFinalizing=true;
   try{micRecorder.stop();}
   catch(error){
    micFinalizing=false;
    voiceBusy=false;
    stopMicrophone(true);
    if(callActive&&session===callSession)listenAgain(500);
   }
  };

  const inspectLevel=()=>{
   if(!callActive||session!==callSession||!micAnalyser||micFinalizing)return;

   micAnalyser.getFloatTimeDomainData(samples);
   let sum=0;
   for(let i=0;i<samples.length;i++)sum+=samples[i]*samples[i];
   const rms=Math.sqrt(sum/samples.length);
   const now=performance.now();
   const calibrating=now-micCalibrationStartedAt<500;
   const threshold=Math.max(0.0018,micNoiseFloor*2.0);

   // Show measured input level so a silent browser input is immediately visible.
   if(now-lastMeterUpdate>=250){
    const detail=el("voice-state-detail");
    if(detail)detail.textContent="Microphone active · "+micLabel;
    lastMeterUpdate=now;
   }

   if(!micSpeechStartedAt){
    if(!calibrating){
     if(rms>threshold)micAboveCount++;
     else{
      micAboveCount=0;
      micNoiseFloor=Math.min(0.01,micNoiseFloor*0.96+rms*0.04);
     }
     if(micAboveCount>=3){
      micSpeechStartedAt=now;
      micLastVoiceAt=now;

     }
    }
   }else{
    if(rms>threshold)micLastVoiceAt=now;
    if(now-micLastVoiceAt>=850||now-micSpeechStartedAt>=18000)finishUtterance();
   }

   if(!micFinalizing)micFrame=requestAnimationFrame(inspectLevel);
  };

  micFrame=requestAnimationFrame(inspectLevel);
 }catch(error){
  stopMicrophone(true);
  if(session!==callSession)return;
  voiceBusy=false;
  callActive=false;
  const denied=error.name==="NotAllowedError"||error.name==="SecurityError";
  setCallState("error",denied?"MICROPHONE BLOCKED":"MICROPHONE ISSUE","I couldn't open your microphone.",denied?"Allow microphone access for localhost in Edge site permissions, then start again.":(error.message||"Check your microphone and try again."));
  el("call-interrupt").disabled=true;
  el("call-end").disabled=true;
  el("call-start").disabled=!(apiConnected&&!!active&&!!routerData?.providers?.length&&voiceCapabilities?.available&&(window.speechSynthesis||voiceCapabilities?.speech_output_available));
  notice("Could not open microphone: "+(error.message||error.name),"error");
 }
}

function startRecognition(session){
 if(!callActive||session!==callSession||voiceBusy||micStream)return;

 // Use the authenticated SovereignOS transcription pipeline directly.
 // Avoid browser-native speech recognition, which can hang without results.
 stopStreamingRecognition();
 liveFinalizing=false;
 startBatchRecognition(session);
}
function listenAgain(delay=350){
 clearListenTimer();
 if(!callActive||voiceBusy)return;
 const session=callSession;
 listenTimer=window.setTimeout(()=>{
  listenTimer=null;
  if(callActive&&!voiceBusy&&session===callSession){liveFinalizing=false;startRecognition(session);}
 },Math.max(0,delay));
}

async function startCall(){
 if(!voiceCapabilities?.available||(!window.speechSynthesis&&!voiceCapabilities?.speech_output_available)){
  notice(voiceCapabilities?.reason||"Voice services are still being checked. Reconnect the local API and try again.","warning");
  return;
 }
 if(!routerData?.providers?.length){
  notice("No AI model is configured. Check your configured model providers before starting a call.","warning");
  return;
 }
 if(!navigator.mediaDevices?.getUserMedia||!window.MediaRecorder){
  notice("Live microphone capture is unavailable in this browser. Open SovereignOS in Microsoft Edge.","error");
  return;
 }
 if(!apiConnected||!active){
  notice("Connect the API and select a workspace before starting voice mode.","warning");
  updateCallSupport();
  return;
 }
 if(callActive)return;
 const approved=await inAppConfirm({
  title:"Allow cloud speech recognition for this call?",
  message:"SovereignOS will send the microphone audio you speak during this call to Google's speech-recognition service to convert it to text. SovereignOS does not save the raw recording. If you decline, the microphone will not start. Cloud recognition is not a local/offline service.",
  confirmLabel:"Allow for this call"
 });
 if(!approved)return;
 callSession++;
 voiceCloudConsentSession=callSession;
 clearListenTimer();
 stopMicrophone(true);
 stopVoicePlayback();
 voiceBusy=false;
 callActive=true;
 el("call-start").disabled=true;
 el("call-interrupt").disabled=false;
 el("call-end").disabled=false;
 setCallState("connecting","CONNECTING","Opening your voice link…","Requesting microphone access. Allow it if prompted.");
 startRecognition(callSession);
}

function interruptCall(){
 callSession++;
 clearListenTimer();
 clearInterval(voiceWaitTimer);
 voiceWaitTimer=null;
 if(voiceTranscriptionController){
  voiceTranscriptionController.abort();
  voiceTranscriptionController=null;
 }
 stopMicrophone(true);
 stopVoicePlayback();
 voiceBusy=false;
 appendTranscript("SYSTEM","Audio interrupted. Playback and any active transcription were stopped.");
 setCallState("idle","INTERRUPTED","Your turn.","Reopening the microphone?");
 if(callActive)listenAgain(650);
}

function endCall(){
 callActive=false;
 callSession++;
 clearListenTimer();
 clearInterval(voiceWaitTimer);
 voiceWaitTimer=null;
 if(voiceTranscriptionController){
  voiceTranscriptionController.abort();
  voiceTranscriptionController=null;
 }
 voiceBusy=false;
 stopMicrophone(true);
 stopVoicePlayback();
 el("call-start").disabled=!(apiConnected&&!!active&&!!routerData?.providers?.length&&voiceCapabilities?.available&&(window.speechSynthesis||voiceCapabilities?.speech_output_available));
 el("call-interrupt").disabled=true;
 el("call-end").disabled=true;
 setCallState("idle","STANDING BY","Your AI is ready when you are.","Call ended. Your transcript remains visible until you leave this page.");
 updateCallSupport();
}
el("call-start").onclick=startCall;el("call-end").onclick=endCall;el("call-interrupt").onclick=interruptCall;
el("call-replay").onclick=()=>{if(lastSpokenText)speak(lastSpokenText,callSession,true,true);};
el("run-libreoffice-test").onclick=async()=>{const button=el("run-libreoffice-test"),status=el("libreoffice-test-status");if(!await inAppConfirm({title:"Create LibreOffice test",message:"SovereignOS will create one new test document in its own agent workspace and ask LibreOffice to open it. It will not inspect or change your existing files.",confirmLabel:"Create and open"}))return;setBusy(button,true,"Creating document…");status.textContent="Creating a local test document and locating LibreOffice…";try{const result=await api("/api/v1/desktop/libreoffice/test",{method:"POST",body:JSON.stringify({})});status.textContent="Opened "+result.file_name+" in LibreOffice. Saved only in the SovereignOS agent workspace.";notice("LibreOffice integration test launched successfully.");}catch(error){status.textContent=error.message;notice("LibreOffice test could not complete: "+error.message,"error");}finally{setBusy(button,false);}};
el("call-text-form").addEventListener("submit",async event=>{event.preventDefault();const field=el("call-text"),send=el("call-send"),message=field.value.trim();if(!message)return;if(!apiConnected||!active){notice("Connect the API and choose a workspace before messaging Sovereign.","warning");return;}if(!routerData?.providers?.length){notice("No AI model is configured. Add a provider credential to the project's local .env file and restart the API.","warning");return;}field.value="";field.style.height="48px";appendTranscript("YOU",message);send.disabled=true;try{const reply=await sendMessage(message,{voice:callActive});if(!callActive)appendTranscript("SOVEREIGN",reply);}catch(error){appendTranscript("SYSTEM",error.message||"Message could not be sent.");notice(error.message||"Message could not be sent.","error");}finally{send.disabled=false;field.focus();}});
el("call-text").addEventListener("input",event=>{event.target.style.height="48px";event.target.style.height=Math.min(event.target.scrollHeight,132)+"px";});
el("call-attach").onclick=()=>{view("chat");window.setTimeout(()=>el("attach-image").click(),0);};
if("serviceWorker" in navigator)window.addEventListener("load",async()=>{try{if(import.meta.env.DEV){const registrations=await navigator.serviceWorker.getRegistrations();await Promise.all(registrations.map(r=>r.unregister()));if("caches" in window){const keys=await caches.keys();await Promise.all(keys.map(k=>caches.delete(k)));}if(navigator.serviceWorker.controller&&!sessionStorage.getItem("so-dev-worker-cleared")){sessionStorage.setItem("so-dev-worker-cleared","1");window.location.reload();}return;}await navigator.serviceWorker.register(import.meta.env.BASE_URL+"sw.js",{updateViaCache:"none"});}catch(error){console.warn("SovereignOS worker update skipped:",error);}});
view("overview");updateCallSupport();autoConnectLocal();
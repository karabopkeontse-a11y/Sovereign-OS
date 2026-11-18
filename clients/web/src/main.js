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
let base="",token="",workspaces=[],active="",conversation=null,routerData=null,callActive=false,voiceBusy=false,apiConnected=false,listenTimer=null,voiceWaitTimer=null,callSession=0,pendingImages=[],cloudSessionApproval="";
let micStream=null,micRecorder=null,micAudioContext=null,micAnalyser=null,micFrame=null,micChunks=[],micDiscard=false,micFinalizing=false,micSpeechStartedAt=0,micLastVoiceAt=0,micNoiseFloor=0.003,micCalibrationStartedAt=0,micAboveCount=0,voiceTranscriptionController=null;
const prefs={sound:true,noticeSeconds:10,voiceRate:1.14,voiceLanguage:"en-GB",voiceName:""};
try{Object.assign(prefs,JSON.parse(localStorage.getItem("sovereign-ui-preferences")||"{}"));}catch(e){}
if([0.96,1.08].includes(Number(prefs.voiceRate)))prefs.voiceRate=1.14;
function savePrefs(){localStorage.setItem("sovereign-ui-preferences",JSON.stringify(prefs));}
let audioContext=null;

let activeAudio=null,activeAudioUrl=null,voiceRequestController=null;

let liveRecognition=null,liveInterimText="",liveFinalizing=false;

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
const titles={overview:["Command centre","Your intelligence. Your devices. Your control."],chat:["Workspace intelligence","A private intelligence workspace."],call:["Call your AI","Talk naturally. Hear Sovereign respond."],models:["Model health","Know which intelligence is ready and why."],iot:["IoT operations","Observe connected systems safely."],approvals:["Approval centre","Your permission is required for consequential actions."],settings:["Settings","Configure your local intelligence connection."]};
function notice(msg,kind="info"){const stack=el("notification-stack");if(!stack)return;const card=document.createElement("article");card.className="toast toast-"+(kind==="error"?"error":kind==="warning"?"warning":"info");const icon=document.createElement("span");icon.className="toast-icon";icon.textContent=kind==="error"?"!":kind==="warning"?"â–³":"âœ¦";const copy=document.createElement("div");copy.className="toast-copy";const title=document.createElement("b");title.textContent=kind==="error"?"Needs attention":kind==="warning"?"System notice":"SovereignOS";const body=document.createElement("p");body.textContent=msg;copy.append(title,body);const close=document.createElement("button");close.className="toast-close";close.type="button";close.setAttribute("aria-label","Dismiss notification");close.textContent="Ã—";close.onclick=()=>card.remove();card.append(icon,copy,close);stack.append(card);notificationSound();const duration=Math.max(6,Number(prefs.noticeSeconds)||10)*1000;window.setTimeout(()=>{card.classList.add("toast-leaving");window.setTimeout(()=>card.remove(),350);},kind==="error"?Math.max(duration,16000):duration);while(stack.children.length>5)stack.firstElementChild.remove();}
function setBusy(button,busy,label){if(!button)return;button.disabled=busy;if(busy){button.dataset.label=button.textContent;button.textContent=label||"Workingâ€¦";}else if(button.dataset.label){button.textContent=button.dataset.label;delete button.dataset.label;}}
async function api(path,options={}){if(!base||!token)throw Error("The local SovereignOS session has not been established. Restart the desktop launcher and reconnect.");const r=await fetch(base.replace(/\/$/,"")+path,{...options,headers:{"Content-Type":"application/json","Authorization":"Bearer "+token,...options.headers}});const d=await r.json().catch(()=>({detail:"Invalid response"}));if(!r.ok)throw Error(d.detail||("HTTP "+r.status));return d;}
function setCoreState(state){document.body.dataset.coreState=state;document.querySelectorAll(".orb-wrap,.chat-welcome,.call-shell").forEach(node=>{node.dataset.coreState=state;});const indicator=el("chat-state");if(indicator){const labels={idle:"CORE READY",thinking:"PROCESSING",speaking:"RESPONDING",listening:"LISTENING",error:"ATTENTION REQUIRED",connecting:"CONNECTING"};indicator.textContent=labels[state]||"CORE READY";indicator.dataset.state=state;}}
function view(name){document.querySelectorAll(".view").forEach(x=>x.classList.add("hidden"));el("view-"+name).classList.remove("hidden");document.querySelectorAll(".nav").forEach(x=>x.classList.toggle("active",x.dataset.view===name));el("title").textContent=titles[name][0];el("header-sub").textContent=titles[name][1];if(name==="call")updateCallSupport();if(name==="settings")bindPreferences();}
document.querySelectorAll(".nav").forEach(b=>b.onclick=()=>view(b.dataset.view));
document.querySelectorAll("[data-go]").forEach(b=>b.onclick=()=>view(b.dataset.go));
function renderWorkspaces(){const s=el("workspace");s.replaceChildren();workspaces.forEach(w=>{const o=document.createElement("option");o.value=w.id;o.textContent=w.name;s.append(o)});s.value=active;const w=workspaces.find(x=>x.id===active);el("active").textContent=w?.name||"Not connected";el("chat-title").textContent=w?.name==="SmartFlow BW"?"Thothi Â· SmartFlow BW":"SovereignOS Â· "+(w?.name||"Workspace");if(el("intel-presence"))el("intel-presence").textContent=w?w.purpose:"No active workspace selected";el("workspace-list").replaceChildren(...workspaces.map(w=>{const d=document.createElement("div");d.className="workspace-row";const b=document.createElement("b");b.textContent=w.name;const p=document.createElement("small");p.textContent=w.purpose;d.append(b,p);return d;}));}
async function autoConnectLocal(){
 const feedback=el("settings-feedback");
 const preferred=Number(new URLSearchParams(location.search).get("apiPort"))||8765;
 const ports=[...Array(20)].map((_,i)=>preferred+i);
 let lastError="No local SovereignOS API answered the bootstrap request.";
 for(let attempt=0;attempt<12;attempt++){
  for(const port of ports){
   try{
    const response=await fetch("http://127.0.0.1:"+port+"/api/v1/bootstrap",{cache:"no-store"});
    if(!response.ok){lastError="API port "+port+" returned HTTP "+response.status+" from bootstrap.";continue;}
    const session=await response.json();
    if(!session.api_url||!session.token){lastError="The local API answered without a valid desktop session.";continue;}
    base=session.api_url;token=session.token;
    await connect();
    return;
   }catch(error){lastError=error?.message||String(error);}
  }
  if(feedback)feedback.textContent="Searching for the local intelligence coreâ€¦ (attempt "+(attempt+1)+"/12)";
  await new Promise(resolve=>setTimeout(resolve,500));
 }
 if(feedback)feedback.textContent="Could not discover the local API. "+lastError+" Confirm the desktop engine is running, then select Reconnect local engine. No URL or token entry is needed."; 
}
async function connect(){
 const button=el("save");
 apiConnected=false;updateCallSupport();
 setBusy(button,true,"Connectingâ€¦");
 el("settings-feedback").textContent="Checking API and authenticatingâ€¦";
 try{
  if(!base||!token)throw Error("Automatic local session is missing. Confirm the SovereignOS desktop engine is running.");
  const h=await fetch(base.replace(/\/$/,"")+"/api/v1/health");
  if(!h.ok)throw Error("API health check failed (HTTP "+h.status+")");
  const ws=await api("/api/v1/workspaces");
  workspaces=ws.workspaces;
  active=workspaces.some(w=>w.id===active)?active:(workspaces[0]?.id||"");
  renderWorkspaces();
  apiConnected=true;
  updateCallSupport();
  el("dot").classList.add("online");
  el("connection").textContent="API connected Â· loading services";
  el("connect").textContent="API connected âœ“";
  el("footer").textContent="API authenticated";
  el("mode").textContent="LIVE API";
  el("settings-feedback").textContent="API authenticated successfully. Loading workspace servicesâ€¦";
  notice("SovereignOS API authenticated. Loading workspace servicesâ€¦");
  const results=await Promise.allSettled([models(),devices(),approvals(),conversations()]);
  const failed=results.filter(x=>x.status==="rejected");
  el("connection").textContent=routerData?.providers?.length?"API connected Â· models configured":"API connected Â· no model configured";
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
async function conversations(){const d=await api("/api/v1/workspaces/"+encodeURIComponent(active)+"/conversations");const box=el("messages");box.replaceChildren();if(!d.conversations.length){conversation=null;box.textContent="No conversations in this workspace yet. Start a new conversation below.";return;}const current=d.conversations.find(c=>c.id===conversation);const chosen=current||d.conversations[0];const history=async c=>{conversation=c.id;box.replaceChildren();const result=await api("/api/v1/workspaces/"+encodeURIComponent(active)+"/conversations/"+encodeURIComponent(c.id)+"/messages");result.messages.forEach(m=>addBubble(m.role,m.content));};const rail=document.createElement("div");rail.className="conversation-history-rail";d.conversations.forEach(c=>{const b=document.createElement("button");b.className="history-item";b.textContent=c.title;b.classList.toggle("history-active",c.id===chosen.id);b.onclick=()=>history(c).catch(err=>notice(err.message,"error"));rail.append(b);});box.append(rail);await history(chosen);}
async function models(){try{const d=await api("/api/v1/models/health");routerData=d;const list=el("model-list");list.replaceChildren();const configured=d.providers;const ready=configured.filter(p=>p.status==="ready").length;const connected=configured.filter(p=>p.status==="connected").length;const activeModel=d.providers.find(p=>p.active);el("models-count").textContent=activeModel?"1 ACTIVE":"0 ACTIVE";el("overview-model-detail").textContent=activeModel?activeModel.provider+" Â· "+activeModel.model:configured.length+" connected Â· choose a model to activate";el("model-summary").textContent=(activeModel?"Active: "+activeModel.provider+" Â· "+activeModel.model:"No active selection")+" Â· "+ready+" ready Â· "+connected+" connected";el("model-badge").textContent=activeModel?activeModel.provider+" Â· "+activeModel.model:"SELECT A MODEL";el("model-ready-label").textContent=configured.length?configured.length+" model"+(configured.length===1?"":"s")+" configured":"No model provider configured";el("model-ready-detail").textContent="Connected means credentials and a client are configured. Ready means a successful request has been completed in this API session.";el("router-state").textContent=configured.length?"ROUTING AVAILABLE":"AWAITING MODEL";el("router-state").className="status-tag "+(configured.length?"status-safe":"status-warn");const select=el("model-select");select.replaceChildren();d.providers.forEach(p=>{const o=document.createElement("option");o.value=JSON.stringify({provider:p.provider,model:p.model});o.textContent=p.provider+" Â· "+p.model+(p.active?" Â· ACTIVE":"");if(p.active)o.selected=true;select.append(o);});providers.forEach(n=>{const p=d.providers.find(x=>x.provider.toLowerCase().includes(n.toLowerCase().split(" ")[0]));const c=document.createElement("article");c.className="model-card "+(p?"model-ready":"model-off");const top=document.createElement("div");top.className="model-card-top";const b=document.createElement("b");b.textContent=n;const badge=document.createElement("span");badge.className="status-tag "+(p?.status==="ready"?"status-safe":p?.status==="connected"?"status-idle":p?.status==="cooldown"?"status-warn":"status-idle");badge.textContent=p?(p.status==="ready"?"READY":p.status==="connected"?"CONNECTED":p.status==="cooldown"?"COOLDOWN":"UNAVAILABLE"):"NOT CONFIGURED";top.append(b,badge);const detail=document.createElement("p");detail.textContent=p?p.model+" Â· "+p.runtime:"No provider credentials loaded by the API";const stats=document.createElement("div");stats.className="model-stats";stats.textContent=p?(p.successes+" successful Â· "+p.failures+" failed"+(p.active?" Â· active selection":"")):"Add at least one provider credential to the project's local .env file, then restart the API.";c.append(top,detail,stats);list.append(c);});el("model-activate").disabled=!d.providers.length;updateCallSupport();await loadSpecialists();const warning=el("model-warnings");if(d.warnings?.length){warning.textContent=d.warnings.join(" ");warning.classList.remove("hidden");}else warning.classList.add("hidden");return d;}catch(e){notice("Could not read model health: "+e.message,"error");throw e;}}
async function loadSpecialists(){
 const list=el("specialist-list"),run=el("run-group-chat");if(!list||!run)return;
 try{
  const data=await api("/api/v1/models/specialists");list.replaceChildren();
  if(!data.specialists.length){const empty=document.createElement("p");empty.textContent="No configured model candidates yet. Add provider credentials in the local API environment and restart SovereignOS.";list.append(empty);run.disabled=true;return;}
  data.specialists.forEach(sp=>{const label=document.createElement("label");label.className="specialist-option";const input=document.createElement("input");input.type="checkbox";input.value=sp.id;input.checked=list.querySelectorAll("input:checked").length<Math.min(2,data.specialists.length);const copy=document.createElement("span");const name=document.createElement("b");name.textContent=sp.title;const detail=document.createElement("small");detail.textContent=sp.provider+" Â· "+sp.model+" Â· "+sp.status;copy.append(name,detail);label.append(input,copy);list.append(label);input.onchange=()=>{const checked=list.querySelectorAll("input:checked");if(checked.length>4){input.checked=false;notice("A group can include up to four Specialists.","warning");}run.disabled=list.querySelectorAll("input:checked").length<2;};});
  run.disabled=list.querySelectorAll("input:checked").length<2;
 }catch(error){list.textContent="Specialist roster unavailable: "+error.message;run.disabled=true;}
}
async function runSpecialistGroup(){
 const mission=el("group-mission").value.trim(),selected=[...el("specialist-list").querySelectorAll("input:checked")].map(x=>x.value),button=el("run-group-chat"),output=el("group-chat-output");
 if(!mission||selected.length<2)return;
 setBusy(button,true,"Specialists are workingâ€¦");output.replaceChildren();
 const status=document.createElement("p");status.textContent="Convening "+selected.length+" Specialists. Each will receive the shared workspace conversation.";output.append(status);
 try{
  const data=await api("/api/v1/workspaces/"+encodeURIComponent(active)+"/group-chat",{method:"POST",body:JSON.stringify({message:mission,conversation_id:conversation,specialist_ids:selected})});
  conversation=data.conversation_id;output.replaceChildren();
  data.responses.forEach(item=>{const card=document.createElement("article");card.className="specialist-response";const heading=document.createElement("b");heading.textContent=item.specialist+" Â· "+item.provider;const body=document.createElement("p");body.textContent=item.reply;card.append(heading,body);output.append(card);});
  notice("Specialist collective completed. All contributions were saved to this workspace conversation.");
 }catch(error){output.textContent="Group session failed: "+error.message;notice(error.message,"error");}
 finally{setBusy(button,false);}
}
async function activateModel(){try{const choice=JSON.parse(el("model-select").value);await api("/api/v1/models/select",{method:"POST",body:JSON.stringify(choice)});await models();notice("Manual model selection updated: "+choice.provider+" Â· "+choice.model+".");}catch(e){notice("Model switch failed: "+e.message,"error");}}
async function devices(){const d=await api("/api/v1/iot/devices");el("devices-count").textContent=d.simulation?d.devices.length+" simulated":"Connected";const list=el("device-list");list.replaceChildren();d.devices.forEach(x=>{const c=document.createElement("article");c.className="device-card";const b=document.createElement("b");b.textContent=x.name;const s=document.createElement("span");s.className="status-tag status-warn";s.textContent="SIMULATION";const p=document.createElement("p");p.textContent=x.value+" "+x.unit+" Â· "+x.kind;c.append(b,s,p);list.append(c);});}
async function approvals(){const d=await api("/api/v1/approvals");const count=d.approvals.length;el("approval-count").textContent=count;el("approval-heading").textContent=count?count+" action"+(count===1?"":"s")+" need your review":"Your approval centre is clear.";el("approval-subtitle").textContent=count?"Review each request before taking action.":"No pending actions have been submitted to the API.";const list=el("approval-list");list.replaceChildren();if(!count){const empty=document.createElement("div");empty.className="approval-empty";empty.innerHTML='<div class="empty-symbol">â—‡</div><strong>All quiet for now</strong><p>When SovereignOS proposes a sensitive action, it will appear here with its purpose, scope, risk and requested permissions.</p>';list.append(empty);}else d.approvals.forEach(a=>{const card=document.createElement("article");card.className="approval-item";card.textContent=JSON.stringify(a);list.append(card);});}
function cleanAssistantText(value){return String(value??"").replace(/\r\n?/g,"\n").replace(/\u00a0/g," ").replace(/^[\x60]{3}[\w-]*\s*$/gm,"").replace(/!\[([^\]]*)\]\([^)]+\)/g,"$1").replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g,"$1 ($2)").replace(/[\x60]([^\x60]+)[\x60]/g,"$1").replace(/^\s{0,3}#{1,6}\s+/gm,"").replace(/^\s*>\s?/gm,"").replace(/^\s*[-*+]\s+/gm,"â€¢ ").replace(/^\s*[-*_]{3,}\s*$/gm,"").replace(/\*\*(.*?)\*\*/g,"$1").replace(/__(.*?)__/g,"$1").replace(/(?<!\w)\*(.*?)\*(?!\w)/g,"$1").replace(/(?<!\w)_(.*?)_(?!\w)/g,"$1").replace(/[ \t]+\n/g,"\n").replace(/\n{3,}/g,"\n\n").trim();}
function speechText(value){let text=cleanAssistantText(value);const urls=[];text=text.replace(/\b(?:https?:\/\/|www\.)[^\s<>]+/gi,match=>{const url=match.replace(/[),.;!?]+$/,"");urls.push(url);return " URLTOKEN"+(urls.length-1)+" ";});text=text.replace(/[#*_~>|{}\[\]]/g," ").replace(/[â€¢â–ªâ—¦]/g," ").replace(/\s*[-â€“â€”]{2,}\s*/g,". ").replace(/\s+/g," ").trim();return text.replace(/URLTOKEN(\d+)/g,(_,index)=>urls[Number(index)]||"");}
function addBubble(role,text,images=[]){const box=el("messages");const welcome=box.querySelector(".chat-welcome");if(welcome)welcome.remove();if(box.childNodes.length===1&&box.firstChild.nodeType===3&&/No conversations|Connect the API/.test(box.textContent))box.replaceChildren();const b=document.createElement("div");b.className="bubble "+(role==="user"?"user":"assistant");if(role==="assistant"){const source=String(text??"");const pattern=/!\[([^\]]*)\]\((https?:\/\/[^\s)]+)\)/g;let cursor=0,match;while((match=pattern.exec(source))){const before=source.slice(cursor,match.index);if(before.trim()){const p=document.createElement("p");p.textContent=cleanAssistantText(before);b.append(p);}const img=document.createElement("img");img.className="assistant-image";img.src=match[2];img.alt=match[1]||"Sovereign illustration";img.loading="lazy";img.referrerPolicy="no-referrer";b.append(img);cursor=pattern.lastIndex;}const tail=source.slice(cursor);if(tail.trim()){const p=document.createElement("p");p.textContent=cleanAssistantText(tail);b.append(p);}if(!b.childNodes.length)b.textContent=cleanAssistantText(source);}else{b.textContent=text;images.forEach(src=>{const img=document.createElement("img");img.className="assistant-image";img.src=src;img.alt="User attached image";img.loading="lazy";b.append(img);});}box.append(b);box.scrollTop=box.scrollHeight;return b;}
async function sendMessage(msg,{voice=false,voiceSession=callSession,images=[]}={}){if(!active)throw Error("Choose a workspace first.");setCoreState("thinking");const requestStarted=performance.now();try{const d=await api("/api/v1/workspaces/"+encodeURIComponent(active)+"/chat",{method:"POST",body:JSON.stringify({message:msg,conversation_id:conversation,voice_mode:voice,images})});const requestMs=Math.round(performance.now()-requestStarted);conversation=d.conversation_id;const modelMs=Number(d.model_latency_ms)||0;el("model-badge").textContent=d.provider+" Â· "+d.model+" Â· "+(d.runtime==="local"?"LOCAL":"CLOUD");el("model-badge").title="Model response: "+(modelMs?modelMs+" ms":"timing unavailable")+" Â· Full API round trip: "+requestMs+" ms";if(d.fallback_used)notice("Model availability notice: "+(d.selected_model||"selected model")+" was unavailable or rate-limited. Sovereign switched to "+d.provider+" Â· "+d.model+" using your configured fallback order.","warning");if(voice){appendTranscript("SOVEREIGN",d.reply);if(callActive&&voiceSession===callSession)speak(d.reply,voiceSession);else setCoreState("idle");}else setCoreState("idle");return d.reply;}catch(error){setCoreState("error");throw error;}}
el("workspace").onchange=async e=>{active=e.target.value;conversation=null;renderWorkspaces();updateCallSupport();try{await conversations();}catch(err){notice(err.message,"error");}};
el("connect").onclick=()=>{if(base&&token)connect();else view("settings");};
el("save").onclick=autoConnectLocal;
function populateVoiceOptions(){const select=el("voice-choice");if(!select||!window.speechSynthesis)return;const current=prefs.voiceName;select.replaceChildren();const automatic=document.createElement("option");automatic.value="";automatic.textContent="Automatic Â· recommended";select.append(automatic);window.speechSynthesis.getVoices().forEach(v=>{const o=document.createElement("option");o.value=v.name;o.textContent=v.name+" Â· "+v.lang;select.append(o);});select.value=current;}
function bindPreferences(){const sound=el("notification-sound"),duration=el("notification-duration"),rate=el("voice-rate"),language=el("voice-language"),voice=el("voice-choice");if(sound){sound.checked=!!prefs.sound;sound.onchange=()=>{prefs.sound=sound.checked;savePrefs();if(prefs.sound)notificationSound();};}if(duration){duration.value=String(prefs.noticeSeconds);duration.onchange=()=>{prefs.noticeSeconds=Number(duration.value)||10;savePrefs();};}if(rate){rate.value=String(prefs.voiceRate);el("voice-rate-value").textContent=Number(prefs.voiceRate).toFixed(2)+"Ã—";rate.oninput=()=>{prefs.voiceRate=Number(rate.value);el("voice-rate-value").textContent=prefs.voiceRate.toFixed(2)+"Ã—";savePrefs();};}if(language){language.value=prefs.voiceLanguage;language.onchange=()=>{prefs.voiceLanguage=language.value;savePrefs();populateVoiceOptions();};}if(voice){populateVoiceOptions();voice.onchange=()=>{prefs.voiceName=voice.value;savePrefs();};}}
bindPreferences();if(window.speechSynthesis)window.speechSynthesis.onvoiceschanged=populateVoiceOptions;
document.querySelectorAll("[data-prompt]").forEach(b=>b.onclick=()=>{const field=el("message");field.value=b.dataset.prompt;field.dispatchEvent(new Event("input",{bubbles:true}));field.focus();});
el("model-activate").onclick=activateModel;
el("run-group-chat").onclick=runSpecialistGroup;
el("refresh-models").onclick=async()=>{setBusy(el("refresh-models"),true,"Refreshingâ€¦");try{await models();notice("Model health refreshed.");}finally{setBusy(el("refresh-models"),false);}};
el("chatform").onsubmit=async e=>{e.preventDefault();const msg=el("message").value.trim()||(pendingImages.length?"Please examine the attached image(s).":"");if(!msg)return;const images=pendingImages.slice();const selected=routerData?.providers?.find(p=>p.active);const cloudProviders=(routerData?.providers||[]).filter(p=>p.runtime==="cloud");const providerName=selected?(selected.provider+" Â· "+selected.model):"the selected AI provider";const cloudNames=cloudProviders.map(p=>p.provider+" Â· "+p.model).join(", ");const approvalKey=active+"|"+cloudProviders.map(p=>p.provider+"|"+p.model).sort().join(";");if(cloudProviders.length&&cloudSessionApproval!==approvalKey){if(!await inAppConfirm({title:"External AI processing",message:"This workspace request may be sent outside this computer to a configured cloud AI model: "+cloudNames+". The router may use a different model from the one manually selected, including for image understanding or availability fallback. Text and attached images may be processed under the providerâ€™s terms. Do not send passwords, identity documents, financial records or confidential material. Allow cloud processing for this workspace session?",confirmLabel:"Allow cloud processing"}))return;cloudSessionApproval=approvalKey;}if(images.length&&!await inAppConfirm({title:"Image privacy check",message:"The attached images will be sent to the vision-capable model selected by SovereignOS. It may differ from your manually selected chat model. Continue only if you are authorised to share these images and they contain no sensitive information.",confirmLabel:"Share images"}))return;addBubble("user",msg,pendingImages);el("message").value="";fitMessageComposer();const button=el("send-button");setBusy(button,true,"Thinkingâ€¦");setCoreState("thinking");const pending=addBubble("assistant","Sovereign is thinkingâ€¦");pending.classList.add("thinking-bubble");try{const reply=await sendMessage(msg,{images});pending.remove();addBubble("assistant",reply);pendingImages=[];renderAttachmentPreview();}catch(err){pending.textContent="I couldn't complete that request: "+err.message;pending.classList.remove("thinking-bubble");notice(err.message,"error");setCoreState("error");}finally{setBusy(button,false);}};
function renderAttachmentPreview(){const box=el("attachment-preview");if(!box)return;box.replaceChildren();box.classList.toggle("hidden",!pendingImages.length);pendingImages.forEach((src,index)=>{const item=document.createElement("div");item.className="attachment-thumb";const img=document.createElement("img");img.src=src;img.alt="Image attachment "+(index+1);const remove=document.createElement("button");remove.type="button";remove.textContent="Ã—";remove.setAttribute("aria-label","Remove image");remove.onclick=()=>{pendingImages.splice(index,1);renderAttachmentPreview();};item.append(img,remove);box.append(item);});}
el("attach-image").onclick=()=>el("image-input").click();
el("image-input").onchange=async event=>{const files=Array.from(event.target.files||[]).slice(0,4-pendingImages.length);for(const file of files){if(!["image/png","image/jpeg","image/webp"].includes(file.type)||file.size>4*1024*1024){notice("Choose PNG, JPEG or WebP images up to 4 MB each.","warning");continue;}const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result));reader.onerror=reject;reader.readAsDataURL(file);});pendingImages.push(data);}event.target.value="";renderAttachmentPreview();};
el("message").addEventListener("keydown",event=>{if(event.key==="Enter"&&!event.shiftKey&&!event.isComposing){event.preventDefault();el("chatform").requestSubmit();}});
el("message").addEventListener("input",fitMessageComposer);
function updateCallSupport(){
 const supported=!!(navigator.mediaDevices?.getUserMedia&&window.MediaRecorder&&(window.AudioContext||window.webkitAudioContext));
 const ready=apiConnected&&!!active;
 const modelReady=!!routerData?.providers?.length;
 el("call-start").disabled=!supported||!ready||!modelReady||callActive;
 if(!supported){
  el("call-description").textContent="Live microphone capture is unavailable here. Open SovereignOS in Microsoft Edge and allow microphone access for localhost.";
  return;
 }
 if(!apiConnected)el("call-description").textContent="Connect the SovereignOS API in Settings first.";
 else if(!active)el("call-description").textContent="Choose a workspace before starting a voice session.";
 else if(!modelReady)el("call-description").textContent="No AI model is configured. Check your configured model providers.";
 else el("call-description").textContent="Ready ? "+(workspaces.find(w=>w.id===active)?.name||"your active workspace")+". Start a conversation and speak naturally at normal volume.";
}function appendTranscript(who,text){const empty=el("call-transcript").querySelector(".transcript-empty");if(empty)empty.remove();const item=document.createElement("div");item.className="transcript-line";const label=document.createElement("b");label.textContent=who;const body=document.createElement("p");body.textContent=text;item.append(label,body);el("call-transcript").append(item);el("call-transcript").scrollTop=el("call-transcript").scrollHeight;}
function setCallState(state,label,title,description){el("call-shell").dataset.state=state;el("call-status").textContent=label;el("call-status").className="status-tag "+(["listening","thinking","speaking"].includes(state)?"status-live":state==="error"?"status-warn":"status-idle");el("voice-orb").classList.toggle("voice-active",["listening","thinking","speaking"].includes(state));const visual=el("voice-state-indicator");if(visual){visual.dataset.state=state;el("voice-state-label").textContent=label;el("voice-state-detail").textContent=description;}setCoreState(state);el("call-title").textContent=title;el("call-description").textContent=description;}
function clearListenTimer(){if(listenTimer!==null){window.clearTimeout(listenTimer);listenTimer=null;}}
async function speak(text,session=callSession){
    stopMicrophone(true);
    stopVoicePlayback();
    voiceBusy=true;
    setCallState("preparing","PREPARING AUDIO","Your response is ready.","Generating private voice audio.");

    const spoken=speechText(text);
    if(!spoken){
        voiceBusy=false;
        if(callActive)listenAgain(250);
        return;
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
                signal:controller.signal
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
        if(session!==callSession||!callActive)return;

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
            else setCoreState("idle");
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
    "Authorization":"Bearer "+token
   },
   body:blob,
   signal:controller.signal
  });
  const data=await response.json().catch(()=>({detail:"Invalid transcription response"}));
  if(!response.ok)throw new Error(data.detail||("HTTP "+response.status));
  if(session!==callSession||!callActive)return;
  const transcript=(data.transcript||"").trim();
  if(!transcript)throw new Error("No speech was detected. Please try again.");
  const elapsed=Math.round(performance.now()-started);
  appendTranscript("YOU",transcript);
  setCallState("thinking","THINKING","Let me think?","Transcription "+(data.processing_ms||elapsed)+" ms ? sending to "+(workspaces.find(w=>w.id===active)?.name||"your workspace")+".");
  const waitStarted=performance.now();
  clearInterval(voiceWaitTimer);
  voiceWaitTimer=window.setInterval(()=>{
   if(session!==callSession)return;
   const seconds=Math.floor((performance.now()-waitStarted)/1000);
   setCallState("thinking","THINKING","Let me think?","Your message is with "+(routerData?.providers?.find(p=>p.active)?.provider||"your selected model")+" ? "+seconds+"s");
  },500);
  await sendMessage(transcript,{voice:true,voiceSession:session});
 }catch(error){
  if(session!==callSession||error.name==="AbortError")return;
  voiceBusy=false;
  appendTranscript("SYSTEM","Voice input issue: "+(error.message||"transcription failed"));
  setCallState("error","VOICE INPUT ISSUE","I couldn't hear that clearly.",error.message||"Please try again.");
  if(callActive)listenAgain(900);
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

  const micPreRoll=[];
  let lastMeterUpdate=0;

  const mimeType=MediaRecorder.isTypeSupported("audio/webm;codecs=opus")?"audio/webm;codecs=opus":
   (MediaRecorder.isTypeSupported("audio/webm")?"audio/webm":"");

  micRecorder=mimeType?new MediaRecorder(stream,{mimeType}):new MediaRecorder(stream);

  micRecorder.ondataavailable=event=>{
   if(!event.data?.size||micDiscard)return;
   if(micSpeechStartedAt){
    micChunks.push(event.data);
   }else{
    micPreRoll.push(event.data);
    // Keep the initial WebM header and the two most recent audio segments.
    if(micPreRoll.length>3)micPreRoll.splice(1,1);
   }
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
  setCallState("listening","LISTENING","I'm listening",gainText+" ? "+micLabel);

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
   const threshold=Math.max(0.0035,micNoiseFloor*2.0);

   // Show measured input level so a silent browser input is immediately visible.
   if(now-lastMeterUpdate>=250){
    const detail=el("voice-state-detail");
    if(detail)detail.textContent="Microphone active ? "+micLabel;
    lastMeterUpdate=now;
   }

   if(!micSpeechStartedAt){
    if(!calibrating){
     if(rms>threshold)micAboveCount++;
     else{
      micAboveCount=0;
      micNoiseFloor=Math.min(0.025,micNoiseFloor*0.96+rms*0.04);
     }
     if(micAboveCount>=2){
      micSpeechStartedAt=now;
      micLastVoiceAt=now;
      micChunks.push(...micPreRoll);
      micPreRoll.length=0;
     }
    }
   }else{
    if(rms>threshold)micLastVoiceAt=now;
    if(now-micLastVoiceAt>=750||now-micSpeechStartedAt>=18000)finishUtterance();
   }

   if(!micFinalizing)micFrame=requestAnimationFrame(inspectLevel);
  };

  micFrame=requestAnimationFrame(inspectLevel);
 }catch(error){
  stopMicrophone(true);
  if(session!==callSession)return;
  voiceBusy=false;
  const denied=error.name==="NotAllowedError"||error.name==="SecurityError";
  setCallState("error",denied?"MICROPHONE BLOCKED":"MICROPHONE ISSUE","I couldn't open your microphone.",denied?"Allow microphone access for localhost in Edge site permissions, then start again.":(error.message||"Check your microphone and try again."));
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

function startCall(){
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
 callSession++;
 clearListenTimer();
 stopMicrophone(true);
 stopVoicePlayback();
 voiceBusy=false;
 callActive=true;
 el("call-start").disabled=true;
 el("call-interrupt").disabled=false;
 el("call-end").disabled=false;
 setCallState("connecting","CONNECTING","Opening your voice link?","Requesting microphone access. Allow it if prompted.");
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
 el("call-start").disabled=!(apiConnected&&!!active);
 el("call-interrupt").disabled=true;
 el("call-end").disabled=true;
 setCallState("idle","STANDING BY","Your AI is ready when you are.","Call ended. Your transcript remains visible until you leave this page.");
 updateCallSupport();
}
el("call-start").onclick=startCall;el("call-end").onclick=endCall;el("call-interrupt").onclick=interruptCall;
el("run-libreoffice-test").onclick=async()=>{const button=el("run-libreoffice-test"),status=el("libreoffice-test-status");if(!await inAppConfirm({title:"Create LibreOffice test",message:"SovereignOS will create one new test document in its own agent workspace and ask LibreOffice to open it. It will not inspect or change your existing files.",confirmLabel:"Create and open"}))return;setBusy(button,true,"Creating documentâ€¦");status.textContent="Creating a local test document and locating LibreOfficeâ€¦";try{const result=await api("/api/v1/desktop/libreoffice/test",{method:"POST",body:JSON.stringify({})});status.textContent="Opened "+result.file_name+" in LibreOffice. Saved only in the SovereignOS agent workspace.";notice("LibreOffice integration test launched successfully.");}catch(error){status.textContent=error.message;notice("LibreOffice test could not complete: "+error.message,"error");}finally{setBusy(button,false);}};
el("call-text-form").addEventListener("submit",async event=>{event.preventDefault();const field=el("call-text"),send=el("call-send"),message=field.value.trim();if(!message)return;if(!apiConnected||!active){notice("Connect the API and choose a workspace before messaging Sovereign.","warning");return;}if(!routerData?.providers?.length){notice("No AI model is configured. Add a provider credential to the project's local .env file and restart the API.","warning");return;}field.value="";field.style.height="48px";appendTranscript("YOU",message);send.disabled=true;try{const reply=await sendMessage(message,{voice:callActive});if(!callActive)appendTranscript("SOVEREIGN",reply);}catch(error){appendTranscript("SYSTEM",error.message||"Message could not be sent.");notice(error.message||"Message could not be sent.","error");}finally{send.disabled=false;field.focus();}});
el("call-text").addEventListener("input",event=>{event.target.style.height="48px";event.target.style.height=Math.min(event.target.scrollHeight,132)+"px";});
el("call-attach").onclick=()=>{view("chat");window.setTimeout(()=>el("attach-image").click(),0);};
if("serviceWorker" in navigator)window.addEventListener("load",async()=>{try{if(import.meta.env.DEV){const registrations=await navigator.serviceWorker.getRegistrations();await Promise.all(registrations.map(r=>r.unregister()));if("caches" in window){const keys=await caches.keys();await Promise.all(keys.map(k=>caches.delete(k)));}if(navigator.serviceWorker.controller&&!sessionStorage.getItem("so-dev-worker-cleared")){sessionStorage.setItem("so-dev-worker-cleared","1");window.location.reload();}return;}await navigator.serviceWorker.register(import.meta.env.BASE_URL+"sw.js",{updateViaCache:"none"});}catch(error){console.warn("SovereignOS worker update skipped:",error);}});
view("overview");updateCallSupport();autoConnectLocal();
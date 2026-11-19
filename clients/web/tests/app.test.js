import test from "node:test";import assert from "node:assert/strict";import{readFile}from"node:fs/promises";const root=new URL("../",import.meta.url);test("PWA manifest is standalone",async()=>{const m=JSON.parse(await readFile(new URL("public/manifest.webmanifest",root),"utf8"));assert.equal(m.display,"standalone");assert.ok(m.icons.length)});test("frontend does not embed provider secrets",async()=>{const s=await readFile(new URL("src/main.js",root),"utf8");assert.doesNotMatch(s,/GROQ_API_KEY|OPENROUTER_API_KEY|NVIDIA_API_KEY/)});test("physical actuation is disabled in UI",async()=>{const s=await readFile(new URL("index.html",root),"utf8");assert.match(s,/Device actuation disabled/)});test("chat path is workspace scoped",async()=>{const s=await readFile(new URL("src/main.js",root),"utf8");assert.match(s,/api\/v1\/workspaces/)});
test("voice workspace exposes replay for the latest spoken answer",async()=>{
 const html=await readFile(new URL("index.html",root),"utf8");
 const js=await readFile(new URL("src/main.js",root),"utf8");
 assert.match(html,/id="call-replay"[^>]*disabled>Replay last answer/);
 assert.match(js,/lastSpokenText=spoken/);
 assert.match(js,/el\("call-replay"\)\.onclick/);
 assert.match(js,/allowOutsideCall=false/);
 assert.match(js,/!callActive&&!allowOutsideCall/);
});

test("chat consumes authenticated server-sent text deltas",async()=>{
 const js=await readFile(new URL("src/main.js",root),"utf8");
 assert.match(js,/\/chat\/stream/);
 assert.match(js,/Accept":"text\/event-stream"/);
 assert.match(js,/eventName==="delta"/);
 assert.match(js,/onDelta\(delta,fullReply\)/);
 assert.match(js,/eventName==="done"/);
});
test("live voice queues complete phrases while generation continues",async()=>{
 const js=await readFile(new URL("src/main.js",root),"utf8");
 assert.match(js,/function createLiveSpeechQueue/);
 assert.match(js,/function takeSpeechPhrases/);
 assert.match(js,/speech\.enqueue\(phrase\)/);
 assert.match(js,/speaking while the rest of the answer is generated/i);
});

test("live voice transcript returns its element for incremental updates",async()=>{
 const js=await readFile(new URL("src/main.js",root),"utf8");
 assert.match(js,/function appendTranscript\(who,text\)[^\n]*return item;/);
 assert.match(js,/const transcript=appendTranscript\("SOVEREIGN",""\);transcriptBody=transcript\.querySelector\("p"\)/);
});

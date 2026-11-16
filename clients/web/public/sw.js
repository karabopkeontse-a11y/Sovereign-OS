const CACHE_NAME="sovereignos-shell-v2";
const SHELL=["./","./manifest.webmanifest","./sovereign-os-mark.svg"];
self.addEventListener("install",event=>{self.skipWaiting();event.waitUntil(caches.open(CACHE_NAME).then(cache=>cache.addAll(SHELL)));});
self.addEventListener("activate",event=>{event.waitUntil((async()=>{const keys=await caches.keys();await Promise.all(keys.filter(key=>key!==CACHE_NAME).map(key=>caches.delete(key)));await self.clients.claim();})());});
self.addEventListener("fetch",event=>{
 const request=event.request;
 if(request.method!=="GET")return;
 const url=new URL(request.url);
 if(url.origin!==self.location.origin||url.pathname.includes("/api/"))return;
 event.respondWith((async()=>{
  const cache=await caches.open(CACHE_NAME);
  try{
   const response=await fetch(request,{cache:"no-store"});
   if(response&&response.ok&&response.type==="basic")cache.put(request,response.clone());
   return response;
  }catch(error){
   const cached=await cache.match(request);
   if(cached)return cached;
   if(request.mode==="navigate")return (await cache.match("./"))||Response.error();
   throw error;
  }
 })());
});